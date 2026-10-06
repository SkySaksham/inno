#!/usr/bin/env python3
"""
report.py  —  Inno: combined_review.json -> PR comment + gate exit code.

Reads combined_review.json (output of merge.py).

Comment layout
--------------
  ## 🔍 Inno PR review
  <summary line>
  <gate status>

  ### ✅ Confirmed by static analysis
  <items with source=linter or source=both>

  ### 🧠 AI-found — needs human judgment
  <items with source=semantic>

  <details> Likely false positives </details>
  <details> N items not shown (cap) </details>

Gate rules
----------
  Linter-backed findings (source=linter|both):
    block on severity >= --fail-on  AND  is_real_issue is not False

  Semantic-only findings (source=semantic):
    block only when severity >= --fail-on  AND  confidence == "high"
    (avoids hallucination-caused merge blocks)

Inline suggestions
------------------
  Inline suggestion buttons only work on lines present in the PR diff.
  Semantic findings have no line number, so their fix goes in a plain
  "Suggested fix" block in the comment body, not as an inline suggestion.

Falls back to job summary only if PR token cannot write.
Exits 1 when merge-blocking findings are found.
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

MARKER    = "<!-- inno-pr-review -->"
LEGACY_MARKERS = ("<!-- inno-review -->",)
RANK      = {"critical": 0, "high": 1, "medium": 2, "low": 3}
ICON      = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
SRC_LABEL = {
    "both":     "🔬 Static + AI",
    "linter":   "🔬 Static analysis",
    "semantic": "🧠 AI semantic",
}
CAT_LABEL = {
    "contract_mismatch": "Contract mismatch",
    "logic_bug":         "Logic bug",
    "weakened_check":    "Weakened check",
    "doc_mismatch":      "Doc mismatch",
}


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

def is_blocking(item, threshold_rank):
    sev_rank = RANK.get(item["severity"], 3)
    if sev_rank > threshold_rank:
        return False

    src = item["source"]
    if src in ("linter", "both"):
        # Fail closed on linter-backed — unless AI explicitly dismissed it
        return item.get("is_real_issue") is not False
    else:
        # Semantic-only: block only on high severity + high confidence
        return item.get("confidence") == "high"


def blocking_items(results, threshold):
    t = RANK[threshold]
    return [r for r in results if is_blocking(r, t)]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def render_linter_item(item):
    f  = item["finding"]
    ai = item.get("ai")
    sev = item["severity"]
    src = item["source"]

    lines = [
        f"#### {ICON.get(sev, '')} {sev.title()} — `{f['file']}:{f['line']}` — "
        f"`{f['rule_id']}` ({f['tool']})  <sub>{SRC_LABEL.get(src, src)}</sub>",
        "",
    ]

    if ai:
        lines.append(ai["reason"])
        if f.get("message"):
            lines += ["", f"**Evidence:** {f['message']}"]
        if ai.get("impact"):
            lines += ["", f"**Impact:** {ai['impact']}"]
        if ai.get("suggested_fix"):
            if not _safe_suggestion(item):
                lines += [
                    "",
                    "**FIXABLE — MANUAL APPLICATION REQUIRED**",
                    "Suggested fix could not be safely attached to the current diff. Please apply manually.",
                ]
            lines += ["", "**Suggested fix:**", "", "````python", ai["suggested_fix"], "````"]
        lines += [
            "",
            f"<sub>AI confidence: {ai['confidence']} · suggestion only, "
            "a human must review before merging</sub>",
        ]
    else:
        lines.append(
            f"{f['message']}\n\n<sub>AI review unavailable; showing analyzer result only.</sub>"
        )

    # Semantic bonus if merged
    sem = item.get("semantic")
    if sem and src == "both":
        cat = CAT_LABEL.get(sem["category"], sem["category"])
        lines += [
            "",
            f"**Also flagged by semantic scan ({cat}):** {sem['explanation']}",
        ]
        if sem.get("evidence"):
            lines += ["", f"> **Evidence:** {sem['evidence']}"]
        if sem.get("suggested_fix"):
            lines += ["", "**Semantic suggested fix:**", "", "````python",
                      sem["suggested_fix"], "````"]

    lines.append("")
    return "\n".join(lines)


def render_semantic_item(item):
    sem = item["semantic"]
    sev = item["severity"]
    conf = item.get("confidence", "low")
    cat  = CAT_LABEL.get(sem["category"], sem["category"])
    func = item.get("func_name", "unknown")
    file = item["file"]

    lines = [
        f"#### {ICON.get(sev, '')} {sev.title()} — `{file}` → `{func}` — "
        f"{cat}  <sub>{SRC_LABEL['semantic']} · confidence: {conf}</sub>",
        "",
        sem["explanation"],
    ]
    if sem.get("evidence"):
        lines += ["", f"> **Evidence:** {sem['evidence']}"]
    if sem.get("suggested_fix"):
        lines += ["", "**Suggested fix:**", "", "````python", sem["suggested_fix"], "````"]
    lines += [
        "",
        "<sub>No inline suggestion button — this finding is outside the diff. "
        "Apply the fix manually if accepted.</sub>",
        "",
    ]
    return "\n".join(lines)


def render_item(item):
    if item["source"] == "semantic":
        return render_semantic_item(item)
    return render_linter_item(item)


# ---------------------------------------------------------------------------
# Full markdown builder
# ---------------------------------------------------------------------------

def build_markdown(data, threshold):
    results = data["results"]

    # Split into groups
    confirmed  = [r for r in results if r["source"] in ("linter", "both")
                  and r.get("is_real_issue") is not False]
    ai_only    = [r for r in results if r["source"] == "semantic"]
    false_pos  = [r for r in results if r.get("is_real_issue") is False]

    block = blocking_items(results, threshold)

    # Sort each group by severity
    def sev_key(r):
        return RANK.get(r["severity"], 3)

    confirmed.sort(key=sev_key)
    ai_only.sort(key=sev_key)

    # Summary counts
    all_real = confirmed + ai_only
    counts   = {s: sum(r["severity"] == s for r in all_real) for s in RANK}
    summary  = " · ".join(f"{ICON[s]} {counts[s]} {s}" for s in RANK if counts[s]) or "no issues"

    md = [MARKER, "## 🔍 Inno PR review", ""]
    analyzed = data.get("head") or os.environ.get("GITHUB_SHA")
    if analyzed:
        md.append(f"**Analyzed commit:** `{str(analyzed)[:12]}` — this review reflects the latest PR commit.")
        md.append("")
    md.append(f"**{len(all_real)} issue(s):** {summary}")
    md.append("")

    if block:
        md.append(
            f"❌ **Merge blocked:** {len(block)} finding(s) at `{threshold}` or above "
            "must be resolved (or dismissed by a human)."
        )
    else:
        md.append(f"✅ **Gate passed:** nothing actionable at `{threshold}` or above.")
    md.append("")

    # --- Confirmed section --------------------------------------------------
    if confirmed:
        md.append("### ✅ Confirmed by static analysis")
        md.append(
            "_These findings are backed by Pylint / Bandit. "
            "High/Critical ones block merge._"
        )
        md.append("")
        for r in confirmed:
            md.append(render_item(r))

    # --- Semantic-only section ----------------------------------------------
    if ai_only:
        md.append("### 🧠 AI-found — needs human judgment")
        md.append(
            "_Found by semantic diff analysis. No linter backing. "
            "Blocks merge only when severity is High/Critical AND confidence is High._"
        )
        md.append("")
        for r in ai_only:
            md.append(render_item(r))

    # --- False positives ----------------------------------------------------
    if false_pos:
        md += [
            "<details><summary>Likely false positives "
            f"({len(false_pos)}) — dismissed by AI</summary>",
            "",
        ]
        for r in false_pos:
            f = r["finding"]
            reason = (r.get("ai") or {}).get("reason", "no reason given")
            md.append(f"- `{f['file']}:{f['line']}` `{f['rule_id']}`: {reason}")
        md += ["", "</details>", ""]

    # --- Overflow notice ----------------------------------------------------
    oc = data.get("overflow_count", 0)
    if oc:
        md.append(
            f"_{oc} additional finding(s) were not shown (cap reached). "
            "Download the `inno-results` artifact for the full list._"
        )

    return "\n".join(md)


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def gh(method, url, token, body=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type":  "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def pr_number():
    path = os.environ.get("GITHUB_EVENT_PATH")
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            ev = json.load(fh)
        return (ev.get("pull_request") or {}).get("number")
    return None


class StaleReview(Exception):
    """Raised when the reviewed commit is no longer the PR head."""


def ensure_current_pr_head(reviewed_sha):
    """Fail closed unless the reviewed SHA still matches the live PR head."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    number = pr_number()
    if not number:
        return True  # Local rendering/tests have no PR to compare.
    if not (token and repo and reviewed_sha):
        raise RuntimeError("Cannot verify the current PR head; refusing to publish review output.")
    url = f"https://api.github.com/repos/{repo}/pulls/{number}"
    try:
        pull = gh("GET", url, token) or {}
    except Exception as exc:
        raise RuntimeError("Could not verify the current PR head; refusing to publish review output.") from exc
    current_sha = ((pull.get("head") or {}).get("sha"))
    if not current_sha:
        raise RuntimeError("GitHub did not return the current PR head; refusing to publish review output.")
    if current_sha != reviewed_sha:
        raise StaleReview(
            "PR changed while review was running. Skipping stale review output; "
            "a newer workflow run will review the latest commit."
        )
    return True


def post_comment(markdown, state=None, require_existing=False, expected_head=None):
    token = os.environ.get("GITHUB_TOKEN")
    repo  = os.environ.get("GITHUB_REPOSITORY")
    num   = pr_number()
    if not (token and repo and num):
        print("[info] not in a PR context; skipping comment")
        return False
    if expected_head:
        ensure_current_pr_head(expected_head)
    base = f"https://api.github.com/repos/{repo}/issues"
    if state is not None:
        encoded = base64.urlsafe_b64encode(
            json.dumps(state, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        markdown += f"\n\n<!-- inno-review-state:{encoded} -->"
    try:
        existing = gh("GET", f"{base}/{num}/comments?per_page=100", token) or []
        mine = next((c for c in existing if any(
            marker in (c.get("body") or "") for marker in (MARKER, *LEGACY_MARKERS)
        )), None)
        if mine:
            if expected_head:
                ensure_current_pr_head(expected_head)
            gh("PATCH", f"{base}/comments/{mine['id']}", token, {"body": markdown})
        elif require_existing:
            print("[error] prior Inno PR comment was not found; refusing to create a duplicate",
                  file=sys.stderr)
            return False
        else:
            if expected_head:
                ensure_current_pr_head(expected_head)
            gh("POST",  f"{base}/{num}/comments",        token, {"body": markdown})
        return True
    except urllib.error.HTTPError as exc:
        print(
            f"[warn] could not post PR comment (HTTP {exc.code}); "
            "falling back to job summary",
            file=sys.stderr,
        )
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _safe_suggestion(item, repo_dir="."):
    """Validate a small AI range against scanner source and diff snapshots."""
    finding = item.get("finding") or {}
    ai = item.get("ai") or {}
    location = finding.get("location") or {}
    fix = ai.get("fix") or {}
    path = fix.get("file")
    finding_path, finding_line = finding.get("file"), finding.get("line")
    start_line, end_line = fix.get("start_line"), fix.get("end_line")
    replacement = fix.get("replacement", "")
    explanation = fix.get("explanation", "")
    changed_nearby = location.get("changed_lines_nearby") or []
    context = location.get("source_context") or {}
    if (not path or path != finding_path or path != location.get("path")
            or not isinstance(finding_line, int) or finding_line < 1
            or not isinstance(start_line, int) or isinstance(start_line, bool)
            or not isinstance(end_line, int) or isinstance(end_line, bool)
            or start_line < 1 or end_line < start_line or end_line - start_line + 1 > 3
            or start_line < finding_line - 1 or end_line > finding_line + 1
            or not isinstance(replacement, str) or not replacement.strip()
            or not isinstance(explanation, str) or not explanation.strip()
            or not changed_nearby or end_line not in changed_nearby):
        return None
    normalized = os.path.normpath(path)
    if os.path.isabs(path) or normalized == ".." or normalized.startswith(".." + os.sep):
        return None
    repo_root = os.path.realpath(repo_dir)
    full_path = os.path.realpath(os.path.join(repo_root, normalized))
    try:
        if os.path.commonpath((repo_root, full_path)) != repo_root:
            return None
    except ValueError:
        return None
    if not os.path.isfile(full_path):
        return None
    try:
        with open(full_path, encoding="utf-8") as fh:
            current = fh.read().splitlines()
    except (OSError, UnicodeError):
        return None
    context_start = context.get("start_line")
    context_lines = context.get("lines")
    if (not isinstance(context_start, int) or not isinstance(context_lines, list)
            or start_line < context_start
            or end_line >= context_start + len(context_lines)):
        return None
    expected_range = context_lines[start_line - context_start:end_line - context_start + 1]
    if end_line > len(current) or current[start_line - 1:end_line] != expected_range:
        return None
    replacement = replacement.replace("\r\n", "\n").rstrip("\n")
    if any(line_text.lstrip().startswith("```") for line_text in replacement.splitlines()):
        return None
    body = (f"**Inno suggested fix** — {explanation.strip()}\n\n"
            "```suggestion\n" + replacement.rstrip("\n") + "\n```")
    return normalized.replace(os.sep, "/"), start_line, end_line, body


def post_suggestions(data):
    """Post eligible suggestions as native pull request review comments."""
    token, repo, number = (os.environ.get("GITHUB_TOKEN"),
                           os.environ.get("GITHUB_REPOSITORY"), pr_number())
    head = data.get("head")
    if head and number:
        ensure_current_pr_head(head)
    if not (token and repo and number and head):
        return 0
    comments, seen = [], set()
    for item in data.get("results", []):
        if item.get("source") not in ("linter", "both") or item.get("is_real_issue") is False:
            continue
        suggestion = _safe_suggestion(item)
        if not suggestion:
            continue
        path, start_line, end_line, body = suggestion
        key = (path, start_line, end_line, body)
        if key not in seen:
            seen.add(key)
            comment = {"path": path, "line": end_line, "side": "RIGHT", "body": body}
            if start_line < end_line:
                comment.update({"start_line": start_line, "start_side": "RIGHT"})
            comments.append(comment)
    if not comments:
        return 0
    try:
        if number:
            ensure_current_pr_head(head)
        gh("POST", f"https://api.github.com/repos/{repo}/pulls/{number}/reviews", token,
           {"commit_id": head, "event": "COMMENT", "comments": comments})
        return len(comments)
    except urllib.error.HTTPError as exc:
        print(f"[warn] could not post native suggestions (HTTP {exc.code})", file=sys.stderr)
        return 0


def main():
    ap = argparse.ArgumentParser(
        description="Post Inno combined review comment and enforce gate."
    )
    ap.add_argument("--review",   required=True,
                    help="combined_review.json from merge.py")
    ap.add_argument("--fail-on",  choices=list(RANK), default="high")
    ap.add_argument("--print",    action="store_true",
                    help="also print the markdown to stdout")
    args = ap.parse_args()

    with open(args.review, encoding="utf-8") as fh:
        data = json.load(fh)

    reviewed_head = data.get("head")
    if pr_number():
        try:
            ensure_current_pr_head(reviewed_head)
        except StaleReview as exc:
            print(str(exc))
            return

    markdown = build_markdown(data, args.fail_on)

    if args.print:
        print(markdown)

    state = {
        "version": 1,
        "initial_head": data.get("head") or os.environ.get("GITHUB_SHA"),
        "initial_results": data.get("results", []) + data.get("overflow", []),
        "threshold": args.fail_on,
        "accepted_suggestions": [],
    }
    try:
        post_comment(markdown, state=state, expected_head=reviewed_head)
        print(f"Posted {post_suggestions(data)} native suggestion(s)")
    except StaleReview as exc:
        print(str(exc))
        return

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        if pr_number():
            try:
                ensure_current_pr_head(reviewed_head)
            except StaleReview as exc:
                print(str(exc))
                return
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")

    block = blocking_items(data["results"], args.fail_on)
    if block:
        print(f"GATE FAILED: {len(block)} blocking finding(s)")
        sys.exit(1)
    print("GATE PASSED")


if __name__ == "__main__":
    main()
