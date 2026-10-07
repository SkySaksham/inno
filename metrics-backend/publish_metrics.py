#!/usr/bin/env python3
"""Publish Inno PR-review counts to the hosted metrics API."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def review_counts(review):
    findings = list(review.get("results", [])) + list(review.get("overflow", []))
    false_positives = sum(item.get("is_real_issue") is False for item in findings)
    fixes_suggested = 0
    for item in findings:
        ai = item.get("ai") or {}
        semantic = item.get("semantic") or {}
        fixes_suggested += bool(ai.get("suggested_fix"))
        fixes_suggested += bool(semantic.get("suggested_fix"))
    return {
        "total_findings": len(findings),
        "false_positives": false_positives,
        "fixes_suggested": fixes_suggested,
    }


def pull_request_identity():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.isfile(event_path):
        return None
    try:
        event = read_json(event_path)
    except (OSError, json.JSONDecodeError):
        return None
    pull = event.get("pull_request") or {}
    head = pull.get("head") or {}
    repository = os.environ.get("GITHUB_REPOSITORY")
    number = pull.get("number")
    sha = head.get("sha") or os.environ.get("GITHUB_SHA")
    if not repository or not isinstance(number, int) or not sha:
        return None
    return {"repository": repository, "pr_number": number, "head_sha": sha}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", help="combined review JSON from merge.py")
    parser.add_argument("--counts", help="verified counts JSON from verify.py")
    args = parser.parse_args()

    identity = pull_request_identity()
    api_url = os.environ.get("INNO_METRICS_API_URL", "https://inno-mv5y.onrender.com").rstrip("/")
    if not identity:
        print("[metrics] Not in a pull request event; skipping.")
        return 0

    try:
        counts = review_counts(read_json(args.review)) if args.review and os.path.isfile(args.review) else {}
        if args.counts and os.path.isfile(args.counts):
            counts.update(read_json(args.counts))
        if not counts:
            print("[metrics] No review counts available; skipping.")
            return 0
        payload = {**identity, **counts}
        payload.setdefault("fixes_accepted", 0)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[metrics] Could not prepare metrics: {exc}", file=sys.stderr)
        return 0

    request = urllib.request.Request(
        f"{api_url}/metrics",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            print(f"[metrics] Saved metrics for {identity['repository']} PR #{identity['pr_number']} ({response.status}).")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[metrics] Could not reach metrics API; review is unaffected: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
