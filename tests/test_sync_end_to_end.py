"""``devarch sync`` end to end: the registered profile and a legacy console.

Audit ledger row 44. The verb resolved its profile from the package's own
``config/profile.json`` first and only fell back to the working directory when
that file was absent. An editable install (``pip install -e .``, the documented
setup) always has the package file, so it shadowed the user's profile:
``devarch sync`` ran the developer's registered projects, and
``devarch sync --project <name>`` answered ``Unknown project`` for a project the
user had registered.

The first two tests run the installed CLI in a fresh process, from a working
directory that holds its own ``config/profile.json``, the way a user runs it.
Nothing is patched. A built wheel ships no ``config/`` and cannot shadow
anything, so they skip when the imported package has no profile beside it.

The third test forces ``PYTHONIOENCODING=cp1252``: sync's transcript carries
arrows and check marks, and a console with that code page used to crash the run
with UnicodeEncodeError after the projects were already built. The variable is
honoured on every OS, so the legacy-console path stays covered without a
Windows runner.
"""

import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from archaeology import cli

# cli.__file__ is the real source file under `pip install -e .`, and a
# site-packages path under a wheel install.
PACKAGE_PROFILE = Path(cli.__file__).resolve().parent.parent / "config" / "profile.json"

requires_editable_install = pytest.mark.skipif(
    not PACKAGE_PROFILE.exists(),
    reason="the imported package ships no config/profile.json, so it cannot shadow a workspace profile",
)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
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


def _workspace(root: Path, repo: Path) -> Path:
    """A working directory whose ./config/profile.json registers two projects."""
    workspace = root / "workspace"
    (workspace / "config").mkdir(parents=True)
    (workspace / "config" / "profile.json").write_text(
        json.dumps(
            {
                "developer": {"name": "T"},
                "sync": {"run_signals": False, "run_analysis": False},
                "projects": [
                    {"name": "good-one", "path": str(repo)},
                    {"name": "missing-one", "path": str(root / "nowhere")},
                ],
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _devarch(cwd: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run the installed CLI from ``cwd`` in a fresh process, as a user does."""
    env = {key: value for key, value in os.environ.items() if key != "DEVARCH_WORKSPACE"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "archaeology.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


@requires_editable_install
def test_sync_project_selects_a_project_registered_in_the_workspace_profile(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = _workspace(tmp_path, repo)

    result = _devarch(workspace, "sync", "--project", "good-one")

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Unknown project" not in output
    assert "Syncing 1 project(s)..." in output
    assert "  Mining good-one..." in output
    assert "    3 commits extracted" in output
    assert "    DB built" in output
    assert "Sync complete." in output
    mined = workspace / "projects" / "good-one" / "data" / "github-commits.csv"
    rows = list(csv.DictReader(io.StringIO(mined.read_text(encoding="utf-8"))))
    assert len(rows) == 3


@requires_editable_install
def test_sync_all_operates_only_on_projects_registered_in_the_workspace_profile(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = _workspace(tmp_path, repo)

    result = _devarch(workspace, "sync")

    output = result.stdout + result.stderr
    # Both registered projects were handled: one built, one was reported missing.
    assert "Syncing 2 project(s)..." in output
    assert "  Mining good-one..." in output
    assert "    3 commits extracted" in output
    assert "  SKIP missing-one: repo not found at" in output
    assert "1 built, 1 failed: missing-one" in output
    assert result.returncode == 1
    # Nothing else: no project from the package's own profile was created.
    created = sorted(p.name for p in (workspace / "projects").iterdir())
    assert created == ["good-one", "missing-one"]


def test_sync_completes_on_a_legacy_console(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = _workspace(tmp_path, repo)

    result = _devarch(
        workspace, "sync", "--project", "good-one", env_extra={"PYTHONIOENCODING": "cp1252"}
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Traceback" not in output
    assert "global-commits.csv" in output
    assert "Sync complete." in output
    assert (workspace / "projects" / "good-one" / "data" / "archaeology.db").exists()
