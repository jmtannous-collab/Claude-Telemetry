# Claude-Telemetry

A [Claude Code](https://code.claude.com) plugin that answers, at the end of your day:

- **How long did my sessions last?**
- **How many times did I have to intervene?**
- **Between interventions, how long did the agent work autonomously — and how much of my day was the agent blocked, waiting on me?**
- **How much time did I lose to usage limits, and how often did I hit them?**
- **How many tokens did each stretch of work consume?**
- **How does all of that break down per skill** (e.g. a build session vs. a code review)?

```
2026-07-18 — 3 sessions, 11 interventions, 1 limit hit
  autonomous 4h10m · blocked on you 5h37m · rate-limited 33m
  by skill:
    ── doc-as-code:build-feature     1h49m   in 1.8M (cache 1.5M) / out 240k
    ══ simplify                      1h09m   in 620k (cache 510k) / out 85k
    ━━ code-review                     39m   in 310k (cache 250k) / out 40k
    ┄┄ (unattributed)                  33m   in 120k (cache 90k) / out 12k
  by hour:
  1h00m │                          ╭──╮                             ╔══╗
        │                          │  │                             ║  ║
    50m │                          │  │                             ║  ║
        │                          │  ╰──╮                          ║  ║
    40m │                          │     │        ┏━━┓              ║  ║
        │                          │     │        ┃  ┃              ║  ║
    30m │                          │     │        ┃  ┃              ║  ║     ╭┄┄┄
        │                          │     │        ┃  ┃              ║  ║     ┆
    20m │                          │     │        ┃  ┃              ║  ║     ┆
        │                          │     │        ┃  ┃              ║  ║     ┆
    10m │                          │     │        ┃  ┃              ║  ╚══╗  ┆
        │                          │     ╰──╮     ┃  ┃              ║     ║  ┆
      0 └──────────────────────────╯━━━━━━━━╰────────────────────────────────────
         0        3        6        9        12       15       18       21   (hour of day)
```

Each day's report ends with a **by-hour line chart of the 24-hour day**, in
the style of Claude Code's own `/stats` charts: one step curve per skill,
tracing how many minutes of each hour were autonomous work in that skill —
a long uninterrupted stretch shows up as a sustained high curve, and idle
skills rest on the baseline. In a terminal (including Claude Code's) the
curves are ANSI-colored, one color per skill — the same color marks the
skill in the by-skill table. Colors are assigned by each skill's first-ever
invocation, so a skill keeps its color across days and reports. With
`NO_COLOR=1` (or piped outside Claude Code) each curve falls back to its
own box-drawing line style, as in the example above;
`FORCE_COLOR`/`CLICOLOR_FORCE` are honored too.

Works on macOS, Linux, and Windows. Requires Python 3.9+ on `PATH` (as
`python3` or `python`); nothing else.

## How it works

Claude Code emits [hook events](https://code.claude.com/docs/en/hooks) at the moments that matter:

| Event | Meaning for telemetry |
|---|---|
| `SessionStart` / `SessionEnd` | Session boundaries |
| `UserPromptSubmit` | You intervened |
| `Stop` | The agent finished its turn and is now waiting on you |
| `Notification` | Permission prompt or 60s idle (recorded for future refinement) |
| `PreToolUse` (Skill) | A skill was invoked — starts a new attribution segment |

Every event also snapshots the `rate_limits` state Claude Code puts on hook
stdin (per-window `used_percentage` and `resets_at`). No hook fires at the
moment a usage limit is actually hit — the API error only lands in the
session transcript — so the report detects limit hits from the transcript's
error markers and resolves the reset time from the last snapshot.

This plugin's hooks append one timestamped line per event to a local monthly
log (`~/.claude/session-metrics/events-YYYY-MM.jsonl` — a stable format owned
by this project; old months can be deleted freely). The report script then
joins that timeline with Claude Code's own session transcripts (which carry
per-message token usage) to produce the daily summary.

Nothing leaves your machine. There is no network access, no server, no
dependency beyond Python 3.9+.

### Metric definitions

- **Autonomous time** — from each of your prompts (`UserPromptSubmit`) to the
  end of the agent's turn (`Stop`).
- **Blocked on you** — from `Stop` to your next prompt. If you walk away for
  the evening mid-session, that gap counts as blocked; interpret long tails
  accordingly.
- **Rate-limited** — from a usage-limit hit until the limit reset or your
  next prompt, whichever comes first (when no reset time can be resolved, a
  conservative five-hour cap applies). This time is carved *out* of "blocked
  on you", so the two never double-count.
- **Days are local calendar days.** Segments crossing midnight are split at
  midnight, so overnight work or waiting is booked to the day it actually
  happened on.
- **Interventions** — every prompt you submit, including the first one of a
  session.
- **Per-skill attribution is sticky** — from the moment a skill is invoked,
  all subsequent autonomous time and tokens in that session belong to it,
  until another skill is invoked or the session ends. Time before any skill
  invocation is `(unattributed)`. Subagent activity is attributed to the main
  session's current skill.

## Install

Add this repository as a plugin marketplace, then install the plugin:

```
/plugin marketplace add jmtannous-collab/Claude-Telemetry
/plugin install claude-telemetry@claude-telemetry
```

Hooks start logging as soon as the plugin is enabled (a session restart may be
required for hooks to load).

## Usage

Ask for your report with the bundled skill:

```
/claude-telemetry:daily-report            # today
/claude-telemetry:daily-report yesterday  # or: today, or a YYYY-MM-DD date
/claude-telemetry:daily-report --date 2026-07-17
/claude-telemetry:daily-report --days 7   # last 7 days
/claude-telemetry:daily-report --sessions # per-session detail
```

Or run the script directly, outside Claude Code:

```
python3 scripts/report.py yesterday --sessions
python3 scripts/report.py --days 7 --sessions
```

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `CLAUDE_TELEMETRY_DIR` | `~/.claude/session-metrics` | Where the monthly `events-YYYY-MM.jsonl` logs are written and read |

## Caveats

- **Transcript token parsing.** Claude Code's transcript JSONL format is
  internal and may change between versions. Autonomous/blocked timing comes
  entirely from this plugin's own event log and is unaffected; the token
  columns *and rate-limited detection* depend on transcript parsing, and the
  report degrades gracefully (other metrics still print) if parsing fails.
- **Mid-turn waits.** An `idle_prompt` notification (Claude finished and is
  waiting on you) now ends the autonomous turn just like a `Stop`, so a
  walk-away that ends without a recorded `Stop` counts as blocked, not as
  work. Permission prompts are different: Claude resumes the *same* turn after
  you approve, and with no "resumed" signal available the post-approval work
  can't be separated out — so time spent waiting at a permission prompt can
  still slightly undercount blocked time.
- **Limit detection is best-effort.** Limit hits are read from transcript
  error markers (`error: "rate_limit"` / `isApiErrorMessage`); conversation
  text is never trusted, so very old Claude Code versions without those
  markers may miss hits. Reset times come from the error text or a logged
  `rate_limits` snapshot — note that `rate_limits` on hook stdin is not part
  of the officially documented payload, so when it's absent the five-hour
  cap applies instead.
- **Crashed sessions.** A session with no `SessionEnd` keeps an open working
  tail (counted to its last event) but drops its trailing blocked time —
  without a session end there's no evidence you were still waiting.
- **Windows shells.** Hook commands try `python3` and fall back to `python`.
  `${CLAUDE_PLUGIN_ROOT}` substitution on Windows has known path-format
  quirks under Git Bash ([claude-code#18527](https://github.com/anthropics/claude-code/issues/18527));
  if events aren't being logged, check which shell your hooks run under.
- **Open sessions.** A session that hasn't ended yet is reported up to its
  last recorded event.

## License

[MIT](LICENSE)
