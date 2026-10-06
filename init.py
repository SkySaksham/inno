"""
init.py

Single entry point for the metadata pipeline.

Does three things:
  1. Checks whether `metadata-branch` exists on the remote.
     - If yes: clones just that branch into ./metadata
     - If no: creates it as an empty orphan branch and pushes it
  2. Runs extract_metadata.py against the main repo
     - optionally only for the changed files passed via --only
  3. Commits and pushes whatever changed back to metadata-branch

This is the ONLY thing the GitHub Actions workflow calls.

Environment variables expected in CI:
    GITHUB_TOKEN
        Used to authenticate git push.

    GITHUB_REPOSITORY
        Repository name, e.g. "SkySaksham/inno".

    GITHUB_SHA
        Commit SHA being indexed.

Local usage also works, falling back to the configured git origin.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd, cwd=None, check=True):
    """Run a command and print useful output."""
    display = " ".join(str(x) for x in cmd)
    location = f"   (in {cwd})" if cwd else ""
    print(f"$ {display}{location}")

    result = subprocess.run(
        [str(x) for x in cmd],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode != 0 and check:
        if result.stderr.strip():
            print(result.stderr.strip())

        raise RuntimeError(
            f"Command failed ({result.returncode}): {display}"
        )

    return result


def get_authenticated_remote_url() -> str:
    """
    Build a token-authenticated GitHub URL for CI pushes.

    Falls back to the repository's existing origin remote for local runs.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")

    if token and repo:
        return f"https://x-access-token:{token}@github.com/{repo}.git"

    return run(
        ["git", "remote", "get-url", "origin"]
    ).stdout.strip()


def remote_branch_exists(branch: str, remote_url: str) -> bool:
    """Return True if the metadata branch already exists remotely."""
    result = run(
        ["git", "ls-remote", "--heads", remote_url, branch],
        check=False,
    )

    return bool(result.stdout.strip())


def create_metadata_branch(
    out_dir: Path,
    branch: str,
    remote_url: str,
):
    """
    Create metadata-branch as an orphan branch.

    The branch starts empty and then gets populated by the extractor.
    """
    print(f"'{branch}' not found on remote. Creating it.")

    if out_dir.exists():
        shutil.rmtree(out_dir)

    out_dir.mkdir(parents=True)

    run(
        ["git", "init", "-b", branch],
        cwd=out_dir,
    )

    run(
        ["git", "config", "user.name", "metadata-bot"],
        cwd=out_dir,
    )

    run(
        [
            "git",
            "config",
            "user.email",
            "metadata-bot@users.noreply.github.com",
        ],
        cwd=out_dir,
    )

    run(
        ["git", "remote", "add", "origin", remote_url],
        cwd=out_dir,
    )

    run(
        [
            "git",
            "commit",
            "--allow-empty",
            "-m",
            "init metadata branch",
        ],
        cwd=out_dir,
    )

    run(
        ["git", "push", "origin", branch],
        cwd=out_dir,
    )


def checkout_existing_branch(
    out_dir: Path,
    branch: str,
    remote_url: str,
):
    """
    Clone only the metadata branch.

    A shallow clone is enough because we only need the current metadata
    state, not the entire branch history.
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)

    print(f"Cloning existing '{branch}' into {out_dir}")

    run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            "--single-branch",
            remote_url,
            str(out_dir),
        ]
    )

    run(
        ["git", "config", "user.name", "metadata-bot"],
        cwd=out_dir,
    )

    run(
        [
            "git",
            "config",
            "user.email",
            "metadata-bot@users.noreply.github.com",
        ],
        cwd=out_dir,
    )


def commit_and_push(
    out_dir: Path,
    branch: str,
    commit_message: str,
):
    """Commit metadata changes if anything actually changed."""
    run(
        ["git", "add", "."],
        cwd=out_dir,
    )

    status = run(
        ["git", "status", "--porcelain"],
        cwd=out_dir,
    )

    if not status.stdout.strip():
        print("Nothing changed — skipping commit.")
        return

    run(
        ["git", "commit", "-m", commit_message],
        cwd=out_dir,
    )

    run(
        ["git", "push", "origin", f"HEAD:{branch}"],
        cwd=out_dir,
    )


def resolve_script_path(repo: Path, script: str) -> Path:
    """
    Resolve the extraction script.

    Supports both:
      --script .inno-tool/extract_metadata.py
      --script /tmp/inno-tool/extract_metadata.py
    """
    script_path = Path(script)

    if script_path.is_absolute():
        return script_path.resolve()

    return (repo / script_path).resolve()


def main():
    parser = argparse.ArgumentParser(
        description="Run the Inno metadata update pipeline."
    )

    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the main repository checkout.",
    )

    parser.add_argument(
        "--out",
        default="./metadata",
        help="Path to check out metadata-branch into.",
    )

    parser.add_argument(
        "--branch",
        default="metadata-branch",
        help="Metadata branch name.",
    )

    parser.add_argument(
        "--script",
        default="extract_metadata.py",
        help="Path to extract_metadata.py. Can be absolute.",
    )

    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        help=(
            "Only process these repository-relative files. "
            "Used for incremental metadata updates."
        ),
    )

    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    out_dir = Path(args.out).resolve()
    script_path = resolve_script_path(repo, args.script)

    if not repo.exists():
        raise RuntimeError(f"Repository path does not exist: {repo}")

    if not script_path.exists():
        raise RuntimeError(
            f"Metadata extraction script does not exist: {script_path}"
        )

    print(f"Repository: {repo}")
    print(f"Metadata output: {out_dir}")
    print(f"Extraction script: {script_path}")

    if args.only is None:
        print("Mode: full repository scan")
    elif args.only:
        print("Mode: incremental")
        print(f"Files to process: {len(args.only)}")

        for path in args.only:
            print(f"  - {path}")
    else:
        print("Mode: incremental")
        print("No files supplied.")

        # Nothing to process.
        return

    remote_url = get_authenticated_remote_url()

    # ------------------------------------------------------------
    # 1. Ensure metadata branch exists locally.
    # ------------------------------------------------------------

    if remote_branch_exists(args.branch, remote_url):
        checkout_existing_branch(
            out_dir,
            args.branch,
            remote_url,
        )
    else:
        create_metadata_branch(
            out_dir,
            args.branch,
            remote_url,
        )

    # ------------------------------------------------------------
    # 2. Run metadata extraction.
    # ------------------------------------------------------------

    command = [
        sys.executable,
        str(script_path),
        "--repo",
        str(repo),
        "--out",
        str(out_dir),
    ]

    # Only add --only when incremental files were supplied.
    if args.only is not None:
        command.append("--only")
        command.extend(args.only)

    print()
    print("Running metadata extraction...")
    print()

    result = subprocess.run(
        command,
        cwd=repo,
        check=False,
    )

    if result.returncode != 0:
        print(
            f"Metadata extraction failed with exit code "
            f"{result.returncode}"
        )
        sys.exit(result.returncode)

    # ------------------------------------------------------------
    # 3. Commit and push metadata changes.
    # ------------------------------------------------------------

    commit_sha = os.environ.get(
        "GITHUB_SHA",
        "local-run",
    )

    commit_and_push(
        out_dir,
        args.branch,
        f"metadata for main@{commit_sha}",
    )


if __name__ == "__main__":
    main()