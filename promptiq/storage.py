"""
SQLite session history — metadata only, never prompt text or code.
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
    session_id     TEXT PRIMARY KEY,
    session_path   TEXT NOT NULL,
    project_name   TEXT,
    timestamp      REAL,
    duration_mins  REAL,
    total_cost     REAL,
    wasted_cost    REAL,
    model          TEXT,
    waste_breakdown TEXT,
    is_complete    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ts ON sessions(timestamp);
"""

_NEW_COLS = [
    ("impact_score", "INTEGER"),
    ("ratio",        "REAL"),
    ("lines_changed","INTEGER"),
    ("files_touched","INTEGER"),
    ("has_tests",    "INTEGER"),
    ("was_committed","INTEGER"),
    ("was_reverted", "INTEGER"),
    ("survived_24h", "INTEGER"),
    ("survived_known","INTEGER"),
    ("git_available","INTEGER DEFAULT 0"),
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
    for col, col_type in _NEW_COLS:
        if col not in existing:
            con.execute(f"ALTER TABLE sessions ADD COLUMN {col} {col_type}")


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert_session(
    report: SessionReport,
    impact_score: Optional[int] = None,
    ratio: Optional[float] = None,
    lines_changed: Optional[int] = None,
    files_touched: Optional[int] = None,
    has_tests: Optional[bool] = None,
    was_committed: Optional[bool] = None,
    was_reverted: Optional[bool] = None,
    survived_24h: Optional[bool] = None,
    survived_known: Optional[bool] = None,
    git_available: bool = False,
) -> None:
    bd: dict[str, int] = {}
    for pa in report.prompt_analyses:
        for p in pa.patterns:
            bd[p.kind] = bd.get(p.kind, 0) + 1

    ts = report.timestamp.replace(tzinfo=timezone.utc).timestamp()

    def _b(v: Optional[bool]) -> Optional[int]:
        return None if v is None else int(v)

    with _conn() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO sessions
              (session_id, session_path, project_name, timestamp, duration_mins,
               total_cost, wasted_cost, model, waste_breakdown, is_complete,
               impact_score, ratio, lines_changed, files_touched,
               has_tests, was_committed, was_reverted, survived_24h, survived_known,
               git_available)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                report.session_id, str(report.session_path), report.project_name,
                ts, report.duration_minutes,
                report.total_cost, report.wasted_cost,
                report.model, json.dumps(bd), int(report.is_complete),
                impact_score, ratio,
                lines_changed, files_touched,
                _b(has_tests), _b(was_committed), _b(was_reverted),
                _b(survived_24h), _b(survived_known),
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
# Read
# ---------------------------------------------------------------------------

def get_week_stats(exclude_id: Optional[str] = None) -> Optional[dict]:
    """
    Historical ratio stats across all sessions (excluding current one).
    Returns None when there are no prior sessions with ratio data.
    """
    exclude_clause = "AND session_id != ?" if exclude_id else ""
    params: tuple = (exclude_id,) if exclude_id else ()

    with _conn() as con:
        row = con.execute(
            f"""
            SELECT COUNT(*) AS n, MAX(ratio) AS best, AVG(ratio) AS avg_all
            FROM sessions
            WHERE ratio IS NOT NULL {exclude_clause}
            """,
            params,
        ).fetchone()

        if not row or not row["n"]:
            return None

        # 7-day average
        row7 = con.execute(
            f"""
            SELECT AVG(ratio) AS avg_7d
            FROM sessions
            WHERE ratio IS NOT NULL
              AND timestamp >= strftime('%s', 'now', '-7 days') {exclude_clause}
            """,
            params,
        ).fetchone()

    avg_7d = row7["avg_7d"] if row7 and row7["avg_7d"] else row["avg_all"]

    return {
        "best":    round(row["best"] or 0, 1),
        "avg_7d":  round(avg_7d or 0, 1),
        "n":       row["n"],
    }


def get_summary_stats() -> dict:
    with _conn() as con:
        row = con.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(total_cost) AS cost,
                   SUM(wasted_cost) AS wasted,
                   AVG(ratio) AS avg_ratio
            FROM sessions
            """
        ).fetchone()
    if not row or not row["n"]:
        return {"n": 0, "cost": 0.0, "wasted": 0.0, "avg_ratio": None}
    return {
        "n":         row["n"],
        "cost":      round(row["cost"] or 0, 4),
        "wasted":    round(row["wasted"] or 0, 4),
        "avg_ratio": round(row["avg_ratio"], 1) if row["avg_ratio"] else None,
    }
