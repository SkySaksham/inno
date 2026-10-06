#!/usr/bin/env python3
"""
semantic_review.py  —  Inno semantic scan, step 2 of 2.

Reads semantic_payloads.json, calls the LLM once per changed function,
and writes semantic_findings.json.

The LLM is asked to find problems that no linter can detect:
  - Contract mismatches  (signature / return-shape / exceptions changed
                          but callers not updated)
  - Logic bugs           (off-by-one, wrong condition, unhandled edge case)
  - Removed/weakened checks (auth, validation, rate-limit)
  - Behavior that contradicts the function's summary or docstring

Every finding MUST carry an evidence field explaining what in the
provided code/callers proves the issue.  The LLM may return an empty
list — that is explicitly a valid answer.

Output schema per finding:
  {
    "category":   "contract_mismatch" | "logic_bug" | "weakened_check" | "doc_mismatch",
    "severity":   "critical" | "high" | "medium" | "low",
    "confidence": "high" | "medium" | "low",
    "title":      "short one-line description",
    "explanation":"1-3 sentences",
    "evidence":   "exact quote / line reference proving this",
    "suggested_fix": "replacement code or empty string"
  }

LLM backend: same INNO_LLM_CMD env var as ai_review.py.
  INNO_LLM_CMD="copilot -p"   (default)
  INNO_LLM_CMD="mock"         offline canned output for testing

Usage:
    python semantic_review.py \\
        --payloads semantic_payloads.json \\
        --out      semantic_findings.json
"""
import argparse
import json
import os
import shlex
import subprocess
import sys

SEVS  = ("critical", "high", "medium", "low")
CONFS = ("high", "medium", "low")
CATS  = ("contract_mismatch", "logic_bug", "weakened_check", "doc_mismatch")
TIMEOUT_S   = 180
MAX_FUNCTIONS = 30   # cap: skip oldest-looking entries beyond this

FINDING_SCHEMA = """{
  "findings": [
    {
      "category":      "contract_mismatch | logic_bug | weakened_check | doc_mismatch",
      "severity":      "critical | high | medium | low",
      "confidence":    "high | medium | low",
      "title":         "short one-line description",
      "explanation":   "1-3 sentences",
      "evidence":      "exact quote or line reference that proves this issue",
      "suggested_fix": "replacement code or empty string"
    }
  ]
}"""


def build_prompt(payload):
    p = payload
    meta_text = json.dumps(p["metadata"], indent=2) if p["metadata"] else "none available"

    callers_text_parts = []
    for c in p["callers"]:
        sites = c.get("call_site_lines") or []
        sites_str = ("\n\n".join(sites)) if sites else "(no call-site found in indexed files)"
        callers_text_parts.append(
            f"Caller `{c['caller_func']}` in `{c['file']}`:\n{sites_str}"
        )
    callers_text = "\n\n---\n\n".join(callers_text_parts) or "none indexed"

    callees_text_parts = []
    for ce in p["callees"]:
        sig  = ce.get("signature") or "unknown signature"
        summ = ce.get("summary")   or ""
        callees_text_parts.append(f"- `{ce['name']}`: {sig}  —  {summ}")
    callees_text = "\n".join(callees_text_parts) or "none indexed"

    return f"""You are a senior engineer performing a semantic code review on a pull request.
You have been given the old and new versions of one function, its diff, its metadata,
the actual call-site code of every caller, and the signatures of its callees.

Your job is to find problems that no linter would catch:
  • Contract mismatches  — signature, return shape, or raised exceptions changed
                           but one or more callers were not updated.
  • Logic bugs           — off-by-one, wrong condition, unhandled edge case.
  • Removed or weakened checks — auth, input validation, rate-limiting.
  • Behavior that contradicts the function's summary or docstring.

Rules:
  1. Every finding MUST have a non-empty `evidence` field that quotes or
     cites the exact code lines proving the issue.  No evidence = no finding.
  2. "No issues found" is a valid and expected answer — return an empty findings list.
  3. The call graph is approximate (matched by name).  Confirm from the actual
     call-site code before reporting a caller mismatch.
  4. Do not invent issues.  If you are unsure, set confidence to "low".
  5. Reply with ONLY a JSON object, no markdown, no extra text, exactly this shape:
{FINDING_SCHEMA}

<function>
name: {p['func_name']}
file: {p['file']}
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
# LLM
# ---------------------------------------------------------------------------

def mock_llm(_payload):
    return json.dumps({"findings": []})


def call_llm(prompt, payload):
    cmd = os.environ.get("INNO_LLM_CMD", "copilot -p")
    if cmd == "mock":
        return mock_llm(payload)
    res = subprocess.run(
        shlex.split(cmd) + [prompt],
        capture_output=True, text=True, timeout=TIMEOUT_S,
    )
    if res.returncode != 0:
        raise RuntimeError(f"LLM command failed: {res.stderr.strip()[:300]}")
    return res.stdout


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_finding(raw):
    """Normalise and validate a single finding dict.  Returns None to discard."""
    if not isinstance(raw, dict):
        return None

    evidence = str(raw.get("evidence", "")).strip()
    if not evidence:
        # No evidence — discard per the rules
        return None

    sev  = str(raw.get("severity",   "")).lower()
    conf = str(raw.get("confidence", "")).lower()
    cat  = str(raw.get("category",   "")).lower()

    return {
        "category":      cat  if cat  in CATS  else "logic_bug",
        "severity":      sev  if sev  in SEVS  else "low",
        "confidence":    conf if conf in CONFS else "low",
        "title":         str(raw.get("title",        "")).strip(),
        "explanation":   str(raw.get("explanation",  "")).strip(),
        "evidence":      evidence,
        "suggested_fix": str(raw.get("suggested_fix", "")).strip(),
    }


def review_one(payload):
    """Call the LLM and return (list[finding], prompt_chars, error_str|None)."""
    prompt = build_prompt(payload)
    last_err = None

    for attempt in range(2):
        retry_suffix = (
            "\nYour previous reply was not valid JSON. "
            "Reply with ONLY the JSON object, no markdown."
            if attempt > 0 else ""
        )
        try:
            raw = call_llm(prompt + retry_suffix, payload)
            obj = extract_json(raw)
            raw_findings = obj.get("findings", [])
            if not isinstance(raw_findings, list):
                raise ValueError("findings must be a list")
            findings = [
                vf for f in raw_findings
                if (vf := validate_finding(f)) is not None
            ]
            return findings, len(prompt), None
        except (ValueError, RuntimeError, subprocess.TimeoutExpired,
                json.JSONDecodeError) as exc:
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
    ap.add_argument("--out", default="semantic_findings.json")
    ap.add_argument("--max", type=int, default=MAX_FUNCTIONS,
                    help="max functions to review (oldest dropped first)")
    ap.add_argument("--dry-run", action="store_true",
                    help="write prompts to semantic_prompts.txt and stop")
    args = ap.parse_args()

    with open(args.payloads, encoding="utf-8") as fh:
        data = json.load(fh)

    payloads = data["functions"][:args.max]
    skipped  = len(data["functions"]) - len(payloads)

    if args.dry_run:
        with open("semantic_prompts.txt", "w", encoding="utf-8") as fh:
            for p in payloads:
                fh.write(f"===== {p['id']} — {p['func_name']} =====\n"
                         f"{build_prompt(p)}\n")
        print(f"Wrote semantic_prompts.txt ({len(payloads)} prompts)")
        return

    results = []
    total_findings = 0

    for p in payloads:
        findings, prompt_chars, err = review_one(p)
        if err:
            print(
                f"[warn] {p['id']} ({p['func_name']}): semantic review failed ({err})",
                file=sys.stderr,
            )
        total_findings += len(findings)
        results.append({
            "id":           p["id"],
            "func_name":    p["func_name"],
            "file":         p["file"],
            "findings":     findings,
            "prompt_chars": prompt_chars,
            "error":        err,
        })

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "base":      data.get("base"),
                "head":      data.get("head"),
                "skipped":   skipped,
                "results":   results,
            },
            fh, indent=2,
        )

    reviewed_ok = sum(1 for r in results if not r["error"])
    print(
        f"Semantic review: {reviewed_ok}/{len(results)} functions OK, "
        f"{total_findings} finding(s) -> {args.out}"
    )


if __name__ == "__main__":
    main()

