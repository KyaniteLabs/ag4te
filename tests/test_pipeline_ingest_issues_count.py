"""Ingest records the issue count the pipeline log reports (audit row 47).

The GITHUB_pipeline summarizes each repo's issues as one object that holds a
per-severity breakdown next to its own ``total``. Ingest used to sum every value
in that object, so each issue was counted twice.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from archaeology.db.pipeline_ingest import (
    get_pipeline_history,
    get_repo_quality_trend,
    ingest_directory,
    ingest_run,
)

# The shape every repo entry carries in a real run log: the breakdown keys add up
# to ``total``, which already counts every issue.
DOCUMENTED_ISSUES = {"total": 2, "critical": 0, "high": 0, "medium": 2, "low": 0}


def _run(*repos: dict) -> dict:
    return {
        "run_timestamp": "2026-09-03T01:05:07Z",
        "status": "COMPLETE",
        "duration_seconds": 158.2,
        "repos": list(repos),
    }


def _repo(name: str, issues: object) -> dict:
    return {"full_name": name, "tier": 1, "status": "clean", "issues": issues, "fixes_applied": 0}


def _stored(db_path: Path) -> dict[str, tuple[int, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT repo_name, issues_count, issues_json FROM pipeline_repo_results"
        )
        return {name: (count, blob) for name, count, blob in rows}
    finally:
        conn.close()


@pytest.mark.parametrize(
    "issues",
    [
        DOCUMENTED_ISSUES,
        {"total": 2, "critical": 0, "high": 0, "medium": 1, "low": 1},
        {"total": 3, "critical": 0, "high": 0, "medium": 2, "low": 1},
        {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0},
        {"total": 5, "critical": 2, "high": 1, "medium": 2, "low": 0},
    ],
)
def test_issues_object_records_its_own_total(tmp_path, issues):
    db_path = tmp_path / "archaeology.db"

    ingest_run(db_path, _run(_repo("acme/checkout", issues)))

    [(count, blob)] = _stored(db_path).values()
    assert count == issues["total"]
    assert count <= issues["total"]
    assert json.loads(blob) == issues


_severities = st.fixed_dictionaries(
    {key: st.integers(min_value=0, max_value=50) for key in ("critical", "high", "medium", "low")}
)


@settings(deadline=None)
@given(severities=_severities, total=st.integers(min_value=0, max_value=200))
def test_recorded_count_never_exceeds_the_source_total(tmp_path_factory, severities, total):
    db_path = tmp_path_factory.mktemp("ingest") / "archaeology.db"
    issues = {**severities, "total": total}

    ingest_run(db_path, _run(_repo("acme/checkout", issues)))

    [(count, _)] = _stored(db_path).values()
    assert count <= total
    assert count == total


def test_history_and_quality_trend_report_the_source_total(tmp_path):
    logs_dir = tmp_path / "repo-pipeline"
    logs_dir.mkdir()
    run = _run(
        _repo("acme/checkout", {"total": 3, "critical": 0, "high": 0, "medium": 2, "low": 1}),
        _repo("acme/clean", {"total": 0, "critical": 0, "high": 0, "medium": 0, "low": 0}),
    )
    (logs_dir / "20260903-010507.json").write_text(json.dumps(run), encoding="utf-8")
    db_path = tmp_path / "archaeology.db"

    stats = ingest_directory(db_path, logs_dir)

    assert stats == {"ingested": 1, "skipped": 0, "errors": []}
    assert _stored(db_path)["acme/checkout"][0] == 3
    assert _stored(db_path)["acme/clean"][0] == 0
    history = get_pipeline_history(db_path, repo_name="acme/checkout")
    trend = get_repo_quality_trend(db_path, "acme/checkout")
    assert [row["issues_count"] for row in history] == [3]
    assert [row["issues_count"] for row in trend] == [3]


@pytest.mark.parametrize(
    "issues, expected",
    [
        ({"critical": 1, "high": 2, "medium": 3, "low": 4}, 10),
        ([{"id": 1}, {"id": 2}], 2),
    ],
)
def test_issues_without_a_total_key_are_still_counted(tmp_path, issues, expected):
    db_path = tmp_path / "archaeology.db"

    ingest_run(db_path, _run(_repo("acme/checkout", issues)))

    [count] = [count for count, _ in _stored(db_path).values()]
    assert count == expected
