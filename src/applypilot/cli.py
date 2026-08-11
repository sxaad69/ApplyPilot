"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: Optional[int] = typer.Option(None, "--min-score", help="Minimum fit score for tailor/cover stages (default: MIN_FIT_SCORE env or 7)."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.config import get_min_fit_score
    if min_score is None:
        min_score = get_min_fit_score()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    list_only: bool = typer.Option(
        False, "--list",
        help="List jobs ready for manual application and exit.",
    ),
    limit: int = typer.Option(1, "--limit", help="Max jobs to apply to."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fill + upload but do NOT submit."),
    engine: str = typer.Option("hermes", "--engine", help="'hermes' (Playwright MCP) or 'claude'."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel workers."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Auto-apply to prepared jobs via Hermes + Playwright MCP."""
    _bootstrap()

    from datetime import datetime, timezone
    from applypilot.database import get_connection

    # --- Utility modes (simple DB bookkeeping, no browser automation) ---

    if mark_applied:
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET apply_status = 'applied', applied_at = ?, apply_error = NULL WHERE url = ?",
            (datetime.now(timezone.utc).isoformat(), mark_applied),
        )
        conn.commit()
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET apply_status = 'failed', apply_error = ?, apply_attempts = 99 WHERE url = ?",
            (fail_reason or "manual", mark_failed),
        )
        conn.commit()
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        conn = get_connection()
        cursor = conn.execute(
            "UPDATE jobs SET apply_status = NULL, apply_error = NULL, apply_attempts = 0 "
            "WHERE apply_status = 'failed' "
            "OR (apply_status IS NOT NULL AND apply_status != 'applied' AND apply_status != 'in_progress')"
        )
        conn.commit()
        console.print(f"[green]Reset {cursor.rowcount} failed job(s) for retry.[/green]")
        return

    if list_only:
        _print_ready_jobs()
        return

    # --- Auto-apply via Hermes + Playwright MCP ---
    from applypilot.apply.launcher import main as apply_main
    apply_main(limit=limit, dry_run=dry_run, engine=engine, workers=workers)


def _print_ready_jobs(limit: int = 100) -> int:
    """Print a numbered list of jobs fully prepared for manual application."""
    from applypilot.db import JobDatabase, JOB_STATUS_COVER_LETTERED

    jobs = JobDatabase().list_jobs(JOB_STATUS_COVER_LETTERED, limit=limit)

    if not jobs:
        console.print("[yellow]No jobs ready to apply yet.[/yellow]")
        console.print("Run [bold]applypilot run[/bold] to discover, score, tailor, and write cover letters.")
        return 0

    console.print(f"\n[bold]Jobs ready for manual application ({len(jobs)})[/bold]\n")
    for i, job in enumerate(jobs, start=1):
        title = job.get("title") or "Untitled"
        company = job.get("company") or job.get("site") or "?"
        fit = job.get("fit_score")
        fit_str = f"{fit}/10" if fit is not None else "?/10"
        url = job.get("application_url") or job.get("url") or ""
        resume_path = job.get("tailored_resume_path") or "n/a"
        cover_path = job.get("cover_letter_path") or "n/a"
        console.print(
            f"  [bold cyan]{i}.[/bold cyan] {title} at {company} | Fit: {fit_str}\n"
            f"       URL: {url}\n"
            f"       Resume: {resume_path}\n"
            f"       Cover letter: {cover_path}"
        )
    return len(jobs)


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    # Pipeline status breakdown (new/scored/tailored/cover_lettered/rejected/error)
    from applypilot.db import JobDatabase
    status_counts = JobDatabase().get_status_stats()
    status_table = Table(title="\nPipeline Status", show_header=True, header_style="bold green")
    status_table.add_column("Status")
    status_table.add_column("Count", justify="right")
    for status, count in status_counts.items():
        status_table.add_row(status, str(count))
    console.print(status_table)

    console.print()


@app.command("list")
def list_jobs(
    status: str = typer.Option(
        "cover_lettered", "--status", "-s",
        help=(
            "Pipeline status to filter by: new, scored, tailored, "
            "cover_lettered, rejected, error (default: cover_lettered = ready to apply)."
        ),
    ),
    limit: int = typer.Option(50, "--limit", "-l", help="Max jobs to show (0 = all)."),
) -> None:
    """List jobs by pipeline status."""
    _bootstrap()

    from applypilot.db import JobDatabase, VALID_JOB_STATUSES

    if status not in VALID_JOB_STATUSES:
        console.print(
            f"[red]Unknown status:[/red] '{status}'. "
            f"Valid: {', '.join(sorted(VALID_JOB_STATUSES))}"
        )
        raise typer.Exit(code=1)

    jobs = JobDatabase().list_jobs(status, limit=limit)

    if not jobs:
        console.print(f"[yellow]No jobs with status '{status}'.[/yellow]")
        return

    table = Table(title=f"Jobs [{status}] ({len(jobs)})", show_header=True, header_style="bold cyan")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", style="bold")
    table.add_column("Company")
    table.add_column("Location")
    table.add_column("Fit", justify="center")
    table.add_column("URL", overflow="fold", no_wrap=False)

    for i, job in enumerate(jobs, start=1):
        fit = job.get("fit_score")
        fit_str = f"{fit}/10" if fit is not None else "-"
        url = (job.get("application_url") or job.get("url") or "")[:80]
        table.add_row(
            str(i),
            (job.get("title") or "Untitled")[:50],
            (job.get("company") or job.get("site") or "?")[:30],
            (job.get("location") or "")[:30],
            fit_str,
            url,
        )
    console.print(table)


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")

    if base_url:
        results.append(("LLM endpoint", ok_mark, f"{base_url} (model: {model})"))
    elif openai_key:
        results.append(("LLM endpoint", ok_mark, f"api.openai.com/v1 (model: {model})"))
    elif os.environ.get("LLM_URL"):
        results.append(("LLM endpoint", ok_mark, f"{os.environ.get('LLM_URL')} (model: {model})"))
    else:
        results.append(("LLM endpoint", fail_mark,
                        "Set OPENAI_BASE_URL + OPENAI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # Connectivity test against the configured OpenAI-compatible endpoint
    if base_url or openai_key or os.environ.get("LLM_URL"):
        try:
            from applypilot.llm import LLMClient
            client = LLMClient(
                base_url or "https://api.openai.com/v1",
                model,
                openai_key or os.environ.get("LLM_API_KEY", ""),
            )
            ping = client.ping()
            if ping["ok"]:
                results.append(("LLM connectivity", ok_mark, ping["detail"]))
            else:
                results.append(("LLM connectivity", fail_mark, ping["detail"]))
        except Exception as e:
            results.append(("LLM connectivity", fail_mark, f"test failed: {e}"))

    # --- Tier 3 (disabled in this build) ---
    results.append(("[dim]Stage 6 auto-apply[/dim]", "[dim]DISABLED[/dim]",
                    "Apply manually using the prepared resumes/cover letters"))

    # CapSolver (unused while Stage 6 is disabled — informational)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", "[dim]ignored[/dim]",
                        "Not used — auto-apply is disabled"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs OPENAI_BASE_URL + OPENAI_API_KEY)[/dim]")

    console.print()
    console.print("[bold]Stage 6 (Auto-Apply):[/bold] [red]DISABLED[/red] in this build — "
                  "apply manually using the prepared resumes and cover letters.")


if __name__ == "__main__":
    app()
