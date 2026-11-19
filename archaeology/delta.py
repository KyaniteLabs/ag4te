"""Compare fresh local Git evidence with a named, optionally rolling baseline."""

import csv
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .classifiers.era_detector import SignalDetector
from .classifiers.work_kind import kind_of
from .db.builder import build_db
from .extractors.git import extract_git_log, repository_coverage
from .pipeline import ProjectRef
from .provenance import digest
from .utils import atomic_write

COMMITS = "github-commits.csv"
CONVENTIONAL = re.compile(r"^\s*([A-Za-z]+)(?:\(([^)]+)\))?!?\s*:")


@contextmanager
def baseline_lock(path):
    """Serialize readers and writers of a baseline across CLI processes."""
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _evidence(project):
    with (project / "data" / COMMITS).open(encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if not {"hash", "message", "parents"} <= set(reader.fieldnames or ()):
            raise ValueError("Baseline needs the current commit CSV schema; run a full dig again")
        commits = list(reader)

    build_db(project)
    measured = SignalDetector(project / "data" / "archaeology.db").detect()
    if measured.get("commit_count", 0) != len(commits):
        raise ValueError("Signals were not computed from the complete mined commit set")
    return commits, measured


def _snapshot(commits, measured, head):
    return {
        "head": head,
        "commits": sorted({commit["hash"] for commit in commits}),
        "signals": [
            {key: signal[key] for key in ("type", "date", "detail", "strength")}
            for signal in measured["signals"]
        ],
        "era_boundaries": [
            [cluster["start_date"], cluster["end_date"]] for cluster in measured["cluster_summary"]
        ],
    }


def _initial_baseline(data, repo):
    if not (data / COMMITS).exists():
        return None
    config = json.loads((data.parent / "project.json").read_text(encoding="utf-8"))
    if config.get("repo_path") != repo:
        raise ValueError(
            "The baseline project belongs to another repository or lacks a source path"
        )
    coverage = json.loads((data / "coverage.json").read_text(encoding="utf-8"))
    expected = coverage.get("artifact_sha256", {}).get(COMMITS)
    if not expected or digest(data / COMMITS) != expected:
        raise ValueError("Baseline commit evidence is stale or unbound; run a full mine again")
    # Measure both snapshots from the same kind of input. A previous signal file
    # may have been produced with different settings or before the latest mine.
    with tempfile.TemporaryDirectory(prefix="devarch-baseline-") as temp:
        project = Path(temp)
        (project / "data").mkdir()
        copied = project / "data" / COMMITS
        shutil.copyfile(data / COMMITS, copied)
        if digest(copied) != expected:
            raise ValueError("Baseline commit evidence changed while reading it")
        commits, measured = _evidence(project)
        return _snapshot(commits, measured, coverage.get("head"))


def _compare(baseline, current, commits):
    known = set(baseline["commits"])
    new = []
    code = Counter()
    merges = 0
    for commit in commits:
        if commit["hash"] in known:
            continue
        subject = commit["message"]
        match = CONVENTIONAL.match(subject)
        merge = len(commit["parents"].split()) > 1
        kind = "merge" if merge else (match[1].lower() if match else kind_of(subject))
        scope = match[2] if match and not merge else None
        if merge:
            merges += 1
        else:
            code[(kind, scope or "")] += 1
        new.append(
            {
                "hash": commit["hash"],
                "kind": kind,
                "scope": scope,
                "subject": subject,
            }
        )
    prior_signals = {(s["type"], s["date"], s["detail"]) for s in baseline["signals"]}
    return {
        "from_head": baseline["head"],
        "to_head": current["head"],
        "new_commits": new,
        "commit_counts": {"code": len(new) - merges, "merge": merges},
        "by_kind_and_scope": [
            {"kind": kind, "scope": scope or None, "count": count}
            for (kind, scope), count in sorted(code.items())
        ],
        "features": [item["subject"] for item in new if item["kind"] == "feat"],
        "new_signals": [
            s
            for s in current["signals"]
            if (s["type"], s["date"], s["detail"]) not in prior_signals
        ],
        "era_boundaries_changed": baseline["era_boundaries"] != current["era_boundaries"],
        "era_boundaries": {
            "before": baseline["era_boundaries"],
            "after": current["era_boundaries"],
        },
    }


def run_delta(repo_path: str, baseline_project: str, *, roll: bool):
    """Write the latest comparison, with the baseline in the same atomic document."""
    repo = str(Path(repo_path).expanduser().resolve())
    coverage = repository_coverage(repo)
    if coverage["shallow"]:
        raise ValueError("Shallow history: use a complete local repository for a delta dig")
    project = ProjectRef.for_cli(baseline_project, must_exist=False).resolve()
    data = project / "data"
    if not data.resolve().is_relative_to(Path("projects").resolve()):
        raise ValueError("Baseline must stay inside the projects directory")
    data.mkdir(parents=True, exist_ok=True)
    latest = data / "latest.json"
    with baseline_lock(data / ".delta.lock"):
        if latest.exists():
            state = json.loads(latest.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("schema_version") != 1:
                raise ValueError("Unsupported delta baseline format")
            if state.get("repository") != repo:
                raise ValueError("The baseline project belongs to another repository")
            baseline = state.get("baseline")
            if not isinstance(baseline, dict) or any(
                not isinstance(baseline.get(field), list)
                for field in ("commits", "signals", "era_boundaries")
            ):
                raise ValueError("Invalid delta baseline in latest.json")
        else:
            baseline = _initial_baseline(data, repo)
        if baseline is None and not roll:
            raise ValueError("No baseline yet; supply --roll to seed one")

        with tempfile.TemporaryDirectory(prefix="devarch-delta-") as temp:
            staging = Path(temp)
            (staging / "data").mkdir()
            extract_git_log(repo, str(staging / "data" / COMMITS))
            commits, measured = _evidence(staging)
        if repository_coverage(repo) != coverage:
            raise ValueError("Repository refs changed during the dig; retry")
        current = _snapshot(commits, measured, coverage["head"])
        result = _compare(baseline or current, current, commits)
        state = {
            "schema_version": 1,
            "repository": repo,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "baseline": current if roll else baseline,
            "delta": result,
        }
        atomic_write(latest, json.dumps(state, indent=2) + "\n")
        return latest, result
