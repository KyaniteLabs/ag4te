"""CI contract for the DevArch Hermes workflow.

Simon asked on PR #94 (2026-11-22) that `.github/workflows/devarch-hermes.yml`
get the same treatment the merged CI-efficiency pass (#128) gave `ci.yml`:

- a `concurrency` block that cancels superseded pull-request runs,
- a `timeout-minutes` on every job,
- the same `setup-python` / `setup-uv` pins.

These are workflow-text assertions, so they read the YAML as text and pin the
exact action SHAs rather than trusting a parser to see them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "devarch-hermes.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The pins main's ci.yml uses after #128. devarch-hermes.yml must match them.
SETUP_PYTHON_PIN = "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0"  # v7.1.0
SETUP_UV_PIN = "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2"  # v10.3.3


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _jobs(text: str) -> dict[str, str]:
    """Map job name -> its block of text."""
    jobs: dict[str, str] = {}
    current: str | None = None
    lines = text.splitlines()
    in_jobs = False
    for line in lines:
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        header = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if header:
            current = header.group(1)
            jobs[current] = ""
            continue
        if current is not None:
            jobs[current] += line + "\n"
    return jobs


def test_workflow_file_exists():
    assert WORKFLOW.is_file(), WORKFLOW


def test_workflow_has_a_concurrency_block_that_cancels_superseded_pull_request_runs():
    text = _text()
    assert re.search(r"^concurrency:", text, re.M), "no concurrency block"
    assert "cancel-in-progress:" in text, "concurrency block does not cancel in progress"
    assert "pull_request" in text, "concurrency block does not mention pull_request"


def test_every_job_has_a_timeout():
    jobs = _jobs(_text())
    assert jobs, "no jobs parsed from the workflow"
    missing = sorted(name for name, body in jobs.items() if "timeout-minutes:" not in body)
    assert not missing, f"jobs without timeout-minutes: {missing}"


def test_setup_actions_use_the_same_pins_as_ci_yml():
    text = _text()
    assert SETUP_PYTHON_PIN in text, "setup-python pin is behind ci.yml's"
    assert SETUP_UV_PIN in text, "setup-uv pin is behind ci.yml's"


def test_ci_yml_actually_uses_the_pins_this_workflow_must_match():
    """The shared pins are only shared if ci.yml still carries them."""
    ci_text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert SETUP_PYTHON_PIN in ci_text, "ci.yml moved on; update this test's pins"
    assert SETUP_UV_PIN in ci_text, "ci.yml moved on; update this test's pins"


def test_no_unpinned_pip_install_of_the_pinned_tools():
    """ci.yml pins pip/build/twine exactly; this workflow must not undo that."""
    for number, line in enumerate(_text().splitlines(), 1):
        if not re.search(r"\bpip install\b", line):
            continue
        for token in line.split("pip install", 1)[1].split():
            if token.startswith("-"):
                continue
            assert "==" in token, f"devarch-hermes.yml:{number} installs {token} unpinned"
