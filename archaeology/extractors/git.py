"""Git log extraction for archaeology pipeline."""

import csv
import os
import subprocess
from pathlib import Path

# What `git rev-parse --local-env-vars` lists, minus the config-injection
# variables (GIT_CONFIG*), which callers use on purpose (e.g. safe.directory in
# CI). Inherited, any of these makes git read a repository other than -C's.
_REPO_LOCAL_ENV = frozenset(
    (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES "
        "GIT_OBJECT_DIRECTORY GIT_DIR GIT_WORK_TREE GIT_IMPLICIT_WORK_TREE GIT_GRAFT_FILE "
        "GIT_INDEX_FILE GIT_NO_REPLACE_OBJECTS GIT_REPLACE_REF_BASE GIT_PREFIX "
        "GIT_SHALLOW_FILE GIT_COMMON_DIR"
    ).split()
)


def git_env() -> dict[str, str]:
    """The caller's environment without variables that redirect git elsewhere."""
    return {k: v for k, v in os.environ.items() if k not in _REPO_LOCAL_ENV}


def _git(repo_path: str, *args: str, missing_ok: bool = False) -> str:
    """Read Git metadata without invoking a shell or repository hooks."""
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(repo_path).expanduser()), *args],
            env=git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git binary not found. Install git and ensure it's on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("git read timed out after 300s") from exc
    if result.returncode and not (missing_ok and not result.stderr.strip()):
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return "" if result.returncode else result.stdout


def _head(repo_path: str) -> str | None:
    """HEAD's commit, or None when HEAD names an unborn branch (other refs may exist)."""
    out = _git(repo_path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", missing_ok=True)
    return out.strip() or None


def repository_coverage(repo_path: str) -> dict:
    """Record local reachability, never imply that remote/deleted refs were fetched."""
    _git(repo_path, "rev-parse", "--git-dir")
    refs = _git(repo_path, "for-each-ref", "--format=%(refname)%09%(objectname)")
    count = int(_git(repo_path, "rev-list", "--all", "--count").strip())
    return {
        "head": _head(repo_path) if count else None,
        "scope": "all locally available refs plus HEAD; no automatic remote fetch",
        "shallow": _git(repo_path, "rev-parse", "--is-shallow-repository").strip() == "true",
        "bare": _git(repo_path, "rev-parse", "--is-bare-repository").strip() == "true",
        "commit_count": count,
        "refs": [dict(zip(("name", "object"), line.split("\t", 1))) for line in refs.splitlines()],
        "roots": _git(repo_path, "rev-list", "--all", "--max-parents=0").splitlines(),
        "gaps": [
            "Deleted, inaccessible and unfetched remote refs are outside this local snapshot."
        ],
    }


def extract_git_log(repo_path: str, output_path: str, verbose: bool = False) -> int:
    """Extract every reachable commit, with NUL-delimited fields and UTC dates.

    Subjects can contain tabs, pipes and unit separators. Git NUL delimiters
    preserve those values rather than silently shifting or dropping CSV fields.
    Empty histories write a header, replacing any stale previous extraction.
    """
    import io
    from datetime import datetime, timezone

    from ..utils import atomic_write

    raw = _git(
        repo_path,
        "log",
        "--no-show-signature",
        "--use-mailmap",
        "-z",
        "--all",
        "--format=%H%x00%at%x00%s%x00%aN%x00%aE%x00%P%x00%an%x00%ae",
    )
    fields = raw.split("\0")
    if fields[-1] == "":
        fields.pop()
    if len(fields) % 8:
        raise RuntimeError("Malformed Git extraction; refusing partial history")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    # email and parents identify people across name spellings and mark
    # merges (more than one parent); older readers ignore extra columns.
    writer.writerow(
        ["hash", "date", "message", "author", "email", "parents", "raw_author", "raw_email"]
    )
    for i in range(0, len(fields), 8):
        sha, date, subject, author, email, parents, raw_author, raw_email = fields[i : i + 8]
        # Epoch seconds, not %aI: old commits can carry offsets such as +2500
        # that git records but Python's ISO parser rejects.
        normalized = datetime.fromtimestamp(int(date), timezone.utc).isoformat()
        writer.writerow([sha, normalized, subject, author, email, parents, raw_author, raw_email])
    count = len(fields) // 8
    expected = int(_git(repo_path, "rev-list", "--all", "--count").strip())
    if count != expected:
        raise RuntimeError("Repository changed during extraction; retry against frozen refs")
    atomic_write(output_path, stream.getvalue())
    if verbose:
        print(f"Extracted {count} commits from {repo_path}")
    return count


def extract_git_log_with_stats(repo_path: str, output_path: str, verbose: bool = False) -> int:
    """Extract git log with file change stats."""
    cmd = [
        "git",
        "-C",
        repo_path,
        "log",
        "--no-show-signature",
        "--use-mailmap",
        "--format=%H%x1f%ai%x1f%s%x1f%aN",
        "--shortstat",
        "--no-ext-diff",
        "--no-textconv",
        "--all",
    ]
    try:
        result = subprocess.run(
            cmd,
            env=git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except FileNotFoundError:
        raise RuntimeError("git binary not found. Install git and ensure it's on PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("git log timed out after 300s. Repository may be too large.")

    if result.returncode != 0:
        raise RuntimeError(f"git log failed: {result.stderr}")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result.stdout)

    if verbose:
        print(f"Extracted git log with stats from {repo_path}")
    return int(_git(repo_path, "rev-list", "--all", "--count").strip())


def get_repo_list(repo_path: str) -> list[str]:
    """Get list of all repos accessible from the given path (for multi-repo extraction)."""
    # If it's a GitHub user/org, use gh API
    try:
        result = subprocess.run(
            ["gh", "repo", "list", "--limit", "100", "--json", "name,url"],
            capture_output=True,
            text=True,
            cwd=repo_path,
            timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError("gh CLI not found. Install GitHub CLI and ensure it's on PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("gh repo list timed out after 60s.")

    if result.returncode == 0:
        import json

        repos = json.loads(result.stdout)
        return [r["name"] for r in repos]
    return []
