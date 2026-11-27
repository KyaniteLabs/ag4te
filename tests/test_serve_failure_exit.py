"""Audit row 49: `devarch serve` must not report a failed Datasette launch as success.

`serve` was the only ``subprocess.run`` in ``archaeology/cli.py`` without error
handling. When Datasette could not start (for example the port was already in
use) it exited non-zero, yet ``devarch serve`` printed the launch line and
exited 0, so an operator was told the server was up while nothing was serving.
"""

import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from archaeology import cli
from archaeology.cli import main


def _make_serve_project(root: Path, name: str = "serve-proj") -> Path:
    proj_dir = root / "projects" / name / "data"
    proj_dir.mkdir(parents=True)
    db_path = proj_dir / "archaeology.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE commits (sha TEXT, author TEXT, date TEXT)")
    conn.commit()
    conn.close()
    return db_path


def _put_datasette_on_path(monkeypatch) -> None:
    """`serve` runs the `datasette` console script installed beside this interpreter."""
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")]),
    )


def test_serve_propagates_a_nonzero_datasette_exit(tmp_path, monkeypatch):
    """A child that exits non-zero must become a non-zero `serve` exit, not a launch claim."""
    monkeypatch.chdir(tmp_path)
    _make_serve_project(tmp_path)

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(3, cmd)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = CliRunner().invoke(main, ["serve", "serve-proj", "--port", "8124"])

    assert result.exit_code != 0, result.output
    assert "nothing is serving" in result.output
    assert "8124" in result.output


@pytest.mark.skipif(os.name == "nt", reason="POSIX socket holding; the unit test covers Windows")
def test_serve_exits_nonzero_when_the_port_is_already_bound(tmp_path, monkeypatch):
    """End-to-end: Datasette cannot bind an occupied port, so `serve` must exit non-zero."""
    monkeypatch.chdir(tmp_path)
    _make_serve_project(tmp_path)
    _put_datasette_on_path(monkeypatch)

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        result = CliRunner().invoke(main, ["serve", "serve-proj", "--port", str(port)])
    finally:
        holder.close()

    assert result.exit_code != 0, result.output
    assert "nothing is serving" in result.output
    assert str(port) in result.output
