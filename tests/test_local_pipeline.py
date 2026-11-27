"""Tests for archaeology/local_pipeline.py LocalPipelineStatus."""

import json
import os
from pathlib import Path

from click.testing import CliRunner

# Set required environment variables BEFORE importing the module
os.environ["ARCHAEOLOGY_PIPELINE_ROOT"] = "/fake/pipeline"
os.environ["ARCHAEOLOGY_REPOS_DIR"] = "/fake/repos"

from archaeology.cli import main
from archaeology.local_pipeline import LocalPipelineStatus


def test_local_pipeline_status_with_dict_issues():
    """Test LocalPipelineStatus with dict issues (normal format)."""
    status = LocalPipelineStatus(
        run_timestamp="2026-01-01T00:00:00Z",
        overall_health="HEALTHY",
        repo="test/repo",
        repo_health="HEALTHY",
        repo_verdict="stable",
        issues={"total": 5, "critical": 0, "high": 1, "medium": 2, "low": 2},
        open_prs=3,
        open_issues=10,
        latest_json=Path("/fake/latest.json"),
    )
    assert status.issue_total == 5
    assert status.issues["total"] == 5
    assert status.issues["high"] == 1


def test_local_pipeline_status_with_list_issues_normalization():
    """Test LocalPipelineStatus with list issues (returns 0 without crashing)."""
    status = LocalPipelineStatus(
        run_timestamp="2026-01-01T00:00:00Z",
        overall_health="HEALTHY",
        repo="test/repo",
        repo_health="HEALTHY",
        repo_verdict="stable",
        issues=[{"type": "bug", "severity": "high"}, {"type": "feature", "severity": "low"}],
        open_prs=3,
        open_issues=10,
        latest_json=Path("/fake/latest.json"),
    )
    # List issues: issue_total returns 0 (AttributeError caught)
    assert status.issue_total == 0
    # issues is still a list (normalization only happens in read_local_pipeline_status)
    assert isinstance(status.issues, list)
    assert len(status.issues) == 2


def test_local_pipeline_status_with_empty_list_issues():
    """Test LocalPipelineStatus with empty list issues."""
    status = LocalPipelineStatus(
        run_timestamp="2026-01-01T00:00:00Z",
        overall_health="HEALTHY",
        repo="test/repo",
        repo_health="HEALTHY",
        repo_verdict="stable",
        issues=[],
        open_prs=0,
        open_issues=0,
        latest_json=Path("/fake/latest.json"),
    )
    assert status.issue_total == 0
    # issues is still a list
    assert isinstance(status.issues, list)


def test_local_pipeline_status_with_none_issues():
    """Test LocalPipelineStatus with None issues (treated as empty dict)."""
    status = LocalPipelineStatus(
        run_timestamp="2026-01-01T00:00:00Z",
        overall_health="HEALTHY",
        repo="test/repo",
        repo_health="HEALTHY",
        repo_verdict="stable",
        issues=None,
        open_prs=0,
        open_issues=0,
        latest_json=Path("/fake/latest.json"),
    )
    assert status.issue_total == 0


def test_local_pipeline_status_with_missing_total_key():
    """Test LocalPipelineStatus with dict issues missing 'total' key."""
    status = LocalPipelineStatus(
        run_timestamp="2026-01-01T00:00:00Z",
        overall_health="HEALTHY",
        repo="test/repo",
        repo_health="HEALTHY",
        repo_verdict="stable",
        issues={"critical": 0, "high": 1, "medium": 2},
        open_prs=3,
        open_issues=10,
        latest_json=Path("/fake/latest.json"),
    )
    # Missing 'total' key should default to 0
    assert status.issue_total == 0


def test_local_pipeline_status_with_invalid_total_value():
    """Test LocalPipelineStatus with non-numeric 'total' value."""
    status = LocalPipelineStatus(
        run_timestamp="2026-01-01T00:00:00Z",
        overall_health="HEALTHY",
        repo="test/repo",
        repo_health="HEALTHY",
        repo_verdict="stable",
        issues={"total": "not-a-number"},
        open_prs=3,
        open_issues=10,
        latest_json=Path("/fake/latest.json"),
    )
    # Invalid total should default to 0
    assert status.issue_total == 0


def test_local_pipeline_missing_latest_json_is_clean_error(tmp_path):
    """A missing pipeline status is one Click error line, not a traceback."""
    result = CliRunner().invoke(
        main,
        ["local-pipeline", "--repo", "devarch-framework", "--pipeline-dir", str(tmp_path)],
    )
    expected = tmp_path / ".omc" / "logs" / "repo-pipeline" / "latest.json"
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert result.output.strip() == f"Error: Local pipeline latest.json not found: {expected}"


def test_local_pipeline_unknown_repo_is_clean_error(tmp_path):
    """An unknown repo in a valid pipeline run is one Click error line, not a traceback."""
    latest = tmp_path / ".omc" / "logs" / "repo-pipeline" / "latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(
        json.dumps(
            {
                "supervision": {
                    "missions": [
                        {"repo": "KyaniteLabs/devarch-framework", "issues": {"total": 0}},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        main,
        ["local-pipeline", "--repo", "totally-unknown-repo", "--pipeline-dir", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert result.output.strip() == (
        "Error: Repo 'totally-unknown-repo' not found in latest local pipeline reviewed repos. "
        "Reviewed: KyaniteLabs/devarch-framework"
    )
