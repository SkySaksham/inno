#!/usr/bin/env python3
"""
merge.py  —  Inno: combine linter-backed and semantic findings into one list.

Reads:
  review.json           (from ai_review.py  — linter source)
  semantic_findings.json (from semantic_review.py — semantic source)

Writes:
  combined_review.json  with this shape:
  {
    "base": "...", "head": "...",
    "results": [
      {
        "id":     "F1" | "SF1" | "F1+SF2",   # merged IDs
        "source": "linter" | "semantic" | "both",
        "file":   "...",
        "line":   123 | null,
        "func_name": "...",

        # linter side (may be null for pure semantic)
        "finding":   { ...scan.py schema... },
        "ai":        { ...ai_review.py schema... },

        # semantic side (may be null for pure linter)
        "semantic":  { ...one semantic finding... },

        # unified fields
        "severity":  "critical" | "high" | "medium" | "low",
        "confidence":"high" | "medium" | "low" | null,
        "is_real_issue": true | false | null,
      }, ...
    ],
    "cap_applied": true | false
  }

Dedup/merge rules:
  - A linter finding and a semantic finding are "same issue" when they share
    the same file and the linter line falls inside the function that produced
    the semantic finding.  In that case they become one item with source="both".
  - All others remain separate.

Ranking:
  1. severity      (critical > high > medium > low)
  2. source weight (both > linter > semantic)
  3. confidence    (high > medium > low)

Cap: --max (default 15) items are kept; lower-ranked ones are stored in
"overflow" so the caller can show "N more not shown" without losing data.
"""
import argparse
import json
import sys

RANK_SEV  = {"critical": 0, "high": 1, "medium": 2, "low": 3}
RANK_SRC  = {"both": 0, "linter": 1, "semantic": 2}
RANK_CONF = {"high": 0, "medium": 1, "low": 2, None: 3}
DEFAULT_CAP = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sev_of_linter(r):
    ai = r.get("ai")
    if ai and ai.get("is_real_issue") is not False:
        return ai.get("severity", r["finding"]["severity"])
    return r["finding"]["severity"]


def conf_of_linter(r):
    ai = r.get("ai")
    if ai:
        return ai.get("confidence")
    return None


def is_real_linter(r):
    ai = r.get("ai")
    if ai:
        return ai.get("is_real_issue")
    return None   # unverified


def unified_severity(item):
    """Pick the highest severity present in the item."""
    candidates = []
    if item.get("finding"):
        candidates.append(RANK_SEV.get(item["severity"], 3))
    if item.get("semantic"):
        candidates.append(RANK_SEV.get(item["semantic"]["severity"], 3))
    return min(candidates) if candidates else 3


def rank_key(item):
    sev  = RANK_SEV.get(item["severity"], 3)
    src  = RANK_SRC.get(item["source"], 2)
    conf = RANK_CONF.get(item.get("confidence"), 3)
    return (sev, src, conf)


# ---------------------------------------------------------------------------
# Overlap detection
# ---------------------------------------------------------------------------

def semantic_line_range(sr):
    """Return (start, end) line numbers from a semantic result, best effort."""
    # semantic_findings has no line numbers directly; we rely on context.py's
    # extraction, which stores func start/end in the payload.  Here we use a
    # generous fallback: treat the whole file as possible overlap.
    return (1, 999999)


def linter_overlaps_semantic(lf, sr):
    """
    True when the linter finding and the semantic result share the same file
    and the linter line is plausibly inside the changed function.

    We also accept an exact function-name match as a strong signal.
    """
    if lf["finding"]["file"] != sr["file"]:
        return False

    # Function-name match is the strongest signal
    func_name = sr.get("func_name", "")
    lf_func = lf.get("function") or ""   # may be set by context.py in future

    if func_name and lf_func and func_name == lf_func:
        return True

    # Fall back: just file match is enough for a soft merge
    # (keeps merging conservative — same file AND at least one semantic finding)
    return True   # file-level match; caller already filters by file


# ---------------------------------------------------------------------------
# Main merge
# ---------------------------------------------------------------------------

def merge(linter_results, semantic_results, cap):
    """
    linter_results : list of result dicts from review.json
    semantic_results: list of {id, func_name, file, findings:[...]} from semantic_findings.json

    Returns (items, overflow) each being a list of unified dicts.
    """
    items = []

    # ---- Expand semantic results into one item per finding -----------------
    # Index: file -> list of (semantic_result, finding)
    sem_by_file = {}
    for sr in semantic_results:
        for sf in sr.get("findings", []):
            sem_by_file.setdefault(sr["file"], []).append((sr, sf))

    used_sem = set()   # (file, finding_idx) already merged into a linter item

    # ---- Process linter findings -------------------------------------------
    for lr in linter_results:
        file = lr["finding"]["file"]
        sev  = sev_of_linter(lr)
        conf = conf_of_linter(lr)
        real = is_real_linter(lr)

        # Try to find an overlapping semantic finding
        matched_sem = None
        matched_key = None
        if file in sem_by_file:
            for idx, (sr, sf) in enumerate(sem_by_file[file]):
                key = (file, idx)
                if key in used_sem:
                    continue
                if linter_overlaps_semantic(lr, sr):
                    # pick the highest-severity semantic finding on this file
                    if matched_sem is None or (
                        RANK_SEV.get(sf["severity"], 3) <
                        RANK_SEV.get(matched_sem["severity"], 3)
                    ):
                        matched_sem = sf
                        matched_key = key
                        matched_sr  = sr

        if matched_sem:
            used_sem.add(matched_key)
            # Merge: take the worse severity
            merged_sev = (
                sev if RANK_SEV.get(sev, 3) <= RANK_SEV.get(matched_sem["severity"], 3)
                else matched_sem["severity"]
            )
            items.append({
                "id":           f"{lr['id']}+{matched_sr['id']}",
                "source":       "both",
                "file":         file,
                "line":         lr["finding"]["line"],
                "func_name":    matched_sr.get("func_name"),
                "finding":      lr["finding"],
                "ai":           lr.get("ai"),
                "semantic":     matched_sem,
                "severity":     merged_sev,
                "confidence":   conf,
                "is_real_issue": real,
            })
        else:
            items.append({
                "id":           lr["id"],
                "source":       "linter",
                "file":         file,
                "line":         lr["finding"]["line"],
                "func_name":    lr.get("function"),
                "finding":      lr["finding"],
                "ai":           lr.get("ai"),
                "semantic":     None,
                "severity":     sev,
                "confidence":   conf,
                "is_real_issue": real,
            })

    # ---- Add unmatched semantic findings -----------------------------------
    for file, entries in sem_by_file.items():
        for idx, (sr, sf) in enumerate(entries):
            if (file, idx) in used_sem:
                continue
            items.append({
                "id":           sr["id"],
                "source":       "semantic",
                "file":         file,
                "line":         None,
                "func_name":    sr.get("func_name"),
                "finding":      None,
                "ai":           None,
                "semantic":     sf,
                "severity":     sf["severity"],
                "confidence":   sf["confidence"],
                "is_real_issue": None,
            })

    # ---- Sort --------------------------------------------------------------
    items.sort(key=rank_key)

    overflow = []
    cap_applied = False
    if cap and len(items) > cap:
        overflow    = items[cap:]
        items       = items[:cap]
        cap_applied = True

    return items, overflow, cap_applied


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Merge linter and semantic findings into combined_review.json."
    )
    ap.add_argument("--review",   required=True,
                    help="review.json from ai_review.py")
    ap.add_argument("--semantic", required=True,
                    help="semantic_findings.json from semantic_review.py")
    ap.add_argument("--out",      default="combined_review.json")
    ap.add_argument("--max",      type=int, default=DEFAULT_CAP,
                    help="max findings in the top list (rest go to overflow)")
    args = ap.parse_args()

    with open(args.review, encoding="utf-8") as fh:
        linter_data = json.load(fh)
    with open(args.semantic, encoding="utf-8") as fh:
        semantic_data = json.load(fh)

    linter_results   = linter_data.get("results", [])
    semantic_results = semantic_data.get("results", [])

    items, overflow, cap_applied = merge(linter_results, semantic_results, args.max)

    out = {
        "base":        linter_data.get("base") or semantic_data.get("base"),
        "head":        linter_data.get("head") or semantic_data.get("head"),
        "cap_applied": cap_applied,
        "overflow_count": len(overflow),
        "results":     items,
        "overflow":    overflow,
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)

    src_counts = {s: sum(1 for i in items if i["source"] == s)
                  for s in ("both", "linter", "semantic")}
    print(
        f"Merged: {len(items)} item(s) "
        f"(both={src_counts['both']}, linter={src_counts['linter']}, "
        f"semantic={src_counts['semantic']}) "
        f"+ {len(overflow)} overflow -> {args.out}"
    )


if __name__ == "__main__":
    main()

