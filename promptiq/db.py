"""
SQLite storage for promptiq — session metadata only, no prompt text or code.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .core import SessionReport

DB_PATH = Path.home() / ".promptiq" / "sessions.db"

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    session_path    TEXT NOT NULL,
    project_name    TEXT,
    timestamp       REAL,
    duration_mins   REAL,
    total_cost      REAL,
    wasted_cost     REAL,
    model           TEXT,
    waste_breakdown TEXT,
    is_complete     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_timestamp ON sessions(timestamp);
"""

# Columns added after v0.1 — migrated lazily on first connect
_NEW_COLUMNS = [
    ("value_score",     "REAL"),
    ("impact_ratio",    "REAL"),
    ("lines_changed",   "INTEGER"),
    ("files_touched",   "INTEGER"),
    ("has_tests",       "INTEGER"),
    ("was_committed",   "INTEGER"),
    ("was_reverted",    "INTEGER"),
    ("survived_24h",    "INTEGER"),
    ("survived_known",  "INTEGER"),
    ("git_available",   "INTEGER DEFAULT 0"),
]


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(_DDL)
        _migrate(con)
        yield con
        con.commit()
    finally:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    existing = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
    for col, col_type in _NEW_COLUMNS:
        if col not in existing:
            con.execute(f"ALTER TABLE sessions ADD COLUMN {col} {col_type}")


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert_session(
    report: SessionReport,
    value_score: Optional[float] = None,
    impact_ratio: Optional[float] = None,
    lines_changed: Optional[int] = None,
    files_touched: Optional[int] = None,
    has_tests: Optional[bool] = None,
    was_committed: Optional[bool] = None,
    was_reverted: Optional[bool] = None,
    survived_24h: Optional[bool] = None,
    survived_known: Optional[bool] = None,
    git_available: bool = False,
) -> None:
    breakdown: dict[str, int] = {}
    for pa in report.prompt_analyses:
        for p in pa.patterns:
            breakdown[p.kind] = breakdown.get(p.kind, 0) + 1

    ts = report.timestamp.replace(tzinfo=timezone.utc).timestamp()

    def _b(v: Optional[bool]) -> Optional[int]:
        return None if v is None else int(v)

    with _conn() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO sessions
              (session_id, session_path, project_name, timestamp, duration_mins,
               total_cost, wasted_cost, model, waste_breakdown, is_complete,
               value_score, impact_ratio, lines_changed, files_touched,
               has_tests, was_committed, was_reverted, survived_24h, survived_known,
               git_available)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                report.session_id,
                str(report.session_path),
                report.project_name,
                ts,
                report.duration_minutes,
                report.total_cost,
                report.wasted_cost,
                report.model,
                json.dumps(breakdown),
                int(report.is_complete),
                value_score,
                impact_ratio,
                lines_changed,
                files_touched,
                _b(has_tests),
                _b(was_committed),
                _b(was_reverted),
                _b(survived_24h),
                _b(survived_known),
                int(git_available),
            ),
        )


def already_indexed(session_id: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Read — waste / cost queries (unchanged from v0.1)
# ---------------------------------------------------------------------------

def get_trend_data(days: int = 30) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT date(timestamp, 'unixepoch') AS day,
                   SUM(total_cost)              AS total,
                   SUM(wasted_cost)             AS wasted
            FROM sessions
            WHERE timestamp >= strftime('%s', 'now', ?)
            GROUP BY day ORDER BY day
            """,
            (f"-{days} days",),
        ).fetchall()
    return [
        {
            "day": r["day"],
            "waste_pct": round((r["wasted"] / r["total"] * 100) if r["total"] else 0, 1),
        }
        for r in rows
    ]


def get_pattern_frequencies() -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT waste_breakdown FROM sessions").fetchall()
    combined: dict[str, int] = {}
    total = len(rows)
    for row in rows:
        try:
            bd = json.loads(row["waste_breakdown"] or "{}")
        except json.JSONDecodeError:
            continue
        for kind in bd:
            combined[kind] = combined.get(kind, 0) + 1
    label = {
        "clarification_loop": "Clarification loops",
        "backtracking": "Backtracking",
        "redundant_tool_calls": "Redundant tool calls",
    }
    return [
        {"pattern": label.get(k, k), "kind": k, "pct": round(c / total * 100, 1) if total else 0}
        for k, c in sorted(combined.items(), key=lambda x: -x[1])
    ]


# ---------------------------------------------------------------------------
# Read — new Impact/Cost ratio queries
# ---------------------------------------------------------------------------

def get_ratio_trend(days: int = 30) -> list[dict]:
    """Impact/Cost ratio per session, last N days — for the headline chart."""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT date(timestamp, 'unixepoch') AS day,
                   AVG(impact_ratio)            AS avg_ratio,
                   project_name
            FROM sessions
            WHERE timestamp >= strftime('%s', 'now', ?)
              AND impact_ratio IS NOT NULL
            GROUP BY day ORDER BY day
            """,
            (f"-{days} days",),
        ).fetchall()
    return [
        {"day": r["day"], "ratio": round(r["avg_ratio"] or 0, 1)}
        for r in rows
    ]


def get_value_distribution() -> list[dict]:
    """Histogram buckets of value_score for all sessions."""
    with _conn() as con:
        rows = con.execute(
            "SELECT value_score FROM sessions WHERE value_score IS NOT NULL"
        ).fetchall()
    if not rows:
        return []

    scores = [r["value_score"] for r in rows]
    max_score = max(scores) if scores else 100
    bucket_size = max(1, max_score / 8)

    buckets: dict[int, int] = {}
    for s in scores:
        b = int(s / bucket_size) * int(bucket_size)
        buckets[b] = buckets.get(b, 0) + 1

    return [
        {"bucket": f"{b}–{b + int(bucket_size)}", "count": c}
        for b, c in sorted(buckets.items())
    ]


def get_waste_value_scatter() -> list[dict]:
    """Waste% vs value_score for all sessions — the insight scatter."""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT total_cost, wasted_cost, value_score, project_name
            FROM sessions
            WHERE value_score IS NOT NULL
            ORDER BY timestamp DESC LIMIT 200
            """
        ).fetchall()
    return [
        {
            "waste_pct": round((r["wasted_cost"] / r["total_cost"] * 100) if r["total_cost"] else 0, 1),
            "value": round(r["value_score"], 1),
            "project": r["project_name"] or "",
        }
        for r in rows
    ]


def get_survival_stat() -> dict:
    """Percentage of committed sessions whose code survived 24h."""
    with _conn() as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN survived_24h = 1 THEN 1 ELSE 0 END) AS survived
            FROM sessions
            WHERE was_committed = 1 AND survived_known = 1
            """
        ).fetchone()
    if not row or not row["total"]:
        return {"pct": None, "total": 0}
    return {
        "pct": round(row["survived"] / row["total"] * 100),
        "total": row["total"],
    }


def get_avg_ratio() -> float:
    """Average impact/cost ratio across all sessions with git data."""
    with _conn() as con:
        row = con.execute(
            "SELECT AVG(impact_ratio) AS avg FROM sessions WHERE impact_ratio IS NOT NULL"
        ).fetchone()
    return round(row["avg"] or 0, 1) if row else 0.0


def get_summary_stats() -> dict:
    with _conn() as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS total_sessions,
                   SUM(total_cost) AS total_cost,
                   SUM(wasted_cost) AS wasted_cost,
                   AVG(CASE WHEN total_cost > 0 THEN wasted_cost/total_cost*100 ELSE 0 END) AS avg_waste_pct,
                   AVG(impact_ratio) AS avg_ratio
            FROM sessions
            """
        ).fetchone()
    if not row or not row["total_sessions"]:
        return {"total_sessions": 0, "total_cost": 0.0, "wasted_cost": 0.0,
                "avg_waste_pct": 0.0, "avg_ratio": None}
    return {
        "total_sessions": row["total_sessions"],
        "total_cost": round(row["total_cost"] or 0, 4),
        "wasted_cost": round(row["wasted_cost"] or 0, 4),
        "avg_waste_pct": round(row["avg_waste_pct"] or 0, 1),
        "avg_ratio": round(row["avg_ratio"], 1) if row["avg_ratio"] is not None else None,
    }
