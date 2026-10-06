#!/usr/bin/env python3
"""Static-only follow-up verification for an Inno PR review."""
import argparse
import base64
from collections import Counter
import json
import os
import subprocess
import sys
import urllib.error

import report

STATE_PREFIX = "<!-- inno-review-state:"
STATE_SUFFIX = " -->"


def load_prior_state():
    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    number = report.pr_number()
    if not (token and repository and number):
        return None
    url = f"https://api.github.com/repos/{repository}/issues/{number}/comments?per_page=100"
    try:
        comments = report.gh("GET", url, token) or []
    except urllib.error.HTTPError:
        return None
    for comment in reversed(comments):
        body = comment.get("body") or ""
        start = body.rfind(STATE_PREFIX)
        if start < 0:
            continue
        end = body.find(STATE_SUFFIX, start)
        if end < 0:
            continue
        try:
            encoded = body[start + len(STATE_PREFIX):end]
            state = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            continue
        if state.get("version") == 1:
            return state
    return None


def eslint_configured(repo="."):
    config_names = {
        ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.mjs",
        ".eslintrc.json", ".eslintrc.yaml", ".eslintrc.yml",
        "eslint.config.js", "eslint.config.cjs", "eslint.config.mjs",
        "eslint.config.ts", "eslint.config.mts", "eslint.config.cts",
    }
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv"}]
        if config_names.intersection(files):
            return True
    package_json = os.path.join(repo, "package.json")
    try:
        with open(package_json, encoding="utf-8") as fh:
            return "eslintConfig" in json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False


def detect(args):
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = {}
    if event_path and os.path.isfile(event_path):
        with open(event_path, encoding="utf-8") as fh:
            event = json.load(fh)
    current = (event.get("pull_request") or {}).get("head", {}).get("sha") or os.environ.get("GITHUB_SHA")
    state = load_prior_state() if event.get("action") == "synchronize" else None
    verification = bool(state and current and state.get("initial_head") != current)
    if verification:
        with open(args.state_out, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    outputs = {
        "mode": "verify" if verification else "initial",
        "eslint": "true" if eslint_configured() else "false",
    }
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as fh:
            for key, value in outputs.items():
                fh.write(f"{key}={value}\n")
    print(json.dumps(outputs))


def identity(finding):
    return (finding.get("file", "").replace("\\", "/"), finding.get("tool"),
            finding.get("rule_id"), finding.get("rule_name"))


def current_findings(path, eslint_path=None):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    findings = list(data.get("findings", []))
    if eslint_path:
        with open(eslint_path, encoding="utf-8") as fh:
            eslint = json.load(fh)
        for file_result in eslint:
            for message in file_result.get("messages", []):
                findings.append({
                    "tool": "eslint",
                    "file": os.path.relpath(file_result.get("filePath", ""), ".").replace("\\", "/"),
                    "line": message.get("line"),
                    "rule_id": message.get("ruleId") or "eslint-config-error",
                    "rule_name": message.get("ruleId") or "eslint-config-error",
                    "message": message.get("message", ""),
                    "severity": "high" if message.get("severity") == 2 else "medium",
                    "confidence": None,
                })
    return findings


def suggestion_was_added(state, result, head):
    finding = result.get("finding") or {}
    ai = result.get("ai") or {}
    location = finding.get("location") or {}
    suggestion = ai.get("suggested_fix", "")
    path = finding.get("file") or location.get("path")
    initial_head = state.get("initial_head")
    if not (suggestion and path and initial_head and head):
        return False
    diff = subprocess.run(
        ["git", "diff", "--unified=0", f"{initial_head}...{head}", "--", path],
        capture_output=True, text=True,
    )
    if diff.returncode:
        return False
    added = [line[1:].strip() for line in diff.stdout.splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    proposed = [line.strip() for line in suggestion.splitlines() if line.strip()]
    if not proposed:
        return False
    width = len(proposed)
    return any(added[index:index + width] == proposed
               for index in range(len(added) - width + 1))


def rate(numerator, denominator):
    return "N/A" if not denominator else f"{100 * numerator / denominator:.1f}%"


def make_verification(state, findings, head, eslint_exit=0):
    initial = state.get("initial_results", [])
    threshold = state.get("threshold", "high")
    threshold_rank = report.RANK.get(threshold, report.RANK["high"])
    baseline_static = []
    semantic_only = []
    false_positives = [item for item in initial if item.get("is_real_issue") is False]
    known_false_positive_keys = Counter(
        identity(item["finding"]) for item in false_positives if item.get("finding")
    )
    suggestions = 0
    for item in initial:
        ai = item.get("ai") or {}
        semantic = item.get("semantic") or {}
        suggestions += bool(ai.get("suggested_fix")) + bool(semantic.get("suggested_fix"))
        if item.get("is_real_issue") is False:
            continue
        finding = item.get("finding")
        if finding:
            baseline_static.append(item)
        elif item.get("source") == "semantic":
            semantic_only.append(item)

    counts = Counter(identity(f) for f in findings)
    remaining, fixed, accepted_ids = [], 0, set(state.get("accepted_suggestions", []))
    old_static_keys = Counter()
    for item in baseline_static:
        finding = item.get("finding") or {}
        key = identity(finding)
        old_static_keys[key] += 1
        if counts[key]:
            counts[key] -= 1
            remaining.append(item)
        else:
            fixed += 1
            if (item.get("ai") or {}).get("suggested_fix") and suggestion_was_added(state, item, head):
                accepted_ids.add(str(item.get("id") or key))
    # Semantic-only findings cannot be proven fixed without another semantic pass.
    remaining.extend(semantic_only)

    new_findings = []
    for finding in findings:
        key = identity(finding)
        if known_false_positive_keys[key]:
            known_false_positive_keys[key] -= 1
        elif old_static_keys[key]:
            old_static_keys[key] -= 1
        else:
            new_findings.append(finding)
    bugs_before = len(baseline_static) + len(semantic_only)
    bugs_fixed = fixed
    remaining_count = len(remaining) + len(new_findings)
    suggested = int(suggestions)
    accepted = min(len(accepted_ids), suggested)
    bug_resolution = rate(bugs_fixed, bugs_before)
    blocking_remaining = [item for item in remaining if report.is_blocking(item, threshold_rank)]
    blocking_remaining += [f for f in new_findings
                           if report.RANK.get(f.get("severity"), 3) <= threshold_rank]

    blocking_tools = {
        (item.get("finding") or {}).get("tool")
        for item in blocking_remaining if item.get("finding")
    }
    blocking_tools.update(finding.get("tool") for finding in blocking_remaining
                          if finding.get("tool"))
    blocking_tools.update(finding.get("tool") for finding in new_findings
                          if report.RANK.get(finding.get("severity"), 3) <= threshold_rank)
    tool_status = {}
    for tool in ("pylint", "bandit", "eslint"):
        if tool == "eslint" and not state.get("eslint_configured", False):
            tool_status[tool] = "NOT CONFIGURED"
        else:
            tool_status[tool] = "FAIL" if tool in blocking_tools or (
                tool == "eslint" and eslint_exit != 0
            ) else "PASS"

    status = not blocking_remaining and eslint_exit == 0
    lines = [
        "<!-- inno-review -->",
        "## Inno PR review — post-fix verification",
        "",
        f"**Analyzed commit:** `{str(head)[:12]}`",
        "",
        "### Before",
        f"- Total bugs/findings detected: **{len(initial)}**",
        f"- High severity: **{sum(1 for i in initial if i.get('severity') in ('critical', 'high'))}**",
        f"- Medium severity: **{sum(1 for i in initial if i.get('severity') == 'medium')}**",
        f"- Low severity: **{sum(1 for i in initial if i.get('severity') == 'low')}**",
        "",
        "### Fix activity",
        f"- Total fixes suggested: **{suggested}**",
        f"- Total fixes accepted/applied by the human: **{accepted}**",
        f"- Total bugs fixed: **{bugs_fixed}**",
        f"- Total false positives: **{len(false_positives)}**",
        f"- Total remaining bugs: **{remaining_count}**",
        "",
        "### Rates",
        f"- Fix acceptance rate: **{rate(accepted, suggested)}**",
        f"- False-positive rate: **{rate(len(false_positives), len(initial))}**",
        f"- Bug resolution rate: **{bug_resolution}**",
        "",
        "### Verification",
        f"```yaml\nPylint: {tool_status['pylint']}\nBandit: {tool_status['bandit']}\nESLint: {tool_status['eslint']}\n```",
        "",
        "```text",
        f"Initial findings: {len(initial)}",
        f"After fixes:       {len(findings)}",
        f"Fixed:             {bugs_fixed}",
        f"Remaining:         {remaining_count}",
        "```",
        "",
    ]
    if status:
        lines += ["## 🎉 READY TO MERGE", "", "All blocking bugs have been fixed and verified.",
                  "The PR has passed the Inno security gate."]
    else:
        lines += ["## ❌ NOT READY TO MERGE", "",
                  "Blocking findings remain. Please address them and push another commit."]

    next_state = dict(state)
    next_state["accepted_suggestions"] = sorted(accepted_ids)
    next_state["latest_head"] = head
    next_state["latest_findings"] = findings
    next_state["metrics"] = {
        "before": len(initial), "fixed": bugs_fixed, "remaining": remaining_count,
        "suggested": suggested, "accepted": accepted,
        "false_positives": len(false_positives),
    }
    return "\n".join(lines), next_state, status


def verify(args):
    with open(args.state, encoding="utf-8") as fh:
        state = json.load(fh)
    if args.eslint:
        state["eslint_configured"] = True
    findings = current_findings(args.findings, args.eslint)
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = {}
    if event_path and os.path.isfile(event_path):
        with open(event_path, encoding="utf-8") as fh:
            event = json.load(fh)
    head = (event.get("pull_request") or {}).get("head", {}).get("sha")
    head = head or os.environ.get("GITHUB_SHA")
    eslint_exit = 0
    if args.eslint_exit:
        try:
            with open(args.eslint_exit, encoding="utf-8") as fh:
                eslint_exit = int(fh.read().strip())
        except (OSError, ValueError):
            eslint_exit = 2
    markdown, next_state, passed = make_verification(state, findings, head, eslint_exit)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    report.post_comment(markdown, state=next_state)
    print(markdown)
    sys.exit(0 if passed else 1)


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    detect_parser = subparsers.add_parser("detect")
    detect_parser.add_argument("--state-out", default="previous_inno_state.json")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--state", required=True)
    verify_parser.add_argument("--findings", required=True)
    verify_parser.add_argument("--eslint")
    verify_parser.add_argument("--eslint-exit")
    args = parser.parse_args()
    if args.command == "detect":
        detect(args)
    else:
        verify(args)


if __name__ == "__main__":
    main()
