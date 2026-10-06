#!/usr/bin/env python3
"""
Inno context builder: findings.json + metadata + live code + diff -> payloads.json

For every finding it collects:
  - the enclosing function's code (from the live file, via ast)
  - the diff hunk for that file
  - metadata: signature, summary, what the function calls, what calls it

Usage (from inside the checked-out PR repo):
    python context.py --findings findings.json --metadata metadata.json --out payloads.json

NOTE: load_metadata() is the ONLY place that knows your metadata format.
If your schema.py differs from the assumed one, edit that function (and the
three small entry_* helpers) and nothing else needs to change.
"""
import argparse
import ast
import json
import os
import subprocess
import sys

MAX_RELATED = 5          # max callers / callees sent to the LLM
MAX_FUNC_LINES = 120     # truncate very long functions
MAX_HUNK_CHARS = 3000    # truncate very long diffs
FALLBACK_WINDOW = 15     # lines either side when the finding is not inside a function


def run(cmd, cwd="."):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


# ---------------------------------------------------------------- metadata ---
def load_metadata(path):
    """
    Assumed format (adjust to match your schema.py):
      {"functions": {
          "utils/db.py::get_user": {
              "file": "utils/db.py", "name": "get_user",
              "signature": "get_user(user_id)", "summary": "...",
              "calls": ["run_query"], "called_by": ["api/users.py::show"]}}}
    A list of such dicts under "functions" also works.
    Returns {} if the file is missing, so the pipeline still runs on ast-only context.
    """
    if not path or not os.path.isfile(path):
        print("[warn] no metadata file; using ast-only context", file=sys.stderr)
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] could not read metadata ({exc}); using ast-only context",
              file=sys.stderr)
        return {}

    funcs = raw.get("functions", raw) if isinstance(raw, dict) else raw
    if isinstance(funcs, list):
        funcs = {f"{e.get('file')}::{e.get('name')}": e
                 for e in funcs if isinstance(e, dict)}
    if not isinstance(funcs, dict):
        return {}
    return {k: v for k, v in funcs.items() if isinstance(v, dict)}


def entry_file(key, entry):
    return entry.get("file") or key.split("::")[0]


def entry_name(key, entry):
    return entry.get("name") or key.split("::")[-1]


def entry_label(key, entry):
    return f"{entry_file(key, entry)}::{entry_name(key, entry)}"


def find_metadata(funcs, file, qualname):
    exact = funcs.get(f"{file}::{qualname}")
    if exact:
        return exact
    short = qualname.split(".")[-1]
    for key, entry in funcs.items():
        if entry_file(key, entry) == file and entry_name(key, entry).split(".")[-1] == short:
            return entry
    return None


def _call_name(c):
    if isinstance(c, dict):
        c = c.get("name") or c.get("target") or ""
    return str(c)


def callers_of(funcs, qualname):
    """Derive callers from other entries' 'calls' lists (matched by short name)."""
    short = qualname.split(".")[-1]
    out = []
    for key, entry in funcs.items():
        for c in entry.get("calls", []) or []:
            if _call_name(c).split(".")[-1] == short:
                out.append(entry_label(key, entry))
                break
    return out


# --------------------------------------------------------------------- ast ---
def find_enclosing(tree, line):
    """Return (qualified_name, node) of the innermost function containing `line`."""
    best = None

    def visit(node, prefix):
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if child.lineno <= line <= (child.end_lineno or child.lineno):
                    if not isinstance(child, ast.ClassDef):
                        best = (name, child)
                    visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return best


def numbered(lines, start):
    return "\n".join(f"{start + i}: {l}" for i, l in enumerate(lines))


# --------------------------------------------------------------------- diff ---
def diff_hunk(repo, base, head, file):
    res = run(["git", "-c", "core.quotepath=off", "diff", "-U3",
               f"{base}...{head}", "--", file], repo)
    text = res.stdout if res.returncode == 0 else ""
    if len(text) > MAX_HUNK_CHARS:
        text = text[:MAX_HUNK_CHARS] + "\n... [diff truncated]"
    return text


# --------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--findings", required=True)
    ap.add_argument("--metadata", default=None)
    ap.add_argument("--out", default="payloads.json")
    args = ap.parse_args()

    with open(args.findings, encoding="utf-8") as fh:
        data = json.load(fh)
    base, head = data.get("base", "origin/main"), data.get("head", "HEAD")
    funcs = load_metadata(args.metadata)

    payloads = []
    for i, f in enumerate(data["findings"], 1):
        try:
            with open(os.path.join(args.repo, f["file"]), encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            print(f"[warn] cannot read {f['file']}, skipping", file=sys.stderr)
            continue
        lines = source.splitlines()

        found = None
        try:
            found = find_enclosing(ast.parse(source), f["line"])
        except SyntaxError:
            pass

        if found:
            qual, node = found
            start, end = node.lineno, node.end_lineno
        else:
            qual = None
            start = max(1, f["line"] - FALLBACK_WINDOW)
            end = min(len(lines), f["line"] + FALLBACK_WINDOW)

        chunk = lines[start - 1:end]
        truncated = len(chunk) > MAX_FUNC_LINES
        code = numbered(chunk[:MAX_FUNC_LINES], start)
        if truncated:
            code += "\n... [function truncated]"

        meta = find_metadata(funcs, f["file"], qual) if (qual and funcs) else None
        related = {}
        if meta:
            called_by = meta.get("called_by") or callers_of(funcs, qual)
            related = {
                "signature": meta.get("signature"),
                "summary": meta.get("summary"),
                "inputs": meta.get("inputs"),
                "outputs": meta.get("outputs"),
                "calls": [_call_name(c) for c in (meta.get("calls") or [])][:MAX_RELATED],
                "called_by": [_call_name(c) for c in called_by][:MAX_RELATED],
            }
            related = {k: v for k, v in related.items() if v}

        payloads.append({
            "id": f"F{i}",
            "finding": f,
            "function": {"name": qual, "start_line": start, "end_line": end, "code": code},
            "diff_hunk": diff_hunk(args.repo, base, head, f["file"]),
            "metadata": related,
            "metadata_found": bool(meta),
        })

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"base": base, "head": head, "payloads": payloads}, fh, indent=2)

    with_meta = sum(p["metadata_found"] for p in payloads)
    print(f"Built {len(payloads)} payloads ({with_meta} with metadata) -> {args.out}")


if __name__ == "__main__":
    main()
