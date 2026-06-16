"""
Git integration for promptiq — diff extraction and commit survival checks.
Every function returns a safe fallback; never raises.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Low-level runner
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str, timeout: int = 8) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, ""


def is_git_repo(path: str) -> bool:
    ok, _ = _git(["rev-parse", "--git-dir"], path)
    return ok


def _repo_root(cwd: str) -> Optional[str]:
    ok, out = _git(["rev-parse", "--show-toplevel"], cwd)
    return out if ok and out else None


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class SessionValue:
    lines_changed: int
    files_touched: int
    has_tests: bool
    was_committed: bool
    was_reverted: bool
    survived_24h: bool
    survived_24h_known: bool   # False when < 24h old
    session_cost: float


# ---------------------------------------------------------------------------
# Spec public API
# ---------------------------------------------------------------------------

def get_session_output(
    session_start: datetime,
    session_end: datetime,
    repo_path: str = ".",
) -> tuple[int, int, bool, Optional[str]]:
    """
    Return (lines_changed, files_touched, has_tests, commit_hash_or_None).
    Uses git diff --stat between session timestamps.
    Returns (0, 0, False, None) on any failure.
    """
    root = _repo_root(repo_path)
    if not root:
        return 0, 0, False, None

    def _fmt(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    # Find commits inside the session window
    ok, log = _git(
        ["log", "--format=%H", f"--after={_fmt(session_start)}", f"--before={_fmt(session_end)}"],
        root,
    )
    commits = [c for c in log.splitlines() if c.strip()] if ok else []
    commit_hash = commits[0] if commits else None  # latest commit

    if commits:
        earliest, latest = commits[-1], commits[0]
        # diff parent-of-earliest to latest
        ok_n, numstat = _git(["diff", "--numstat", f"{earliest}^", latest], root)
        if not ok_n:
            # earliest might be the first commit — diff against empty tree
            ok_n, numstat = _git(
                ["diff", "--numstat",
                 "4b825dc642cb6eb9a060e54bf8d69288fbee4904", latest],
                root,
            )
    else:
        # No commits — measure uncommitted working-tree changes
        ok_n, numstat = _git(["diff", "--numstat", "HEAD"], root)
        _, staged = _git(["diff", "--numstat", "--staged"], root)
        if staged:
            numstat = (numstat + "\n" + staged).strip()

    lines_added = lines_removed = files = 0
    has_tests = False

    for line in (numstat.splitlines() if ok_n else []):
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        a, r, fname = parts
        try:
            lines_added   += int(a) if a != "-" else 0
            lines_removed += int(r) if r != "-" else 0
        except ValueError:
            pass
        files += 1
        low = fname.lower()
        if any(p in low for p in ("/test", "/tests/", "_test.", ".test.", ".spec.", "_spec.")):
            has_tests = True

    return lines_added + lines_removed, files, has_tests, commit_hash


def check_survival(
    commit_hash: str,
    repo_path: str = ".",
) -> Optional[bool]:
    """
    Was this commit reverted in subsequent history?
    Returns True if alive, False if reverted, None if too recent (< 24h).
    """
    if not commit_hash:
        return True

    root = _repo_root(repo_path)
    if not root:
        return None

    # Check commit age
    ok_ts, ts_out = _git(
        ["log", "-1", "--format=%ct", commit_hash], root
    )
    if ok_ts and ts_out:
        commit_time = datetime.fromtimestamp(int(ts_out), tz=timezone.utc)
        age = datetime.now(timezone.utc) - commit_time
        if age.total_seconds() < 86400:
            return None  # too recent to judge

    # Check if commit is still reachable from HEAD
    ok_anc, _ = _git(["merge-base", "--is-ancestor", commit_hash, "HEAD"], root)
    if not ok_anc:
        return False  # no longer in current history

    # Check for explicit revert commits
    ok_log, log_out = _git(
        ["log", "--format=%s %b", f"{commit_hash}..HEAD"], root
    )
    if ok_log:
        short = commit_hash[:7]
        for line in log_out.splitlines():
            if "revert" in line.lower() and (short in line or commit_hash in line):
                return False

    return True


# ---------------------------------------------------------------------------
# Scoring (spec formulas, exact)
# ---------------------------------------------------------------------------

def impact_score(v: SessionValue) -> int:
    """Returns 0–100."""
    base = min(50, (v.lines_changed * 0.1) + (v.files_touched * 2))
    bonus = 0
    if v.has_tests:      bonus += 20
    if v.was_committed:  bonus += 20
    if v.survived_24h:   bonus += 15
    if v.was_reverted:   bonus -= 40
    return max(0, min(100, int(base + bonus)))


def impact_cost_ratio(score: int, cost: float) -> float:
    if cost == 0:
        return 0.0
    return round(score / cost, 1)


# ---------------------------------------------------------------------------
# High-level helper used by cli.py
# ---------------------------------------------------------------------------

def compute_session_value(
    session_start: datetime,
    session_end: datetime,
    session_cost: float,
    repo_path: str,
) -> Optional[SessionValue]:
    """Returns None when not in a git repo."""
    if not repo_path or not is_git_repo(repo_path):
        return None

    lines, files, has_tests, commit_hash = get_session_output(
        session_start, session_end, repo_path
    )

    committed = bool(commit_hash)

    reverted = False
    survival: Optional[bool] = None
    survival_known = False

    if commit_hash:
        survival = check_survival(commit_hash, repo_path)
        if survival is None:
            survival_known = False
            survival = False
        else:
            survival_known = True
            reverted = not survival

    return SessionValue(
        lines_changed=lines,
        files_touched=files,
        has_tests=has_tests,
        was_committed=committed,
        was_reverted=reverted,
        survived_24h=bool(survival),
        survived_24h_known=survival_known,
        session_cost=session_cost,
    )
