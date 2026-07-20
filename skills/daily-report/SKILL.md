---
name: daily-report
description: Show the Claude Code telemetry report — sessions, interventions, autonomous vs blocked time, and token usage per skill. Use when the user asks for their session report, telemetry, daily usage summary, or how much time/tokens their sessions consumed.
---

Present the report to the user as a **colored inline image plus the plain-text
chart**. A relayed chat message renders as markdown and strips ANSI color, so
the color has to arrive as an image; the text version is kept for copy/paste
and search.

## Step 1 — render the colored image

Force truecolor output and pipe it through the PNG renderer:

```
env -u TERM_PROGRAM COLORTERM=truecolor python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" $ARGUMENTS | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ansi2png.py" "${TMPDIR:-/tmp}/claude_daily_report.png"
```

Then display `${TMPDIR:-/tmp}/claude_daily_report.png` inline (send it to the
user as a rendered image). The curves render in each skill's color — blue,
green, etc. — matching the colored dot next to that skill in the "by skill"
table.

`ansi2png.py` needs Pillow (`pip install pillow`). If it errors (Pillow
missing, or not on macOS where the bundled Menlo font lives), skip the image
and just show the text version from Step 2 — do not block the report on it.

## Step 2 — show the plain-text chart

Run the same report with color disabled and show its output verbatim in a
code block:

```
NO_COLOR=1 python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" $ARGUMENTS
```

With `NO_COLOR` each curve uses the box-drawing line style shown next to its
skill in the "by skill" table (── vs ┄┄ …), so overlapping curves stay
tellable-apart in monochrome.

(On Windows, if `python3` is not found, run the same commands with `python`;
in cmd/PowerShell set the environment variable separately, e.g.
`$env:NO_COLOR=1` first.)

## Options

Pass these through to **both** commands when the user's request implies them:

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
