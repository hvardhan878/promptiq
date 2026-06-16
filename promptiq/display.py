"""
Rich terminal output for promptiq.
Three colours only: white (default), YELLOW, GREEN, RED.
Bold only on the session name in the header.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.text import Text

from .core import SessionReport, PromptAnalysis
from .git import SessionValue, impact_score as _calc_score, impact_cost_ratio

YELLOW = "#F5A623"
GREEN  = "#4CAF50"
RED    = "#E53935"

_LABEL_W = 10  # "WASTED    " — label + padding, value starts at col 12 (2 indent + 10)


def _con() -> Console:
    return Console(highlight=False, markup=True)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _time_label(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (now - dt).days
    h = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    clock = f"{h}:{dt.minute:02d}{suffix}"
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"yesterday {clock}"
    if days < 7:
        return dt.strftime("%a ") + clock
    return dt.strftime("%b %-d")


def _dur(minutes: float) -> str:
    m = max(0, round(minutes))
    if m < 60:
        return f"{m} min"
    h, rm = divmod(m, 60)
    return f"{h}h {rm}min" if rm else f"{h}h"


def _ratio_str(r: float) -> str:
    return f"{int(r)}x" if r == int(r) else f"{r:.1f}x"


def _waste_bar(pct: float) -> Text:
    filled = round(pct / 100 * 20)
    t = Text()
    if filled > 0:
        t.append("█" * filled, style=f"bold {RED}")
    t.append("░" * (20 - filled), style="dim")
    return t


def _primary_pattern(pa: PromptAnalysis):
    return max(pa.patterns, key=lambda p: p.wasted_turns) if pa.patterns else None


def _metric(label: str, value_text: Text, con: Console) -> None:
    t = Text("  ")
    t.append(label.ljust(_LABEL_W), style="dim")
    t.append_text(value_text)
    con.print(t)


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------

def render_session(
    report: SessionReport,
    sv: Optional[SessionValue],
    score: Optional[int],         # impact_score 0-100
    ratio: Optional[float],       # impact_cost_ratio
    week_stats: Optional[dict],   # None = first session (skip RATIO + week line)
    con: Optional[Console] = None,
) -> None:
    if con is None:
        con = _con()

    w = max(60, min(getattr(con, "width", 80) or 80, 80))
    inner = w - 2
    sep = "  " + "─" * (w - 4)

    # ── Box header ──────────────────────────────────────────────────────────
    hfill    = "─" * max(0, inner - 11)        # "─ PROMPTIQ " = 11 chars
    subtitle = (
        f"{report.project_name} · "
        f"{_time_label(report.timestamp)} · "
        f"{_dur(report.duration_minutes)}"
    )
    sub_pad  = (" " + subtitle[:inner - 2]).ljust(inner)

    con.print(f"[bold]┌─ PROMPTIQ {hfill}┐[/bold]")
    con.print(f"[bold]│{sub_pad}│[/bold]")
    con.print(f"[bold]└{'─' * inner}┘[/bold]")
    con.print()

    is_first = week_stats is None

    # ── SPENT ───────────────────────────────────────────────────────────────
    _metric("SPENT", Text(f"${report.total_cost:.2f}"), con)

    # ── WASTED ──────────────────────────────────────────────────────────────
    pct = report.waste_pct
    t = Text()
    t.append(f"${report.wasted_cost:.2f}", style=YELLOW)
    t.append("  ")
    t.append_text(_waste_bar(pct))
    t.append(f"  {pct:3.0f}%", style=YELLOW)
    _metric("WASTED", t, con)

    # ── IMPACT ──────────────────────────────────────────────────────────────
    if sv is not None and score is not None:
        tests_part = " · tests ✓" if sv.has_tests else ""
        detail = (
            f"{sv.files_touched} file{'s' if sv.files_touched != 1 else ''} · "
            f"{sv.lines_changed} lines{tests_part}"
        )
        if sv.was_reverted:
            t = Text()
            t.append(f"{score}/100 ⚠ commit was reverted", style=YELLOW)
            _metric("IMPACT", t, con)
        elif not sv.was_committed:
            _metric("IMPACT", Text("— no commit detected", style="dim"), con)
        elif not sv.survived_24h_known:
            t = Text(f"{score}/100  {detail} ")
            t.append("· survival pending", style="dim")
            _metric("IMPACT", t, con)
        else:
            _metric("IMPACT", Text(f"{score}/100  {detail}"), con)
    # (silent when no git data — spec says skip IMPACT if no git/commit)

    # ── RATIO ───────────────────────────────────────────────────────────────
    if not is_first and ratio is not None and week_stats:
        best = week_stats.get("best") or 0
        avg  = week_stats.get("avg_7d") or 0

        r_text = Text(_ratio_str(ratio) + "    ")

        is_new_best = best == 0 or ratio > best * 1.01
        if is_new_best:
            r_text.append("▲ new best", style=GREEN)
        elif avg > 0 and ratio >= avg * 1.05:
            r_text.append(f"▲ above your average ({_ratio_str(avg)})", style=GREEN)
        elif avg > 0 and ratio <= avg * 0.95:
            r_text.append(f"▼ below your average ({_ratio_str(avg)})", style=RED)
        else:
            r_text.append(f"≈ at your average ({_ratio_str(avg)})", style="dim")

        _metric("RATIO", r_text, con)

    con.print()
    con.print(sep)
    con.print()

    # ── Prompt list ─────────────────────────────────────────────────────────
    worst = report.worst_prompts

    if not worst:
        con.print("  [dim]clean session — no significant waste detected[/dim]")
    else:
        count = len(worst)
        plural = "s" if count > 1 else ""
        con.print(f"  [dim]{count} prompt{plural} killed this session[/dim]")
        con.print()

        for rank, pa in enumerate(worst, 1):
            txt = pa.prompt_text.replace("\n", " ").strip()
            if len(txt) > 50:
                txt = txt[:49] + "…"
            quoted   = f'"{txt}"'
            cost_str = f"${pa.wasted_cost:.2f}"

            # Right-align cost at w-2; "  N  " = 5 chars
            pad = max(2, w - 2 - 5 - len(quoted) - len(cost_str))

            row = Text(f"  {rank}  ")
            row.append(quoted)
            row.append(" " * pad)
            row.append(cost_str, style=YELLOW)
            con.print(row)

            pat = _primary_pattern(pa)
            if pat:
                con.print(f"     [dim]{pat.description}[/dim]")
                con.print(f"     [dim]{pat.missing_hint}[/dim]")

            con.print()

    con.print(sep)
    con.print()

    # ── YOUR WEEK ───────────────────────────────────────────────────────────
    if not is_first and week_stats and ratio is not None:
        best = week_stats.get("best") or 0
        avg  = week_stats.get("avg_7d") or 0
        week_text = (
            f"  [dim]YOUR WEEK   "
            f"best {_ratio_str(best)} · "
            f"avg {_ratio_str(avg)} · "
            f"today {_ratio_str(ratio)}[/dim]"
        )
        con.print(week_text)
        con.print()

    # ── CTA ─────────────────────────────────────────────────────────────────
    con.print(
        "  [dim]run [bold]promptiq --suggest[/bold] "
        "to get rewrites in Claude Code[/dim]"
    )
    con.print()


# ---------------------------------------------------------------------------
# Multi-session view (--last N)
# ---------------------------------------------------------------------------

def render_multi(
    reports: list[SessionReport],
    sv_map: dict[str, Optional[SessionValue]],
    score_map: dict[str, Optional[int]],
    ratio_map: dict[str, Optional[float]],
    week_stats: Optional[dict],
    con: Optional[Console] = None,
) -> None:
    if con is None:
        con = _con()

    w     = max(60, min(getattr(con, "width", 80) or 80, 80))
    inner = w - 2
    sep   = "  " + "─" * (w - 4)

    # ── Box header ──────────────────────────────────────────────────────────
    hfill    = "─" * max(0, inner - 11)
    n        = len(reports)
    oldest   = min(reports, key=lambda r: r.timestamp)
    newest   = max(reports, key=lambda r: r.timestamp)
    span     = f"last {n} sessions · {_time_label(oldest.timestamp)}"
    sub_pad  = (" " + span[:inner - 2]).ljust(inner)

    con.print(f"[bold]┌─ PROMPTIQ {hfill}┐[/bold]")
    con.print(f"[bold]│{sub_pad}│[/bold]")
    con.print(f"[bold]└{'─' * inner}┘[/bold]")
    con.print()

    total_cost   = sum(r.total_cost for r in reports)
    total_wasted = sum(r.wasted_cost for r in reports)
    pct = (total_wasted / total_cost * 100) if total_cost else 0

    # ── SPENT ───────────────────────────────────────────────────────────────
    _metric("SPENT", Text(f"${total_cost:.2f}"), con)

    # ── WASTED ──────────────────────────────────────────────────────────────
    t = Text()
    t.append(f"${total_wasted:.2f}", style=YELLOW)
    t.append("  ")
    t.append_text(_waste_bar(pct))
    t.append(f"  {pct:3.0f}%", style=YELLOW)
    _metric("WASTED", t, con)

    # ── IMPACT avg ──────────────────────────────────────────────────────────
    scores = [s for s in score_map.values() if s is not None]
    if scores:
        avg_score = round(sum(scores) / len(scores))
        _metric("IMPACT", Text(f"avg {avg_score}/100  ({len(scores)} sessions with git data)"), con)

    # ── RATIO avg ───────────────────────────────────────────────────────────
    ratios = [r for r in ratio_map.values() if r is not None]
    if ratios and week_stats is not None:
        avg_ratio = round(sum(ratios) / len(ratios), 1)
        _metric("RATIO", Text(f"avg {_ratio_str(avg_ratio)}"), con)

    con.print()
    con.print(sep)
    con.print()

    # ── Per-session summary ──────────────────────────────────────────────────
    for r in reports:
        ratio = ratio_map.get(r.session_id)
        label = f"{r.project_name} · {_time_label(r.timestamp)}"
        ratio_part = f"  {_ratio_str(ratio)}" if ratio is not None else ""
        waste_part = (
            f"  [{'#F5A623'}]${r.wasted_cost:.2f} wasted ({r.waste_pct:.0f}%)[/]"
            if r.wasted_cost > 0
            else "  [dim]clean[/dim]"
        )
        con.print(f"  [dim]·[/dim] {label}{ratio_part}{waste_part}")

    # ── Worst prompts across all sessions ───────────────────────────────────
    all_worst = sorted(
        [pa for r in reports for pa in r.prompt_analyses if pa.wasted_cost > 0],
        key=lambda pa: pa.wasted_cost, reverse=True,
    )[:3]

    if all_worst:
        con.print()
        con.print(f"  [dim]worst prompts across {n} sessions[/dim]")
        con.print()
        for rank, pa in enumerate(all_worst, 1):
            txt = pa.prompt_text.replace("\n", " ").strip()
            if len(txt) > 50:
                txt = txt[:49] + "…"
            quoted   = f'"{txt}"'
            cost_str = f"${pa.wasted_cost:.2f}"
            pad      = max(2, w - 2 - 5 - len(quoted) - len(cost_str))
            row = Text(f"  {rank}  ")
            row.append(quoted)
            row.append(" " * pad)
            row.append(cost_str, style=YELLOW)
            con.print(row)
            pat = _primary_pattern(pa)
            if pat:
                con.print(f"     [dim]{pat.description}[/dim]")
                con.print(f"     [dim]{pat.missing_hint}[/dim]")
            con.print()

    con.print(sep)
    con.print()

    # ── YOUR WEEK ───────────────────────────────────────────────────────────
    if week_stats and ratios:
        best = week_stats.get("best") or 0
        avg  = week_stats.get("avg_7d") or 0
        today_r = ratios[0]  # most recent
        con.print(
            f"  [dim]YOUR WEEK   "
            f"best {_ratio_str(best)} · "
            f"avg {_ratio_str(avg)} · "
            f"today {_ratio_str(today_r)}[/dim]"
        )
        con.print()

    con.print(
        "  [dim]run [bold]promptiq --suggest[/bold] "
        "to get rewrites in Claude Code[/dim]"
    )
    con.print()
