"""
promptiq entry point.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from . import __version__
from .core import SessionReport, analyze_session, find_sessions
from .git import SessionValue, compute_session_value, impact_score, impact_cost_ratio
from . import storage
from .display import render_session, render_multi

console = Console(highlight=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _analyse_one(path) -> tuple[SessionReport, Optional[SessionValue], Optional[int], Optional[float]]:
    """Parse session + git value. Never raises."""
    report = analyze_session(path)
    sv: Optional[SessionValue] = None
    if report.cwd and report.session_end:
        try:
            sv = compute_session_value(
                report.timestamp, report.session_end,
                report.total_cost, report.cwd,
            )
        except Exception:
            sv = None

    score: Optional[int]   = impact_score(sv) if sv is not None else None
    ratio: Optional[float] = impact_cost_ratio(score, report.total_cost) if score is not None else None
    return report, sv, score, ratio


def _save(
    report: SessionReport,
    sv: Optional[SessionValue],
    score: Optional[int],
    ratio: Optional[float],
) -> None:
    kwargs: dict = {"git_available": sv is not None}
    if sv is not None:
        kwargs.update(
            impact_score=score,
            ratio=ratio,
            lines_changed=sv.lines_changed,
            files_touched=sv.files_touched,
            has_tests=sv.has_tests,
            was_committed=sv.was_committed,
            was_reverted=sv.was_reverted,
            survived_24h=sv.survived_24h,
            survived_known=sv.survived_24h_known,
        )
    storage.upsert_session(report, **kwargs)


def _generate_suggestions(reports: list[SessionReport]) -> Path:
    all_pa = [pa for r in reports for pa in r.prompt_analyses if pa.wasted_cost > 0]
    worst  = sorted(all_pa, key=lambda p: p.wasted_cost, reverse=True)[:3]

    lines = [
        "You are a prompt coach. For each prompt below, rewrite it to eliminate",
        "the waste pattern described. Use actual file names and context from this",
        "codebase. Be specific — no generic advice. Show the rewrite, then explain",
        "in one sentence what made the original inefficient.",
        "",
        "---",
        "",
    ]
    for rank, pa in enumerate(worst, 1):
        lines += [
            f"## Prompt {rank}  (+${pa.wasted_cost:.2f} wasted)",
            "",
            f'**Original:** "{pa.prompt_text}"',
            "",
            "**Waste patterns detected:**",
        ]
        for p in pa.patterns:
            lines += [f"- {p.description}", f"  - {p.missing_hint}"]
        lines += [
            "",
            "**Rewrite:**",
            "",
            "*(Claude Code will fill this in)*",
            "",
            "**Why the original was inefficient:**",
            "",
            "*(Claude Code will fill this in)*",
            "",
            "---",
            "",
        ]

    out = Path.cwd() / "promptiq-suggestions.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--last", "-n", default=1, metavar="N",
              help="Analyse last N sessions aggregated.")
@click.option("--suggest", is_flag=True,
              help="Generate rewrite suggestions and open in Claude Code.")
@click.option("--version", is_flag=True, help="Show version and exit.")
def main(last: int, suggest: bool, version: bool) -> None:
    """Prompt intelligence for Claude Code — detect waste, cut cost."""

    if version:
        console.print(f"promptiq {__version__}")
        return

    # Discover sessions
    try:
        paths = find_sessions(last)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    # Analyse each session
    reports:    list[SessionReport]            = []
    sv_map:     dict[str, Optional[SessionValue]] = {}
    score_map:  dict[str, Optional[int]]       = {}
    ratio_map:  dict[str, Optional[float]]     = {}

    for path in paths:
        try:
            report, sv, score, ratio = _analyse_one(path)
        except Exception as e:
            console.print(f"[dim]skipping {path.name}: {e}[/dim]")
            continue

        # Capture week stats BEFORE saving so we compare against prior history
        week_stats = storage.get_week_stats(exclude_id=report.session_id)

        try:
            _save(report, sv, score, ratio)
        except Exception:
            pass

        reports.append(report)
        sv_map[report.session_id]    = sv
        score_map[report.session_id] = score
        ratio_map[report.session_id] = ratio

    if not reports:
        console.print("[red]No sessions could be analysed.[/red]")
        sys.exit(1)

    if suggest:
        _run_suggest(reports)
        return

    # Render
    if len(reports) == 1:
        r = reports[0]
        render_session(
            r,
            sv_map[r.session_id],
            score_map[r.session_id],
            ratio_map[r.session_id],
            week_stats,  # None on first session ever
        )
    else:
        # For multi-session, use week_stats from before the first (oldest) session
        oldest = min(reports, key=lambda r: r.timestamp)
        week_stats_multi = storage.get_week_stats(exclude_id=oldest.session_id)
        render_multi(reports, sv_map, score_map, ratio_map, week_stats_multi)


def _run_suggest(reports: list[SessionReport]) -> None:
    out = _generate_suggestions(reports)
    console.print()
    console.print(f"[dim]suggestions written to[/dim] [white]{out}[/white]")
    console.print()

    # Resolve full path to claude binary so subprocess finds it even when
    # /opt/homebrew/bin (or similar) isn't on the subprocess PATH.
    import shutil
    claude_bin = shutil.which("claude")
    opened = False
    if claude_bin:
        try:
            subprocess.Popen([claude_bin, str(out)])
            console.print("[dim]opened in Claude Code[/dim]")
            opened = True
        except OSError:
            pass

    if not opened:
        # Print without Rich markup so the path is never word-wrapped mid-token
        print(f"  open in Claude Code:  claude {out}")

    console.print()
