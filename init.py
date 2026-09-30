"""
init.py

Single entry point for the metadata pipeline. Does three things:
  1. Checks whether `metadata-branch` exists on the remote.
     - If yes: clones just that branch into ./metadata
     - If no:  creates it (empty orphan branch) and pushes it
  2. Runs extract_metadata.py against the main repo, writing into ./metadata
  3. Commits and pushes whatever changed back to metadata-branch

This is the ONLY thing the GitHub Actions workflow calls. All the actual
logic lives here in Python, not in YAML — the workflow file just does
checkout, sets up Python, and runs `python init.py`.

Usage (matches how the workflow calls it):
    python init.py

Environment variables expected in CI (both are auto-set by GitHub Actions,
you don't need to configure them):
    GITHUB_TOKEN        - used to authenticate git push
    GITHUB_REPOSITORY   - e.g. "SkySaksham/inno"

Local usage (outside CI) works too, falling back to whatever `origin`
remote and git credentials you already have configured.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def run(cmd, cwd=None, check=True):
    print(f"$ {' '.join(cmd)}" + (f"   (in {cwd})" if cwd else ""))
    result = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0 and check:
        print(result.stderr.strip())
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return result


def get_authenticated_remote_url() -> str:
    """Builds a token-authenticated URL for CI pushes. Falls back to the
    existing `origin` remote for local runs where GITHUB_TOKEN isn't set."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")  # e.g. "SkySaksham/inno"
    if token and repo:
        return f"https://x-access-token:{token}@github.com/{repo}.git"
    return run(["git", "remote", "get-url", "origin"]).stdout.strip()


def remote_branch_exists(branch: str, remote_url: str) -> bool:
    result = run(["git", "ls-remote", "--heads", remote_url, branch], check=False)
    return bool(result.stdout.strip())


def create_metadata_branch(out_dir: Path, branch: str, remote_url: str):
    """Branch doesn't exist yet anywhere — create it as an empty orphan
    branch and push it, then leave out_dir as that fresh checkout."""
    print(f"'{branch}' not found on remote. Creating it.")
    if out_dir.exists():
        import shutil
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    run(["git", "init", "-b", branch], cwd=out_dir)
    run(["git", "config", "user.name", "metadata-bot"], cwd=out_dir)
    run(["git", "config", "user.email", "metadata-bot@users.noreply.github.com"], cwd=out_dir)
    run(["git", "remote", "add", "origin", remote_url], cwd=out_dir)
    run(["git", "commit", "--allow-empty", "-m", "init metadata branch"], cwd=out_dir)
    run(["git", "push", "origin", branch], cwd=out_dir)


def checkout_existing_branch(out_dir: Path, branch: str, remote_url: str):
    """Branch already exists remotely — clone just that branch."""
    if out_dir.exists():
        import shutil
        shutil.rmtree(out_dir)
    print(f"Cloning existing '{branch}' into {out_dir}")
    run(["git", "clone", "--branch", branch, "--single-branch", remote_url, str(out_dir)])
    run(["git", "config", "user.name", "metadata-bot"], cwd=out_dir)
    run(["git", "config", "user.email", "metadata-bot@users.noreply.github.com"], cwd=out_dir)


def commit_and_push(out_dir: Path, branch: str, commit_message: str):
    run(["git", "add", "."], cwd=out_dir)
    status = run(["git", "status", "--porcelain"], cwd=out_dir)
    if not status.stdout.strip():
        print("Nothing changed — skipping commit.")
        return
    run(["git", "commit", "-m", commit_message], cwd=out_dir)
    run(["git", "push", "origin", f"HEAD:{branch}"], cwd=out_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Path to the main repo checkout")
    parser.add_argument("--out", default="./metadata", help="Path to check out metadata-branch into")
    parser.add_argument("--branch", default="metadata-branch")
    parser.add_argument("--script", default="extract_metadata.py",
                         help="Extraction script to run, relative to --repo")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    out_dir = Path(args.out).resolve()

    remote_url = get_authenticated_remote_url()

    # Step 1: ensure metadata branch exists and is checked out at out_dir
    if remote_branch_exists(args.branch, remote_url):
        checkout_existing_branch(out_dir, args.branch, remote_url)
    else:
        create_metadata_branch(out_dir, args.branch, remote_url)

    # Step 2: run extraction (this is YOUR tool — init.py just calls it)
    script_path = repo / args.script
    print(f"Running {script_path} ...")
    result = subprocess.run(
        [sys.executable, str(script_path), "--repo", str(repo), "--out", str(out_dir)],
        cwd=repo,
    )
    if result.returncode != 0:
        sys.exit(result.returncode)

    # Step 3: commit and push whatever the extraction produced
    commit_sha = os.environ.get("GITHUB_SHA", "local-run")
    commit_and_push(out_dir, args.branch, f"metadata for main@{commit_sha}")


if __name__ == "__main__":
    main()