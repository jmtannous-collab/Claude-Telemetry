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
  autonomous 4h32m · blocked on you 38m · rate-limited 2h29m
  by skill:
    doc-as-code:build-feature   3h10m   in 1.8M (cache 1.5M) / out 240k
    code-review                   41m   in 620k (cache 510k) / out 85k
    (unattributed)                41m   in 310k (cache 250k) / out 40k
```

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

This plugin's hooks append one timestamped line per event to a local log
(`~/.claude/session-metrics/events.jsonl` — a stable format owned by this
project). The report script then joins that timeline with Claude Code's own
session transcripts (which carry per-message token usage) to produce the daily
summary.

Nothing leaves your machine. There is no network access, no server, no
dependency beyond Python 3.9+.

### Metric definitions

- **Autonomous time** — from each of your prompts (`UserPromptSubmit`) to the
  end of the agent's turn (`Stop`).
- **Blocked on you** — from `Stop` to your next prompt. If you walk away for
  the evening mid-session, that gap counts as blocked; interpret long tails
  accordingly.
- **Rate-limited** — from a usage-limit hit until the limit reset or your
  next prompt, whichever comes first. This time is carved *out* of "blocked
  on you", so the two never double-count.
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
/claude-telemetry:daily-report --date 2026-07-17
/claude-telemetry:daily-report --days 7   # last 7 days
/claude-telemetry:daily-report --sessions # per-session detail
```

Or run the script directly, outside Claude Code:

```
python3 scripts/report.py --days 7 --sessions
```

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `CLAUDE_TELEMETRY_DIR` | `~/.claude/session-metrics` | Where `events.jsonl` is written and read |

## Caveats

- **Transcript token parsing.** Claude Code's transcript JSONL format is
  internal and may change between versions. Timing metrics come entirely from
  this plugin's own event log and are unaffected; only the token columns
  depend on transcript parsing, and the report degrades gracefully (times
  still print) if parsing fails.
- **Mid-turn permission prompts** are also "waiting on you" but occur before
  `Stop` fires. `Notification` events are logged today and will be folded into
  blocked time in a future version; until then blocked time slightly
  undercounts.
- **Limit detection is best-effort.** Limit hits are read from transcript
  error markers (`error: "rate_limit"` / `isApiErrorMessage`), with a
  conservative text fallback for older Claude Code versions. If a reset time
  can't be found in the error text or a `rate_limits` snapshot, the limited
  interval ends at your next prompt.
- **Windows shells.** Hook commands try `python3` and fall back to `python`.
  `${CLAUDE_PLUGIN_ROOT}` substitution on Windows has known path-format
  quirks under Git Bash ([claude-code#18527](https://github.com/anthropics/claude-code/issues/18527));
  if events aren't being logged, check which shell your hooks run under.
- **Open sessions.** A session that hasn't ended yet is reported up to its
  last recorded event.

## License

[MIT](LICENSE)
