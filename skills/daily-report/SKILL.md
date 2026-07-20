---
name: daily-report
description: Show the Claude Code telemetry report — sessions, interventions, autonomous vs blocked time, and token usage per skill. Use when the user asks for their session report, telemetry, daily usage summary, or how much time/tokens their sessions consumed.
---

Run the report and show its output **verbatim in a code block**.

A relayed chat message renders as markdown, which strips ANSI color, so the
report is run with `NO_COLOR=1`: each skill's curve then uses a distinct
box-drawing line style (`──`, `━━`, `══`, `┅┅`, `┈┈`; `┄┄` for unattributed)
instead of relying on color. The same style marks the skill in the "by skill"
table, so overlapping curves in the "by hour" chart stay tellable-apart in
monochrome.

```
NO_COLOR=1 python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" $ARGUMENTS
```

(On Windows, if `python3` is not found, use `python`; in cmd/PowerShell set
the variable separately first, e.g. `$env:NO_COLOR=1`.)

## Options

Pass these through when the user's request implies them:

- `--date YYYY-MM-DD` — a specific day (default: today)
- `--days N` — the last N days (e.g. "this week" → `--days 7`)
- `--sessions` — per-session detail
- `--log PATH` — alternate event log location

## After showing the output

Add at most one or two sentences of observation (e.g. an unusually high
blocked ratio) — do not editorialize at length, and do not recompute or
restate the numbers in a different format.

If the script reports that no event log exists, tell the user the plugin's
hooks have not recorded anything yet and that a session restart after
installing the plugin may be needed for hooks to load.
