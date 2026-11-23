"""Regression tests for session discovery."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from archaeology import cli


@pytest.fixture(autouse=True)
def source_path(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))


def _session(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "user", "message": {"content": "Please build the feature"}}) + "\n"
    )


def test_recursive_discovery_and_subagent_opt_in(tmp_path):
    root = tmp_path / "sessions"
    _session(root / "project-a" / "one.jsonl")
    _session(root / "project-a" / "subagents" / "agent.jsonl")
    _session(root / "project-b" / "two.jsonl")
    output = tmp_path / "out.md"

    def run(*options):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "archaeology.extractors.sessions",
                "--sessions-dir",
                str(root),
                "--output",
                str(output),
                *options,
            ],
            capture_output=True,
            text=True,
        )

    result = run()
    assert result.returncode == 0, result.stderr
    assert "Found 2 session files" in result.stdout
    assert "Extracted from 2" in output.read_text()
    result = run("--project-slug", "project-a")
    assert result.returncode == 0, result.stderr
    assert "Found 1 session files" in result.stdout
    result = run("--project-slug", "project-a", "--include-subagents")
    assert result.returncode == 0, result.stderr
    assert "Found 2 session files" in result.stdout
    result = run("--project-slug", "../project-b")
    assert result.returncode != 0


def test_empty_discovery_fails_without_writing_output(tmp_path):
    output = tmp_path / "out.md"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "archaeology.extractors.sessions",
            "--sessions-dir",
            str(tmp_path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "no session" in (result.stderr + result.stdout).lower()
    assert not output.exists()


def test_cli_forwards_discovery_options_without_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "projects/demo/data").mkdir(parents=True)
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = CliRunner().invoke(
        cli.main,
        [
            "extract-sessions",
            "demo",
            "--sessions-dir",
            str(tmp_path),
            "--project-slug",
            "project-a",
            "--include-subagents",
        ],
    )
    assert result.exit_code == 0, result.output
    cmd, kwargs = calls[0]
    assert "--include-subagents" in cmd and "--project-slug=project-a" in cmd
    assert "timeout" not in kwargs


def test_project_slug_cannot_escape_session_root(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    outside = tmp_path / "outside"
    _session(outside / "secret.jsonl")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "archaeology.extractors.sessions",
            "--sessions-dir",
            str(root),
            "--output",
            str(tmp_path / "out.md"),
            "--project-slug",
            "linked",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "inside the sessions directory" in result.stderr


def test_cli_accepts_leading_dash_slug(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "projects/demo/data").mkdir(parents=True)
    _session(tmp_path / "sessions/-proj-a/one.jsonl")
    result = CliRunner().invoke(
        cli.main,
        [
            "extract-sessions",
            "demo",
            "--sessions-dir",
            str(tmp_path / "sessions"),
            "--project-slug=-proj-a",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (
        "Please build the feature" in (tmp_path / "projects/demo/data/raw-sessions.md").read_text()
    )


def test_cli_error_prefix_is_not_duplicated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "projects/demo/data").mkdir(parents=True)
    result = CliRunner().invoke(
        cli.main, ["extract-sessions", "demo", "--sessions-dir", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "Error: Error:" not in result.output
    assert "no session files found" in result.output


def test_default_scope_uses_project_repo_and_all_projects_is_explicit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser reads this on Windows
    project = tmp_path / "projects/demo"
    (project / "data").mkdir(parents=True)
    (project / "project.json").write_text(json.dumps({"repo_path": "/work/my.repo"}))
    from archaeology.extractors.sessions import claude_project_slug

    # Folder name for this platform's absolute form of the path (Windows adds the drive: C--work-my-repo).
    slug = claude_project_slug(os.path.abspath("/work/my.repo"))
    _session(tmp_path / f".claude/projects/{slug}/one.jsonl")
    _session(tmp_path / ".claude/projects/-other/two.jsonl")
    runner = CliRunner()
    result = runner.invoke(cli.main, ["extract-sessions", "demo"])
    assert result.exit_code == 0, result.output
    assert "Extracted from 1" in (project / "data/raw-sessions.md").read_text()
    result = runner.invoke(cli.main, ["extract-sessions", "demo", "--all-projects"])
    assert result.exit_code == 0, result.output
    assert "Extracted from 2" in (project / "data/raw-sessions.md").read_text()


def test_default_scope_requires_repo_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser reads this on Windows
    (tmp_path / "projects/demo/data").mkdir(parents=True)
    _session(tmp_path / ".claude/projects/-other/two.jsonl")
    result = CliRunner().invoke(cli.main, ["extract-sessions", "demo"])
    assert result.exit_code != 0
    assert "--project-slug" in result.output
    assert not (tmp_path / "projects/demo/data/raw-sessions.md").exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="golden Claude Code folder names for POSIX repo paths"
)
@pytest.mark.parametrize(
    ("repo", "slug"),
    [
        ("/work/my_proj", "-work-my-proj"),
        ("/work/a b+c", "-work-a-b-c"),
        ("/work/café", "-work-caf-"),
        ("/work/x\U0001f600", "-work-x--"),
    ],
)
def test_default_scope_matches_claude_code_folder_names(tmp_path, monkeypatch, repo, slug):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser reads this on Windows
    project = tmp_path / "projects/demo"
    (project / "data").mkdir(parents=True)
    (project / "project.json").write_text(json.dumps({"repo_path": repo}))
    _session(tmp_path / f".claude/projects/{slug}/one.jsonl")
    result = CliRunner().invoke(cli.main, ["extract-sessions", "demo"])
    assert result.exit_code == 0, result.output
    assert "Extracted from 1" in (project / "data/raw-sessions.md").read_text()


@pytest.mark.skipif(
    sys.platform == "win32", reason="golden Claude Code folder names for POSIX repo paths"
)
def test_default_scope_finds_hashed_folder_for_long_repo_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser reads this on Windows
    project = tmp_path / "projects/demo"
    (project / "data").mkdir(parents=True)
    repo = "/w/" + "d" * 250
    (project / "project.json").write_text(json.dumps({"repo_path": repo}))
    slug = ("-w-" + "d" * 250)[:200] + "-56jznd"  # computed by Claude Code's own hash
    _session(tmp_path / f".claude/projects/{slug}/one.jsonl")
    # Another project sharing the first 200 characters must not be picked up.
    _session(tmp_path / f".claude/projects/{slug[:-6]}1x2y3z/other.jsonl")
    result = CliRunner().invoke(cli.main, ["extract-sessions", "demo"])
    assert result.exit_code == 0, result.output
    assert "Extracted from 1" in (project / "data/raw-sessions.md").read_text()


def test_long_path_hash_matches_claude_code_for_astral_characters():
    from archaeology.extractors.sessions import claude_project_slug

    name = claude_project_slug("/w/" + "\U0001f600" * 120)
    assert name == "-w-" + "-" * 197 + "-vuzjip"
