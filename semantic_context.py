#!/usr/bin/env python3
"""
semantic_context.py  —  Inno semantic scan, step 1 of 2.

Builds per-changed-function payloads for the semantic LLM pass.

For every function whose body changed between base and head:
  - old source  (git show base:file, then AST-extract the function)
  - new source  (live file)
  - the unified diff hunk for that file
  - function metadata from the index (signature, summary, params, returns)
  - caller call-site snippets  (the actual lines where this function is called)
  - callee signatures          (what this function calls)

Output:  semantic_payloads.json
  {
    "base": "...", "head": "...",
    "functions": [
      {
        "id": "SF1",
        "func_name": "foo",
        "file": "src/foo.py",
        "old_source": "...",
        "new_source": "...",
        "diff_hunk": "...",
        "metadata": { signature, summary, params, returns },
        "callers": [
          { "file": "src/bar.py", "caller_func": "bar",
            "call_site_lines": "42: result = foo(x, y)" }
        ],
        "callees": [
          { "name": "helper", "signature": "helper(a: int) -> str",
            "summary": "..." }
        ]
      }, ...
    ]
  }

Usage:
    python semantic_context.py \\
        --repo .  --base origin/main  --head HEAD \\
        --metadata metadata/  \\
        --out semantic_payloads.json
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
MAX_HUNK_CHARS   = 4000
MAX_CALL_SNIPPET = 5   # lines of context around each call site
MAX_CALLERS      = 6
MAX_CALLEES      = 6


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run(cmd, cwd="."):
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


def git_show_file(repo, ref, filepath):
    """Return the content of a file at a given git ref, or None."""
    res = run(["git", "show", f"{ref}:{filepath}"], repo)
    return res.stdout if res.returncode == 0 else None


def diff_hunk_for_file(repo, base, head, filepath):
    res = run(
        ["git", "-c", "core.quotepath=off", "diff", "-U3",
         f"{base}...{head}", "--", filepath],
        repo,
    )
    text = res.stdout if res.returncode == 0 else ""
    if len(text) > MAX_HUNK_CHARS:
        text = text[:MAX_HUNK_CHARS] + "\n... [diff truncated]"
    return text


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def extract_functions(source):
    """Return {name: (node, source_text)} for every function in source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seg = ast.get_source_segment(source, node) or ""
            out[node.name] = (node, seg)
    return out


def find_call_sites(source, func_name, context=MAX_CALL_SNIPPET):
    """
    Return a list of strings, each being a small snippet (up to `context`
    lines above + 1 line + up to `context` lines below) around every line
    that calls func_name.

    Detection is simple name-match; good enough for call-site evidence.
    """
    lines = source.splitlines()
    snippets = []
    for i, line in enumerate(lines):
        # Match  func_name(  optionally preceded by . or whitespace
        if re.search(rf'\b{re.escape(func_name)}\s*\(', line):
            start = max(0, i - context)
            end   = min(len(lines), i + context + 1)
            chunk = lines[start:end]
            numbered = "\n".join(
                f"{start + j + 1}: {l}" for j, l in enumerate(chunk)
            )
            snippets.append(numbered)
    return snippets


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def load_metadata_dir(meta_dir):
    """
    Load the Inno metadata directory (index.json + functions/*.json + call_graph.json).

    Returns (index, funcs_by_file, call_graph) where:
      index        = index.json content
      funcs_by_file= {filepath: {func_name: entry}}
      call_graph   = call_graph.json content
    """
    meta_path = Path(meta_dir)
    index, call_graph = {}, {}
    funcs_by_file = {}

    index_file = meta_path / "index.json"
    if index_file.exists():
        try:
            index = json.loads(index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    cg_file = meta_path / "call_graph.json"
    if cg_file.exists():
        try:
            call_graph = json.loads(cg_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    functions_dir = meta_path / "functions"
    if functions_dir.exists():
        for shard in functions_dir.glob("*.json"):
            try:
                shard_data = json.loads(shard.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(shard_data, dict):
                continue
            for fname, entry in shard_data.items():
                if not isinstance(entry, dict):
                    continue
                fp = entry.get("file", "")
                fp_norm = norm(fp)
                funcs_by_file.setdefault(fp_norm, {})[fname] = entry

    return index, funcs_by_file, call_graph


def make_signature(entry):
    """Build a human-readable signature string from a function metadata entry."""
    name = entry.get("name", "?")
    params = entry.get("params") or []
    param_strs = []
    for p in params:
        pname = p.get("name", "?")
        ptype = p.get("type") or "unknown"
        param_strs.append(f"{pname}: {ptype}")
    ret = entry.get("returns") or "unknown"
    return f"{name}({', '.join(param_strs)}) -> {ret}"


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_payloads(repo, base, head, meta_dir):
    """
    Main logic: return a list of per-changed-function payload dicts.
    """
    changed = changed_lines(repo, base, head)
    if not changed:
        return []

    _, funcs_by_file, call_graph = load_metadata_dir(meta_dir) if meta_dir else ({}, {}, {})

    # Collect all source files we can read (for call-site search later)
    all_sources = {}   # filepath -> source text

    payloads = []
    counter = 0

    for filepath, changed_lineset in changed.items():
        abs_path = os.path.join(repo, filepath)
        if not os.path.isfile(abs_path):
            continue

        try:
            new_source = open(abs_path, encoding="utf-8").read()
        except OSError:
            print(f"[warn] cannot read {filepath}", file=sys.stderr)
            continue

        all_sources[norm(filepath)] = new_source

        old_source = git_show_file(repo, base, filepath)

        new_funcs = extract_functions(new_source)
        old_funcs = extract_functions(old_source) if old_source else {}

        diff = diff_hunk_for_file(repo, base, head, filepath)
        file_meta = funcs_by_file.get(norm(filepath), {})

        for fname, (new_node, new_seg) in new_funcs.items():
            # Only include functions whose lines overlap with the changed set
            fn_lines = set(range(new_node.lineno, (new_node.end_lineno or new_node.lineno) + 1))
            if not fn_lines & changed_lineset:
                continue

            old_seg = old_funcs.get(fname, (None, ""))[1]

            # Skip functions whose body did not change at all
            if old_seg and old_seg.strip() == new_seg.strip():
                continue

            counter += 1
            meta_entry = file_meta.get(fname)

            # Build metadata block
            metadata = {}
            if meta_entry:
                metadata = {
                    "signature": make_signature(meta_entry),
                    "summary":   meta_entry.get("summary", ""),
                    "params":    meta_entry.get("params") or [],
                    "returns":   meta_entry.get("returns"),
                    "type_confidence": meta_entry.get("type_confidence"),
                }

            # Callee info  (what this function calls)
            callees = []
            callee_names = (meta_entry or {}).get("calls") or []
            for callee_name in callee_names[:MAX_CALLEES]:
                # Search across all files
                for fp2, fm2 in funcs_by_file.items():
                    if callee_name in fm2:
                        ce = fm2[callee_name]
                        callees.append({
                            "name":      callee_name,
                            "signature": make_signature(ce),
                            "summary":   ce.get("summary", ""),
                        })
                        break
                else:
                    callees.append({"name": callee_name, "signature": None, "summary": None})

            # Caller info  (who calls this function, with call-site snippets)
            cg_entry   = call_graph.get(fname, {})
            caller_names = cg_entry.get("called_by", [])[:MAX_CALLERS]
            callers = []

            for caller_name in caller_names:
                # Find which file the caller lives in
                caller_file = None
                caller_source = None
                for fp2, fm2 in funcs_by_file.items():
                    if caller_name in fm2:
                        caller_file = fm2[caller_name].get("file", fp2)
                        break

                if caller_file:
                    cf_norm = norm(caller_file)
                    if cf_norm not in all_sources:
                        abs_cf = os.path.join(repo, caller_file)
                        if os.path.isfile(abs_cf):
                            try:
                                all_sources[cf_norm] = open(abs_cf, encoding="utf-8").read()
                            except OSError:
                                pass
                    caller_source = all_sources.get(cf_norm)

                snippets = find_call_sites(caller_source, fname) if caller_source else []
                callers.append({
                    "file":            caller_file or "unknown",
                    "caller_func":     caller_name,
                    "call_site_lines": snippets,
                })

            payloads.append({
                "id":          f"SF{counter}",
                "func_name":   fname,
                "file":        filepath,
                "start_line":  new_node.lineno,
                "changed_lines": sorted(fn_lines & changed_lineset),
                "old_source":  old_seg or "",
                "new_source":  new_seg,
                "diff_hunk":   diff,
                "metadata":    metadata,
                "callers":     callers,
                "callees":     callees,
            })

    return payloads


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Build per-function semantic review payloads."
    )
    ap.add_argument("--repo",     default=".",           help="repo root")
    ap.add_argument("--base",     required=True,         help="e.g. origin/main")
    ap.add_argument("--head",     default="HEAD")
    ap.add_argument("--metadata", default=None,          help="path to metadata/ dir")
    ap.add_argument("--out",      default="semantic_payloads.json")
    args = ap.parse_args()

    payloads = build_payloads(args.repo, args.base, args.head, args.metadata)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {"base": args.base, "head": args.head, "functions": payloads},
            fh, indent=2,
        )

    print(f"Built {len(payloads)} semantic payload(s) -> {args.out}")


if __name__ == "__main__":
    main()
