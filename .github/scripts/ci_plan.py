#!/usr/bin/env python3
"""Plan one CI run: whether to test at all, and which test legs run in full.

Every required check name (``test (<os>, <python>)``) is emitted on every pull
request, so branch protection always sees all nine. A leg that has nothing to
prove runs *light*: it reports success from a few-second job on Linux instead of
starting a macOS or Windows machine. Light legs, on pull requests only:

* every macOS and Windows leg when the pull request changes nothing but prose
  (see ``is_docs_path``); the three Linux legs still run the whole suite, which
  includes the README tests;
* macOS on Python 3.10 and 3.11. In the 30 days to 2026-11-22 neither failed
  without Linux on the same Python version also failing, while macOS 3.12 did.
  The weekly scheduled run and any main push that was not already tested run
  them in full.

On a push to main the whole run is skipped when ``merged_tree_tested`` proves
the pushed tree is the pull request head that already passed this workflow.

Any API error or surprise falls back to the full matrix: this script can make a
run do more, never report green on work it did not check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable

import merged_tree_tested

OSES = ("ubuntu-latest", "macos-latest", "windows-latest")
PYTHONS = ("3.10", "3.11", "3.12")
LIGHT_RUNNER = "ubuntu-latest"

_DOCS = [
    re.compile(p)
    for p in (
        r"^docs/",
        r"^[^/]+\.md$",  # top-level prose: README, CHANGELOG, ...
        r"^plugins/[^/]+/(README|THIRD-PARTY-NOTICES|CHANGELOG)\.md$",
        r"^LICENSE[^/]*$",
        r"^llms\.txt$",
        r"^\.github/ISSUE_TEMPLATE/",
        r"^\.github/[^/]+\.md$",
        r"^\.github/FUNDING\.yml$",
    )
]


def is_docs_path(path: str) -> bool:
    return any(p.search(path) for p in _DOCS)


def docs_only(paths: Iterable[str]) -> bool:
    paths = list(paths)
    return bool(paths) and all(is_docs_path(p) for p in paths)


def matrix(event: str, prose_only: bool) -> list[dict]:
    legs = []
    for os_name in OSES:
        for python in PYTHONS:
            why = ""
            if event == "pull_request" and os_name != "ubuntu-latest":
                if prose_only:
                    why = "the pull request changes only prose; the Linux legs run the full suite"
                elif os_name == "macos-latest" and python != "3.12":
                    why = (
                        "macOS runs Python 3.12 on pull requests; "
                        "3.10 and 3.11 run on the weekly schedule and untested main pushes"
                    )
            legs.append(
                {
                    "os": os_name,
                    "python-version": python,
                    "runner": LIGHT_RUNNER if why else os_name,
                    "light": bool(why),
                    "why": why,
                }
            )
    return legs


def changed_files(fetch: merged_tree_tested.Fetch, repository: str, number: int) -> list[str]:
    files: list[str] = []
    for page in range(1, 31):  # the API stops at 3000 files
        batch = fetch(f"/repos/{repository}/pulls/{number}/files?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("unexpected pull request files response")
        files += [f["filename"] for f in batch if isinstance(f, dict) and "filename" in f]
        if len(batch) < 100:
            return files
    raise ValueError("pull request touches more files than the API lists")


def plan(
    event: str,
    fetch: merged_tree_tested.Fetch | None,
    repository: str,
    sha: str,
    pr_number: int | None,
    workflow: str,
    base: str,
) -> tuple[bool, list[dict], str]:
    if event == "push":
        try:
            tested, reason = merged_tree_tested.decide(fetch, repository, sha, workflow, base)
        except Exception as error:  # any failure means "run everything"
            tested, reason = False, f"could not decide ({type(error).__name__}: {error})"
        return (not tested), matrix(event, False), reason
    if event == "pull_request" and pr_number:
        try:
            files = changed_files(fetch, repository, pr_number)
            prose = docs_only(files)
            reason = f"{len(files)} changed files, prose only: {prose}"
        except Exception as error:
            prose, reason = False, f"could not list changed files ({type(error).__name__}: {error})"
        return True, matrix(event, prose), reason
    return True, matrix(event, False), f"{event}: full matrix"


def main(argv: list[str] | None = None, fetch: merged_tree_tested.Fetch | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--pr", type=int, default=None)
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--base", default="main")
    args = parser.parse_args(argv)
    if fetch is None:
        try:
            fetch = merged_tree_tested._fetch_with(
                os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                os.environ["GITHUB_TOKEN"],
            )
        except Exception as error:
            print(f"no API access ({error}); running the full matrix", file=sys.stderr)
            fetch = None
    run, legs, reason = plan(
        args.event, fetch, args.repository, args.sha, args.pr, args.workflow, args.base
    )
    print(f"CI plan: run={run}; {reason}", file=sys.stderr)
    for leg in legs:
        if leg["light"]:
            print(
                f"  light: test ({leg['os']}, {leg['python-version']}): {leg['why']}",
                file=sys.stderr,
            )
    print(f"run={'true' if run else 'false'}")
    print(f"matrix={json.dumps(legs, separators=(',', ':'))}")
    print(f"reason={' '.join(reason.split())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
