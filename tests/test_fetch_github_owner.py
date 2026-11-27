"""`devarch fetch-github` must never succeed with an empty dataset (audit row 46).

The command used to default its owner to an empty string, list the authenticated
account's repositories, build every contributors URL as ``repos//<name>/...``
(GitHub answers 404), then report "Saved 0 repos" and exit 0 after writing a
zero-repo ``github-repos.json``. A later dashboard reported "0 commits across
0 repos" from that file.

These tests pin the replacement contract:

* a missing or blank owner fails loudly and writes nothing;
* the README's documented positional form is accepted, as are ``--owner`` and
  ``ARCHAEOLOGY_GITHUB_OWNER``;
* a fetch that returns zero repositories is an error, and an existing dataset
  is left untouched.
"""

import json
import re
import shlex
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import archaeology.visualization.github_fetcher as github_fetcher
from archaeology.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
OWNER_ENV = "ARCHAEOLOGY_GITHUB_OWNER"
OUTPUT_RELATIVE = Path("global") / "data" / "github-repos.json"


@pytest.fixture
def fetch_workspace(tmp_path, monkeypatch):
    """A clean working directory with no owner in the environment."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(OWNER_ENV, raising=False)
    return tmp_path


def _readme_fetch_github_argv():
    """The README's documented ``devarch fetch-github`` form, as CLI arguments.

    ``<placeholders>`` become sample values and ``[optional]`` parts are
    dropped, so the test runs the form a README reader is told to type.
    """
    text = README.read_text(encoding="utf-8")
    match = re.search(r"`(devarch fetch-github [^`]*)`", text)
    assert match, "README no longer documents `devarch fetch-github`"
    argv = []
    for token in shlex.split(match.group(1)):
        if token.startswith("["):
            assert token.endswith("]"), f"unexpected README notation: {token}"
            continue
        argv.append("example-owner" if token.startswith("<") else token)
    assert argv[0] == "devarch", argv
    return argv[1:]


def _assert_nothing_written(workspace):
    assert not (workspace / OUTPUT_RELATIVE).exists()


def test_readme_documented_form_is_accepted(fetch_workspace):
    dataset = {"total_repos": 2, "total_commits": 9}
    with patch.object(github_fetcher, "save_github_data", return_value=dataset) as save:
        result = CliRunner().invoke(main, _readme_fetch_github_argv())
    assert result.exit_code == 0, result.output
    assert save.call_args.args[0] == str(OUTPUT_RELATIVE)
    assert save.call_args.kwargs["owner"] == "example-owner"
    assert "2 repos, 9 total commits" in result.output


def test_owner_option_is_still_accepted(fetch_workspace):
    dataset = {"total_repos": 1, "total_commits": 3}
    with patch.object(github_fetcher, "save_github_data", return_value=dataset) as save:
        result = CliRunner().invoke(main, ["fetch-github", "--owner", "example-owner"])
    assert result.exit_code == 0, result.output
    assert save.call_args.kwargs["owner"] == "example-owner"


def test_owner_env_var_is_still_used(fetch_workspace, monkeypatch):
    monkeypatch.setenv(OWNER_ENV, "env-owner")
    dataset = {"total_repos": 1, "total_commits": 3}
    with patch.object(github_fetcher, "save_github_data", return_value=dataset) as save:
        result = CliRunner().invoke(main, ["fetch-github"])
    assert result.exit_code == 0, result.output
    assert save.call_args.kwargs["owner"] == "env-owner"


def test_positional_owner_overrides_the_env_default(fetch_workspace, monkeypatch):
    monkeypatch.setenv(OWNER_ENV, "env-owner")
    dataset = {"total_repos": 1, "total_commits": 3}
    with patch.object(github_fetcher, "save_github_data", return_value=dataset) as save:
        result = CliRunner().invoke(main, ["fetch-github", "explicit-owner"])
    assert result.exit_code == 0, result.output
    assert save.call_args.kwargs["owner"] == "explicit-owner"


def test_missing_owner_fails_loudly(fetch_workspace):
    with patch.object(github_fetcher, "_gh", return_value="[]") as gh:
        result = CliRunner().invoke(main, ["fetch-github"])
    assert result.exit_code != 0, result.output
    assert "owner" in result.output.lower()
    gh.assert_not_called()
    _assert_nothing_written(fetch_workspace)


@pytest.mark.parametrize(
    "argv",
    [
        ["fetch-github", ""],
        ["fetch-github", "   "],
        ["fetch-github", "--owner", ""],
        ["fetch-github", "--owner", "  \t "],
    ],
    ids=["positional-empty", "positional-space", "option-empty", "option-space"],
)
def test_blank_owner_is_rejected(argv, fetch_workspace):
    with (
        patch.object(github_fetcher, "save_github_data") as save,
        patch.object(github_fetcher, "_gh") as gh,
    ):
        result = CliRunner().invoke(main, argv)
    assert result.exit_code != 0, result.output
    assert "owner" in result.output.lower()
    assert save.call_count == 0
    assert gh.call_count == 0
    _assert_nothing_written(fetch_workspace)


def test_conflicting_owner_forms_are_rejected(fetch_workspace):
    with patch.object(github_fetcher, "save_github_data") as save:
        result = CliRunner().invoke(main, ["fetch-github", "owner-one", "--owner", "owner-two"])
    assert result.exit_code != 0, result.output
    assert save.call_count == 0
    _assert_nothing_written(fetch_workspace)


def test_zero_repos_fails_and_keeps_the_existing_dataset(fetch_workspace):
    output_path = fetch_workspace / OUTPUT_RELATIVE
    output_path.parent.mkdir(parents=True)
    previous = '{"owner": "example-owner", "total_repos": 1, "total_commits": 5, "repos": []}'
    output_path.write_text(previous, encoding="utf-8")

    runner = CliRunner()
    # (a) the repo-list call fails; (b) every repo is skipped as a fork
    for gh_result in (None, json.dumps([{"name": "a-fork", "isFork": True}])):
        with (
            patch.object(github_fetcher, "_gh", return_value=gh_result),
            patch.object(github_fetcher, "_gh_api", return_value=None),
        ):
            result = runner.invoke(main, ["fetch-github", "example-owner"])
        assert result.exit_code != 0, result.output
        assert "0 repos" in result.output
        assert output_path.read_text(encoding="utf-8") == previous


def test_save_github_data_writes_the_dataset_it_fetched(tmp_path):
    repo_list = json.dumps(
        [
            {
                "name": "alpha",
                "isFork": False,
                "primaryLanguage": {"name": "Python"},
                "createdAt": "2026-01-02T03:04:05Z",
                "updatedAt": "2026-02-03T04:05:06Z",
                "description": "demo",
                "diskUsage": 12,
            }
        ]
    )
    endpoints = []

    def fake_gh_api(endpoint, jq_filter=None):
        endpoints.append(endpoint)
        return "someone:3" if "contributors" in endpoint else "Python:120"

    output_path = tmp_path / "nested" / "github-repos.json"
    with (
        patch.object(github_fetcher, "_gh", return_value=repo_list),
        patch.object(github_fetcher, "_gh_api", side_effect=fake_gh_api),
    ):
        data = github_fetcher.save_github_data(output_path, owner="example-org")

    assert data["owner"] == "example-org"
    assert data["total_repos"] == 1
    assert data["total_commits"] == 3
    assert data["repos"][0]["authors"] == {"someone": 3}
    assert json.loads(output_path.read_text(encoding="utf-8")) == data
    assert endpoints, "no GitHub API call was made"
    assert all(endpoint.startswith("repos/example-org/alpha/") for endpoint in endpoints), endpoints


def test_fetch_all_repos_rejects_a_blank_owner_without_calling_github():
    with patch.object(github_fetcher, "_gh") as gh:
        with pytest.raises(github_fetcher.GitHubFetchError, match="owner"):
            github_fetcher.fetch_all_repos(owner="   ")
    gh.assert_not_called()
