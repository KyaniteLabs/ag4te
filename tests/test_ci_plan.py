"""CI plan: every required check reports, and only provably redundant work is skipped."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))

import ci_plan  # noqa: E402
import merged_tree_tested  # noqa: E402

REPO = "KyaniteLabs/ag4te"
SHA = "a" * 40
HEAD = "b" * 40
REQUIRED = {f"test ({o}, {p})" for o in ci_plan.OSES for p in ci_plan.PYTHONS}


def names(legs):
    return {f"test ({leg['os']}, {leg['python-version']})" for leg in legs}


def files_fetch(paths):
    def fetch(path):
        assert "/pulls/7/files" in path
        return [{"filename": p} for p in paths] if "page=1" in path else []

    return fetch


def full(legs):
    return {(leg["os"], leg["python-version"]) for leg in legs if not leg["light"]}


def test_code_pr_reports_every_required_check_and_trims_only_old_macos():
    run, legs, _ = ci_plan.plan(
        "pull_request", files_fetch(["archaeology/cli.py"]), REPO, SHA, 7, "ci.yml", "main"
    )
    assert run and names(legs) == REQUIRED
    light = {(leg["os"], leg["python-version"]) for leg in legs if leg["light"]}
    assert light == {("macos-latest", "3.10"), ("macos-latest", "3.11")}
    assert all(leg["runner"] == "ubuntu-latest" for leg in legs if leg["light"])
    assert all(leg["runner"] == leg["os"] for leg in legs if not leg["light"])


def test_prose_only_pr_keeps_linux_full_and_lightens_macos_and_windows():
    run, legs, _ = ci_plan.plan(
        "pull_request",
        files_fetch(["README.md", "docs/index.html", "plugins/ag4te-hermes/README.md"]),
        REPO,
        SHA,
        7,
        "ci.yml",
        "main",
    )
    assert run and names(legs) == REQUIRED
    assert full(legs) == {("ubuntu-latest", p) for p in ci_plan.PYTHONS}


def test_product_markdown_and_workflows_are_not_prose():
    for path in (
        "projects/demo/deliverables/reports/ARCHAEOLOGY-REPORT.md",
        "skills/excavate/SKILL.md",
        "analysis-vectors/x.md",
        ".github/workflows/ci.yml",
        "setup.py",
    ):
        assert not ci_plan.is_docs_path(path), path
    assert not ci_plan.docs_only([])


def test_api_failure_on_a_pr_runs_everything():
    def broken(path):
        raise OSError("down")

    run, legs, reason = ci_plan.plan("pull_request", broken, REPO, SHA, 7, "ci.yml", "main")
    assert run and "could not" in reason
    assert len(full(legs)) == 7  # only the old-macOS trim, which needs no API


def test_push_skips_only_a_tree_its_pr_already_passed():
    def fetch(path):
        if path.endswith(f"/commits/{SHA}/pulls"):
            return [
                {
                    "number": 7,
                    "merged_at": "x",
                    "merge_commit_sha": SHA,
                    "base": {"ref": "main"},
                    "head": {"sha": HEAD},
                }
            ]
        if "/git/commits/" in path:
            return {"tree": {"sha": "t"}}
        return {
            "workflow_runs": [
                {
                    "head_sha": HEAD,
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "success",
                    "run_number": 1,
                }
            ]
        }

    run, legs, _ = ci_plan.plan("push", fetch, REPO, SHA, None, "ci.yml", "main")
    assert not run
    run, legs, _ = ci_plan.plan("push", None, REPO, SHA, None, "ci.yml", "main")
    assert run and len(full(legs)) == 9


def test_push_into_another_base_is_not_skipped():
    def fetch(path):
        return [
            {
                "number": 7,
                "merged_at": "x",
                "merge_commit_sha": SHA,
                "base": {"ref": "master"},
                "head": {"sha": HEAD},
            }
        ]

    tested, _ = merged_tree_tested.decide(fetch, REPO, SHA, "ci.yml", "main")
    assert not tested


def test_schedule_runs_the_full_matrix_and_main_prints_outputs(capsys):
    assert (
        ci_plan.main(
            ["--event", "schedule", "--repository", REPO, "--sha", SHA], fetch=lambda p: None
        )
        == 0
    )
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert out["run"] == "true"
    assert len(full(json.loads(out["matrix"]))) == 9
