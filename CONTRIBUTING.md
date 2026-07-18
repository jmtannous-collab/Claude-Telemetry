# Contributing

Issues and pull requests are welcome.

## Ground rules

- Keep the plugin dependency-free: Python 3.9+ standard library only, no
  network access, nothing leaves the user's machine.
- The event log (`events.jsonl`) is this project's public format — changes to
  its schema must stay backward-compatible or bump the plugin's major version.
- Claude Code's transcript format is internal to Claude Code; keep all
  transcript parsing isolated in `scripts/report.py` so breakage stays
  contained to the token columns.

## Testing a change

1. Install the plugin from your local checkout (`/plugin marketplace add
   <path-to-checkout>`, then `/plugin install`).
2. Run a short Claude Code session, invoke a skill, answer a prompt or two.
3. Check `~/.claude/session-metrics/events.jsonl` for the expected events.
4. Run `python3 scripts/report.py --sessions` and verify the numbers.
