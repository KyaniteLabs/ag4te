"""Interface tests for the serve/cascade/sync pipeline verbs.

The adapter characterization (``test_pipeline_contract.py``) pins the CLI
transcript byte-for-byte, including for these verbs, but ``sync``'s full flow
can only run there as an error path: the verb resolves its profile from the
package's ``config/profile.json`` first, which names the developer's real
projects. These tests drive the service directly with a hermetic profile.
Nothing is mocked except the Datasette launch.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from archaeology import pipeline
from archaeology.pipeline import PipelineError, ProjectRef

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    env = {
        **os.environ,
        **GIT_ENV,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def git(*args, date=None):
        full = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}
        subprocess.run(
            ["git", "-C", str(path), *args], check=True, env={**env, **full}, capture_output=True
        )

    git("init", "-q", "-b", "main")
    for i, day in enumerate(("2026-01-01", "2026-01-02", "2026-02-20")):
        (path / f"f{i}.txt").write_text(f"change {i}\n")
        git("add", "-A")
        git("commit", "-q", "-m", f"commit {i}", date=f"{day}T12:00:00+0000")
    return path


class Lines:
    """on_line collector: (text, err) pairs."""

    def __init__(self):
        self.items = []

    def __call__(self, text, err=False):
        self.items.append((text, err))

    def text(self):
        return "\n".join(t for t, _ in self.items)


# ── sync ───────────────────────────────────────────────────────────────────────


def _profile(root: Path, repo: Path) -> Path:
    profile = root / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "developer": {"name": "T"},
                "sync": {"run_signals": True, "run_analysis": False, "default_min_gap_days": 3},
                "projects": [{"name": "demo", "repo_path": str(repo)}],
            }
        ),
        encoding="utf-8",
    )
    return profile


def test_sync_full_flow_hermetic_profile(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)

    lines = Lines()
    outcome = pipeline.sync(["demo"], profile_path=str(_profile(tmp_path, repo)), on_line=lines)
    assert outcome.exit_code == 0
    text = lines.text()
    assert "Syncing 1 project(s)..." in text
    assert "  Mining demo..." in text
    assert "    3 commits extracted" in text
    assert "  Building DB for demo..." in text
    assert "    DB built" in text
    assert "  Detecting signals for demo..." in text
    assert "Sync complete." in text

    # the project was initialized from the profile and fully populated
    proj = ws / "projects" / "demo"
    assert (proj / "project.json").exists()
    assert (proj / "data" / "github-commits.csv").exists()
    assert (proj / "data" / "archaeology.db").exists()
    assert (proj / "data" / "detected-signals.json").exists()

    # global aggregation ran
    gdata = ws / "global" / "data"
    assert (gdata / "global-commits.csv").exists()
    assert (gdata / "project-summaries.json").exists()
    assert (gdata / "global.db").exists()
    summaries = json.loads((gdata / "project-summaries.json").read_text())
    assert summaries[0]["name"] == "demo"
    assert summaries[0]["total_commits"] == 3


def test_sync_missing_profile_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lines = Lines()
    outcome = pipeline.sync(["demo"], profile_path=str(tmp_path / "nope.json"), on_line=lines)
    assert outcome.exit_code == 1
    assert lines.items == [
        (
            "No profile.json found. Create one at config/profile.json with your project list.",
            True,
        )
    ]


def test_sync_skip_mine_skips_extraction_but_builds(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    # pre-mined data, so the build can run without mining
    pipeline.init(ProjectRef.for_cli("demo", must_exist=False), "d", "")
    pipeline.mine(str(repo), ProjectRef.for_cli("demo"), expand_repo_path=False)

    lines = Lines()
    outcome = pipeline.sync(
        ["demo"], skip_mine=True, profile_path=str(_profile(tmp_path, repo)), on_line=lines
    )
    assert outcome.exit_code == 0
    text = lines.text()
    assert "Mining demo..." not in text
    assert "Building DB for demo..." in text


# ── serve ──────────────────────────────────────────────────────────────────────


def test_prepare_serve_requires_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    ref = ProjectRef.for_cli("empty", must_exist=False)
    pipeline.init(ref, "d", "")
    with pytest.raises(PipelineError) as exc:
        pipeline.prepare_serve(ref, 8001, False)
    assert exc.value.kind == "no-database"
    assert str(exc.value) == (
        f"Database not found at {os.path.join('projects', 'empty', 'data', 'archaeology.db')}. "
        "Run 'devarch build-db empty' first."
    )


def test_prepare_serve_builds_command_and_metadata(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    ref = ProjectRef.for_cli("alpha", must_exist=False)
    pipeline.init(ref, "Alpha", "https://example.com/a")
    pipeline.mine(str(repo), ref, expand_repo_path=False)
    pipeline.build_db(ref)
    (ws / "config").mkdir()
    (ws / "config" / "datasette-metadata.yaml").write_text(
        'title: "Archaeology Database"\ndescription: test\n', encoding="utf-8"
    )

    launch = pipeline.prepare_serve(ref, 8123, True)
    assert launch.cmd[0] == "datasette"
    assert launch.db_path == os.path.join("projects", "alpha", "data", "archaeology.db")
    assert launch.cmd[1] == launch.db_path
    assert "--port" in launch.cmd and "8123" in launch.cmd
    assert "--cors" in launch.cmd
    assert launch.temp_metadata is True
    content = Path(launch.metadata_src).read_text(encoding="utf-8")
    assert 'title: "ALPHA Archaeology Database"' in content
    Path(launch.metadata_src).unlink()

    plain = pipeline.prepare_serve(ref, 8124, False)
    assert "--cors" not in plain.cmd

    # a project-specific metadata file wins over the workspace default and
    # is used in place (no temp copy, nothing to clean up)
    proj_meta = ws / "projects" / "alpha" / "datasette-metadata.yaml"
    proj_meta.write_text('title: "Alpha custom"\n', encoding="utf-8")
    own = pipeline.prepare_serve(ref, 8125, False)
    i = own.cmd.index("--metadata") + 1
    assert own.cmd[i] == os.path.join("projects", "alpha", "datasette-metadata.yaml")
    assert own.temp_metadata is False


# ── cascade ────────────────────────────────────────────────────────────────────


def test_cascade_returns_exit_verdict_without_raising(tmp_path, monkeypatch):
    """Missing eras emits the transcript and reports exit 1 instead of raising."""
    repo = _make_repo(tmp_path / "repo")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    ref = ProjectRef.for_cli("alpha", must_exist=False)
    pipeline.init(ref, "Alpha", "")
    pipeline.mine(str(repo), ref, expand_repo_path=False)

    lines = Lines()
    outcome = pipeline.cascade(ref, on_line=lines)
    assert outcome.exit_code == 1
    assert "ERROR: No commit-eras.json found at" in lines.text()


def test_cascade_reaches_its_verdict_with_opportunity_disabled(tmp_path, monkeypatch):
    """Step 7 used to construct the disabled OpportunityAnalyzer, which always
    raises, so cascade crashed before printing a verdict whenever eras existed."""
    repo = _make_repo(tmp_path / "repo")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    ref = ProjectRef.for_cli("alpha", must_exist=False)
    pipeline.init(ref, "Alpha", "")
    pipeline.mine(str(repo), ref, expand_repo_path=False)
    eras = ws / "projects" / "alpha" / "data" / "commit-eras.json"
    eras.write_text(json.dumps({"eras": []}), encoding="utf-8")

    lines = Lines()
    outcome = pipeline.cascade(ref, dry_run=True, skip_mine=True, on_line=lines)
    text = lines.text()
    assert "[7/7] Running opportunity analyzers..." in text
    assert "SKIP: opportunity analyzers are disabled" in text
    assert outcome.exit_code in (0, 1)


def test_sync_cli_and_pipeline_share_profile_validation(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from archaeology import cli

    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    profile = config / "profile.json"
    profile.write_text(json.dumps({"projects": [{"name": "demo"}]}), encoding="utf-8")
    package_profile = Path(cli.__file__).parent.parent / "config" / "profile.json"
    real_exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda p: False if Path(p).resolve() == package_profile.resolve() else real_exists(p),
    )

    cli_result = CliRunner().invoke(cli.main, ["sync"])
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)

    assert outcome.exit_code == cli_result.exit_code == 1
    assert lines.text() + "\n" == cli_result.output


def test_sync_cli_matches_pipeline_on_demo(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from archaeology.cli import main

    repo = _make_repo(tmp_path / "repo")
    profile = _profile(tmp_path, repo)
    monkeypatch.setattr(pipeline, "_resolve_profile_path", lambda: str(profile))
    cli_workspace = tmp_path / "cli"
    api_workspace = tmp_path / "api"
    cli_workspace.mkdir()
    api_workspace.mkdir()
    monkeypatch.chdir(cli_workspace)
    cli_result = CliRunner().invoke(main, ["sync", "--project", "demo", "--skip-signals"])
    monkeypatch.chdir(api_workspace)
    lines = Lines()
    outcome = pipeline.sync(["demo"], skip_signals=True, profile_path=str(profile), on_line=lines)

    assert cli_result.exit_code == outcome.exit_code == 0
    assert cli_result.output == lines.text() + "\n"
    assert outcome.project_statuses == {"demo": "built"}
    assert outcome.built_count == 1
    relative_csv = Path("projects/demo/data/github-commits.csv")
    assert (cli_workspace / relative_csv).read_text() == (api_workspace / relative_csv).read_text()


@pytest.mark.parametrize("bad", ["../evil", "a/b", ".."])
def test_sync_rejects_unsafe_project_names_before_writing(tmp_path, monkeypatch, bad):
    repo = _make_repo(tmp_path / "source")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    profile = _profile(tmp_path, repo)
    data = json.loads(profile.read_text())
    data["projects"][0]["name"] = bad
    profile.write_text(json.dumps(data))
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1
    assert "Invalid project entries" in lines.text()
    assert sorted(p.name for p in workspace.iterdir()) == []
    assert not (tmp_path / "evil").exists()


def test_sync_with_a_mistyped_project_name_fails_before_writing(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    profile = _profile(tmp_path, repo)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    lines = Lines()
    outcome = pipeline.sync(["demo", "dmeo"], profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1
    assert "Unknown project: dmeo" in lines.text()
    assert "Sync complete." not in lines.text()
    assert list(workspace.iterdir()) == []


def test_bare_repository_is_mined_by_sync_and_pipeline(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "source")
    bare = tmp_path / "mirror.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(bare)], check=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)

    ref = ProjectRef.for_cli("direct", must_exist=False)
    pipeline.init(ref, "direct", "")
    mined = pipeline.mine(str(bare), ProjectRef.for_cli("direct"))
    assert mined.commits == 3
    coverage = json.loads((workspace / "projects/direct/data/coverage.json").read_text())
    assert coverage["bare"] is True

    lines = Lines()
    outcome = pipeline.sync(["demo"], profile_path=str(_profile(tmp_path, bare)), on_line=lines)
    assert outcome.exit_code == 0
    assert "3 commits extracted" in lines.text()
    assert (workspace / "projects/demo/data/github-commits.csv").exists()


def test_cli_mine_accepts_bare_repository(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from archaeology.cli import main

    repo = _make_repo(tmp_path / "source")
    bare = tmp_path / "mirror.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(bare)], check=True)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["init", "demo"]).exit_code == 0
    result = runner.invoke(main, ["mine", str(bare), "-p", "demo"])
    assert result.exit_code == 0, result.output
    assert "Extracted 3 commits" in result.output


def test_require_git_repo_rejects_a_folder_inside_a_repository(tmp_path):
    repo = _make_repo(tmp_path / "source")
    (repo / "sub").mkdir()
    with pytest.raises(PipelineError) as inner:
        pipeline.require_git_repo(str(repo / "sub"))
    assert inner.value.kind == "not-git"
    assert "inside" in inner.value.message
    with pytest.raises(PipelineError) as git_dir:
        pipeline.require_git_repo(str(repo / ".git"))
    assert git_dir.value.kind == "not-git"
    assert pipeline.require_git_repo(str(repo)) == str(repo)


def test_require_git_repo_ignores_inherited_git_dir(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "source")
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    with pytest.raises(PipelineError) as exc:
        pipeline.require_git_repo(str(plain))
    assert exc.value.kind == "not-git"


def test_require_git_repo_reports_dubious_ownership(tmp_path, monkeypatch):
    bare = tmp_path / "mirror.git"
    bare.mkdir()
    real_run = subprocess.run

    def refuse(cmd, *args, **kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(
                cmd, 128, b"", b"fatal: detected dubious ownership in repository"
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(pipeline.subprocess, "run", refuse)
    with pytest.raises(RuntimeError, match="dubious ownership"):
        pipeline.require_git_repo(str(bare))


def test_bare_repository_with_unborn_head_is_mined(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "source")
    bare = tmp_path / "mirror.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(bare)], check=True)
    subprocess.run(
        ["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/unborn"], check=True
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    pipeline.init(ProjectRef.for_cli("unborn", must_exist=False), "unborn", "")
    assert pipeline.mine(str(bare), ProjectRef.for_cli("unborn")).commits == 3
    coverage = json.loads((workspace / "projects/unborn/data/coverage.json").read_text())
    assert coverage["head"] is None
    assert coverage["commit_count"] == 3


def test_inherited_git_dir_cannot_redirect_mining(tmp_path, monkeypatch):
    from archaeology.extractors.git import extract_git_log

    wanted = _make_repo(tmp_path / "wanted")
    other = _make_repo(tmp_path / "other")
    (other / "extra").write_text("x")
    subprocess.run(["git", "-C", str(other), "add", "."], check=True, env={**os.environ, **GIT_ENV})
    subprocess.run(
        [
            "git",
            "-C",
            str(other),
            "-c",
            "user.name=O",
            "-c",
            "user.email=o@example.com",
            "commit",
            "-qm",
            "other only",
        ],
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    csv_path = tmp_path / "out.csv"
    extract_git_log(str(wanted), str(csv_path))
    assert "other only" not in csv_path.read_text()


def test_cli_mine_rejects_a_folder_inside_a_repository(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from archaeology.cli import main

    repo = _make_repo(tmp_path / "source")
    (repo / "sub").mkdir()
    (tmp_path / "projects" / "sub" / "data").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["mine", str(repo / "sub"), "-p", "sub"])
    assert result.exit_code != 0
    assert "inside the repository at" in result.output
    assert not (tmp_path / "projects" / "sub" / "data" / "github-commits.csv").exists()


def test_sync_names_the_enclosing_repository_for_a_subfolder(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "source")
    (repo / "sub").mkdir()
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"projects": [{"name": "sub", "repo_path": str(repo / "sub")}]}))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1
    assert "inside the repository at" in lines.text()


def test_sync_isolates_builder_failures(tmp_path, monkeypatch):
    import sqlite3

    from click.testing import CliRunner

    from archaeology import analysis_runner
    from archaeology.classifiers import era_detector
    from archaeology.cli import main

    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [
                    {"name": name, "repo_path": str(tmp_path)} for name in ("one", "bad", "three")
                ],
                "sync": {"run_signals": True, "run_analysis": True},
            }
        )
    )
    monkeypatch.setattr(pipeline, "_resolve_profile_path", lambda: str(profile))

    def build(cmd, **kwargs):
        project = Path(cmd[cmd.index("--project-root") + 1])
        if project.name == "bad":
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(1, cmd, stderr="builder denied access")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="builder denied access")
        db = project / "data" / "archaeology.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE commits (date TEXT)")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    seen_signals = []
    seen_analysis = []

    def detect(name, config):
        seen_signals.append(name)
        return {"signals": [], "cluster_summary": []}

    def analyze(name, verbose=False):
        seen_analysis.append(name)
        return {}

    monkeypatch.setattr(era_detector, "detect_signals", detect)
    monkeypatch.setattr(analysis_runner, "run_analysis_vectors", analyze)
    monkeypatch.setattr(pipeline.subprocess, "run", build)
    result = CliRunner().invoke(main, ["sync", "--skip-mine"])
    assert result.exit_code == 1
    assert "builder denied access" in result.output
    assert "2 built" in result.output and "1 failed" in result.output and "bad" in result.output
    assert (tmp_path / "projects/one/data/archaeology.db").exists()
    assert (tmp_path / "projects/three/data/archaeology.db").exists()
    assert seen_signals == ["one", "three"]
    assert seen_analysis == ["one", "three"]
    assert "Traceback" not in result.output


def test_sync_missing_sqlite_utils_fails_before_writes(tmp_path, monkeypatch):
    import importlib.util

    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"projects": [{"name": "one", "repo_path": str(tmp_path)}]}))
    original = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: None if name == "sqlite_utils" else original(name)
    )
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1
    assert "sqlite-utils is not installed" in lines.text()
    assert not (tmp_path / "projects").exists()


def test_build_db_child_failure_is_clean(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from archaeology.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "projects/demo/data").mkdir(parents=True)

    def fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="builder denied access")

    monkeypatch.setattr(subprocess, "run", fail)
    result = CliRunner().invoke(main, ["build-db", "demo"])
    assert result.exit_code == 1
    assert "builder denied access" in result.output
    assert "Traceback" not in result.output


def test_sync_continues_after_signal_failure(tmp_path, monkeypatch):
    import sqlite3

    from archaeology.classifiers import era_detector

    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [
                    {"name": name, "repo_path": str(tmp_path)} for name in ("bad", "good")
                ],
                "sync": {"run_signals": True, "run_analysis": False},
            }
        )
    )

    def build(cmd, **kwargs):
        project = Path(cmd[cmd.index("--project-root") + 1])
        with sqlite3.connect(project / "data" / "archaeology.db") as conn:
            conn.execute("CREATE TABLE commits (date TEXT)")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    seen = []

    def detect(name, config):
        seen.append(name)
        if name == "bad":
            raise RuntimeError("broken signal data")
        return {"signals": [], "cluster_summary": []}

    monkeypatch.setattr(pipeline.subprocess, "run", build)
    monkeypatch.setattr(era_detector, "detect_signals", detect)
    lines = Lines()
    outcome = pipeline.sync(None, skip_mine=True, profile_path=str(profile), on_line=lines)
    assert seen == ["bad", "good"]
    assert outcome.exit_code == 1
    assert "broken signal data" in lines.text()


def test_sync_continues_when_project_initialization_fails(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.chdir(tmp_path)
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "bad").write_text("not a directory")
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [
                    {"name": name, "repo_path": str(tmp_path)} for name in ("bad", "good")
                ],
                "sync": {"run_signals": False, "run_analysis": False},
            }
        )
    )

    def build(cmd, **kwargs):
        project = Path(cmd[cmd.index("--project-root") + 1])
        with sqlite3.connect(project / "data" / "archaeology.db") as conn:
            conn.execute("CREATE TABLE commits (date TEXT)")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(pipeline.subprocess, "run", build)
    lines = Lines()
    outcome = pipeline.sync(None, skip_mine=True, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1
    assert outcome.project_statuses == {"bad": "failed", "good": "built"}
    assert (projects / "good/data/archaeology.db").exists()


def test_single_project_child_error_omits_traceback(capfd):
    import sys

    import click
    from click.testing import CliRunner

    from archaeology.cli import _run_child

    @click.command()
    def run():
        _run_child(
            [
                sys.executable,
                "-c",
                "raise RuntimeError(bytes.fromhex('6368696c642064657461696c').decode())",
            ]
        )

    result = CliRunner().invoke(run)
    captured = capfd.readouterr()
    assert result.exit_code == 1
    assert "child detail" in result.output
    assert "Traceback" not in result.output + captured.err


def _two_project_profile(tmp_path, *, signals=True):
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [
                    {"name": name, "repo_path": str(tmp_path)} for name in ("bad", "good")
                ],
                "sync": {"run_signals": signals, "run_analysis": False},
            }
        )
    )
    return profile


def _fake_builder(cmd, **kwargs):
    import sqlite3

    project = Path(cmd[cmd.index("--project-root") + 1])
    with sqlite3.connect(project / "data" / "archaeology.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS commits (date TEXT)")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_sync_isolates_unexpected_errors_and_counts_final_status(tmp_path, monkeypatch):
    from archaeology.classifiers import era_detector

    monkeypatch.chdir(tmp_path)
    profile = _two_project_profile(tmp_path)
    seen = []

    def detect(name, config):
        seen.append(name)
        if name == "bad":
            raise TypeError("'<' not supported between instances of 'int' and 'str'")
        return {"signals": [], "cluster_summary": []}

    monkeypatch.setattr(pipeline.subprocess, "run", _fake_builder)
    monkeypatch.setattr(era_detector, "detect_signals", detect)
    lines = Lines()
    outcome = pipeline.sync(None, skip_mine=True, profile_path=str(profile), on_line=lines)
    assert seen == ["bad", "good"]
    assert outcome.project_statuses == {"bad": "failed", "good": "built"}
    assert outcome.built_count == 1
    assert "1 built, 1 failed: bad" in lines.text()


def test_sync_failure_is_not_reported_as_complete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = _two_project_profile(tmp_path, signals=False)

    def build(cmd, **kwargs):
        if Path(cmd[cmd.index("--project-root") + 1]).name == "bad":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return _fake_builder(cmd, **kwargs)

    monkeypatch.setattr(pipeline.subprocess, "run", build)
    lines = Lines()
    outcome = pipeline.sync(None, skip_mine=True, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1
    assert "Sync complete." not in lines.text()
    assert ("Sync finished with failures.", True) in lines.items
    assert any("global/ reflects only: good" in t and err for t, err in lines.items)


def test_sync_leaves_global_untouched_and_says_so_when_nothing_built(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = _two_project_profile(tmp_path, signals=False)
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )
    lines = Lines()
    outcome = pipeline.sync(None, skip_mine=True, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1
    assert any("global/ was not updated" in t and err for t, err in lines.items)


def test_sync_skips_repository_without_commits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    repo = tmp_path / "empty"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"projects": [{"name": "emp", "repo_path": str(repo)}]}))
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 0, lines.text()
    assert outcome.project_statuses == {"emp": "skipped"}
    assert "SKIP emp: no commits yet" in lines.text()
    assert "Traceback" not in lines.text()


def test_sync_missing_repository_path_fails_the_run_and_names_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gone = tmp_path / "gone"
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"projects": [{"name": "ghost", "repo_path": str(gone)}]}))
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1
    assert outcome.project_statuses == {"ghost": "failed"}
    assert any(f"SKIP ghost: repo not found at {gone}" in text for text, _ in lines.items)
    assert "Sync complete." not in lines.text()
    assert ("Sync finished with failures.", True) in lines.items
    assert not (tmp_path / "global" / "data" / "project-summaries.json").exists()


def test_sync_checks_for_the_commits_table_before_running_vectors(tmp_path, monkeypatch):
    import sqlite3

    from archaeology import analysis_runner

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "projects" / "stale" / "data" / "archaeology.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE pipeline_runs (id INTEGER)")
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [{"name": "stale", "repo_path": str(tmp_path / "repo")}],
                "sync": {"run_signals": False, "run_analysis": True},
            }
        )
    )
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    calls = []
    monkeypatch.setattr(
        analysis_runner, "run_analysis_vectors", lambda *a, **kw: calls.append(a) or {}
    )
    lines = Lines()
    outcome = pipeline.sync(None, skip_mine=True, profile_path=str(profile), on_line=lines)
    assert calls == []
    assert outcome.exit_code == 1
    assert outcome.project_statuses == {"stale": "failed"}
    assert "no 'commits' table (the project was never mined)" in lines.text()
    assert "no such table: commits" not in lines.text()
    assert ("Sync finished with failures.", True) in lines.items


def test_sync_summaries_list_only_projects_that_were_mined(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    _make_repo(tmp_path / "src")
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [
                    {"name": "ghost", "repo_path": str(tmp_path / "gone")},
                    {"name": "real", "repo_path": str(tmp_path / "src")},
                ],
                "sync": {"run_signals": False, "run_analysis": False},
            }
        )
    )
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1, lines.text()
    assert outcome.project_statuses == {"ghost": "failed", "real": "built"}
    summaries = json.loads((tmp_path / "global" / "data" / "project-summaries.json").read_text())
    assert [entry["name"] for entry in summaries] == ["real"]
    assert summaries[0]["total_commits"] == 3


def test_run_child_timeout_is_clean():
    import sys

    import click
    from click.testing import CliRunner

    from archaeology.cli import _run_child

    @click.command()
    def run():
        _run_child([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)

    result = CliRunner().invoke(run)
    assert result.exit_code == 1
    assert "timed out" in result.output
    assert "Traceback" not in result.output


def test_sync_mining_error_of_any_type_skips_only_that_project(tmp_path, monkeypatch):
    from archaeology.extractors import git as git_extractor

    for name in ("bad", "good"):
        _make_repo(tmp_path / name)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [{"name": n, "repo_path": str(tmp_path / n)} for n in ("bad", "good")],
                "sync": {"run_signals": False, "run_analysis": False},
            }
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    real = git_extractor.extract_git_log

    def extract(repo_path, output_path, verbose=False):
        if Path(repo_path).name == "bad":
            raise ValueError("offset must be a timedelta strictly between -24h and 24h")
        return real(repo_path, output_path, verbose=verbose)

    real_run = subprocess.run

    def run(cmd, **kwargs):
        if "--project-root" in cmd:
            return _fake_builder(cmd, **kwargs)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(git_extractor, "extract_git_log", extract)
    monkeypatch.setattr(pipeline.subprocess, "run", run)
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.project_statuses == {"bad": "failed", "good": "built"}
    assert "Git extraction failed: offset must be" in lines.text()
    assert outcome.exit_code == 1


def test_sync_legacy_settings_apply_with_warning(tmp_path, monkeypatch):
    import sqlite3

    from archaeology.classifiers import era_detector

    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [{"name": "demo", "repo_path": str(tmp_path)}],
                "sync_settings": {
                    "auto_mine": False,
                    "auto_signals": True,
                    "min_gap_days": 14,
                    "unknown": True,
                },
                "sync": {"run_analysis": False, "typo": True},
            }
        )
    )
    seen = []

    def detect(name, config):
        seen.append(config["min_gap_days"])
        return {"signals": [], "cluster_summary": []}

    def build(cmd, **kwargs):
        project = Path(cmd[cmd.index("--project-root") + 1])
        with sqlite3.connect(project / "data" / "archaeology.db") as conn:
            conn.execute("CREATE TABLE commits (date TEXT)")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(era_detector, "detect_signals", detect)
    monkeypatch.setattr(pipeline.subprocess, "run", build)
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 0
    assert seen == [14]
    assert "deprecated" in lines.text().lower()
    assert "unknown" in lines.text() and "typo" in lines.text()
    assert "Mining demo" not in lines.text()


def test_sync_canonical_settings_override_legacy(tmp_path, monkeypatch):
    import sqlite3

    from archaeology.classifiers import era_detector

    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [{"name": "demo", "repo_path": str(tmp_path)}],
                "sync_settings": {"auto_mine": False, "min_gap_days": 14},
                "sync": {
                    "run_mine": False,
                    "run_signals": True,
                    "run_analysis": False,
                    "default_min_gap_days": 6,
                },
            }
        )
    )
    seen = []

    def detect(name, config):
        seen.append(config["min_gap_days"])
        return {"signals": [], "cluster_summary": []}

    def build(cmd, **kwargs):
        project = Path(cmd[cmd.index("--project-root") + 1])
        with sqlite3.connect(project / "data" / "archaeology.db") as conn:
            conn.execute("CREATE TABLE commits (date TEXT)")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(era_detector, "detect_signals", detect)
    monkeypatch.setattr(pipeline.subprocess, "run", build)
    outcome = pipeline.sync(None, profile_path=str(profile))
    assert outcome.exit_code == 0
    assert seen == [6]


@pytest.mark.parametrize(("gap_days", "expected"), [(3, True), (14, False)])
def test_sync_legacy_gap_changes_detected_signals(tmp_path, monkeypatch, gap_days, expected):
    import sqlite3

    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "projects": [{"name": "demo", "repo_path": str(tmp_path)}],
                "sync_settings": {"auto_mine": False, "min_gap_days": gap_days},
                "sync": {"run_analysis": False},
            }
        )
    )

    def build(cmd, **kwargs):
        project = Path(cmd[cmd.index("--project-root") + 1])
        with sqlite3.connect(project / "data" / "archaeology.db") as conn:
            conn.execute("CREATE TABLE commits (date TEXT, author TEXT, message TEXT)")
            conn.executemany(
                "INSERT INTO commits VALUES (?, ?, ?)",
                [
                    ("2026-01-01", "Dev", "first"),
                    ("2026-01-11", "Dev", "second"),
                ],
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(pipeline.subprocess, "run", build)
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 0
    report = json.loads((tmp_path / "projects/demo/data/detected-signals.json").read_text())
    assert any(signal["type"] == "gap" for signal in report["signals"]) is expected


@pytest.mark.parametrize(
    "settings",
    [
        {"sync": {"run_signals": "false"}},
        {"sync": {"default_min_gap_days": "14"}},
        {"sync": {"default_min_gap_days": True}},
        {"sync": {"default_min_gap_days": -1}},
        {"sync_settings": {"auto_mine": 0}},
    ],
)
def test_sync_rejects_mistyped_settings_before_writing(tmp_path, monkeypatch, settings):
    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({"projects": [{"name": "demo", "repo_path": str(tmp_path)}], **settings})
    )
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(profile), on_line=lines)
    assert outcome.exit_code == 1
    assert "must be true/false" in lines.text()
    section, values = next(iter(settings.items()))
    assert f"{section}.{next(iter(values))}" in lines.text()
    assert not (tmp_path / "projects").exists()


def _identity_history(repo):
    repo.mkdir()
    env = {
        **os.environ,
        **GIT_ENV,
        "GIT_COMMITTER_NAME": "Committer",
        "GIT_COMMITTER_EMAIL": "c@example.test",
    }
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env=env)
    from datetime import date, timedelta

    for day in range(28):
        bot = day >= 12
        when = (date(2026, 1, 1) + timedelta(days=day)).isoformat() + "T12:00:00+0000"
        commit_env = {
            **env,
            "GIT_AUTHOR_NAME": "dependabot[bot]" if bot else "Real Human",
            "GIT_AUTHOR_EMAIL": "dep@example.test" if bot else "human@example.test",
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
        }
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "work"],
            check=True,
            env=commit_env,
        )
    return repo


def test_mailmap_cannot_hide_author_class_shift(tmp_path, monkeypatch):
    repo = _identity_history(tmp_path / "repo")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    ref = ProjectRef.for_cli("demo", must_exist=False)
    pipeline.init(ref, "demo", "")

    def shifts():
        pipeline.mine(str(repo), ref)
        pipeline.build_db(ref)
        return [s for s in pipeline.signals(ref).signals if s["type"] == "author_class"]

    assert [(s["from_class"], s["to_class"]) for s in shifts()] == [("human", "bot")]
    (repo / ".mailmap").write_text(
        "Real Human <human@example.test> dependabot[bot] <dep@example.test>\n"
    )
    assert [(s["from_class"], s["to_class"]) for s in shifts()] == [("human", "bot")]


def test_sync_uses_custom_profile_identities(tmp_path, monkeypatch):
    repo = _identity_history(tmp_path / "repo")
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config/profile.json").write_text(
        json.dumps({"developer": {"name": "Real Human", "agent_identities": []}})
    )
    custom = tmp_path / "profile.json"
    custom.write_text(
        json.dumps(
            {
                "developer": {
                    "name": "Owner",
                    "aliases": ["Real Human"],
                    "agent_identities": ["dependabot[bot]"],
                },
                "sync": {"run_signals": True, "run_analysis": False},
                "projects": [{"name": "demo", "repo_path": str(repo)}],
            }
        )
    )
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    assert pipeline.sync(["demo"], profile_path=str(custom)).exit_code == 0
    result = json.loads((workspace / "projects/demo/data/detected-signals.json").read_text())
    shifts = [s for s in result["signals"] if s["type"] == "author_class"]
    assert [(s["from_class"], s["to_class"]) for s in shifts] == [("human", "agent")]
    assert "Owner to dependabot[bot]" in shifts[0]["detail"]


def test_mining_does_not_run_repository_configured_signature_programs(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "source")
    marker = tmp_path / "ran"
    program = tmp_path / "fake-gpg"
    program.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    program.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "config", "log.showSignature", "true"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "gpg.program", str(program)], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "commit", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    header, body = head.split("\n\n", 1)
    signed = (
        header
        + "\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n -----END PGP SIGNATURE-----\n\n"
        + body
    )
    # Bytes, not text: text-mode pipes turn \n into \r\n on Windows and corrupt the object.
    sha = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
            input=signed.encode(),
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/signed", sha], check=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    pipeline.init(ProjectRef.for_cli("sig", must_exist=False), "sig", "")
    pipeline.mine(str(repo), ProjectRef.for_cli("sig"))
    assert not marker.exists()


def test_mining_accepts_out_of_range_timezone_offsets(tmp_path):
    from archaeology.extractors.git import extract_git_log

    repo = _make_repo(tmp_path / "repo")
    env = {**os.environ, **GIT_ENV}

    def git(*args, stdin=None):
        # Bytes, not text: text-mode pipes turn \n into \r\n on Windows and corrupt the object.
        return (
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                env=env,
                capture_output=True,
                input=stdin.encode() if stdin is not None else None,
            )
            .stdout.decode()
            .strip()
        )

    tree = git("rev-parse", "HEAD^{tree}")
    parent = git("rev-parse", "HEAD")
    body = (
        f"tree {tree}\nparent {parent}\n"
        "author Old <old@example.com> 1700000000 +2500\n"
        "committer Old <old@example.com> 1700000000 +2500\n\nancient import\n"
    )
    sha = git("hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=body)
    git("update-ref", "refs/heads/old", sha)
    csv_path = tmp_path / "out.csv"
    extract_git_log(str(repo), str(csv_path))
    assert f"{sha},2023-11-14T22:13:20+00:00,ancient import" in csv_path.read_text()


def test_mining_keeps_config_passed_through_the_environment(tmp_path, monkeypatch):
    from archaeology.extractors.git import git_env

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")
    monkeypatch.setenv("GIT_DIR", str(tmp_path))
    env = git_env()
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert "GIT_DIR" not in env


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs non-UTF-8 file names")
def test_repository_path_that_is_not_utf8_is_accepted(tmp_path):
    repo = _make_repo(Path(os.fsdecode(os.fsencode(str(tmp_path)) + b"/caf\xe9")))
    assert pipeline.require_git_repo(str(repo)) == str(repo)


def test_bare_repository_name_with_a_unicode_line_separator_is_accepted(tmp_path):
    bare = tmp_path / "bare\u2028x.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], check=True, env={**os.environ, **GIT_ENV}
    )
    assert pipeline.require_git_repo(str(bare)) == str(bare)


def test_sync_refuses_a_shallow_clone(tmp_path, monkeypatch):
    source = _make_repo(tmp_path / "source")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{source}", str(shallow)],
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(_profile(tmp_path, shallow)), on_line=lines)
    assert outcome.exit_code == 1
    assert "Shallow history" in lines.text()
    assert not (workspace / "projects" / "demo" / "data" / "github-commits.csv").exists()


def test_sync_rebinds_mining_evidence_so_audit_sees_no_drift(tmp_path, monkeypatch):
    from archaeology.audit import check_mined_history

    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    profile = _profile(tmp_path, repo)
    pipeline.mine(str(repo), ProjectRef("demo"))  # binds coverage.json to the mined bytes
    (repo / "later.txt").write_text("later\n")
    for args in (
        ["add", "-A"],
        ["-c", "user.name=T", "-c", "user.email=t@e", "commit", "-qm", "later"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env={**os.environ, **GIT_ENV})
    assert pipeline.sync(["demo"], profile_path=str(profile), skip_signals=True).exit_code == 0
    codes = {f.code for f in check_mined_history("demo", workspace)}
    assert not codes & {
        "MINING_ARTIFACT_DRIFT",
        "MINING_HISTORY_DRIFT",
        "MINING_MANIFEST_INVALID",
    }, codes


def test_cascade_refuses_a_shallow_clone(tmp_path, monkeypatch):
    source = _make_repo(tmp_path / "source")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{source}", str(shallow)],
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    monkeypatch.delenv("DEVARCH_WORKSPACE", raising=False)
    ref = ProjectRef.for_cli("alpha", must_exist=False)
    pipeline.init(ref, "Alpha", "")
    config_path = ws / "projects" / "alpha" / "project.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repo_path"] = str(shallow)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    lines = Lines()
    outcome = pipeline.cascade(ref, on_line=lines)
    assert outcome.exit_code == 1
    assert "Shallow history" in lines.text()
    assert not (ws / "projects" / "alpha" / "data" / "github-commits.csv").exists()


def test_sync_labels_a_failure_after_extraction_as_mining(tmp_path, monkeypatch):
    import archaeology.extractors.git as extractor

    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    real = extractor.repository_coverage
    calls = []

    def moving_refs(path):  # a different snapshot on every call: refs moved mid-mine
        calls.append(path)
        return {**real(path), "snapshot": len(calls)}

    monkeypatch.setattr(extractor, "repository_coverage", moving_refs)
    lines = Lines()
    outcome = pipeline.sync(None, profile_path=str(_profile(tmp_path, repo)), on_line=lines)
    assert outcome.exit_code == 1
    assert "Error: Mining failed: Repository refs changed" in lines.text()
    assert "Git stats extraction failed" not in lines.text()
