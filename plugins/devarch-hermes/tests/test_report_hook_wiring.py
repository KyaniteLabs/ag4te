"""The report hook is wired and fires on a real run.

The demo proves the hooks are *registered*; this proves the registered
`post_tool_call` hook actually fires after a real tool call and writes the
report ledger. It drives the plugin's own `register()` surface with the same
call shape Hermes uses, so a rename or a dropped `register_hook` fails here.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_INIT = PLUGIN_DIR / "__init__.py"


class _Ctx:
    """Minimal stand-in for the Hermes plugin context."""

    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}
        self.tools: dict[str, object] = {}
        self.commands: dict[str, object] = {}

    def get_config(self, key: str, default: object = None) -> object:
        return None

    def register_hook(self, name: str, fn: object) -> None:
        self.hooks[name] = fn

    def register_command(
        self, name: str, handler: object = None, description: str = "", args_hint: str = ""
    ) -> None:
        self.commands[name] = handler

    def register_tool(self, **kwargs: object) -> None:
        self.tools[str(kwargs["name"])] = kwargs["handler"]

    def register_skill(self, name: str, path: Path) -> None:
        return None


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "hook@example.com")
    _git(path, "config", "user.name", "Hook")
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Hook",
            "GIT_AUTHOR_EMAIL": "hook@example.com",
            "GIT_COMMITTER_NAME": "Hook",
            "GIT_COMMITTER_EMAIL": "hook@example.com",
        }
    )
    for index, (stamp, message) in enumerate(
        [
            ("2026-01-01T12:00:00", "start"),
            ("2026-01-05T12:00:00", "early"),
            ("2026-02-20T12:00:00", "after gap"),
        ]
    ):
        (path / f"f{index}.txt").write_text(message + "\n", encoding="utf-8")
        _git(path, "add", f"f{index}.txt")
        commit_env = env.copy()
        commit_env["GIT_AUTHOR_DATE"] = stamp
        commit_env["GIT_COMMITTER_DATE"] = stamp
        _git(path, "commit", "-m", message, env=commit_env)
    return path


@pytest.fixture
def registered(tmp_path, monkeypatch):
    """Load the plugin through its own register() surface."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib.util

    spec = importlib.util.spec_from_file_location("devarch_hermes_plugin", PLUGIN_INIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ctx = _Ctx()
    module.register(ctx)
    ctx.plugin = module  # type: ignore[attr-defined]
    ctx.home = home  # type: ignore[attr-defined]
    return ctx


def test_hooks_are_registered_under_the_names_the_manifest_declares(registered):
    assert "post_tool_call" in registered.hooks
    assert "pre_llm_call" in registered.hooks


def test_report_hook_fires_on_a_real_tool_run_and_writes_the_ledger(registered):
    """A real devarch_pipeline call must leave one ledger row."""
    plugin = registered.plugin
    repo = _repo(registered.home / "repo")

    tool = registered.tools["devarch_pipeline"]
    result = tool({"repo_path": str(repo), "project": "hookdemo"})
    assert '"ok": true' in result.replace("'", '"'), result[:400]

    # Fire the hook the way Hermes fires it after a tool call.
    registered.hooks["post_tool_call"](
        tool_name="devarch_pipeline", args={"repo_path": str(repo)}, result=result
    )

    ledger = plugin._load_json(plugin._ledger_path(), [])
    assert len(ledger) == 1, ledger
    row = ledger[0]
    assert row["tool"] == "devarch_pipeline"
    assert row["project"] == "hookdemo"
    assert row["ok"] is True
    assert row["commits"] == 3
    assert row["signals"] >= 1
    assert row["output_path"].endswith("ARCHAEOLOGY-REPORT.md")
    # The ledger must exist as a real file a later reader can open.
    assert json.loads(Path(plugin._ledger_path()).read_text(encoding="utf-8")) == ledger


def test_report_hook_ignores_tools_that_are_not_devarch(registered):
    """The hook must not record rows for unrelated tools."""
    plugin = registered.plugin
    registered.hooks["post_tool_call"](tool_name="some_other_tool", args={}, result={"ok": True})
    assert plugin._load_json(plugin._ledger_path(), []) == []
