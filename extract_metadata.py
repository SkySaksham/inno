"""
extract_metadata.py

Incremental repository metadata extractor.

What it does:
  1. Reads tracked Python files from git.
  2. In incremental mode, processes only files supplied through --only.
  3. Reuses metadata for unchanged files and unchanged functions.
  4. Calls Copilot only for genuinely new/changed functions.
  5. Rebuilds the call graph from the merged metadata.
  6. Rebuilds index.json from the current repository state.
  7. Removes metadata for deleted files/functions.
  8. Writes:
       index.json
       call_graph.json
       functions/<file>.json

The LLM is used only for:
  - missing/inferred types
  - one-sentence function summaries

AST remains the source of truth for:
  - function names
  - declared types
  - line numbers
  - direct calls
"""

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run_command(cmd, cwd=None, check=True):
    """Run a subprocess and return the completed process."""
    result = subprocess.run(
        [str(x) for x in cmd],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0 and check:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()

        details = stderr or stdout or "no output"

        raise RuntimeError(
            f"Command failed ({result.returncode}): "
            f"{' '.join(str(x) for x in cmd)}\n"
            f"{details}"
        )

    return result


def git_tracked_python_files(repo_root: Path) -> list[str]:
    """
    Return Python files tracked by git.

    This is intentionally NOT repo_root.rglob("*.py").

    That prevents tool files, generated files, virtual environments,
    metadata directories, etc. from accidentally entering the index.
    """
    result = run_command(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "-z",
            "--",
            "*.py",
        ],
    )

    raw = result.stdout

    if not raw:
        return []

    files = [
        path
        for path in raw.split("\0")
        if path
    ]

    return sorted(set(files))


def git_blob_hashes(repo_root: Path) -> dict[str, str]:
    """
    Get git blob hashes for all tracked Python files in one git call.
    """
    result = run_command(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "-s",
            "-z",
            "--",
            "*.py",
        ],
    )

    hashes = {}

    for record in result.stdout.split("\0"):
        if not record.strip():
            continue

        header, filepath = record.split("\t", 1)

        parts = header.split()

        if len(parts) < 2:
            continue

        blob_hash = parts[1]
        hashes[filepath] = blob_hash

    return hashes


def git_head(repo_root: Path) -> str:
    """Return the current repository HEAD SHA."""
    result = run_command(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "HEAD",
        ],
    )

    return result.stdout.strip() or "unknown"


# ---------------------------------------------------------------------------
# Hashing / AST helpers
# ---------------------------------------------------------------------------

def git_blob_hash(filepath: str) -> str:
    """
    Fallback hash helper.

    The main index path uses git_blob_hashes() for efficiency, but this
    function remains useful for local/direct calls.
    """
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
    """
    Hash only the function source.

    If a function's body/source is unchanged, its previous LLM metadata
    can safely be reused.
    """
    return hashlib.sha256(
        source_segment.encode("utf-8")
    ).hexdigest()[:16]


def annotation_to_str(node) -> str | None:
    """Convert an AST annotation into source-like text."""
    if node is None:
        return None

    try:
        return ast.unparse(node)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Function extraction
# ---------------------------------------------------------------------------

def extract_functions_from_file(
    filepath: str,
    source: str,
) -> dict:
    """
    Extract function-level facts using Python AST.

    Returns:
      {
        function_name: {
          file,
          line_start,
          line_end,
          params,
          returns,
          body_hash,
          body_source,
          calls
        }
      }
    """
    tree = ast.parse(source)

    functions = {}

    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue

        params = []

        # Positional-only arguments.
        for arg in getattr(node.args, "posonlyargs", []):
            params.append(
                {
                    "name": arg.arg,
                    "type": annotation_to_str(arg.annotation),
                }
            )

        # Normal positional arguments.
        for arg in node.args.args:
            params.append(
                {
                    "name": arg.arg,
                    "type": annotation_to_str(arg.annotation),
                }
            )

        # *args
        if node.args.vararg is not None:
            params.append(
                {
                    "name": node.args.vararg.arg,
                    "type": annotation_to_str(
                        node.args.vararg.annotation
                    ),
                }
            )

        # Keyword-only arguments.
        for arg in node.args.kwonlyargs:
            params.append(
                {
                    "name": arg.arg,
                    "type": annotation_to_str(arg.annotation),
                }
            )

        # **kwargs
        if node.args.kwarg is not None:
            params.append(
                {
                    "name": node.args.kwarg.arg,
                    "type": annotation_to_str(
                        node.args.kwarg.annotation
                    ),
                }
            )

        returns = annotation_to_str(node.returns)

        body_source = (
            ast.get_source_segment(source, node)
            or ""
        )

        calls = []

        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue

            fname = None

            if isinstance(child.func, ast.Name):
                fname = child.func.id

            elif isinstance(child.func, ast.Attribute):
                fname = child.func.attr

            if fname:
                calls.append(fname)

        functions[node.name] = {
            "name": node.name,
            "file": filepath,
            "line_start": node.lineno,
            "line_end": getattr(
                node,
                "end_lineno",
                node.lineno,
            ),
            "params": params,
            "returns": returns,
            "body_hash": body_hash(body_source),
            "body_source": body_source,
            "calls": sorted(set(calls)),
        }

    return functions


# ---------------------------------------------------------------------------
# Copilot prompt
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------

def call_llm(prompt: str, repo_root: Path) -> str:
    """Call GitHub Copilot CLI from the repository root."""
    copilot = shutil.which("copilot")

    if not copilot:
        raise RuntimeError(
            "GitHub Copilot CLI was not found on PATH. "
            "Install/authenticate Copilot CLI first."
        )

    env = os.environ.copy()

    if env.get("GITHUB_TOKEN") and not env.get("GH_TOKEN"):
        env["GH_TOKEN"] = env["GITHUB_TOKEN"]

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
            "GitHub Copilot CLI failed with exit code "
            f"{result.returncode}"
        )

    output = result.stdout.strip()

    if not output:
        raise RuntimeError(
            "GitHub Copilot CLI returned an empty response."
        )

    return output


def parse_llm_json(raw: str) -> dict:
    """Parse Copilot JSON, tolerating accidental formatting."""
    raw = raw.strip()

    # Normal case.
    try:
        result = json.loads(raw)

        if not isinstance(result, dict):
            raise ValueError(
                "Copilot response was not a JSON object."
            )

        return result

    except json.JSONDecodeError:
        pass

    # Markdown fence.
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
                raise ValueError(
                    "Copilot response was not a JSON object."
                )

            return result

        except json.JSONDecodeError:
            pass

    # Last-resort outer JSON object extraction.
    start = raw.find("{")
    end = raw.rfind("}")

    if start != -1 and end > start:
        candidate = raw[start:end + 1]

        try:
            result = json.loads(candidate)

            if not isinstance(result, dict):
                raise ValueError(
                    "Copilot response was not a JSON object."
                )

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
    """Ask Copilot for missing types and a summary."""
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

    if result.get("name") != func_name:
        raise RuntimeError(
            "Copilot returned metadata for "
            f"{result.get('name')!r} instead of "
            f"{func_name!r}."
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


# ---------------------------------------------------------------------------
# Metadata cache
# ---------------------------------------------------------------------------

def load_existing_functions(out_dir: Path) -> dict:
    """
    Load previous metadata.

    Internal key:
        (filepath, function_name)

    This avoids incorrectly mixing functions with the same name in
    different source files.
    """
    existing = {}

    functions_dir = out_dir / "functions"

    if not functions_dir.exists():
        return existing

    for shard_file in functions_dir.glob("*.json"):
        try:
            with open(
                shard_file,
                encoding="utf-8",
            ) as f:
                shard = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        if not isinstance(shard, dict):
            continue

        for function_name, entry in shard.items():
            if not isinstance(entry, dict):
                continue

            filepath = entry.get("file")

            if not filepath:
                continue

            name = entry.get(
                "name",
                function_name,
            )

            existing[(filepath, name)] = entry

    return existing


def shard_name_for(filepath: str) -> str:
    """Convert source path to stable JSON shard filename."""
    return (
        filepath
        .replace("/", "_")
        .replace("\\", "_")
        .replace(".py", "")
        + ".json"
    )


# ---------------------------------------------------------------------------
# Call graph
# ---------------------------------------------------------------------------

def build_call_graph(all_functions: dict) -> dict:
    """
    Build the same calls/called_by graph shape used by the existing
    metadata format.
    """
    by_name = {}

    for (_, name), data in all_functions.items():
        by_name[name] = data

    graph = {
        name: {
            "calls": [],
            "called_by": [],
        }
        for name in by_name
    }

    for name, data in by_name.items():
        for called in data.get("calls", []):
            if called in graph:
                graph[name]["calls"].append(called)
                graph[called]["called_by"].append(name)

    for entry in graph.values():
        entry["calls"] = sorted(
            set(entry["calls"])
        )
        entry["called_by"] = sorted(
            set(entry["called_by"])
        )

    return graph


# ---------------------------------------------------------------------------
# Cache / enrichment helpers
# ---------------------------------------------------------------------------

def needs_llm(
    func_data: dict,
    cached_entry: dict | None,
) -> bool:
    """
    Only call Copilot when this function is genuinely new or its source
    changed.

    This is the major cache optimization.

    Missing type hints alone do NOT trigger another Copilot call if the
    function body/source has not changed.
    """
    if cached_entry is None:
        return True

    return (
        cached_entry.get("body_hash")
        != func_data.get("body_hash")
    )


def merge_cached_enrichment(
    ast_data: dict,
    cached: dict,
) -> dict:
    """
    Keep fresh AST facts while reusing previous LLM-derived metadata.
    """
    data = dict(ast_data)

    cached_params = cached.get("params") or []
    fresh_params = data.get("params") or []

    merged_params = []

    for index, fresh_param in enumerate(fresh_params):
        fresh_type = fresh_param.get("type")

        if fresh_type is not None:
            merged_params.append(
                {
                    "name": fresh_param["name"],
                    "type": fresh_type,
                }
            )
            continue

        cached_type = None

        if index < len(cached_params):
            cached_param = cached_params[index]

            if (
                cached_param.get("name")
                == fresh_param.get("name")
            ):
                cached_type = cached_param.get("type")

        merged_params.append(
            {
                "name": fresh_param["name"],
                "type": cached_type or "unknown",
            }
        )

    data["params"] = merged_params

    if data.get("returns") is None:
        data["returns"] = cached.get(
            "returns",
            "unknown",
        )

    data["summary"] = cached.get(
        "summary",
        "",
    )

    data["type_confidence"] = cached.get(
        "type_confidence",
        "inferred",
    )

    return data


def merge_llm_result(
    ast_data: dict,
    inferred: dict,
) -> dict:
    """
    Merge Copilot output with AST facts.

    AST-declared types always win.
    """
    data = dict(ast_data)

    final_params = []

    inferred_params = inferred.get(
        "params",
        [],
    )

    for index, param in enumerate(
        ast_data.get("params", [])
    ):
        declared_type = param.get("type")

        if declared_type is not None:
            final_params.append(
                {
                    "name": param["name"],
                    "type": declared_type,
                }
            )
            continue

        inferred_type = "unknown"

        if index < len(inferred_params):
            inferred_type = inferred_params[index].get(
                "type",
                "unknown",
            )

        final_params.append(
            {
                "name": param["name"],
                "type": inferred_type,
            }
        )

    data["params"] = final_params

    data["returns"] = (
        ast_data.get("returns")
        or inferred.get(
            "returns",
            "unknown",
        )
    )

    data["summary"] = inferred.get(
        "summary",
        "",
    )

    data["type_confidence"] = inferred.get(
        "type_confidence",
        "inferred",
    )

    return data


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def normalize_relative_path(path: str) -> str:
    """
    Normalize a repository-relative path supplied through --only.
    """
    normalized = os.path.normpath(path).replace(
        os.sep,
        "/",
    )

    if normalized in ("", "."):
        raise ValueError(
            f"Invalid repository path: {path!r}"
        )

    if normalized.startswith("../") or normalized == "..":
        raise ValueError(
            f"Path must be repository-relative: {path!r}"
        )

    return normalized


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--repo",
        required=True,
        help="Path to repository root.",
    )

    parser.add_argument(
        "--out",
        required=True,
        help="Path to metadata output directory.",
    )

    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        help=(
            "Optional repository-relative Python files to process. "
            "When supplied, all other files reuse existing metadata."
        ),
    )

    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    out_dir = Path(args.out).resolve()

    if not repo_root.exists():
        raise RuntimeError(
            f"Repository does not exist: {repo_root}"
        )

    (out_dir / "functions").mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("=" * 72)
    print("INNO METADATA EXTRACTOR")
    print("=" * 72)
    print(f"Repository : {repo_root}")
    print(f"Output     : {out_dir}")
    print()

    # ------------------------------------------------------------------
    # 1. Determine current repository Python files.
    # ------------------------------------------------------------------

    current_files = git_tracked_python_files(
        repo_root
    )

    current_file_set = set(current_files)

    print(
        f"Tracked Python files in repository: "
        f"{len(current_files)}"
    )

    # ------------------------------------------------------------------
    # 2. Load old metadata.
    # ------------------------------------------------------------------

    existing_functions = load_existing_functions(
        out_dir
    )

    metadata_exists = (
        (out_dir / "index.json").exists()
        or bool(existing_functions)
    )

    print(
        f"Existing cached functions: "
        f"{len(existing_functions)}"
    )

    # ------------------------------------------------------------------
    # 3. Decide which source files need AST processing.
    # ------------------------------------------------------------------

    if args.only is None:
        target_files = current_files

        print("Mode: FULL INDEX")
        print(
            "All tracked Python files will be parsed."
        )

    else:
        requested_files = [
            normalize_relative_path(path)
            for path in args.only
            if path.strip()
        ]

        requested_files = sorted(
            set(requested_files)
        )

        # On the very first run there is no cache to incrementally update.
        # Therefore a complete index is required.
        if not metadata_exists:
            target_files = current_files

            print(
                "Mode: INITIAL INDEX"
            )
            print(
                "No existing metadata found; "
                "performing a full index."
            )

        else:
            target_files = requested_files

            print("Mode: INCREMENTAL")

            if target_files:
                print(
                    f"Files supplied by workflow: "
                    f"{len(target_files)}"
                )

                for filepath in target_files:
                    print(f"  - {filepath}")

            else:
                print(
                    "No files supplied; nothing to process."
                )
                return

    print()

    # ------------------------------------------------------------------
    # 4. Start from cached metadata for files that still exist.
    #
    #    This is the key incremental behavior.
    # ------------------------------------------------------------------

    all_functions = {}

    for key, cached_entry in existing_functions.items():
        filepath, _ = key

        # Remove metadata for files that were deleted from the repo.
        if filepath not in current_file_set:
            continue

        all_functions[key] = dict(
            cached_entry
        )

    # ------------------------------------------------------------------
    # 5. Parse only target files.
    # ------------------------------------------------------------------

    files_reparsed = 0
    functions_reparsed = 0

    for rel_path in target_files:
        abs_path = repo_root / rel_path

        # Deleted files are intentionally skipped.
        # Their old metadata is already removed above because they are
        # absent from current_file_set.
        if not abs_path.exists():
            print(
                f"Deleted or missing: {rel_path}"
            )
            continue

        if rel_path not in current_file_set:
            print(
                f"Skipping untracked file: {rel_path}"
            )
            continue

        try:
            source = abs_path.read_text(
                encoding="utf-8"
            )
        except UnicodeDecodeError:
            print(
                f"Skipping non-UTF8 Python file: "
                f"{rel_path}"
            )
            continue

        file_functions = extract_functions_from_file(
            rel_path,
            source,
        )

        files_reparsed += 1
        functions_reparsed += len(
            file_functions
        )

        # Remove every previous function from this file.
        # This automatically handles deleted/renamed functions.
        for key in list(all_functions):
            if key[0] == rel_path:
                del all_functions[key]

        # Add the freshly parsed functions.
        for name, data in file_functions.items():
            all_functions[
                (rel_path, name)
            ] = data

    print(
        f"AST files parsed: {files_reparsed}"
    )

    print(
        f"Functions parsed: {functions_reparsed}"
    )

    # ------------------------------------------------------------------
    # 6. Build global call graph before LLM enrichment.
    # ------------------------------------------------------------------

    call_graph = build_call_graph(
        all_functions
    )

    # ------------------------------------------------------------------
    # 7. Prepare summary lookup.
    # ------------------------------------------------------------------

    def summary_for(name: str):
        """
        Find an existing summary by function name.

        This preserves the original metadata format, where call_graph
        references functions by name.
        """
        for (_, func_name), data in all_functions.items():
            if func_name != name:
                continue

            summary = data.get("summary")

            if summary:
                return summary

        return None

    # ------------------------------------------------------------------
    # 8. Identify functions requiring Copilot.
    # ------------------------------------------------------------------

    pending = []

    for key, data in list(
        all_functions.items()
    ):
        filepath, func_name = key

        cached = existing_functions.get(key)

        if needs_llm(
            data,
            cached,
        ):
            pending.append(
                (
                    key,
                    data,
                    cached,
                )
            )
        elif cached is not None:
            # Reuse cached LLM enrichment while preserving fresh AST data.
            all_functions[key] = (
                merge_cached_enrichment(
                    data,
                    cached,
                )
            )

    print()
    print(
        f"Functions requiring Copilot: "
        f"{len(pending)}"
    )

    # ------------------------------------------------------------------
    # 9. Copilot enrichment.
    #
    #    Calls are parallelized because each function is independent.
    #
    #    Configure with:
    #      INNO_LLM_CONCURRENCY=4
    #
    #    Default = 4.
    # ------------------------------------------------------------------

    if pending:
        try:
            concurrency = int(
                os.environ.get(
                    "INNO_LLM_CONCURRENCY",
                    "4",
                )
            )
        except ValueError:
            concurrency = 4

        concurrency = max(
            1,
            min(concurrency, 8),
        )

        print(
            f"Copilot concurrency: "
            f"{concurrency}"
        )

        pending_function_names = {
            key: data["name"]
            for key, data, _ in pending
        }

        def enrich_one(item):
            key, data, _cached = item

            func_name = data["name"]

            callee_summaries = {
                called: summary_for(called)
                for called in call_graph.get(
                    func_name,
                    {},
                ).get("calls", [])
                if summary_for(called)
            }

            caller_summaries = {
                caller: summary_for(caller)
                for caller in call_graph.get(
                    func_name,
                    {},
                ).get("called_by", [])
                if summary_for(caller)
            }

            inferred = infer_missing_metadata(
                func_name=func_name,
                func_data=data,
                callee_summaries=callee_summaries,
                caller_summaries=caller_summaries,
                repo_root=repo_root,
            )

            return (
                key,
                merge_llm_result(
                    data,
                    inferred,
                ),
            )

        llm_results = {}

        with ThreadPoolExecutor(
            max_workers=concurrency
        ) as executor:
            futures = [
                executor.submit(
                    enrich_one,
                    item,
                )
                for item in pending
            ]

            for index, future in enumerate(
                as_completed(futures),
                start=1,
            ):
                key, enriched = future.result()

                llm_results[key] = enriched

                print(
                    f"Copilot completed "
                    f"{index}/{len(futures)}: "
                    f"{key[0]}::{key[1]}"
                )

        for key, enriched in llm_results.items():
            all_functions[key] = enriched

    # ------------------------------------------------------------------
    # 10. Remove temporary source code from metadata.
    # ------------------------------------------------------------------

    for data in all_functions.values():
        data.pop(
            "body_source",
            None,
        )

    # ------------------------------------------------------------------
    # 11. Rebuild the call graph after all functions are finalized.
    # ------------------------------------------------------------------

    call_graph = build_call_graph(
        all_functions
    )

    # ------------------------------------------------------------------
    # 12. Rewrite function shards.
    #
    #     We intentionally rebuild the small JSON shards from the merged
    #     metadata. This guarantees deleted functions/files disappear.
    # ------------------------------------------------------------------

    functions_dir = out_dir / "functions"

    functions_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Remove stale shards.
    for shard_file in functions_dir.glob(
        "*.json"
    ):
        shard_file.unlink()

    by_file = {}

    for (filepath, func_name), data in all_functions.items():
        shard = shard_name_for(filepath)

        by_file.setdefault(
            shard,
            {},
        )[func_name] = data

    for shard, funcs in sorted(
        by_file.items()
    ):
        with open(
            functions_dir / shard,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                funcs,
                f,
                indent=2,
                sort_keys=True,
            )
            f.write("\n")

    # ------------------------------------------------------------------
    # 13. Build top-level index.
    # ------------------------------------------------------------------

    blob_hashes = git_blob_hashes(
        repo_root
    )

    index_files = {}

    for rel_path in current_files:
        functions = []

        for (filepath, func_name), _data in all_functions.items():
            if filepath == rel_path:
                functions.append(func_name)

        index_files[rel_path] = {
            "blob_hash": blob_hashes.get(
                rel_path,
                git_blob_hash(
                    str(repo_root / rel_path)
                ),
            ),
            "functions": sorted(
                functions
            ),
        }

    index = {
        "indexed_sha": git_head(
            repo_root
        ),
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
            sort_keys=True,
        )
        f.write("\n")

    # ------------------------------------------------------------------
    # 14. Write call graph.
    # ------------------------------------------------------------------

    with open(
        out_dir / "call_graph.json",
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            call_graph,
            f,
            indent=2,
            sort_keys=True,
        )
        f.write("\n")

    # ------------------------------------------------------------------
    # 15. Summary.
    # ------------------------------------------------------------------

    print()
    print("=" * 72)
    print("METADATA COMPLETE")
    print("=" * 72)

    print(
        f"Indexed functions : "
        f"{len(all_functions)}"
    )

    print(
        f"Indexed files     : "
        f"{len(current_files)}"
    )

    print(
        f"AST files parsed  : "
        f"{files_reparsed}"
    )

    print(
        f"Copilot calls     : "
        f"{len(pending)}"
    )

    print(
        f"Metadata output   : "
        f"{out_dir}"
    )

    print("=" * 72)


if __name__ == "__main__":
    main()