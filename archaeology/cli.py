"""DevArch Framework CLI."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import click
from click.core import ParameterSource

from .analysis_runner import (
    failed_vectors,
    run_analysis_vectors,
)
from .classifiers.era_detector import detect_signals
from .extractors.git import extract_git_log, extract_git_log_with_stats, repository_coverage


def _run_child(cmd, **kwargs):
    try:
        result = subprocess.run(cmd, check=True, stderr=subprocess.PIPE, text=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.output or str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        detail = str(detail).strip()
        if detail.startswith("Error: "):
            detail = detail[7:]
        if "Traceback (most recent call last):" in detail:
            detail = detail.splitlines()[-1]
        raise click.ClickException(detail) from exc
    except subprocess.TimeoutExpired as exc:
        raise click.ClickException(
            f"{Path(str(cmd[0])).name} timed out after {exc.timeout:g}s"
        ) from exc
    if result.stderr:
        click.echo(result.stderr, err=True, nl=False)
    return result


def _check_name(project_name):
    """Reject unsafe project names before any command touches the filesystem."""
    from .pipeline import validate_project_name

    try:
        validate_project_name(project_name)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def _project_dir(project_name):
    """Resolve project directory path."""
    _check_name(project_name)
    d = os.path.join("projects", project_name)
    if not os.path.isdir(d):
        click.echo(f"Project '{project_name}' not found at {d}/", err=True)
        click.echo(f"Run 'devarch init {project_name}' first.", err=True)
        sys.exit(1)
    return d


def _viz_template_path(filename):
    """Resolve a visualization template against the installed package.

    Templates ship as package data, so resolving them against the CWD only
    works when the CLI happens to run from the repository source root.
    """
    return Path(__file__).parent / "visualization" / filename


@click.group()
@click.version_option(package_name="devarch-framework")
def main():
    """DevArch Framework - forensic mining of software development history."""
    pass


@main.command()
@click.argument("project_name")
@click.option(
    "--description", default="Draft archaeology project", help="Human-readable project description"
)
@click.option(
    "--repo-url", default="https://github.com/example/example", help="GitHub repository URL"
)
def init(project_name, description, repo_url):
    """Create a new project directory with default config."""
    _check_name(project_name)
    project_dir = os.path.join("projects", project_name)
    os.makedirs(os.path.join(project_dir, "data"), exist_ok=True)
    os.makedirs(os.path.join(project_dir, "deliverables"), exist_ok=True)

    config_path = os.path.join(project_dir, "project.json")
    if os.path.exists(config_path):
        click.echo(f"Project '{project_name}' already exists at {config_path}")
        return

    config = {
        "name": project_name,
        "description": description,
        "repo_url": repo_url,
        "developer": {},
        "timeline": {},
        "overrides": {},
        "visualization": {},
        "data_sources": {},
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    click.echo(f"Created project '{project_name}' at {project_dir}/")


@main.command()
@click.option(
    "--project", "project_name", default="demo-archaeology", help="Demo project name to create"
)
@click.option("--force", is_flag=True, help="Overwrite an existing demo project")
@click.option(
    "--build-db", is_flag=True, help="Build the demo SQLite database after creating files"
)
def demo(project_name, force, build_db):
    """Create a sanitized demo archaeology project."""
    _check_name(project_name)
    from .demo import create_demo_project

    try:
        project_root = create_demo_project(Path.cwd(), project_name=project_name, force=force)
    except FileExistsError as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(1)
    click.echo(f"Created sanitized demo project at {project_root}")
    click.echo(f"Try: devarch build-db {project_name}")
    click.echo(f"Then: devarch audit {project_name} --fail-on HIGH")
    if build_db:
        cmd = [sys.executable, "-m", "archaeology.db.builder", "--project-root", str(project_root)]
        _env = os.environ.copy()
        _pkg_root = str(Path(__file__).parent.parent)
        _env["PYTHONPATH"] = _pkg_root + (
            (os.pathsep + _env["PYTHONPATH"]) if _env.get("PYTHONPATH") else ""
        )
        result = _run_child(cmd, timeout=300, env=_env)
        if result.returncode != 0:
            raise click.exceptions.Exit(result.returncode)


@main.command()
@click.argument("repo_path")
@click.option("--project", "-p", required=True, help="Project name to extract into")
@click.option("--verbose", "-v", is_flag=True)
def mine(repo_path, project, verbose):
    """Phase 1: Extract data from a git repository."""
    _check_name(project)

    project_dir = _project_dir(project)
    data_dir = os.path.join(project_dir, "data")

    if not os.path.isdir(os.path.expanduser(repo_path)):
        click.echo(f"Repository not found: {repo_path}", err=True)
        sys.exit(1)

    from .pipeline import PipelineError, require_git_repo

    try:
        require_git_repo(repo_path)
    except (PipelineError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    repo_path = str(Path(repo_path).expanduser().resolve())
    try:
        coverage = repository_coverage(repo_path)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if coverage["shallow"]:
        raise click.ClickException(
            "Shallow history: fetch complete history before mining; no fetch performed"
        )

    click.echo(f"Extracting git log from {repo_path}...")

    csv_path = os.path.join(data_dir, "github-commits.csv")
    try:
        count = extract_git_log(repo_path, csv_path, verbose=verbose)
        click.echo(f"  Extracted {count} commits to {csv_path}")
    except (RuntimeError, Exception) as e:
        click.echo(f"Error: Git extraction failed: {e}", err=True)
        sys.exit(1)

    stats_path = os.path.join(data_dir, "github-commits-with-stats.txt")
    try:
        extract_git_log_with_stats(repo_path, stats_path, verbose=verbose)
    except (RuntimeError, Exception) as e:
        click.echo(f"Error: Git stats extraction failed: {e}", err=True)
        sys.exit(1)

    if repository_coverage(repo_path) != coverage:
        raise click.ClickException("Repository refs changed during mining; retry before analysis")
    import hashlib

    from .utils import atomic_write

    coverage["artifact_sha256"] = {
        Path(path).name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in (csv_path, stats_path)
    }
    atomic_write(Path(data_dir) / "coverage.json", json.dumps(coverage, indent=2))
    config_path = Path(project_dir) / "project.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repo_path"] = repo_path
    config["mined_history_manifest_required"] = True
    atomic_write(config_path, json.dumps(config, indent=2))

    click.echo(f"Phase 1 complete for '{project}'.")


@main.command()
@click.argument("project_name")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def build_db(project_name, verbose):
    """Phase 1.5: Build SQLite database from extracted data."""
    project_dir = _project_dir(project_name)
    db_path = os.path.join(project_dir, "data", "archaeology.db")

    cmd = [sys.executable, "-m", "archaeology.db.builder", "--project-root", project_dir]
    if verbose:
        cmd.append("--verbose")

    _env = os.environ.copy()
    _pkg_root = str(Path(__file__).parent.parent)
    _env["PYTHONPATH"] = _pkg_root + (
        (os.pathsep + _env["PYTHONPATH"]) if _env.get("PYTHONPATH") else ""
    )
    result = _run_child(cmd, timeout=300, env=_env)
    if result.returncode == 0 and os.path.exists(db_path):
        click.echo(f"Database built at {db_path}")
    else:
        click.echo(f"Build failed with exit code {result.returncode}", err=True)
        sys.exit(result.returncode)


@main.command()
@click.argument("project_name")
@click.option("--port", default=8001, help="Port for Datasette server")
@click.option(
    "--unsafe-cors",
    is_flag=True,
    help="Enable Datasette CORS headers. Off by default for local data safety.",
)
def serve(project_name, port, unsafe_cors):
    """Launch Datasette for a project."""
    project_dir = _project_dir(project_name)
    db_path = os.path.join(project_dir, "data", "archaeology.db")

    if not os.path.exists(db_path):
        click.echo(f"Database not found at {db_path}. Run 'devarch build-db {project_name}' first.")
        sys.exit(1)

    # Load project config for display name
    project_config = {}
    config_path = os.path.join(project_dir, "project.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                project_config = json.load(f)
        except json.JSONDecodeError as e:
            click.echo(f"Error: Invalid JSON in {config_path}: {e}", err=True)
            sys.exit(1)

    display_name = project_config.get("visualization", {}).get("title", project_name.upper())

    # Use project-specific metadata if it exists, otherwise default
    project_metadata = os.path.join(project_dir, "datasette-metadata.yaml")
    default_metadata = os.path.join("config", "datasette-metadata.yaml")

    metadata_src = None
    if os.path.exists(project_metadata):
        metadata_src = project_metadata
    elif os.path.exists(default_metadata):
        # Create a temp metadata with project name injected
        with open(default_metadata, encoding="utf-8") as f:
            content = f.read()
        content = content.replace(
            'title: "Archaeology Database"',
            f'title: "{display_name} Archaeology Database"',
        )
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, prefix="arch-metadata-"
        )
        tmp.write(content)
        tmp.close()
        metadata_src = tmp.name

    # datasette's --setting takes "NAME VALUE" as two arguments; the
    # comma-joined single-argument form is rejected by every datasette in the
    # supported range (>=0.64.0), which left `serve` unable to start.
    cmd = [
        "datasette",
        db_path,
        "--port",
        str(port),
        "--setting",
        "sql_time_limit_ms",
        "5000",
    ]
    if unsafe_cors:
        cmd.append("--cors")
    if metadata_src:
        cmd.extend(["--metadata", metadata_src])

    click.echo(f"Launching Datasette at http://localhost:{port}")
    try:
        subprocess.run(cmd, check=True, timeout=300)
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            f"Datasette exited with code {exc.returncode}; nothing is serving on port {port}. "
            "If the port is already in use, pass a different --port."
        ) from exc
    finally:
        if metadata_src and metadata_src.startswith(tempfile.gettempdir()):
            os.unlink(metadata_src)


@main.command()
@click.argument("project_name")
@click.option("--config", "config_path", help="Path to custom config JSON")
@click.option("--min-gap-days", type=int, help="Override min gap days for signal detection")
@click.option("--verbose", "-v", is_flag=True)
def signals(project_name, config_path, min_gap_days, verbose):
    """Phase 2: Detect development signals (gaps, velocity shifts, etc).

    Signals are patterns in the data, NOT era definitions. Eras are
    hand-curated narrative judgments based on these signals.
    """

    config = {}
    if config_path:
        try:
            with open(config_path) as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            click.echo(f"Error: Invalid JSON in {config_path}: {e}", err=True)
            sys.exit(1)
    if min_gap_days is not None:
        config["min_gap_days"] = min_gap_days

    db_path = os.path.join(_project_dir(project_name), "data", "archaeology.db")
    if not os.path.exists(db_path):
        click.echo(f"No database found. Run 'devarch build-db {project_name}' first.", err=True)
        sys.exit(1)

    result = detect_signals(project_name, config=config or None)
    if result.get("signals"):
        click.echo(
            f"Detected {len(result['signals'])} signals "
            f"across {len(result['cluster_summary'])} clusters."
        )
    else:
        click.echo("No significant patterns detected in the commit history.")


@main.command()
@click.argument("project_name")
@click.option("--sessions-dir", help="Directory containing .jsonl session files")
@click.option("--project-slug", help="Only scan this Claude Code project folder")
@click.option("--include-subagents", is_flag=True, help="Include subagent sessions")
@click.option("--all-projects", is_flag=True, help="Scan all Claude Code projects")
@click.option("--verbose", "-v", is_flag=True)
def extract_sessions(
    project_name, sessions_dir, project_slug, include_subagents, all_projects, verbose
):
    """Extract Claude Code session data."""
    project_dir = _project_dir(project_name)
    output_path = os.path.join(project_dir, "data", "raw-sessions.md")

    cmd = [sys.executable, "-m", "archaeology.extractors.sessions", f"--output={output_path}"]
    if sessions_dir:
        cmd.append(f"--sessions-dir={sessions_dir}")
    if project_slug:
        cmd.append(f"--project-slug={project_slug}")
    if include_subagents:
        cmd.append("--include-subagents")
    if all_projects:
        cmd.append("--all-projects")
    cmd.append(f"--project={project_name}")

    result = _run_child(cmd)
    if result.returncode == 0:
        click.echo(f"Sessions extracted to {output_path}")
    else:
        click.echo("Session extraction failed", err=True)
        sys.exit(result.returncode)


@main.command()
@click.argument("project_name")
@click.option(
    "--vector",
    "-v",
    "vectors",
    multiple=True,
    help="Run specific analysis vector(s). Repeat for multiple.",
)
@click.option(
    "--prompts",
    is_flag=True,
    help="Show legacy prompt-template instructions instead of running automation",
)
@click.option("--verbose", is_flag=True, help="Print vector execution detail")
def analyze(project_name, vectors, prompts, verbose):
    """Phase 3: Run automated analysis vectors against project data."""
    from .analysis_runner import AnalysisRunner

    available = AnalysisRunner.VECTORS
    unknown = set(vectors) - set(available) if vectors else set()
    if unknown:
        for vector in sorted(unknown):
            click.echo(f"Unknown vector: {vector}", err=True)
        raise click.exceptions.Exit(1)

    target = list(vectors) if vectors else list(available)
    project_dir = _project_dir(project_name)
    deliverables_dir = os.path.join(project_dir, "deliverables")
    analysis_dir = os.path.join(deliverables_dir, "analysis")
    os.makedirs(analysis_dir, exist_ok=True)

    if prompts:
        vectors_dir = os.path.join(os.path.dirname(__file__), "..", "analysis-vectors")
        click.echo(f"Analysis prompt templates for '{project_name}'")
        for vec_name in target:
            prompt_path = os.path.join(vectors_dir, f"{vec_name}.md")
            output_path = os.path.join(analysis_dir, f"analysis-{vec_name}.md")
            click.echo(f"  [{vec_name}] prompt={prompt_path} output={output_path}")
        return

    click.echo(f"Running automated analysis vectors for '{project_name}'")
    click.echo(f"Vectors: {', '.join(target)}")
    results = run_analysis_vectors(project_name, verbose=verbose, vectors=target)
    failed = failed_vectors(results)
    for name, path in results.items():
        click.echo(f"  {name}: {path}")
    if failed:
        raise click.exceptions.Exit(1)


@main.command()
@click.argument("project_name")
@click.option(
    "--analyzers", "-a", multiple=True, help="Specific analyzer(s) to run (default: all 14)"
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def opportunity(project_name, analyzers, verbose):
    """Disabled pending evidence and data-exposure hardening."""
    raise click.ClickException(
        "opportunity is disabled pending readiness hardening. Use init, mine, build-db, signals, analyze, visualize, export-report and audit."
    )


@main.command("public-case-study")
@click.option(
    "--output",
    "output_dir",
    default="public-case-study",
    help="Output directory for the sanitized public case study",
)
@click.option(
    "--project",
    "project_name",
    default="demo-archaeology",
    help="Temporary/generated sanitized demo project name",
)
@click.option(
    "--force", is_flag=True, default=True, help="Overwrite existing generated demo project"
)
def public_case_study(output_dir, project_name, force):
    """Generate a sanitized public case-study showroom."""
    _check_name(project_name)
    from .report import export_public_case_study

    path = export_public_case_study(
        Path.cwd(), output_dir=output_dir, project_name=project_name, force=force
    )
    click.echo(f"Public case study exported to {path}")


@main.command("local-pipeline")
@click.option(
    "--repo",
    "repo_name",
    default="dev-archaeology",
    help="Repository name or owner/name to inspect",
)
@click.option(
    "--pipeline-dir",
    default=None,
    help="Path to the local GITHUB_pipeline workspace (defaults to ARCHAEOLOGY_PIPELINE_ROOT)",
)
@click.option(
    "--repos-dir",
    default=None,
    help="Directory containing local repositories (defaults to ARCHAEOLOGY_REPOS_DIR)",
)
@click.option(
    "--top-repos", default=20, type=int, help="Number of active repos to review when --run is used"
)
@click.option(
    "--review-days", default=30, type=int, help="Commit lookback window when --run is used"
)
@click.option(
    "--run", "run_first", is_flag=True, help="Run the local pipeline before reading latest.json"
)
@click.option(
    "--fail-on-issues",
    is_flag=True,
    help="Exit nonzero if the repo has any local-pipeline findings",
)
def local_pipeline(
    repo_name, pipeline_dir, repos_dir, top_repos, review_days, run_first, fail_on_issues
):
    """Read or run the local GITHUB_pipeline verification status."""
    from .local_pipeline import read_local_pipeline_status, run_local_pipeline, status_lines

    # Resolve pipeline_dir if not provided
    if pipeline_dir is None:
        pipeline_dir = os.environ.get("ARCHAEOLOGY_PIPELINE_ROOT", "")
        if not pipeline_dir:
            raise click.UsageError(
                "ARCHAEOLOGY_PIPELINE_ROOT environment variable not set. "
                "Please set it to your local GITHUB_pipeline workspace path, "
                "or use --pipeline-dir option."
            )

    if run_first:
        # repos_dir only needed for running the pipeline
        if repos_dir is None:
            repos_dir = os.environ.get("ARCHAEOLOGY_REPOS_DIR", "")
            if not repos_dir:
                raise click.UsageError(
                    "ARCHAEOLOGY_REPOS_DIR environment variable not set. "
                    "Please set it to the directory containing your local repositories, "
                    "or use --repos-dir option."
                )
        run_local_pipeline(
            pipeline_dir=pipeline_dir,
            repos_dir=repos_dir,
            top_repos=top_repos,
            review_days=review_days,
        )
    try:
        status = read_local_pipeline_status(pipeline_dir, repo_name)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    for line in status_lines(status):
        click.echo(line)
    if fail_on_issues and status.issue_total > 0:
        raise click.exceptions.Exit(1)


@main.command("export-report")
@click.argument("project_name")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["markdown", "md", "html"]),
    default="markdown",
    help="Report format to export",
)
@click.option(
    "--output",
    "output_path",
    help="Output path. Defaults to project deliverables/ARCHAEOLOGY-REPORT.<ext>",
)
def export_report_cmd(project_name, fmt, output_path):
    """Export an archaeology report from analysis outputs."""
    from .report import export_report

    project_dir = _project_dir(project_name)
    try:
        path = export_report(project_name, project_dir, output_path=output_path, fmt=fmt)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Report exported to {path}")


@main.command()
@click.argument("project_name")
def visualize(project_name):
    """Phase 4: Generate visualization HTML from template."""
    from .visualization.history import render_history

    try:
        output = render_history(Path(_project_dir(project_name)))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Visualization generated at {output}")


@main.command()
@click.argument("project_name")
@click.option(
    "--logs-dir",
    help="Path to pipeline logs directory (default: <ARCHAEOLOGY_PIPELINE_ROOT>/.omc/logs/repo-pipeline)",
)
@click.option("--verbose", "-v", is_flag=True)
def ingest_pipeline(project_name, logs_dir, verbose):
    """Ingest GITHUB_pipeline run logs into archaeology database.

    Imports pipeline JSON logs from .omc/logs/repo-pipeline/ into
    pipeline_runs and pipeline_repo_results tables for historical tracking.
    """
    from .db.pipeline_ingest import ingest_directory

    project_dir = _project_dir(project_name)
    db_path = os.path.join(project_dir, "data", "archaeology.db")

    if not os.path.exists(db_path):
        click.echo(f"Database not found. Run 'devarch build-db {project_name}' first.", err=True)
        sys.exit(1)

    # No guessing under the home folder: the logs come from --logs-dir or the pipeline root.
    if not logs_dir:
        pipeline_root = os.environ.get("ARCHAEOLOGY_PIPELINE_ROOT", "")
        if pipeline_root:
            logs_dir = os.path.join(pipeline_root, ".omc", "logs", "repo-pipeline")

    if not logs_dir or not os.path.isdir(logs_dir):
        click.echo(
            "Pipeline logs directory not found. Pass --logs-dir, or set "
            "ARCHAEOLOGY_PIPELINE_ROOT to your GITHUB_pipeline workspace.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"Ingesting pipeline logs from {logs_dir}...")
    stats = ingest_directory(Path(db_path), Path(logs_dir), verbose=verbose)
    click.echo(
        f"  Ingested: {stats['ingested']}, Skipped: {stats['skipped']}, Errors: {len(stats['errors'])}"
    )
    for err in stats["errors"]:
        click.echo(f"  ERROR: {err}", err=True)


@main.command()
@click.argument("project_name")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing")
@click.option("--skip-mine", is_flag=True, help="Skip git mining (use existing data)")
def cascade(project_name, dry_run, skip_mine):
    """Disabled pending evidence and data-exposure hardening."""
    raise click.ClickException(
        "cascade is disabled pending readiness hardening. Use init, mine, build-db, signals, analyze, visualize, export-report and audit."
    )


@main.command()
@click.argument("project_name")
@click.option(
    "--fail-on",
    type=click.Choice(["CRITICAL", "HIGH", "MEDIUM", "LOW"]),
    default="HIGH",
    help="Lowest severity that causes nonzero exit",
)
def audit(project_name, fail_on):
    """Run forensic audit quality gate."""
    _check_name(project_name)
    from .audit import has_blocking_findings, run_audit, summarize

    findings = run_audit(project_name, root=Path.cwd())
    summary = summarize(findings)
    click.echo(f"Forensic audit for '{project_name}'")
    click.echo("Summary: " + ", ".join(f"{k}={v}" for k, v in summary.items() if v))
    if not findings:
        click.echo("PASS: no findings")
        return
    for finding in findings:
        click.echo(finding.format())
    if has_blocking_findings(findings, fail_on=fail_on):
        raise click.exceptions.Exit(1)


@main.command()
@click.argument("project_name")
def validate(project_name):
    """Validate the generated history HTML and its evidence binding."""
    from .validators.history import validate_history

    try:
        validate_history(Path(_project_dir(project_name)))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("PASS: generated history HTML and evidence binding")


@main.command()
@click.option(
    "--project",
    "-p",
    "projects",
    multiple=True,
    help="Sync specific project(s) only. Defaults to all in profile.json.",
)
@click.option("--skip-mine", is_flag=True, help="Skip git extraction (use cached data)")
@click.option("--skip-signals", is_flag=True, help="Skip signal detection")
@click.option("--verbose", "-v", is_flag=True)
def sync(projects, skip_mine, skip_signals, verbose):
    """Sync registered projects and aggregate into global narrative."""
    from .pipeline import sync as sync_projects

    outcome = sync_projects(
        projects,
        skip_mine=skip_mine,
        skip_signals=skip_signals,
        verbose=verbose,
        on_line=lambda text, err=False: click.echo(text, err=err),
    )
    if outcome.exit_code:
        raise click.exceptions.Exit(outcome.exit_code)


@main.command("global-viz")
@click.option(
    "--output",
    "output_dir",
    default="global/deliverables",
    help="Output directory for the global visualization",
)
@click.option("--top", "top_n", type=int, help="Limit to top N repos by commit count")
@click.option("--year", type=int, help="Only include repos updated in this year")
@click.option("--verbose", "-v", is_flag=True)
def global_viz(output_dir, top_n, year, verbose):
    """Generate cross-repo visualization from synced global data."""
    from .visualization.global_data_builder import prepare_global_visualization_data

    global_dir = "global"
    data_dir = os.path.join(global_dir, "data")
    github_json = os.path.join(data_dir, "github-repos.json")
    commits_csv = os.path.join(data_dir, "global-commits.csv")

    if not os.path.exists(commits_csv) and not os.path.exists(github_json):
        click.echo(
            "No global data found. Run 'devarch fetch-github <owner>' or 'devarch sync' first.",
            err=True,
        )
        sys.exit(1)

    # Build visualization data
    click.echo("Building global visualization data...")
    viz_data = prepare_global_visualization_data(global_dir, top_n=top_n, year=year)

    # Write viz data JSON
    viz_json_path = os.path.join(data_dir, "global-viz-data.json")
    with open(viz_json_path, "w") as f:
        json.dump(viz_data, f, indent=2)
    click.echo(f"  Data written to {viz_json_path}")

    # Hydrate template
    template_path = _viz_template_path("global-template.html")
    if not os.path.exists(template_path):
        click.echo(f"Template not found at {template_path}", err=True)
        sys.exit(1)

    with open(template_path, encoding="utf-8") as f:
        html = f.read()

    # Inline the data JSON
    safe_data = (
        json.dumps(viz_data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )

    # Replace the placeholder lines inside the script block
    # Template has: "// GLOBAL_DATA_PLACEHOLDER\nwindow.GLOBAL_DATA = {};"
    old_placeholder = "// GLOBAL_DATA_PLACEHOLDER\nwindow.GLOBAL_DATA = {};"
    new_inline = f"window.GLOBAL_DATA = {safe_data};\n  window.dispatchEvent(new Event('global-data-loaded'));"
    if old_placeholder in html:
        html = html.replace(old_placeholder, new_inline)
    elif "window.GLOBAL_DATA = {};" in html:
        html = html.replace("window.GLOBAL_DATA = {};", f"window.GLOBAL_DATA = {safe_data};")
    else:
        click.echo("Warning: could not find GLOBAL_DATA placeholder in template", err=True)

    # Write output
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "global.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    click.echo(f"Global visualization generated at {output_path}")
    meta = viz_data.get("meta", {})
    click.echo(
        f"  {meta.get('total_commits', '?')} commits across {meta.get('total_repos', '?')} repos"
    )


@main.command("multi-project-dashboard")
@click.option(
    "--output",
    "output_dir",
    default="global/deliverables",
    help="Output directory for the dashboard",
)
@click.option("--top", "top_n", type=int, help="Limit to top N repos by commit count")
@click.option("--year", type=int, help="Only include repos updated in this year")
@click.option("--verbose", "-v", is_flag=True)
def multi_project_dashboard(output_dir, top_n, year, verbose):
    """Generate comprehensive multi-project dashboard visualization."""
    from .visualization.global_data_builder import prepare_dashboard_data

    global_dir = "global"
    data_dir = os.path.join(global_dir, "data")
    github_json = os.path.join(data_dir, "github-repos.json")

    if not os.path.exists(github_json):
        click.echo("No GitHub data found. Run 'devarch fetch-github <owner>' first.", err=True)
        sys.exit(1)

    # Build dashboard data
    click.echo("Building dashboard data...")
    dashboard_data = prepare_dashboard_data(global_dir, top_n=top_n, year=year)

    # Write dashboard data JSON
    dashboard_json_path = os.path.join(data_dir, "dashboard-data.json")
    with open(dashboard_json_path, "w") as f:
        json.dump(dashboard_data, f, indent=2)
    if verbose:
        click.echo(f"  Data written to {dashboard_json_path}")

    # Hydrate template
    template_path = _viz_template_path("multi-project-dashboard.html")
    if not os.path.exists(template_path):
        click.echo(f"Template not found at {template_path}", err=True)
        sys.exit(1)

    with open(template_path, encoding="utf-8") as f:
        html = f.read()

    # Inline the data JSON
    safe_data = (
        json.dumps(dashboard_data)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )

    # Replace the placeholder
    old_placeholder = "// DATA_PLACEHOLDER\nwindow.DASHBOARD_DATA = {};"
    new_inline = f"window.DASHBOARD_DATA = {safe_data};\n  window.dispatchEvent(new Event('dashboard-data-loaded'));"
    if old_placeholder in html:
        html = html.replace(old_placeholder, new_inline)
    elif "window.DASHBOARD_DATA = {};" in html:
        html = html.replace("window.DASHBOARD_DATA = {};", f"window.DASHBOARD_DATA = {safe_data};")
    else:
        click.echo("Warning: could not find DASHBOARD_DATA placeholder in template", err=True)

    # Write output
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "dashboard.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    click.echo(f"Multi-project dashboard generated at {output_path}")
    meta = dashboard_data.get("meta", {})
    click.echo(
        f"  {meta.get('total_commits', '?')} commits across {meta.get('total_repos', '?')} repos"
    )
    click.echo(f"  Period: {meta.get('first_date', '?')} to {meta.get('last_date', '?')}")


@main.command("fetch-github")
@click.argument("owner", required=False)
@click.option(
    "--owner",
    "owner_option",
    envvar="ARCHAEOLOGY_GITHUB_OWNER",
    help="GitHub username/org (same as OWNER)",
)
@click.option(
    "--output", "output_path", default="global/data/github-repos.json", help="Output JSON path"
)
def fetch_github(owner, owner_option, output_path):
    """Fetch repo metadata from GitHub API for all repos (no cloning)."""
    from .visualization.github_fetcher import GitHubFetchError, save_github_data

    owner = (owner or "").strip()
    owner_option = (owner_option or "").strip()
    if owner and owner_option:
        from_env = (
            click.get_current_context().get_parameter_source("owner_option")
            is ParameterSource.ENVIRONMENT
        )
        if from_env:
            owner_option = ""  # an explicit OWNER wins over the environment default
        elif owner != owner_option:
            raise click.UsageError("OWNER and --owner name different owners; pass the owner once")
    owner = owner or owner_option
    if not owner:
        raise click.UsageError(
            "fetch-github needs an owner: devarch fetch-github <OWNER> "
            "or set ARCHAEOLOGY_GITHUB_OWNER"
        )
    click.echo(f"Fetching repos for {owner} from GitHub...")
    try:
        data = save_github_data(output_path, owner=owner)
    except GitHubFetchError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"  {data['total_repos']} repos, {data['total_commits']} total commits")


@main.command()
@click.argument("project_name")
def benchmark(project_name):
    """Generate agent performance benchmark visualization."""
    from .visualization.agent_benchmark import run_benchmark_analysis

    project_dir = _project_dir(project_name)

    click.echo(f"Analyzing agent performance for '{project_name}'...")

    try:
        output_path = run_benchmark_analysis(project_dir)
        click.echo(f"Benchmark visualization generated at {output_path}")
    except FileNotFoundError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error generating benchmark: {e}", err=True)
        sys.exit(1)


@main.command("dashboard")
@click.option("--port", default=8080, help="Port to serve on")
@click.option("--no-open", is_flag=True, help="Don't open browser automatically")
def dashboard(port, no_open):
    """Disabled pending evidence and data-exposure hardening."""
    raise click.ClickException(
        "dashboard is disabled pending readiness hardening. Use init, mine, build-db, signals, analyze, visualize, export-report and audit."
    )


@main.command("publish-static")
@click.option("--output", "output_dir", default="site", help="Output directory for the static site")
def publish_static(output_dir):
    """Disabled pending evidence and data-exposure hardening."""
    raise click.ClickException(
        "publish-static is disabled pending readiness hardening. Use init, mine, build-db, signals, analyze, visualize, export-report and audit."
    )


if __name__ == "__main__":
    main()
