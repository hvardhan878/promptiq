"""
Core parsing and analysis logic for promptiq.
Single file — designed to be readable and hackable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

# Tools that only read / explore — no visible output to user
EXPLORATION_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "Bash", "ToolSearch"})
EXPLORATION_NO_OUTPUT = frozenset({"Read", "Grep", "Glob", "LS"})

# Tools that produce output (files written, code changed)
OUTPUT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# ---------------------------------------------------------------------------
# Model pricing  (input $/M, output $/M, cache_write $/M, cache_read $/M)
# ---------------------------------------------------------------------------

MODEL_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4-8":            (15.0,  75.0,  18.75, 1.50),
    "claude-opus-4-7":            (15.0,  75.0,  18.75, 1.50),
    "claude-sonnet-4-6":          (3.0,   15.0,   3.75, 0.30),
    "claude-sonnet-4-5":          (3.0,   15.0,   3.75, 0.30),
    "claude-sonnet-4-5-20251001": (3.0,   15.0,   3.75, 0.30),
    "claude-haiku-4-5":           (0.80,   4.0,   1.00, 0.08),
    "claude-3-5-sonnet-20241022": (3.0,   15.0,   3.75, 0.30),
    "claude-3-5-haiku-20241022":  (0.80,   4.0,   1.00, 0.08),
    "claude-3-opus-20240229":     (15.0,  75.0,  18.75, 1.50),
    "claude-3-sonnet-20240229":   (3.0,   15.0,   3.75, 0.30),
    "claude-3-haiku-20240307":    (0.25,   1.25,  0.30, 0.03),
}
DEFAULT_PRICING = (3.0, 15.0, 3.75, 0.30)


def tokens_to_cost(
    input_tokens: int,
    output_tokens: int,
    model: str,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> float:
    in_p, out_p, cw_p, cr_p = MODEL_PRICING.get(model, DEFAULT_PRICING)
    return (
        input_tokens * in_p
        + output_tokens * out_p
        + cache_creation * cw_p
        + cache_read * cr_p
    ) / 1_000_000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    input_args: dict
    tool_use_id: str


@dataclass
class WastePattern:
    kind: str        # "backtracking" | "clarification_loop" | "redundant_tool_calls"
    description: str
    missing_hint: str
    wasted_turns: int


@dataclass
class PromptAnalysis:
    prompt_text: str
    turn_index: int
    tool_calls: list[ToolCall]
    patterns: list[WastePattern]
    wasted_cost: float
    total_cost: float  # total cost of all turns responding to this prompt


@dataclass
class SessionReport:
    session_id: str
    session_path: Path
    project_name: str
    timestamp: datetime
    duration_minutes: float
    total_cost: float
    wasted_cost: float
    model: str
    prompt_analyses: list[PromptAnalysis]
    is_complete: bool
    cwd: str = ""           # working directory the session ran in
    session_end: Optional[datetime] = None

    @property
    def waste_pct(self) -> float:
        if self.total_cost <= 0:
            return 0.0
        return (self.wasted_cost / self.total_cost) * 100

    @property
    def worst_prompts(self) -> list[PromptAnalysis]:
        return sorted(
            [p for p in self.prompt_analyses if p.wasted_cost > 0],
            key=lambda p: p.wasted_cost,
            reverse=True,
        )[:3]


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def find_sessions(n: int = 1) -> list[Path]:
    """Return paths to the N most recent Claude Code session JSONL files."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        raise FileNotFoundError(
            f"Claude Code projects directory not found at {base}\n"
            "Make sure Claude Code is installed and you have run at least one session."
        )

    jsonl_files = list(base.rglob("*.jsonl"))
    if not jsonl_files:
        raise FileNotFoundError(
            f"No session files found in {base}\n"
            "Run a Claude Code session first, then try again."
        )

    jsonl_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonl_files[:n]


def session_project_name(path: Path) -> str:
    """Derive a human-readable project name from the encoded directory name."""
    encoded = path.parent.name
    # Encoded paths look like "-Users-harsh-Desktop-myproject"
    # Replace leading - with nothing, remaining - with /
    clean = encoded.lstrip("-").replace("-", "/")
    parts = [p for p in clean.split("/") if p]
    return parts[-1] if parts else encoded


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------

def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def _is_human_message(entry: dict) -> bool:
    """True if this user entry is actual human text, not a tool result."""
    if entry.get("type") != "user":
        return False
    content = entry.get("message", {}).get("content", [])
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        has_text = False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return False
            if block.get("type") == "text" and block.get("text", "").strip():
                has_text = True
        return has_text
    return False


def _human_text(entry: dict) -> str:
    content = entry.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _is_system_message(text: str) -> bool:
    """Filter out harness-injected messages (task notifications, etc.)."""
    return text.startswith("<task-notification>") or text.startswith("<system>")


def parse_session(path: Path) -> tuple[list[dict], list[dict]]:
    """
    Parse a JSONL session file.
    Returns (human_entries, assistant_entries) as raw dicts.
    Skips malformed lines silently.
    """
    human_entries: list[dict] = []
    assistant_entries: list[dict] = []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise OSError(f"Cannot read {path}: {e}") from e

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = entry.get("type", "")
        if t == "user" and _is_human_message(entry):
            txt = _human_text(entry)
            if txt and not _is_system_message(txt):
                human_entries.append(entry)
        elif t == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                has_tool = any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in content
                )
                has_text = any(
                    isinstance(b, dict) and b.get("type") == "text"
                    for b in content
                )
                if has_tool or has_text:
                    assistant_entries.append(entry)

    return human_entries, assistant_entries


# ---------------------------------------------------------------------------
# Grouping turns by prompt
# ---------------------------------------------------------------------------

def _group_by_prompt(
    human_entries: list[dict],
    assistant_entries: list[dict],
) -> list[tuple[dict, list[dict]]]:
    """
    Pair each human message with all assistant entries that follow it
    (up to the next human message), ordered by timestamp.
    """
    # Build a timeline: (timestamp, kind, entry)
    Timeline = list[tuple[datetime, str, dict]]
    timeline: Timeline = []

    for e in human_entries:
        ts = _parse_ts(e.get("timestamp")) or datetime.min
        timeline.append((ts, "human", e))

    seen_ids: set[str] = set()
    for e in assistant_entries:
        # Deduplicate by extracting unique tool_use ids
        msg = e.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            entry_tool_ids = {
                b.get("id", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            }
        else:
            entry_tool_ids = set()

        # Accept this entry if it has text content or new tool calls
        has_text = any(
            isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
            for b in (content if isinstance(content, list) else [])
        )
        new_tool_ids = entry_tool_ids - seen_ids

        if has_text or new_tool_ids:
            seen_ids |= new_tool_ids
            ts = _parse_ts(e.get("timestamp")) or datetime.min
            timeline.append((ts, "assistant", e))

    timeline.sort(key=lambda x: x[0])

    groups: list[tuple[dict, list[dict]]] = []
    current_human: Optional[dict] = None
    current_responses: list[dict] = []

    for _, kind, entry in timeline:
        if kind == "human":
            if current_human is not None:
                groups.append((current_human, current_responses))
            current_human = entry
            current_responses = []
        else:
            if current_human is not None:
                current_responses.append(entry)

    if current_human is not None:
        groups.append((current_human, current_responses))

    return groups


# ---------------------------------------------------------------------------
# Waste pattern detection
# ---------------------------------------------------------------------------

def _extract_tool_calls(assistant_entries: list[dict]) -> list[ToolCall]:
    """Flatten all unique tool_use blocks from a list of assistant entries."""
    seen_ids: set[str] = set()
    calls: list[ToolCall] = []
    for entry in assistant_entries:
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tid = block.get("id", "")
            if tid and tid in seen_ids:
                continue
            seen_ids.add(tid)
            calls.append(ToolCall(
                name=block.get("name", ""),
                input_args=block.get("input", {}),
                tool_use_id=tid,
            ))
    return calls


def _detect_backtracking(tool_calls: list[ToolCall]) -> Optional[WastePattern]:
    """Same output file written/edited more than once → prompt was underspecified."""
    file_counts: dict[str, int] = {}
    for call in tool_calls:
        if call.name in OUTPUT_TOOLS:
            fp = call.input_args.get("file_path") or call.input_args.get("path", "")
            if fp:
                file_counts[fp] = file_counts.get(fp, 0) + 1

    extra = sum(c - 1 for c in file_counts.values() if c > 1)
    if extra == 0:
        return None

    # Show only the filename (truncated), not the full path
    def _short(fp: str) -> str:
        name = Path(fp).name
        return name[:20] + "…" if len(name) > 20 else name

    files = [_short(fp) for fp, c in file_counts.items() if c > 1]
    file_str = ", ".join(files[:2])
    if len(files) > 2:
        file_str += f" +{len(files) - 2} more"
    times = "twice" if extra == 2 else f"{extra}x"
    return WastePattern(
        kind="backtracking",
        description=f"rewrote output {times}",
        missing_hint="→ describe the expected output before asking Claude to write",
        wasted_turns=extra,
    )


def _detect_clarification_loop(tool_calls: list[ToolCall]) -> Optional[WastePattern]:
    """3+ exploration calls before any output → prompt lacked context."""
    exploration_count = 0
    for call in tool_calls:
        if call.name in OUTPUT_TOOLS:
            break  # first output — stop counting
        if call.name in EXPLORATION_NO_OUTPUT:
            exploration_count += 1

    if exploration_count < 3:
        return None

    extra = exploration_count - 2  # first 2 are reasonable
    return WastePattern(
        kind="clarification_loop",
        description=f"{exploration_count} tool calls before first output",
        missing_hint="→ name the files and describe the change you want",
        wasted_turns=max(1, extra),
    )


def _canonical_key(call: ToolCall) -> str:
    """Stable key for deduplication: tool name + most discriminating argument."""
    name = call.name
    inp = call.input_args
    if name == "Read":
        sig = inp.get("file_path", inp.get("path", ""))
    elif name == "Grep":
        sig = f"{inp.get('pattern', inp.get('query', ''))}::{inp.get('path', inp.get('directory', ''))}"
    elif name == "Glob":
        sig = inp.get("pattern", "")
    elif name == "LS":
        sig = inp.get("path", inp.get("directory", ""))
    elif name in ("Write", "Edit", "MultiEdit"):
        sig = inp.get("file_path", inp.get("path", ""))
    elif name == "Bash":
        sig = inp.get("command", "")
    else:
        sig = json.dumps(inp, sort_keys=True)
    return f"{name}::{sig}"


def _detect_redundant_calls(prompt_tool_calls: list[ToolCall]) -> Optional[WastePattern]:
    """Identical (tool, args) repeated within this prompt's response turns."""
    key_counts: dict[str, int] = {}
    for call in prompt_tool_calls:
        k = _canonical_key(call)
        key_counts[k] = key_counts.get(k, 0) + 1

    redundant = sum(c - 1 for c in key_counts.values() if c > 1)
    if redundant == 0:
        return None

    # Find the most-repeated tool to name in the description
    worst_key = max((k for k, c in key_counts.items() if c > 1), key=lambda k: key_counts[k])
    tool_name = worst_key.split("::")[0].lower()
    worst_count = key_counts[worst_key]
    times_str = f"{worst_count} times"

    return WastePattern(
        kind="redundant_tool_calls",
        description=f"same {tool_name} called {times_str}",
        missing_hint="→ specify which directory or pattern to target",
        wasted_turns=redundant,
    )


# ---------------------------------------------------------------------------
# Cost calculation per prompt group
# ---------------------------------------------------------------------------

def _group_cost(response_entries: list[dict], model: str) -> float:
    total = 0.0
    for entry in response_entries:
        usage = entry.get("message", {}).get("usage", {})
        total += tokens_to_cost(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=model,
            cache_creation=usage.get("cache_creation_input_tokens", 0),
            cache_read=usage.get("cache_read_input_tokens", 0),
        )
    return total


# ---------------------------------------------------------------------------
# Main analysis pipeline
# ---------------------------------------------------------------------------

def analyze_session(path: Path) -> SessionReport:
    """Full analysis pipeline for a single session JSONL file."""
    human_entries, assistant_entries = parse_session(path)

    if not human_entries and not assistant_entries:
        raise ValueError(f"No parseable content in {path}")

    # Determine model and cwd from entries
    models = []
    cwds = []
    for e in assistant_entries + human_entries:
        m = e.get("message", {}).get("model", "")
        if m:
            models.append(m)
        c = e.get("cwd", "")
        if c:
            cwds.append(c)
    model = models[0] if models else "claude-sonnet-4-6"
    cwd = cwds[0] if cwds else ""

    # Timestamps
    all_entries = human_entries + assistant_entries
    timestamps = [_parse_ts(e.get("timestamp")) for e in all_entries]
    timestamps = [t for t in timestamps if t]
    first_ts = min(timestamps) if timestamps else datetime.now()
    last_ts = max(timestamps) if timestamps else datetime.now()
    duration_mins = (last_ts - first_ts).total_seconds() / 60

    # Total session cost (sum all assistant usage)
    total_cost = 0.0
    for e in assistant_entries:
        usage = e.get("message", {}).get("usage", {})
        total_cost += tokens_to_cost(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=model,
            cache_creation=usage.get("cache_creation_input_tokens", 0),
            cache_read=usage.get("cache_read_input_tokens", 0),
        )

    # Group by prompt
    groups = _group_by_prompt(human_entries, assistant_entries)

    prompt_analyses: list[PromptAnalysis] = []
    for i, (human_entry, response_entries) in enumerate(groups):
        prompt_text = _human_text(human_entry).strip()
        if not prompt_text or len(prompt_text) < 3:
            continue

        prompt_tool_calls = _extract_tool_calls(response_entries)
        prompt_cost = _group_cost(response_entries, model)

        patterns: list[WastePattern] = []

        bt = _detect_backtracking(prompt_tool_calls)
        if bt:
            patterns.append(bt)

        cl = _detect_clarification_loop(prompt_tool_calls)
        if cl:
            patterns.append(cl)

        rd = _detect_redundant_calls(prompt_tool_calls)
        if rd:
            patterns.append(rd)

        # Estimate wasted cost as fraction of prompt's total cost
        total_wasted_turns = sum(p.wasted_turns for p in patterns)
        total_response_turns = max(len(prompt_tool_calls), 1)
        waste_fraction = min(total_wasted_turns / total_response_turns, 0.75)
        wasted_cost = prompt_cost * waste_fraction

        prompt_analyses.append(PromptAnalysis(
            prompt_text=prompt_text,
            turn_index=i,
            tool_calls=prompt_tool_calls,
            patterns=patterns,
            wasted_cost=wasted_cost,
            total_cost=prompt_cost,
        ))

    total_wasted = sum(p.wasted_cost for p in prompt_analyses)

    # Session completion: ended with a write or meaningful bash (git/build)
    all_tool_calls = _extract_tool_calls(assistant_entries)
    is_complete = False
    for call in reversed(all_tool_calls):
        if call.name in OUTPUT_TOOLS:
            is_complete = True
            break
        if call.name == "Bash":
            cmd = call.input_args.get("command", "")
            if any(kw in cmd for kw in ("git commit", "git push", "git add", "npm run", "make ", "cargo build")):
                is_complete = True
            break

    return SessionReport(
        session_id=path.stem,
        session_path=path,
        project_name=session_project_name(path),
        timestamp=first_ts,
        duration_minutes=duration_mins,
        total_cost=total_cost,
        wasted_cost=total_wasted,
        model=model,
        prompt_analyses=prompt_analyses,
        is_complete=is_complete,
        cwd=cwd,
        session_end=last_ts,
    )
