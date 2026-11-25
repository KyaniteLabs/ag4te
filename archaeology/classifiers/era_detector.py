"""Signal detection module for development archaeology.

Detects development signals from commit history stored in a SQLite database
using 5 heuristics: gap detection, velocity shifts, author changes, scope
changes, and cross-repo activation.

This module does NOT define eras. It surfaces patterns and signals that a
human (or LLM with full analysis context) uses to define narrative eras.
Era definitions live in a hand-curated eras.json file.
"""

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from itertools import groupby
from pathlib import Path

from ..utils import _load_json, _parse_date, _script_dir

# Default config values
DEFAULTS = {
    "min_gap_days": 3,
    "velocity_shift_factor": 2.0,
    "scope_change_keywords": [
        "refactor",
        "rewrite",
        "restructure",
        "migration",
        "architecture",
    ],
    "cross_repo_activation_threshold": 3,
    "author_debounce_days": 7,
    "author_debounce_commits": None,
}


def _load_defaults_json() -> dict:
    """Load settings from config/defaults.json if available."""
    defaults_path = _script_dir().parent.parent / "config" / "defaults.json"
    if defaults_path.exists():
        try:
            with open(defaults_path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("signal_detection", {})
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _identity_values(value):
    if isinstance(value, dict):
        return [
            item
            for group in value.values()
            if isinstance(group, list)
            for item in group
            if isinstance(item, str)
        ]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _identity_settings(db_path, config):
    project = Path(db_path).parent.parent
    project_config = _load_json(project / "project.json") or {}
    workspace_profile = Path.cwd() / "config" / "profile.json"
    packaged_profile = _script_dir().parent / "config" / "profile.json"
    explicit_profile = config.get("identity_profile")
    profile = (
        explicit_profile
        if isinstance(explicit_profile, dict)
        else (
            _load_json(workspace_profile if workspace_profile.exists() else packaged_profile) or {}
        )
    )
    developer = {}
    sources = (
        (profile, config)
        if isinstance(explicit_profile, dict)
        else (project_config, profile, config)
    )
    for source in sources:
        if isinstance(source, dict) and isinstance(source.get("developer"), dict):
            developer.update(source["developer"])

    aliases = {}
    canonical = developer.get("name") or developer.get("email") or ""
    if isinstance(canonical, str) and canonical.strip():
        values = [canonical, developer.get("email"), *_identity_values(developer.get("aliases"))]
        for alias in values:
            if isinstance(alias, str) and alias.strip():
                aliases[alias.strip().casefold()] = canonical.strip()

    agents = {
        name.strip().casefold()
        for name in _identity_values(developer.get("agent_identities"))
        if name.strip()
    }
    for path in (project / "data" / "identity_map.json", project / "identity_map.json"):
        identity_map = _load_json(path) or {}
        if not isinstance(identity_map, dict):
            continue
        mapping = identity_map.get("aliases", identity_map)
        if isinstance(mapping, dict):
            for alias, name in mapping.items():
                if (
                    isinstance(alias, str)
                    and isinstance(name, str)
                    and alias.strip()
                    and name.strip()
                ):
                    aliases[alias.strip().casefold()] = name.strip()
        agents.update(
            name.strip().casefold()
            for name in _identity_values(identity_map.get("agent_identities"))
            if name.strip()
        )
    return aliases, agents


_BOT_IDENTITY = re.compile(r"(?:^|[._+-])bot$")


def _identity_class(author, email, canonical, agents):
    values = (author.strip().casefold(), email.strip().casefold(), canonical.strip().casefold())
    if any(value in agents for value in values if value):
        return "agent"
    raw_values = (author.strip().casefold(), email.strip().casefold())
    if any(
        value.split("@", 1)[0].endswith("[bot]") or _BOT_IDENTITY.search(value.split("@", 1)[0])
        for value in raw_values
    ):
        return "bot"
    if any(value.split("@", 1)[0] in ("repo-pipeline", "github-actions") for value in raw_values):
        return "bot"
    return "human"


class SignalDetector:
    """Detect development signals from commit history.

    Surfaces patterns (gaps, velocity shifts, author changes, scope keywords,
    new repo activation) as signals. Does NOT define eras — that is a human
    narrative judgment.

    Args:
        db_path: Path to the SQLite database containing a 'commits' table.
        config: Optional dict overriding default settings.
    """

    def __init__(self, db_path: str | Path, config: dict | None = None):
        self.db_path = Path(db_path)
        merged = {**DEFAULTS, **_load_defaults_json()}
        if config:
            merged.update(config)
        self.config = merged
        self.author_aliases, self.agent_identities = _identity_settings(self.db_path, merged)

        self.min_gap_days: int = merged["min_gap_days"]
        self.velocity_shift_factor: float = merged["velocity_shift_factor"]
        self.scope_change_keywords: list[str] = merged["scope_change_keywords"]
        self.cross_repo_threshold: int = merged["cross_repo_activation_threshold"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self) -> dict:
        """Run all heuristics and return structured signals + clusters.

        Returns:
            Dict with keys: project_meta, signals, cluster_summary,
            active_days_summary.
        """
        commits = self._load_commits()
        if not commits:
            return {"signals": [], "cluster_summary": []}

        gap_signals = self._detect_gaps(commits)
        velocity_signals = self._detect_velocity_shifts(commits)
        author_signals = self._detect_author_changes(commits)
        scope_signals = self._detect_scope_changes(commits)
        repo_signals = self._detect_cross_repo(commits)

        all_signals = gap_signals + velocity_signals + author_signals + scope_signals + repo_signals
        all_signals.sort(key=lambda s: (s["index"], s["type"]))

        clusters = self._build_clusters(commits)

        return {
            "commit_count": len(commits),
            "date_range": {
                "first": commits[0]["date"][:10] if commits[0]["date"] else "",
                "last": commits[-1]["date"][:10] if commits[-1]["date"] else "",
            },
            "active_days": len({c["date"][:10] for c in commits if len(c["date"]) >= 10}),
            "signals": all_signals,
            "cluster_summary": clusters,
        }

    def save(self, output_path: str | Path, result: dict | None = None) -> Path:
        """Write detected signals to a JSON file.

        Args:
            output_path: Path to write the JSON file.
            result: Signal dict (if None, runs detect() first).

        Returns:
            Path to the written file.
        """
        if result is None:
            result = self.detect()
        from ..utils import atomic_write

        output_path = Path(output_path)
        atomic_write(output_path, json.dumps(result, indent=2, ensure_ascii=False))
        return output_path

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_commits(self) -> list[dict]:
        """Load commits from the SQLite database, ordered by date."""
        if not self.db_path.exists():
            return []

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(commits)")}
            optional = [
                name for name in ("repo", "email", "raw_author", "raw_email") if name in columns
            ]
            cursor = conn.execute(
                f"SELECT {', '.join(['date', 'author', 'message', *optional])} FROM commits ORDER BY date ASC"
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            try:
                conn.close()
            except NameError:
                pass

        commits = []
        for row in rows:
            author = str(row["author"]) if row["author"] else ""
            email = str(row["email"]) if "email" in columns and row["email"] else ""
            canonical = (
                self.author_aliases.get(email.strip().casefold())
                or self.author_aliases.get(author.strip().casefold())
                or author
            )
            raw_author = (
                str(row["raw_author"]) if "raw_author" in columns and row["raw_author"] else author
            )
            raw_email = (
                str(row["raw_email"]) if "raw_email" in columns and row["raw_email"] else email
            )
            raw_canonical = (
                self.author_aliases.get(raw_email.strip().casefold())
                or self.author_aliases.get(raw_author.strip().casefold())
                or raw_author
            )
            commits.append(
                {
                    "date": str(row["date"]) if row["date"] else "",
                    "author": canonical,
                    "email": email,
                    "author_class": _identity_class(
                        raw_author, raw_email, raw_canonical, self.agent_identities
                    ),
                    "message": str(row["message"]) if row["message"] else "",
                    "repo": str(row["repo"]) if "repo" in columns and row["repo"] else "",
                }
            )
        # SQL sorted the date *text*; mixed UTC offsets make that disagree with
        # real order. Re-sort by the parsed instant (stable for unparseable rows).
        commits.sort(key=lambda c: _parse_date(c["date"]) or datetime.min)
        return commits

    # ------------------------------------------------------------------
    # Signal: Gap detection
    # ------------------------------------------------------------------

    def _detect_gaps(self, commits: list[dict]) -> list[dict]:
        """Find positions where the gap between consecutive commits exceeds
        min_gap_days."""
        if len(commits) < 2:
            return []

        signals = []
        for i in range(1, len(commits)):
            prev_date = _parse_date(commits[i - 1]["date"])
            curr_date = _parse_date(commits[i]["date"])
            if prev_date is None or curr_date is None:
                continue
            gap = (curr_date - prev_date).days
            if gap >= self.min_gap_days:
                signals.append(
                    {
                        "index": i,
                        "date": commits[i]["date"][:10] if len(commits[i]["date"]) >= 10 else "",
                        "type": "gap",
                        "detail": f"{gap}-day gap since previous commit",
                        "strength": "strong" if gap >= 7 else "moderate",
                    }
                )
        return signals

    # ------------------------------------------------------------------
    # Signal: Velocity shift
    # ------------------------------------------------------------------

    def _detect_velocity_shifts(self, commits: list[dict]) -> list[dict]:
        """Find day-level velocity shifts between clusters.

        Compares average daily commit rate in consecutive active-day windows.
        Only flags shifts between days, not within them.
        """
        if len(commits) < 10:
            return []

        # Build day-level data
        day_counts: dict[str, int] = {}
        day_of: list[str] = []
        for c in commits:
            dt = _parse_date(c["date"])
            day = dt.date().isoformat() if dt else (c["date"][:10] if len(c["date"]) >= 10 else "")
            day_of.append(day)
            if day:
                day_counts[day] = day_counts.get(day, 0) + 1

        days_sorted = sorted(day_counts.keys())
        if len(days_sorted) < 3:
            return []

        # Compare consecutive day windows (3-day rolling average)
        window = 3
        signals = []
        flagged_days = set()

        for i in range(window, len(days_sorted)):
            before_avg = sum(day_counts.get(d, 0) for d in days_sorted[i - window : i]) / window
            after_avg = sum(day_counts.get(d, 0) for d in days_sorted[i : i + window]) / max(
                1, min(window, len(days_sorted) - i)
            )

            if before_avg == 0:
                continue

            ratio = after_avg / before_avg
            day = days_sorted[i]

            if (
                ratio >= self.velocity_shift_factor or ratio <= 1.0 / self.velocity_shift_factor
            ) and day not in flagged_days:
                flagged_days.add(day)
                direction = "up" if ratio > 1 else "down"
                display_ratio = max(ratio, 1.0 / ratio)
                # Find the commit index for this day
                idx = next(
                    (j for j, d in enumerate(day_of) if d == day),
                    0,
                )
                signals.append(
                    {
                        "index": idx,
                        "date": day,
                        "type": "velocity",
                        "detail": f"{display_ratio:.1f}x {direction}shift ({before_avg:.0f}→{after_avg:.0f} commits/day avg)",
                        "strength": "strong" if display_ratio >= 4.0 else "moderate",
                    }
                )

        return self._fold_velocity(signals)

    @staticmethod
    def _fold_velocity(signals: list[dict], within_days: int = 7) -> list[dict]:
        """Keep one signal per run: the rolling window flags several days in a
        row for the same shift. Same-direction flags within ``within_days`` of
        the previous one fold into it; the stronger detail wins."""

        def ratio(sg: dict) -> float:
            try:
                return float(sg["detail"].split("x", 1)[0])
            except (ValueError, KeyError):
                return 0.0

        out: list[dict] = []
        last_day: dict[str, datetime] = {}
        for sg in signals:
            direction = "up" if "upshift" in sg["detail"] else "down"
            day = _parse_date(sg["date"])
            prev = out[-1] if out else None
            if (
                prev is not None
                and day is not None
                and direction in last_day
                and ("upshift" in prev["detail"]) == (direction == "up")
                and (day - last_day[direction]).days <= within_days
            ):
                if ratio(sg) > ratio(prev):
                    prev.update(detail=sg["detail"], strength=sg["strength"])
                prev["end"] = sg["date"]
                last_day[direction] = day
                continue
            out.append(sg)
            if day is not None:
                last_day[direction] = day
        return out

    # ------------------------------------------------------------------
    # Signal: Author change
    # ------------------------------------------------------------------

    def _detect_author_changes(self, commits: list[dict]) -> list[dict]:
        """Report sustained changes in the daily primary author or author class."""
        days = max(0, int(self.config.get("author_debounce_days", 7)))
        commit_limit = self.config.get("author_debounce_commits")
        commit_limit = max(1, int(commit_limit)) if commit_limit is not None else None
        current = None
        candidate = None
        candidate_start = None
        candidate_index = 0
        candidate_count = 0
        signals = []

        def day_of(item):
            parsed = _parse_date(item[1]["date"])
            return parsed.date() if parsed else None

        for day, group in groupby(enumerate(commits), key=day_of):
            if day is None:
                continue
            counts = Counter()
            first_indices = {}
            for index, commit in group:
                if not commit["author"]:
                    continue
                identity = (commit["author"], commit.get("author_class", "human"))
                counts[identity] += 1
                first_indices.setdefault(identity, index)
            if not counts:
                continue
            highest = max(counts.values())
            tied = [identity for identity, count in counts.items() if count == highest]
            primary = current if current in tied else candidate if candidate in tied else tied[0]
            if current is None:
                current = primary
                continue
            if primary == current:
                candidate = None
                continue
            if primary != candidate:
                candidate = primary
                candidate_start = day
                candidate_index = first_indices[primary]
                candidate_count = 0
            candidate_count += counts[primary]
            if (day - candidate_start).days < days and (
                commit_limit is None or candidate_count < commit_limit
            ):
                continue
            before, before_class = current
            after, after_class = candidate
            if before_class != after_class:
                signal = {
                    "type": "author_class",
                    "detail": f"author class shifts from {before_class} to {after_class} ({before} to {after})",
                    "from_class": before_class,
                    "to_class": after_class,
                    "strength": "strong",
                }
            else:
                signal = {
                    "type": "author",
                    "detail": f"primary author shifts from {before} to {after}",
                    "strength": "moderate",
                }
            signals.append(
                {"index": candidate_index, "date": candidate_start.isoformat(), **signal}
            )
            current = candidate
            candidate = None
        return signals

    # ------------------------------------------------------------------
    # Signal: Scope change keywords
    # ------------------------------------------------------------------

    def _detect_scope_changes(self, commits: list[dict]) -> list[dict]:
        """Find concentrated bursts of scope-change keywords.

        Only flags positions where 3+ scope-change commits cluster within
        a 20-commit window. Isolated refactors don't indicate an era boundary.
        """
        if not self.scope_change_keywords or len(commits) < 10:
            return []

        window = 20
        threshold = 3

        # Pre-compute which commits match
        matches = []
        for i, commit in enumerate(commits):
            message_lower = commit["message"].lower()
            matched = [kw for kw in self.scope_change_keywords if kw in message_lower]
            matches.append(matched)

        # Find windows with concentrated scope changes
        signals = []
        flagged = set()
        for i in range(len(commits) - window + 1):
            count = sum(1 for j in range(i, i + window) if matches[j])
            if count >= threshold:
                # Flag the center of the burst
                center = i + window // 2
                keywords = set()
                for j in range(i, i + window):
                    if matches[j]:
                        keywords.update(matches[j])

                # Round to nearest day boundary to avoid flagging every commit
                day = commits[center]["date"][:10] if len(commits[center]["date"]) >= 10 else ""
                if day not in flagged:
                    flagged.add(day)
                    signals.append(
                        {
                            "index": center,
                            "date": day,
                            "type": "scope",
                            "detail": f"concentrated {', '.join(sorted(keywords))} burst ({count} in {window} commits)",
                            "strength": "strong" if count >= 6 else "moderate",
                        }
                    )

        return signals

    # ------------------------------------------------------------------
    # Signal: Cross-repo activation
    # ------------------------------------------------------------------

    def _detect_cross_repo(self, commits: list[dict]) -> list[dict]:
        """Find positions where new repos accumulate significant commits."""
        if not commits:
            return []

        seen_repos: dict[str, int] = {}
        signals = []

        for i, commit in enumerate(commits):
            repo = commit["repo"]
            if not repo:
                continue

            if repo not in seen_repos:
                seen_repos[repo] = 0
            seen_repos[repo] += 1

            if seen_repos[repo] == self.cross_repo_threshold and i > 0:
                first_appearance = next(
                    (j for j in range(i, -1, -1) if commits[j]["repo"] == repo),
                    i,
                )
                if first_appearance > 0:
                    signals.append(
                        {
                            "index": first_appearance,
                            "date": commits[first_appearance]["date"][:10]
                            if len(commits[first_appearance]["date"]) >= 10
                            else "",
                            "type": "repo_activation",
                            "detail": f"repo {repo} reaches {self.cross_repo_threshold} commits",
                            "strength": "moderate",
                        }
                    )

        return signals

    # ------------------------------------------------------------------
    # Cluster summary (gap-based natural groupings)
    # ------------------------------------------------------------------

    def _build_clusters(self, commits: list[dict]) -> list[dict]:
        """Group commits into natural clusters separated by multi-day gaps.

        These are factual groupings, not narrative eras. They provide the
        raw material for era definition.
        """
        if not commits:
            return []

        # Find day-level gaps
        day_commits: dict[str, list[int]] = {}
        for i, c in enumerate(commits):
            day = c["date"][:10] if len(c["date"]) >= 10 else ""
            if day:
                day_commits.setdefault(day, []).append(i)

        days_sorted = sorted(day_commits.keys())

        if not days_sorted:
            return []

        # Split into clusters at gaps >= min_gap_days
        clusters = []
        cluster_days = [days_sorted[0]]

        for i in range(1, len(days_sorted)):
            prev = datetime.strptime(days_sorted[i - 1], "%Y-%m-%d")
            curr = datetime.strptime(days_sorted[i], "%Y-%m-%d")
            gap = (curr - prev).days

            if gap >= self.min_gap_days:
                # Close current cluster
                clusters.append(self._summarize_cluster(commits, cluster_days, day_commits))
                cluster_days = [days_sorted[i]]
            else:
                cluster_days.append(days_sorted[i])

        # Close last cluster
        if cluster_days:
            clusters.append(self._summarize_cluster(commits, cluster_days, day_commits))

        return clusters

    def _summarize_cluster(
        self,
        commits: list[dict],
        days: list[str],
        day_commits: dict[str, list[int]],
    ) -> dict:
        """Build a summary dict for a cluster of active days."""
        indices = []
        for d in days:
            indices.extend(day_commits.get(d, []))
        indices.sort()

        cluster_commits = [commits[i] for i in indices]
        authors = Counter(c["author"] for c in cluster_commits if c["author"])
        repos = Counter(c["repo"] for c in cluster_commits if c["repo"])

        return {
            "start_date": days[0],
            "end_date": days[-1],
            "active_days": len(days),
            "commit_count": len(cluster_commits),
            "primary_author": authors.most_common(1)[0][0] if authors else "",
            "dominant_repo": repos.most_common(1)[0][0] if repos else "",
            "daily_breakdown": {d: len(day_commits.get(d, [])) for d in days},
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _deduplicate(boundaries: list[tuple], min_gap: int = 5) -> list[tuple]:
        """Remove boundary entries whose indices are too close together."""
        if not boundaries:
            return []

        sorted_bounds = sorted(boundaries, key=lambda b: b[0])
        deduped = [sorted_bounds[0]]
        for b in sorted_bounds[1:]:
            if b[0] - deduped[-1][0] >= min_gap:
                deduped.append(b)
        return deduped


# ----------------------------------------------------------------------
# Convenience function (called by CLI)
# ----------------------------------------------------------------------


def detect_signals(project_name: str, config: dict | None = None) -> dict:
    """Detect signals for a project by name.

    Resolves the project database at projects/<name>/data/archaeology.db,
    runs detection, and saves results to projects/<name>/data/detected-signals.json.

    Args:
        project_name: Name of the project (directory under projects/).
        config: Optional config overrides.

    Returns:
        Signal detection result dict.
    """
    repo_root = Path.cwd()
    project_dir = repo_root / "projects" / project_name
    db_path = project_dir / "data" / "archaeology.db"

    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}", flush=True)
        return {"signals": [], "cluster_summary": []}

    detector = SignalDetector(db_path, config=config)
    result = detector.detect()

    output_path = project_dir / "data" / "detected-signals.json"
    detector.save(output_path, result)

    signals = result.get("signals", [])
    clusters = result.get("cluster_summary", [])
    print(f"Detected {len(signals)} signals, {len(clusters)} clusters")
    for sig_type in sorted(set(s["type"] for s in signals)):
        count = sum(1 for s in signals if s["type"] == sig_type)
        print(f"  {sig_type}: {count} signals")
    for cluster in clusters:
        print(
            f"  Cluster: {cluster['start_date']} -> {cluster['end_date']} "
            f"({cluster['commit_count']} commits, {cluster['active_days']} days)"
        )
    print(f"Saved to {output_path}")

    return result


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------


def main() -> None:
    """CLI entry point for standalone signal detection.

    Usage:
        python -m archaeology.classifiers.era_detector --project <name>
        python -m archaeology.classifiers.era_detector --db <path> [--output <path>]
    """
    parser = argparse.ArgumentParser(description="Detect development signals from commit history")
    parser.add_argument(
        "--project",
        help="Project name (resolves to projects/<name>/data/archaeology.db)",
    )
    parser.add_argument(
        "--db",
        help="Direct path to the SQLite database",
    )
    parser.add_argument(
        "--output",
        help="Output path for detected-signals.json",
    )
    parser.add_argument(
        "--config",
        help="Path to a JSON config file with overrides",
    )
    parser.add_argument(
        "--min-gap-days",
        type=int,
        help="Minimum gap in days to flag as a signal",
    )
    args = parser.parse_args()

    config_overrides: dict = {}
    if args.config:
        config_path = Path(args.config)
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                config_overrides = json.load(f)

    if args.min_gap_days is not None:
        config_overrides["min_gap_days"] = args.min_gap_days

    if args.project:
        detect_signals(args.project, config=config_overrides or None)
        return

    if not args.db:
        parser.error("Either --project or --db is required")

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}", flush=True)
        return

    detector = SignalDetector(db_path, config=config_overrides or None)
    result = detector.detect()

    output_path = Path(args.output) if args.output else db_path.parent / "detected-signals.json"
    detector.save(output_path, result)

    signals = result.get("signals", [])
    print(f"Detected {len(signals)} signals from {db_path}")
    for sig in signals:
        print(f"  [{sig['type']}] {sig['date']}: {sig['detail']}")


if __name__ == "__main__":
    main()
