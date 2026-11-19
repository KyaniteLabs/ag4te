import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from archaeology.cli import main


def git(repo, *args, date="2025-01-01T12:00:00Z"):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    return subprocess.check_output(["git", "-C", str(repo), *args], env=env, text=True).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, "init", "-q")
    git(folder, "commit", "--allow-empty", "-qm", "chore: seed history")
    return folder


def delta(repo, *options):
    return CliRunner().invoke(main, ["dig", str(repo), "--delta", "history", *options])


def test_delta_rolls_only_when_requested(repo):
    assert delta(repo).exit_code != 0
    assert delta(repo, "--roll").exit_code == 0
    git(
        repo,
        "commit",
        "--allow-empty",
        "-qm",
        "feat(cli)!: add reports",
        date="2025-02-01T12:00:00Z",
    )
    head = git(repo, "rev-parse", "HEAD")
    path = Path("projects/history/data/latest.json")
    for options in ((), (), ("--roll",)):
        result = delta(repo, *options)
        assert result.exit_code == 0, result.output
        report = json.loads(path.read_text(encoding="utf-8"))["delta"]
        assert report["new_commits"] == [
            {"hash": head, "kind": "feat", "scope": "cli", "subject": "feat(cli)!: add reports"}
        ]
        assert report["commit_counts"] == {"code": 1, "merge": 0}
        assert any(signal["type"] == "gap" for signal in report["new_signals"])
        assert report["era_boundaries_changed"] is True
    result = delta(repo, "--roll")
    assert result.exit_code == 0, result.output
    report = json.loads(path.read_text(encoding="utf-8"))["delta"]
    assert report["new_commits"] == report["new_signals"] == []
    assert report["era_boundaries_changed"] is False


def test_wrong_repository_keeps_previous_delta(repo):
    assert delta(repo, "--roll").exit_code == 0
    path = Path("projects/history/data/latest.json")
    saved = path.read_bytes()
    other = repo.parent / "other"
    git(repo, "clone", "-q", str(repo), str(other))
    result = delta(other, "--roll")
    assert result.exit_code != 0 and "another repository" in result.output
    assert path.read_bytes() == saved


def test_delta_can_start_from_an_existing_full_mine(repo):
    runner = CliRunner()
    assert runner.invoke(main, ["init", "history"]).exit_code == 0
    result = runner.invoke(main, ["mine", str(repo), "--project", "history"])
    assert result.exit_code == 0, result.output
    old_commits = Path("projects/history/data/github-commits.csv").read_bytes()
    git(repo, "commit", "--allow-empty", "-qm", "fix: new evidence", date="2025-01-02T12:00:00Z")
    result = delta(repo)
    assert result.exit_code == 0, result.output
    stored = json.loads(Path("projects/history/data/latest.json").read_text(encoding="utf-8"))
    assert stored["delta"]["commit_counts"] == {"code": 1, "merge": 0}
    assert Path("projects/history/data/github-commits.csv").read_bytes() == old_commits


def test_delta_rejects_shallow_history(repo):
    shallow = repo.parent / "shallow"
    git(repo, "clone", "-q", "--depth", "1", repo.as_uri(), str(shallow))
    result = delta(shallow, "--roll")
    assert result.exit_code != 0 and "Shallow history" in result.output
    assert not Path("projects/history/data/latest.json").exists()


def test_extract_sessions_since_filters_before_parsing(repo):
    runner = CliRunner()
    assert runner.invoke(main, ["init", "sessions"]).exit_code == 0
    folder = repo.parent / "-sessions"
    folder.mkdir()
    for name, mtime in [("older", 1_600_000_000), ("newer", 1_800_000_000)]:
        path = folder / f"{name}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
    result = runner.invoke(
        main,
        [
            "extract-sessions",
            "sessions",
            "--sessions-dir=-sessions",
            "--since",
            "2025-01-01T00:00:00Z",
        ],
    )
    assert result.exit_code == 0, result.output
    text = Path("projects/sessions/data/raw-sessions.md").read_text(encoding="utf-8")
    assert "Extracted from 1 Claude Code session logs" in text
