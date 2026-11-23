"""Regression tests for canonical author signals."""

import csv
import json
import os
import sqlite3
import subprocess
from datetime import date, timedelta

from archaeology.classifiers.era_detector import detect_signals
from archaeology.extractors.git import extract_git_log, extract_git_log_with_stats


def test_mining_uses_repository_mailmap(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Alias",
        "GIT_AUTHOR_EMAIL": "alias@example.test",
        "GIT_COMMITTER_NAME": "Committer",
        "GIT_COMMITTER_EMAIL": "commit@example.test",
    }

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)

    git("init", "-q")
    (repo / ".mailmap").write_text(
        "Canonical <canonical@example.test> Alias <alias@example.test>\n"
    )
    (repo / "file").write_text("hello")
    git("add", ".")
    git("commit", "-q", "-m", "initial")
    csv_path = tmp_path / "commits.csv"
    extract_git_log(str(repo), str(csv_path))
    with csv_path.open(newline="") as f:
        row = next(csv.DictReader(f))
    assert (row["author"], row["email"]) == ("Canonical", "canonical@example.test")
    stats_path = tmp_path / "stats.txt"
    extract_git_log_with_stats(str(repo), str(stats_path))
    assert "Canonical" in stats_path.read_text()
    bare = tmp_path / "mirror.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(repo), str(bare)], check=True, env=env)
    extract_git_log(str(bare), str(csv_path))
    with csv_path.open(newline="") as f:
        bare_row = next(csv.DictReader(f))
    assert (bare_row["author"], bare_row["email"]) == ("Canonical", "canonical@example.test")


def test_aliases_and_classes_report_only_sustained_handoffs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "projects/demo"
    (project / "data").mkdir(parents=True)
    (project / "project.json").write_text(json.dumps({"name": "demo"}))
    (project / "identity_map.json").write_text(
        json.dumps({"aliases": {"Alias Two": "Developer", "RA": "Research Agent"}})
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config/profile.json").write_text(
        json.dumps(
            {
                "developer": {
                    "name": "Developer",
                    "aliases": ["Alias One", "alias@example.test"],
                    "agent_identities": ["Research Agent", "agent@example.test"],
                }
            }
        )
    )
    rows = []
    for day in range(60):
        if day < 40:
            name, email = [
                ("Developer", "dev@example.test"),
                ("Alias One", "different@example.test"),
                ("Unlisted Name", "alias@example.test"),
                ("Alias Two", "other@example.test"),
            ][day // 10]
        elif day < 50:
            name, email = "Builder[bot]", "builder-bot@example.test"
        else:
            name, email = "RA", "worker@example.test"
        rows.append(((date(2026, 1, 1) + timedelta(days=day)).isoformat(), name, email, "work"))
    with sqlite3.connect(project / "data/archaeology.db") as conn:
        conn.execute("CREATE TABLE commits (date TEXT, author TEXT, email TEXT, message TEXT)")
        conn.executemany("INSERT INTO commits VALUES (?, ?, ?, ?)", rows)
    result = detect_signals("demo")
    author_signals = [s for s in result["signals"] if s["type"] in ("author", "author_class")]
    shifts = [s for s in author_signals if s["type"] == "author_class"]
    assert [(s["from_class"], s["to_class"]) for s in shifts] == [
        ("human", "bot"),
        ("bot", "agent"),
    ]
    assert all(s["strength"] == "strong" for s in shifts)
    assert not [s for s in author_signals if "Alias" in s["detail"]]


def test_short_author_change_is_debounced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "projects/demo"
    (project / "data").mkdir(parents=True)
    rows = []
    for day in range(25):
        name = "Build[bot]" if 10 <= day < 12 else "Developer"
        rows.append(((date(2026, 1, 1) + timedelta(days=day)).isoformat(), name, "work"))
    with sqlite3.connect(project / "data/archaeology.db") as conn:
        conn.execute("CREATE TABLE commits (date TEXT, author TEXT, message TEXT)")
        conn.executemany("INSERT INTO commits VALUES (?, ?, ?)", rows)
    result = detect_signals("demo")
    assert not [s for s in result["signals"] if s["type"] in ("author", "author_class")]


def test_commit_debounce_emits_one_same_class_shift(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "projects/demo"
    (project / "data").mkdir(parents=True)
    with sqlite3.connect(project / "data/archaeology.db") as conn:
        conn.execute("CREATE TABLE commits (date TEXT, author TEXT, message TEXT)")
        conn.executemany(
            "INSERT INTO commits VALUES (?, ?, ?)",
            [
                ("2026-01-01", "First", "work"),
                ("2026-01-02T01:00:00", "Second", "work"),
                ("2026-01-02T02:00:00", "Second", "work"),
                ("2026-01-02T03:00:00", "Second", "work"),
                ("2026-01-03", "Second", "work"),
            ],
        )
    result = detect_signals("demo", config={"author_debounce_commits": 3})
    shifts = [s for s in result["signals"] if s["type"] == "author"]
    assert len(shifts) == 1
    assert shifts[0]["strength"] == "moderate"
    assert "First to Second" in shifts[0]["detail"]


def test_current_packaged_profile_aliases_override_stale_project_settings(tmp_path, monkeypatch):
    from archaeology.classifiers import era_detector

    monkeypatch.chdir(tmp_path)
    package = tmp_path / "package"
    (package / "archaeology").mkdir(parents=True)
    (package / "config").mkdir()
    (package / "config/profile.json").write_text(
        json.dumps(
            {
                "developer": {
                    "name": "Developer",
                    "aliases": ["Old Alias"],
                }
            }
        )
    )
    project = tmp_path / "projects/demo"
    (project / "data").mkdir(parents=True)
    (project / "project.json").write_text(
        json.dumps(
            {
                "developer": {
                    "name": "Developer",
                    "aliases": [],
                }
            }
        )
    )
    with sqlite3.connect(project / "data/archaeology.db") as conn:
        conn.execute("CREATE TABLE commits (date TEXT, author TEXT, message TEXT)")
        rows = [
            (
                (date(2026, 1, 1) + timedelta(days=day)).isoformat(),
                "Developer" if day < 10 else "Old Alias",
                "work",
            )
            for day in range(20)
        ]
        conn.executemany("INSERT INTO commits VALUES (?, ?, ?)", rows)
    monkeypatch.setattr(era_detector, "_script_dir", lambda: package / "archaeology")
    result = detect_signals("demo")
    assert not [signal for signal in result["signals"] if signal["type"] == "author"]


def test_human_names_and_bot_domain_do_not_imply_bot():
    from archaeology.classifiers.era_detector import _identity_class

    for name, email in [
        ("Abbott", "abbott@example.test"),
        ("Talbot", "talbot@example.test"),
        ("Robotics", "person@example.test"),
        ("Alice [bot] Smith", "alice@example.test"),
        ("Alice", "alice@bot.example"),
    ]:
        assert _identity_class(name, email, name, set()) == "human"
