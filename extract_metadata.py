"""
extract_repo_metadata.py

Walks a Python repo, extracts function-level metadata (signatures, params,
return types, call graph) via `ast`, and calls an LLM only for what `ast`
can't determine on its own: missing type hints and one-line summaries.

Design principles (see project notes):
  - Facts come from code (ast), never the LLM.
  - The LLM only fills gaps: inferred types + summaries.
  - Summaries are cached by a hash of the function body, so unchanged
    functions never trigger a new LLM call.
  - Output is sharded per source file + one call_graph.json + one index.json,
    matching the schema: metadata/index.json, metadata/functions/<file>.json,
    metadata/call_graph.json.

Usage:
    python extract_repo_metadata.py --repo /path/to/repo --out /path/to/metadata

You must implement `call_llm()` below to point at whatever model you're
using (GitHub Models, Azure OpenAI, Anthropic, etc). It's isolated in one
function so swapping providers doesn't touch the rest of the script.
"""

import ast
import hashlib
import json
import os
import subprocess
import argparse
from pathlib import Path



def git_blob_hash(filepath: str) -> str:
    """Real git blob hash for a file, so drift can be checked with `git ls-tree`."""
    try:
        result = subprocess.run(
            ["git", "hash-object", filepath],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except Exception:
        with open(filepath, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()


def body_hash(source_segment: str) -> str:
    """Hash of just the function body text, used as the LLM-summary cache key."""
    return hashlib.sha256(source_segment.encode("utf-8")).hexdigest()[:16]



def annotation_to_str(node) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def extract_functions_from_file(filepath: str, source: str) -> dict:
    """Returns {func_name: {params, returns, line_start, line_end, body_hash,
    body_source, calls: [...]}} for every top-level and class-level function."""
    tree = ast.parse(source)
    functions = {}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        params = []
        for arg in node.args.args:
            params.append({
                "name": arg.arg,
                "type": annotation_to_str(arg.annotation),
            })

        returns = annotation_to_str(node.returns)

        body_source = ast.get_source_segment(source, node) or ""

        # Direct calls made inside this function (name-only resolution —
        # good enough for a one-hop call graph; doesn't resolve dynamic
        # dispatch or imported-module aliases beyond the simple case).
        calls = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                fname = None
                if isinstance(child.func, ast.Name):
                    fname = child.func.id
                elif isinstance(child.func, ast.Attribute):
                    fname = child.func.attr
                if fname:
                    calls.append(fname)

        functions[node.name] = {
            "file": filepath,
            "line_start": node.lineno,
            "line_end": getattr(node, "end_lineno", node.lineno),
            "params": params,
            "returns": returns,
            "body_hash": body_hash(body_source),
            "body_source": body_source,
            "calls": sorted(set(calls)),
        }

    return functions



PROMPT_TEMPLATE = """You are generating structured metadata for one function \
from a codebase, to be stored in a repo-wide metadata index. You will be \
given: the function's full source code, its file path, and (if available) \
the names and one-line summaries of functions it calls and functions that \
call it.

Return ONLY a JSON object in this exact shape, nothing else, no markdown \
fences, no explanation:

{{
  "name": "{func_name}",
  "params": [
    {{"name": "param_name", "type": "inferred_or_declared_type"}}
  ],
  "returns": "inferred_or_declared_return_type",
  "summary": "one sentence, under 20 words, describing what this function does",
  "type_confidence": "declared" or "inferred"
}}

Rules:
- If the function already has type hints in its signature, use those exact \
types and set type_confidence to 'declared'.
- If a parameter or return type has no hint, infer the most likely type from \
the function body and set type_confidence to 'inferred' for that function.
- The summary must describe only what the function does, not how it's \
implemented.
- If the function calls other functions whose summaries are provided, you \
may use that context to write a more accurate summary, but do not copy \
their summaries verbatim.
- If you cannot confidently infer a type, use 'unknown' rather than guessing.
- Do not include any function other than the one provided.

Function file path: {file_path}
Function source code:
{function_code}

Context — functions this one calls (name: summary):
{callee_summaries}

Context — functions that call this one (name: summary):
{caller_summaries}
"""


def call_llm(prompt: str) -> str:
    print("LLM CALL PLACEHOLDER")
    return ""


def infer_missing_metadata(func_name: str, func_data: dict,
                            callee_summaries: dict, caller_summaries: dict) -> dict:
    print(f"LLM CALL PLACEHOLDER: {func_name}")

    params = [
        {
            "name": p["name"],
            "type": p["type"] or "unknown"
        }
        for p in func_data["params"]
    ]

    return {
        "params": params,
        "returns": func_data["returns"] or "unknown",
        "summary": "Summary pending LLM integration.",
        "type_confidence": "inferred"
    }


def needs_llm(func_data: dict, cached_entry: dict | None) -> bool:
    """LLM is skipped entirely if types are fully declared AND the body
    hash matches a cached summary — this is what keeps token usage low."""
    has_missing_types = (
        func_data["returns"] is None
        or any(p["type"] is None for p in func_data["params"])
    )
    cache_hit = cached_entry is not None and cached_entry.get("body_hash") == func_data["body_hash"]
    return has_missing_types or not cache_hit



def load_existing_functions(out_dir: Path) -> dict:
    """Flatten all existing function shards into {func_name: entry} for
    cache lookups by body_hash."""
    existing = {}
    functions_dir = out_dir / "functions"
    if not functions_dir.exists():
        return existing
    for shard_file in functions_dir.glob("*.json"):
        with open(shard_file) as f:
            shard = json.load(f)
        existing.update(shard)
    return existing


def shard_name_for(filepath: str) -> str:
    return filepath.replace("/", "_").replace("\\", "_").replace(".py", "") + ".json"


def build_call_graph(all_functions: dict) -> dict:
    graph = {name: {"calls": [], "called_by": []} for name in all_functions}
    for name, data in all_functions.items():
        for called in data["calls"]:
            if called in graph:
                graph[name]["calls"].append(called)
                graph[called]["called_by"].append(name)
    for entry in graph.values():
        entry["calls"] = sorted(set(entry["calls"]))
        entry["called_by"] = sorted(set(entry["called_by"]))
    return graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="Path to the repo root")
    parser.add_argument("--out", required=True, help="Path to write metadata/ into")
    parser.add_argument("--only", nargs="*", default=None,
                         help="Optional: only process these file paths (for PR-diff mode)")
    args = parser.parse_args()

    repo_root = Path(args.repo)
    out_dir = Path(args.out)
    (out_dir / "functions").mkdir(parents=True, exist_ok=True)

    existing_functions = load_existing_functions(out_dir)

    py_files = args.only or [
        str(p.relative_to(repo_root))
        for p in repo_root.rglob("*.py")
        if ".git" not in p.parts
    ]

    all_functions = {}
    index_files = {}

    for rel_path in py_files:
        abs_path = repo_root / rel_path
        if not abs_path.exists():
            continue

        with open(abs_path, "r", encoding="utf-8") as f:
            source = f.read()

        file_functions = extract_functions_from_file(str(rel_path), source)
        all_functions.update(file_functions)

        index_files[str(rel_path)] = {
            "blob_hash": git_blob_hash(str(abs_path)),
            "functions": list(file_functions.keys()),
        }

    call_graph = build_call_graph(all_functions)

    def summary_for(name):
        cached = existing_functions.get(name)
        return cached.get("summary") if cached else None

    for name, data in all_functions.items():
        cached = existing_functions.get(name)
        if needs_llm(data, cached):
            callee_summaries = {c: summary_for(c) for c in call_graph[name]["calls"] if summary_for(c)}
            caller_summaries = {c: summary_for(c) for c in call_graph[name]["called_by"] if summary_for(c)}
            inferred = infer_missing_metadata(name, data, callee_summaries, caller_summaries)

            final_params = []
            for i, p in enumerate(data["params"]):
                if p["type"] is not None:
                    final_params.append(p)
                else:
                    final_params.append(inferred["params"][i] if i < len(inferred["params"]) else p)
            data["params"] = final_params
            data["returns"] = data["returns"] or inferred.get("returns", "unknown")
            data["summary"] = inferred.get("summary", "")
            data["type_confidence"] = inferred.get("type_confidence", "inferred")
        else:
            data["summary"] = cached["summary"]
            data["type_confidence"] = "declared"

        del data["body_source"]

    # Write sharded per-file function metadata
    by_file = {}
    for name, data in all_functions.items():
        shard = shard_name_for(data["file"])
        by_file.setdefault(shard, {})[name] = data

    for shard, funcs in by_file.items():
        with open(out_dir / "functions" / shard, "w") as f:
            json.dump(funcs, f, indent=2)

    # Write call graph
    with open(out_dir / "call_graph.json", "w") as f:
        json.dump(call_graph, f, indent=2)

    # Write top-level index
    index = {
        "indexed_sha": subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True
        ).stdout.strip() or "unknown",
        "files": index_files,
    }
    with open(out_dir / "index.json", "w") as f:
        json.dump(index, f, indent=2)

    print(f"Indexed {len(all_functions)} functions across {len(py_files)} files.")
    print(f"LLM calls made: {sum(1 for n, d in all_functions.items() if d.get('type_confidence') == 'inferred')}")


if __name__ == "__main__":
    main()
