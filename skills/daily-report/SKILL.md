---
name: daily-report
description: Show the Claude Code telemetry report — sessions, interventions, autonomous vs blocked time, and token usage per skill. Use when the user asks for their session report, telemetry, daily usage summary, or how much time/tokens their sessions consumed.
---

Run the report script and show the user its output verbatim in a code block:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" $ARGUMENTS
```

(On Windows, if `python3` is not found, run the same command with `python`.)

Supported options, to pass through when the user's request implies them:

- `--date YYYY-MM-DD` — a specific day (default: today)
- `--days N` — the last N days (e.g. "this week" → `--days 7`)
- `--sessions` — per-session detail
- `--log PATH` — alternate event log location

Each day includes a "by hour" line chart (one step curve per skill, showing
minutes of autonomous work per hour of the day). Each curve renders in its
skill's color, matching the colored dot next to that skill in the "by skill"
table; the box-drawing line style still distinguishes curves where colors
are unavailable. Color renders only when the report is written straight to a
terminal — so when you run this command via a tool and relay its output into
a message, the ANSI codes are not interpreted and appear as raw
`[38;2;...m` sequences. For the true colored chart, the user should run the
command themselves by typing `!` followed by the command in the prompt; in
Claude Code's terminal and in any regular terminal each curve then renders in
its skill's color.

After showing the output, add at most one or two sentences of observation
(e.g. an unusually high blocked ratio) — do not editorialize at length, and do
not recompute or restate the numbers in a different format.

If the script reports that no event log exists, tell the user the plugin's
hooks have not recorded anything yet and that a session restart after
installing the plugin may be needed for hooks to load.
