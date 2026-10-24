#!/usr/bin/env python3
"""
GitHub auto-upload helper for Technocore contributions.

Creates a public (or private) repository, uploads the current directory's
files, and prints the repo URL + latest commit hash — ready to be signed
into a contribution proof.

Requirements: the `gh` CLI authenticated (`gh auth login`), or a GitHub
personal access token in the GITHUB_TOKEN environment variable.
"""
from __future__ import annotations

import argparse
import json
import os
import fnmatch
import subprocess
import sys
import urllib.request

API = "https://api.github.com"

# Files that must never be uploaded, even if .gitignore is missing or wrong.
# This helper is meant to run in the folder that holds the DID identity,
# so it cannot rely on .gitignore alone.
SENSITIVE_PATTERNS = (
    "*.pem",
    "*.key",
    "*.secret",
    "passphrase*",
    "identity.pem",
    ".env",
    ".env.*",
    "hosts.yml",
    ".git-credentials",
)
SENSITIVE_EXCEPTIONS = {".env.example", ".env.sample"}


def run(cmd: list[str], check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=cwd)


def find_sensitive_files(names) -> list[str]:
    """Return the staged paths that look like secrets / private identity files."""
    hits = []
    for name in names:
        base = os.path.basename(name)
        if base in SENSITIVE_EXCEPTIONS:
            continue
        if any(fnmatch.fnmatch(base, pattern) for pattern in SENSITIVE_PATTERNS):
            hits.append(name)
    return hits


def ensure_no_sensitive_tracked(workdir: str) -> None:
    """Abort if a sensitive file is already tracked in workdir's git repo."""
    r = run(["git", "ls-files"], check=False, cwd=workdir)
    if r.returncode != 0:
        return  # not a git repo yet — the staged check will cover it
    hits = find_sensitive_files([line for line in r.stdout.splitlines() if line.strip()])
    if hits:
        raise RuntimeError(
            "sensitive file(s) already tracked by git: " + ", ".join(sorted(hits))
            + " — remove them from history before uploading"
        )


def ensure_no_sensitive_staged(workdir: str) -> None:
    """Abort (after unstaging) if `git add -A` staged a sensitive file."""
    r = run(["git", "diff", "--cached", "--name-only"], check=False, cwd=workdir)
    staged = [line for line in r.stdout.splitlines() if line.strip()]
    hits = find_sensitive_files(staged)
    if hits:
        run(["git", "reset"], check=False, cwd=workdir)
        raise RuntimeError(
            "refusing to upload sensitive file(s): " + ", ".join(sorted(hits))
            + " — remove them / fix .gitignore, then re-run"
        )


def have_gh() -> bool:
    try:
        r = run(["gh", "--version"], check=False)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def gh_username() -> str:
    r = run(["gh", "api", "user", "--jq", ".login"])
    if r.returncode != 0:
        raise RuntimeError("gh not authenticated — run 'gh auth login'")
    return r.stdout.strip()


def token_username(token: str) -> str:
    req = urllib.request.Request(API + "/user", headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "technocore-upload",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())["login"]


def create_repo_api(token: str, name: str, private: bool, description: str) -> str | None:
    body = json.dumps({
        "name": name,
        "private": private,
        "description": description,
        "auto_init": False,
    }).encode()
    req = urllib.request.Request(API + "/user/repos", data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "technocore-upload",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["full_name"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        if e.code == 422 and "already exists" in detail:
            return None  # repo exists — push to it
        raise RuntimeError(f"create repo failed ({e.code}): {detail}")


def push_with_token(token: str, repo_full: str, name: str, branch: str, workdir: str) -> str:
    """Push using the token for this push only (works without gh).

    The token is passed as a one-shot push URL and is never written into
    .git/config: `origin` is always the clean, token-free URL.
    """
    subprocess.run(["git", "init", "-b", branch], cwd=workdir, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=workdir, check=True, capture_output=True)
    ensure_no_sensitive_staged(workdir)
    # Only commit if there are changes (in workdir, not the caller's cwd)
    r = run(["git", "diff", "--cached", "--quiet"], check=False, cwd=workdir)
    if r.returncode != 0:
        subprocess.run(["git", "commit", "-m", f"Technocore contribution: {name}"],
                       cwd=workdir, check=True, capture_output=True)
    clean_url = f"https://github.com/{repo_full}.git"
    subprocess.run(["git", "remote", "remove", "origin"], cwd=workdir, check=False, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", clean_url], cwd=workdir, check=True, capture_output=True)
    push_url = f"https://x-access-token:{token}@github.com/{repo_full}.git"
    subprocess.run(["git", "push", push_url, f"{branch}:{branch}"],
                   cwd=workdir, check=True, capture_output=True)
    subprocess.run(["git", "branch", "--set-upstream-to", f"origin/{branch}", branch],
                   cwd=workdir, check=False, capture_output=True)
    r = run(["git", "rev-parse", "HEAD"], check=False, cwd=workdir)
    return r.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-upload a Technocore contribution to GitHub.")
    parser.add_argument("name", help="repository name (e.g. technocore-did-gameplay)")
    parser.add_argument("--private", action="store_true", help="create a private repo (default: public)")
    parser.add_argument("--public", action="store_true",
                        help="create a public repo (the default; accepted for README compatibility)")
    parser.add_argument("--description", default="Technocore DID contribution", help="repo description")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--dir", default=".", help="directory to upload (default: current)")
    args = parser.parse_args()

    if args.private and args.public:
        parser.error("--private and --public are mutually exclusive")
    workdir = os.path.abspath(args.dir)
    branch = args.branch

    try:
        ensure_no_sensitive_tracked(workdir)
        token = os.environ.get("GITHUB_TOKEN", "")
        if have_gh():
            username = gh_username()
            # Create repo with gh
            vis = "private" if args.private else "public"
            r = run(["gh", "repo", "create", args.name, f"--{vis}", "--source", workdir,
                     "--description", args.description, "--push"], check=False)
            if r.returncode != 0:
                # Repo may already exist — push to it directly
                r = run(["gh", "repo", "view", f"{username}/{args.name}"], check=False)
                if r.returncode != 0:
                    raise RuntimeError("gh repo create failed")
                subprocess.run(["git", "init", "-b", branch], cwd=workdir, check=True, capture_output=True)
                subprocess.run(["git", "add", "-A"], cwd=workdir, check=True, capture_output=True)
                ensure_no_sensitive_staged(workdir)
                rc = run(["git", "diff", "--cached", "--quiet"], check=False, cwd=workdir)
                if rc.returncode != 0:
                    subprocess.run(["git", "commit", "-m", f"Technocore contribution: {args.name}"],
                                   cwd=workdir, check=True, capture_output=True)
                subprocess.run(["git", "remote", "remove", "origin"], cwd=workdir, check=False, capture_output=True)
                subprocess.run(["git", "remote", "add", "origin",
                                f"https://github.com/{username}/{args.name}.git"],
                               cwd=workdir, check=True, capture_output=True)
                subprocess.run(["git", "push", "-u", "origin", branch], cwd=workdir, check=True, capture_output=True)
            commit = run(["git", "rev-parse", "HEAD"], cwd=workdir).stdout.strip()
            print(f"repo:    https://github.com/{username}/{args.name}")
            print(f"commit:  {commit}")
            return 0

        if token:
            username = token_username(token)
            full = create_repo_api(token, args.name, args.private, args.description)
            if full is None:
                full = f"{username}/{args.name}"
            commit = push_with_token(token, full, args.name, branch, workdir)
            print(f"repo:    https://github.com/{full}")
            print(f"commit:  {commit}")
            return 0

        print("no auth found: install/authenticate `gh` (gh auth login) or set GITHUB_TOKEN", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
