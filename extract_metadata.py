"""
extract_repo_metadata.py

Walks a Python repo, extracts function-level metadata (signatures, params,
return types, call graph) via `ast`, and calls an LLM only for what `ast`
can't determine on its own: missing type hints and one-line summaries.

Design principles:

  - Facts come from code (ast), never the LLM.
  - The LLM only fills gaps: inferred types + summaries.
  - Summaries are cached by a hash of the function body, so unchanged
    functions never trigger a new LLM call.
  - Output is sharded per source file + one call_graph.json + one index.json,
    matching the schema:
      metadata/index.json
      metadata/functions/<file>.json
      metadata/call_graph.json.

Usage:

    python extract_repo_metadata.py --repo /path/to/repo --out /path/to/metadata

GitHub Copilot CLI must be installed and authenticated before running this
script.

Copilot is invoked from the repository root, so it can inspect repository
files when additional context is needed.
"""

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


def git_blob_hash(filepath: str) -> str:
    """Real git blob hash for a file, so drift can be checked with `git ls-tree`."""
    try:
        result = subprocess.run(
            ["git", "hash-object", filepath],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    except Exception:
        with open(filepath, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()


def body_hash(source_segment: str) -> str:
    """Hash of just the function body text, used as the LLM-summary cache key."""
    return hashlib.sha256(
        source_segment.encode("utf-8")
    ).hexdigest()[:16]


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
            params.append(
                {
                    "name": arg.arg,
                    "type": annotation_to_str(arg.annotation),
                }
            )

        returns = annotation_to_str(node.returns)

        body_source = ast.get_source_segment(source, node) or ""

        # Direct calls made inside this function.
        #
        # This is intentionally simple:
        #   foo()       -> foo
        #   obj.foo()   -> foo
        #
        # It does not attempt to resolve imports, aliases, dynamic dispatch,
        # etc.
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


PROMPT_TEMPLATE = """You are generating structured metadata for ONE function
from a Python codebase.

You are running inside the repository that contains this function.

You may inspect other files in the repository if doing so helps you understand
the function, its callers, its callees, imported types, classes, constants,
configuration, or data structures.

Return ONLY a JSON object in this exact shape.
Do not return markdown.
Do not use code fences.
Do not add explanations.

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

1. Preserve any type hints already present in the function exactly.
2. If a parameter has no type hint, infer its most likely type.
3. If the return type has no annotation, infer its most likely type.
4. If you cannot confidently infer a type, use "unknown".
5. Generate a concise one-sentence summary under 20 words.
6. The summary must describe WHAT the function does, not implementation details.
7. You may inspect the repository to understand imported classes, helper
   functions, models, constants, configuration, etc.
8. Do not invent types that contradict the actual repository.
9. Do not include any function other than the one provided.
10. The "params" array must contain the same parameters as the function.
11. "name" must be exactly the supplied function name.
12. If existing type hints are present, use "declared" as type_confidence.
13. If any missing type had to be inferred, use "inferred" as type_confidence.

Function name:
{func_name}

Function file path:
{file_path}

Function source code:
{function_code}

Context — functions this one calls (name: summary):
{callee_summaries}

Context — functions that call this one (name: summary):
{caller_summaries}
"""


def call_llm(prompt: str, repo_root: Path) -> str:
    """
    Call GitHub Copilot CLI from inside the repository.

    Running Copilot with cwd=repo_root gives it the repository as its working
    directory, allowing it to inspect relevant files when the prompt requires
    additional context.
    """

    copilot = shutil.which("copilot")

    if not copilot:
        raise RuntimeError(
            "GitHub Copilot CLI was not found on PATH. "
            "Install/authenticate Copilot CLI before running extract_metadata.py."
        )

    env = os.environ.copy()

    # GitHub CLI / Copilot tooling can use GH_TOKEN. Keep the existing
    # GITHUB_TOKEN supplied by GitHub Actions and expose it as GH_TOKEN too.
    if env.get("GITHUB_TOKEN") and not env.get("GH_TOKEN"):
        env["GH_TOKEN"] = env["GITHUB_TOKEN"]

    print("Calling GitHub Copilot...")

    result = subprocess.run(
        [
            copilot,
            "--prompt",
            prompt,
            "--silent",
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        print("Copilot stderr:")
        print(result.stderr.strip())

        raise RuntimeError(
            f"GitHub Copilot CLI failed with exit code {result.returncode}"
        )

    output = result.stdout.strip()

    if not output:
        raise RuntimeError(
            "GitHub Copilot CLI returned an empty response."
        )

    return output


def parse_llm_json(raw: str) -> dict:
    """
    Parse Copilot's JSON response.

    The prompt asks for raw JSON, but this also tolerates accidental markdown
    code fences or surrounding text.
    """

    raw = raw.strip()

    # Normal case: Copilot returned exactly JSON.
    try:
        result = json.loads(raw)

        if not isinstance(result, dict):
            raise ValueError("Copilot response was not a JSON object.")

        return result

    except json.JSONDecodeError:
        pass

    # Tolerate ```json ... ``` despite explicitly asking Copilot not to use it.
    if raw.startswith("```"):
        lines = raw.splitlines()

        if lines and lines[0].startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        cleaned = "\n".join(lines).strip()

        try:
            result = json.loads(cleaned)

            if not isinstance(result, dict):
                raise ValueError("Copilot response was not a JSON object.")

            return result

        except json.JSONDecodeError:
            pass

    # Last-resort extraction of the outermost JSON object.
    start = raw.find("{")
    end = raw.rfind("}")

    if start != -1 and end != -1 and end > start:
        candidate = raw[start : end + 1]

        try:
            result = json.loads(candidate)

            if not isinstance(result, dict):
                raise ValueError("Copilot response was not a JSON object.")

            return result

        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        "Could not parse GitHub Copilot response as JSON.\n"
        f"Raw response:\n{raw}"
    )


def infer_missing_metadata(
    func_name: str,
    func_data: dict,
    callee_summaries: dict,
    caller_summaries: dict,
    repo_root: Path,
) -> dict:
    """
    Ask Copilot to infer missing types and generate the function summary.
    """

    prompt = PROMPT_TEMPLATE.format(
        func_name=func_name,
        file_path=func_data["file"],
        function_code=func_data["body_source"],
        callee_summaries=json.dumps(
            callee_summaries,
            indent=2,
        ),
        caller_summaries=json.dumps(
            caller_summaries,
            indent=2,
        ),
    )

    raw = call_llm(prompt, repo_root)
    result = parse_llm_json(raw)

    # Validate the basic shape before the result is merged into AST metadata.
    if result.get("name") != func_name:
        raise RuntimeError(
            f"Copilot returned metadata for "
            f"{result.get('name')!r} instead of {func_name!r}."
        )

    if not isinstance(result.get("params"), list):
        raise RuntimeError(
            f"Copilot returned invalid params for {func_name}."
        )

    if "returns" not in result:
        result["returns"] = "unknown"

    if "summary" not in result:
        result["summary"] = ""

    if "type_confidence" not in result:
        result["type_confidence"] = "inferred"

    return result


def needs_llm(func_data: dict, cached_entry: dict | None) -> bool:
    """
    LLM is skipped entirely if types are fully declared AND the body
    hash matches a cached summary.

    This keeps token usage low.
    """

    has_missing_types = (
        func_data["returns"] is None
        or any(
            p["type"] is None
            for p in func_data["params"]
        )
    )

    cache_hit = (
        cached_entry is not None
        and cached_entry.get("body_hash") == func_data["body_hash"]
    )

    return has_missing_types or not cache_hit


def load_existing_functions(out_dir: Path) -> dict:
    """
    Flatten all existing function shards into {func_name: entry} for
    cache lookups by body_hash.
    """

    existing = {}

    functions_dir = out_dir / "functions"

    if not functions_dir.exists():
        return existing

    for shard_file in functions_dir.glob("*.json"):
        with open(shard_file, encoding="utf-8") as f:
            shard = json.load(f)

        existing.update(shard)

    return existing


def shard_name_for(filepath: str) -> str:
    return (
        filepath
        .replace("/", "_")
        .replace("\\", "_")
        .replace(".py", "")
        + ".json"
    )


def build_call_graph(all_functions: dict) -> dict:
    graph = {
        name: {
            "calls": [],
            "called_by": [],
        }
        for name in all_functions
    }

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

    parser.add_argument(
        "--repo",
        required=True,
        help="Path to the repo root",
    )

    parser.add_argument(
        "--out",
        required=True,
        help="Path to write metadata/ into",
    )

    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        help="Optional: only process these file paths (for PR-diff mode)",
    )

    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    out_dir = Path(args.out).resolve()

    (out_dir / "functions").mkdir(
        parents=True,
        exist_ok=True,
    )

    existing_functions = load_existing_functions(out_dir)

    py_files = args.only or [
        str(p.relative_to(repo_root))
        for p in repo_root.rglob("*.py")
        if ".git" not in p.parts
    ]

    all_functions = {}
    index_files = {}

    # ------------------------------------------------------------
    # 1. AST EXTRACTION
    # ------------------------------------------------------------

    for rel_path in py_files:
        abs_path = repo_root / rel_path

        if not abs_path.exists():
            continue

        with open(
            abs_path,
            "r",
            encoding="utf-8",
        ) as f:
            source = f.read()

        file_functions = extract_functions_from_file(
            str(rel_path),
            source,
        )

        all_functions.update(file_functions)

        index_files[str(rel_path)] = {
            "blob_hash": git_blob_hash(str(abs_path)),
            "functions": list(file_functions.keys()),
        }

    # ------------------------------------------------------------
    # 2. BUILD CALL GRAPH
    # ------------------------------------------------------------

    call_graph = build_call_graph(all_functions)

    def summary_for(name):
        cached = existing_functions.get(name)

        return (
            cached.get("summary")
            if cached
            else None
        )

    # ------------------------------------------------------------
    # 3. LLM ENRICHMENT
    # ------------------------------------------------------------

    for name, data in all_functions.items():
        cached = existing_functions.get(name)

        if needs_llm(data, cached):

            callee_summaries = {
                c: summary_for(c)
                for c in call_graph[name]["calls"]
                if summary_for(c)
            }

            caller_summaries = {
                c: summary_for(c)
                for c in call_graph[name]["called_by"]
                if summary_for(c)
            }

            inferred = infer_missing_metadata(
                name,
                data,
                callee_summaries,
                caller_summaries,
                repo_root,
            )

            # ------------------------------------------------
            # Preserve AST-declared parameter types.
            # Only use Copilot for missing types.
            # ------------------------------------------------

            final_params = []

            for i, p in enumerate(data["params"]):

                if p["type"] is not None:
                    # AST knows this type exactly.
                    final_params.append(p)

                else:
                    # AST had no type, so use Copilot's inference.
                    if i < len(inferred["params"]):
                        inferred_param = inferred["params"][i]

                        final_params.append(
                            {
                                "name": p["name"],
                                "type": inferred_param.get(
                                    "type",
                                    "unknown",
                                ),
                            }
                        )

                    else:
                        final_params.append(
                            {
                                "name": p["name"],
                                "type": "unknown",
                            }
                        )

            data["params"] = final_params

            # ------------------------------------------------
            # Preserve AST return annotation if one exists.
            # Otherwise use Copilot's inferred return type.
            # ------------------------------------------------

            data["returns"] = (
                data["returns"]
                or inferred.get("returns", "unknown")
            )

            # ------------------------------------------------
            # Copilot-generated summary.
            # ------------------------------------------------

            data["summary"] = inferred.get(
                "summary",
                "",
            )

            data["type_confidence"] = inferred.get(
                "type_confidence",
                "inferred",
            )

        else:
            # Cached function:
            # reuse the previous summary and metadata.
            data["summary"] = cached["summary"]

            data["type_confidence"] = "declared"

        # body_source is only needed while generating the LLM prompt.
        # Do not store the source code in metadata.
        del data["body_source"]

    # ------------------------------------------------------------
    # 4. WRITE SHARDED FUNCTION METADATA
    # ------------------------------------------------------------

    by_file = {}

    for name, data in all_functions.items():
        shard = shard_name_for(data["file"])

        by_file.setdefault(
            shard,
            {},
        )[name] = data

    for shard, funcs in by_file.items():
        with open(
            out_dir / "functions" / shard,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                funcs,
                f,
                indent=2,
            )

    # ------------------------------------------------------------
    # 5. WRITE CALL GRAPH
    # ------------------------------------------------------------

    with open(
        out_dir / "call_graph.json",
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            call_graph,
            f,
            indent=2,
        )

    # ------------------------------------------------------------
    # 6. WRITE TOP-LEVEL INDEX
    # ------------------------------------------------------------

    index = {
        "indexed_sha": subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "HEAD",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        or "unknown",

        "files": index_files,
    }

    with open(
        out_dir / "index.json",
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            index,
            f,
            indent=2,
        )

    # ------------------------------------------------------------
    # 7. SUMMARY
    # ------------------------------------------------------------

    llm_calls = sum(
        1
        for _, data in all_functions.items()
        if data.get("type_confidence") == "inferred"
    )

    print(
        f"Indexed {len(all_functions)} functions "
        f"across {len(py_files)} files."
    )

    print(f"LLM calls made: {llm_calls}")


if __name__ == "__main__":
    main()