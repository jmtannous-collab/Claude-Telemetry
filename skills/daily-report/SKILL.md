---
name: daily-report
description: Show the Claude Code telemetry report — sessions, interventions, autonomous vs blocked time, and token usage per skill. Use when the user asks for their session report, telemetry, daily usage summary, or how much time/tokens their sessions consumed.
---

Run the report script and show the user its output verbatim in a code block:

```
NO_COLOR=1 python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" $ARGUMENTS
```

(On Windows, if `python3` is not found, run the same command with `python`;
in cmd/PowerShell set the environment variable accordingly, e.g.
`$env:NO_COLOR=1` first.)

Supported options, to pass through when the user's request implies them:

- `--date YYYY-MM-DD` — a specific day (default: today)
- `--days N` — the last N days (e.g. "this week" → `--days 7`)
- `--sessions` — per-session detail
- `--log PATH` — alternate event log location

Each day includes a "by hour" line chart (one step curve per skill, showing
minutes of autonomous work per hour of the day). `NO_COLOR=1` makes each
curve use the box-drawing line style shown next to its skill in the "by
skill" table, which stays readable inside your code block — without it the
output contains raw ANSI color codes that render as garbage there. If the
user wants the colored version, tell them to run the same command themselves
by typing `!` followed by the command in the prompt (without `NO_COLOR=1`):
in Claude Code's terminal and in any regular terminal each curve then
renders in its skill's color.

After showing the output, add at most one or two sentences of observation
(e.g. an unusually high blocked ratio) — do not editorialize at length, and do
not recompute or restate the numbers in a different format.

If the script reports that no event log exists, tell the user the plugin's
hooks have not recorded anything yet and that a session restart after
installing the plugin may be needed for hooks to load.
