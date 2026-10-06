#!/usr/bin/env python3
"""
Inno PR scan: git diff -> Pylint + Bandit -> one normalized findings.json

Usage (from inside the checked-out repo being reviewed):
    python scan.py --base origin/main --head HEAD --out findings.json

Only findings on lines changed in the PR are kept (use --all to keep everything).
This script never fails the build by itself; gating happens after AI ranking.
"""
import argparse
import json
import os
import re
import subprocess
import sys

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def norm(path):
    return os.path.normpath(path).replace("\\", "/")


def changed_lines(repo, base, head):
    """Return {file: set(changed line numbers)} for added/modified .py files."""
    res = run(
        ["git", "-c", "core.quotepath=off", "diff", "-U0",
         "--diff-filter=ACMR", f"{base}...{head}", "--", "*.py"],
        repo,
    )
    if res.returncode != 0:
        sys.exit(f"git diff failed: {res.stderr.strip()}")

    changed, current = {}, None
    for line in res.stdout.splitlines():
        if line.startswith("+++ "):
            path = line[4:]
            current = norm(path[2:]) if path.startswith("b/") else None
            if current:
                changed.setdefault(current, set())
        else:
            m = HUNK_RE.match(line)
            if m and current:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                changed[current].update(range(start, start + count))
    return changed


def run_pylint(repo, files):
    if not files:
        return []
    # C = convention, R = refactor: too noisy for PR comments.
    # import-error is disabled because the runner may not have the project's deps.
    res = run(
        [sys.executable, "-m", "pylint", "--output-format=json",
         "--disable=C,R,import-error", *files],
        repo,
    )
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        print(f"[warn] pylint produced no JSON: {res.stderr.strip()}", file=sys.stderr)
        return []

    sev_map = {"fatal": "high", "error": "high", "warning": "medium"}
    return [
        {
            "tool": "pylint",
            "file": norm(d["path"]),
            "line": d["line"],
            "rule_id": d["message-id"],
            "rule_name": d["symbol"],
            "message": d["message"],
            "severity": sev_map.get(d["type"], "low"),
            "confidence": None,
        }
        for d in data
    ]


def run_bandit(repo, files):
    if not files:
        return []
    res = run([sys.executable, "-m", "bandit", "-f", "json", "-q", *files], repo)
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        print(f"[warn] bandit produced no JSON: {res.stderr.strip()}", file=sys.stderr)
        return []

    return [
        {
            "tool": "bandit",
            "file": norm(r["filename"]),
            "line": r["line_number"],
            "rule_id": r["test_id"],
            "rule_name": r["test_name"],
            "message": r["issue_text"],
            "severity": r["issue_severity"].lower(),
            "confidence": r["issue_confidence"].lower(),
        }
        for r in data.get("results", [])
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", required=True, help="e.g. origin/main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--out", default="findings.json")
    ap.add_argument("--all", action="store_true",
                    help="keep findings on untouched lines too")
    args = ap.parse_args()

    changed = changed_lines(args.repo, args.base, args.head)
    head_sha = run(["git", "rev-parse", args.head], args.repo).stdout.strip() or args.head
    files = sorted(f for f in changed if os.path.isfile(os.path.join(args.repo, f)))
    print(f"Changed Python files: {files or 'none'}")

    findings = run_pylint(args.repo, files) + run_bandit(args.repo, files)

    if not args.all:
        findings = [f for f in findings if f["line"] in changed.get(f["file"], set())]

    # The analyzer supplies the target line; preserve its exact diff mapping
    # and current source snapshot so report.py never trusts an AI-chosen path.
    for finding in findings:
        path, line = finding["file"], finding["line"]
        try:
            with open(os.path.join(args.repo, path), encoding="utf-8") as source_file:
                source_lines = source_file.read().splitlines()
            finding["location"] = {
                "path": path, "line": line,
                "changed": line in changed.get(path, set()),
                "current_source": source_lines[line - 1] if 1 <= line <= len(source_lines) else None,
            }
        except (OSError, UnicodeError):
            finding["location"] = {"path": path, "line": line, "changed": False,
                                   "current_source": None}

    findings.sort(key=lambda f: (SEV_ORDER.get(f["severity"], 3), f["file"], f["line"]))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {"base": args.base, "head": head_sha,
             "changed_files": files, "findings": findings},
            fh, indent=2,
        )

    counts = {s: sum(f["severity"] == s for f in findings) for s in SEV_ORDER}
    print(f"Findings: {len(findings)} "
          f"(high={counts['high']}, medium={counts['medium']}, low={counts['low']})")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
