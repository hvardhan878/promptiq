# promptiq

## Your Impact/Cost Ratio

Every Claude Code session has a cost (dollars spent on tokens) and a value (lines shipped, tests added, commits that survived). Your **Impact/Cost ratio** divides value by cost — a ratio of 36x means you got 36 points of measurable output per dollar. It's the number that tells you whether your AI investment is working, or just burning budget on clarification loops.

```
┌─ PROMPTIQ ──────────────────────────────────────────────┐
│ auth-refactor · today 3:42pm · 47 min                   │
└─────────────────────────────────────────────────────────┘

  SPENT     $2.40
  WASTED    $1.22  ████████░░░░░░░░░░░░  51%
  IMPACT    87/100  12 files · 340 lines · tests ✓
  RATIO     36x    ▲ above your average (28x)

  ──────────────────────────────────────────────────────

  3 prompts killed this session

  1  "refactor the auth module"                    $0.52
     6 tool calls before first output
     → name the files and describe the change you want

  2  "fix the tests"                               $0.38
     same grep called 3 times
     → specify which directory or pattern to target

  3  "clean this up"                               $0.32
     rewrote output twice
     → describe the expected output before asking Claude to write

  ──────────────────────────────────────────────────────

  YOUR WEEK   best 91x · avg 28x · today 36x

  run promptiq --suggest to get rewrites in Claude Code
```

---

**Why promptiq exists:** Vague prompts cost real money — the AI spends turns reading files you could have named, rewriting code you could have spec'd. promptiq shows you exactly where your tokens went and what to say instead.

![terminal output demo](https://placehold.co/640x320/0c0c0c/888?text=promptiq+demo+GIF)

---

## Install & run in 30 seconds

```bash
pip install promptiq
promptiq
```

Zero config. Zero API key. Works on first run.

---

## Usage

```bash
promptiq            # analyse most recent session
promptiq --last 5   # last 5 sessions aggregated
promptiq --suggest  # generate rewrites, open in Claude Code
```

### --suggest flow

`promptiq --suggest` writes `promptiq-suggestions.md` to your current directory — your 3 worst prompts, their patterns, and a prompt-coach instruction block at the top. Then opens the file in Claude Code. You get codebase-aware rewrites with zero extra API cost.

---

## Architecture

- **`promptiq/core.py`** — JSONL parsing + waste detection; single file, typed dataclasses, no ML
- **`promptiq/git.py`** — git diff extraction, commit survival, `impact_score` formula (0–100)
- **`promptiq/display.py`** — Rich terminal output; three colours, spec-exact formatting
- **`promptiq/storage.py`** — SQLite at `~/.promptiq/sessions.db`; metadata only, never prompt text
- **`promptiq/cli.py`** — Click entry point; three flags, zero config, graceful git fallback

---

## How detection works

| Pattern | Signal | Root cause |
|---|---|---|
| **Clarification loop** | 3+ Read/Grep/LS calls before first Write | Prompt didn't name the files or target |
| **Backtracking** | Same file written/edited 2+ times | Prompt didn't specify the expected result |
| **Redundant tool calls** | Identical (tool, args) repeated in prompt | Prompt was ambiguous about scope |

Waste cost = that prompt's share of session tokens × number of wasted turns.

## Impact score

```python
def impact_score(v: SessionValue) -> int:
    base = min(50, (v.lines_changed * 0.1) + (v.files_touched * 2))
    bonus = 0
    if v.has_tests:     bonus += 20
    if v.was_committed: bonus += 20
    if v.survived_24h:  bonus += 15
    if v.was_reverted:  bonus -= 40
    return max(0, min(100, int(base + bonus)))
```

`RATIO = impact_score / session_cost` — higher is better.

---

## Privacy

promptiq never stores prompt text or code. `~/.promptiq/sessions.db` holds only: session ID, timestamp, cost totals, pattern counts, impact score, ratio.

---

## Roadmap

1. **Team tier** — aggregate waste across a team's sessions, shared pattern leaderboard
2. **Multi-agent support** — Codex CLI and Gemini CLI session format parsers
