#!/usr/bin/env python3
"""
semantic_review.py  —  Inno semantic scan, step 2 of 2.

Reads semantic_payloads.json, calls the LLM once per changed function
(concurrently), and writes semantic_findings.json.

LLM backend is configured via llm.py (env vars):
  INNO_LLM_BACKEND   openai | gemini | cmd | mock
  OPENAI_API_KEY     needed for openai backend
  GEMINI_API_KEY     needed for gemini backend
  INNO_LLM_CMD       needed for cmd backend  (e.g. "copilot -p")
  INNO_LLM_MODEL     optional model override
  INNO_LLM_TIMEOUT   per-call timeout in seconds (default 120)

Speed:
  --workers N   concurrent LLM calls (default 4)

The LLM is asked to find problems that no linter can detect:
  - Contract mismatches  (signature / return-shape / exceptions changed
                          but callers not updated)
  - Logic bugs           (off-by-one, wrong condition, unhandled edge case)
  - Removed/weakened checks (auth, validation, rate-limit)
  - Behavior that contradicts the function's summary or docstring

Every finding MUST carry an evidence field.  Empty list is valid.

Usage:
    python semantic_review.py \\
        --payloads semantic_payloads.json \\
        --out      semantic_findings.json \\
        --workers  6
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import llm   # shared backend (llm.py, same directory)

SEVS  = ("critical", "high", "medium", "low")
CONFS = ("high", "medium", "low")
CATS  = ("contract_mismatch", "logic_bug", "weakened_check", "doc_mismatch")
MAX_FUNCTIONS   = 30
DEFAULT_WORKERS = 4

FINDING_SCHEMA = """{
  "findings": [
    {
      "category":      "contract_mismatch | logic_bug | weakened_check | doc_mismatch",
      "severity":      "critical | high | medium | low",
      "confidence":    "high | medium | low",
      "title":         "short one-line description",
      "explanation":   "1-3 sentences",
      "evidence":      "exact quote or line reference that proves this issue",
      "suggested_fix": "replacement code or empty string",
      "fix": {"start_line": 1, "end_line": 1, "replacement": "code",
              "explanation": "why"} | null
    }
  ]
}"""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_prompt(payload):
    p = payload
    meta_text = json.dumps(p["metadata"], indent=2) if p["metadata"] else "none available"

    callers_parts = []
    for c in p["callers"]:
        sites = c.get("call_site_lines") or []
        sites_str = ("\n\n".join(sites)) if sites else "(no call-site found in indexed files)"
        callers_parts.append(
            f"Caller `{c['caller_func']}` in `{c['file']}`:\n{sites_str}"
        )
    callers_text = "\n\n---\n\n".join(callers_parts) or "none indexed"

    callees_parts = []
    for ce in p["callees"]:
        sig  = ce.get("signature") or "unknown signature"
        summ = ce.get("summary")   or ""
        callees_parts.append(f"- `{ce['name']}`: {sig}  —  {summ}")
    callees_text = "\n".join(callees_parts) or "none indexed"

    return f"""
    You are a senior engineer performing a semantic code review on a pull request.
You have been given the old and new versions of one function, its diff, its metadata,
the actual call-site code of every caller, and the signatures of its callees.

Your job is to find problems that no linter would catch:
  • Contract mismatches  — signature, return shape, or raised exceptions changed
                           but one or more callers were not updated.
  • Logic bugs           — off-by-one, wrong condition, unhandled edge case.
  • Removed or weakened checks — auth, input validation, rate-limiting.
  • Behavior that contradicts the function's summary or docstring.

Rules:
  1. Every finding MUST have a non-empty `evidence` field quoting or
     citing the exact code lines proving the issue. No evidence = no finding.
  2. "No issues found" is a valid and expected answer — return an empty findings list.
  3. The call graph is approximate (matched by name). Confirm from the actual
     call-site code before reporting a caller mismatch.
  4. Do not invent issues. If you are unsure, set confidence to "low".
  5. Include a minimal structured fix for each finding when safely possible. Use only
     changed_lines and the supplied function. The range must be at most 3 lines and
     include a changed line. Otherwise set fix to null and provide a textual fix.
  6. Reply with ONLY a JSON object, no markdown, no extra text, exactly this shape:
{FINDING_SCHEMA}

<function>
name: {p['func_name']}
file: {p['file']}
start_line: {p['start_line']}
changed_lines: {json.dumps(p['changed_lines'])}
</function>

<old_source>
{p['old_source'] or '(new function — no previous version)'}
</old_source>

<new_source>
{p['new_source']}
</new_source>

<diff>
{p['diff_hunk']}
</diff>

<metadata>
{meta_text}
</metadata>

<callers>
{callers_text}
</callers>

<callees>
{callees_text}
</callees>
"""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def _validate_finding(raw):
    """Normalise and validate one finding dict. Returns None to discard."""
    if not isinstance(raw, dict):
        return None
    evidence = str(raw.get("evidence", "")).strip()
    if not evidence:
        return None   # no evidence → discard

    sev  = str(raw.get("severity",   "")).lower()
    conf = str(raw.get("confidence", "")).lower()
    cat  = str(raw.get("category",   "")).lower()
    fix = raw.get("fix")
    if isinstance(fix, dict):
        start, end = fix.get("start_line"), fix.get("end_line")
        replacement = str(fix.get("replacement", "")).strip("\n")
        explanation = str(fix.get("explanation", "")).strip()
        if (isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int)
                or start < 1 or end < start or end - start + 1 > 3
                or not replacement.strip() or not explanation):
            fix = None
        else:
            fix = {"start_line": start, "end_line": end,
                   "replacement": replacement, "explanation": explanation}
    else:
        fix = None
    suggested_fix = str(raw.get("suggested_fix", "")).strip()
    if fix:
        suggested_fix = fix["replacement"]
    return {
        "category":      cat  if cat  in CATS  else "logic_bug",
        "severity":      sev  if sev  in SEVS  else "low",
        "confidence":    conf if conf in CONFS else "low",
        "title":         str(raw.get("title",        "")).strip(),
        "explanation":   str(raw.get("explanation",  "")).strip(),
        "evidence":      evidence,
        "suggested_fix": suggested_fix,
        "fix": fix,
    }


# ---------------------------------------------------------------------------
# Per-function review  (runs in thread pool)
# ---------------------------------------------------------------------------

def review_one(payload):
    """
    Call the LLM for one function payload.

    Returns (findings: list, prompt_chars: int, error: str|None).
    Thread-safe — llm.call_llm() is stateless.

    Raises llm.AuthenticationError directly — caller will abort batch.
    """
    prompt   = build_prompt(payload)
    last_err = None

    for attempt in range(2):
        retry_sfx = (
            "\nYour previous reply was not valid JSON. "
            "Reply with ONLY the JSON object, no markdown."
            if attempt > 0 else ""
        )
        try:
            raw      = llm.call_llm(prompt + retry_sfx, context_hint="semantic")
            obj      = _extract_json(raw)
            raw_list = obj.get("findings", [])
            if not isinstance(raw_list, list):
                raise ValueError("findings must be a list")
            findings = [
                vf for f in raw_list
                if (vf := _validate_finding(f)) is not None
            ]
            return findings, len(prompt), None
        except llm.AuthenticationError:
            raise   # do not retry auth failures
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            if llm.is_auth_error(exc):
                raise llm.AuthenticationError(str(exc)) from exc
            last_err = str(exc)

    return [], len(prompt), last_err


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Semantic LLM review of changed functions."
    )
    ap.add_argument("--payloads", required=True,
                    help="semantic_payloads.json from semantic_context.py")
    ap.add_argument("--out",     default="semantic_findings.json")
    ap.add_argument("--max",     type=int, default=MAX_FUNCTIONS,
                    help="max functions to review (oldest dropped first)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="concurrent LLM calls (default 4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="write prompts to semantic_prompts.txt and stop")
    args = ap.parse_args()

    with open(args.payloads, encoding="utf-8") as fh:
        data = json.load(fh)

    payloads = data["functions"][:args.max]
    skipped  = len(data["functions"]) - len(payloads)

    backend = llm.active_backend()
    print(f"LLM backend: {backend}  |  workers: {args.workers}  |  "
          f"functions: {len(payloads)} (skipped {skipped})")

    if args.dry_run:
        with open("semantic_prompts.txt", "w", encoding="utf-8") as fh:
            for p in payloads:
                fh.write(f"===== {p['id']} — {p['func_name']} =====\n"
                         f"{build_prompt(p)}\n")
        print(f"Wrote semantic_prompts.txt ({len(payloads)} prompts)")
        return

    # Run preflight check once before starting workers
    try:
        llm.preflight_check()
    except llm.AuthenticationError as exc:
        print(f"\n[FATAL] LLM authentication preflight failed: {exc}\n"
              "Aborting semantic review immediately to prevent repeated errors.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[warn] LLM preflight warning: {exc}", file=sys.stderr)

    # ---- Concurrent LLM calls ---------------------------------------------
    # Build a lookup so we can re-assemble results in original order.
    idx_map  = {p["id"]: i for i, p in enumerate(payloads)}
    results  = [None] * len(payloads)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(review_one, p): p for p in payloads}
        done = 0
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                findings, chars, err = fut.result()
            except llm.AuthenticationError as exc:
                print(f"\n[FATAL] Authentication failure during semantic review of {p['id']}: {exc}\n"
                      "Aborting remaining reviews.", file=sys.stderr)
                pool.shutdown(wait=False, cancel_futures=True)
                sys.exit(1)

            done += 1
            status = f"✓ {len(findings)} finding(s)" if not err else f"✗ {err[:80]}"
            print(f"  [{done}/{len(payloads)}] {p['id']} ({p['func_name']}): {status}",
                  flush=True)
            results[idx_map[p["id"]]] = {
                "id":           p["id"],
                "func_name":    p["func_name"],
                "file":         p["file"],
                "start_line":   p["start_line"],
                "changed_lines": p["changed_lines"],
                "findings":     findings,
                "prompt_chars": chars,
                "error":        err,
            }

    total_findings = sum(len(r["findings"]) for r in results)
    reviewed_ok    = sum(1 for r in results if not r["error"])

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "base":    data.get("base"),
                "head":    data.get("head"),
                "backend": backend,
                "skipped": skipped,
                "results": results,
            },
            fh, indent=2,
        )

    print(
        f"\nSemantic review: {reviewed_ok}/{len(results)} functions OK, "
        f"{total_findings} finding(s) -> {args.out}"
    )


if __name__ == "__main__":
    main()
