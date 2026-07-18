#!/usr/bin/env python3
"""Append one Claude Code hook event to the telemetry log.

Invoked by every hook in hooks/hooks.json. Reads the hook payload from stdin
and appends a single JSON line to the current month's log,
$CLAUDE_TELEMETRY_DIR/events-YYYY-MM.jsonl
(default dir: ~/.claude/session-metrics):

    {"ts": "<UTC ISO-8601>", "event": "<hook_event_name>", "payload": {...}}

Must never block or fail the session: any error exits 0 silently.
"""

import datetime
import json
import os
import sys

# Payload keys worth keeping. Everything else (prompt text, notification
# message bodies, tool arguments beyond the skill name) is dropped so the log
# stays small and free of conversation content.
KEEP_KEYS = (
    "hook_event_name",
    "session_id",
    "transcript_path",
    "cwd",
    "permission_mode",
    "agent_id",
    "agent_type",
    "tool_name",
    "notification_type",
    "source",
    "reason",
    # Structured usage-limit state (five_hour / seven_day windows with
    # used_percentage and resets_at) that Claude Code includes on hook stdin.
    # Snapshotting it on every event lets the report resolve reset times when
    # a limit is hit.
    "rate_limits",
)


def main():
    raw = sys.stdin.read()
    try:
        full = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        full = {}

    payload = {k: full[k] for k in KEEP_KEYS if k in full}

    # Notification text is short system-generated status (permission prompts,
    # idle, usage limits) — kept because limit detection needs it. It is the
    # only place a `message` field is retained.
    if full.get("hook_event_name") == "Notification" and "message" in full:
        payload["message"] = str(full["message"])[:500]

    # For Skill invocations keep the skill name (and args, which are short)
    # but not other tools' potentially huge inputs.
    if full.get("tool_name") == "Skill":
        tool_input = full.get("tool_input") or {}
        payload["tool_input"] = {
            k: v for k, v in tool_input.items() if k in ("skill", "args")
        }

    now = datetime.datetime.now(datetime.timezone.utc)
    line = {
        # Milliseconds so closely spaced events (Stop then PreToolUse) keep
        # their true order when the report sorts by timestamp.
        "ts": now.isoformat(timespec="milliseconds"),
        "event": full.get("hook_event_name", "unknown"),
        "payload": payload,
    }

    log_dir = os.environ.get("CLAUDE_TELEMETRY_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "session-metrics"
    )
    os.makedirs(log_dir, exist_ok=True)
    # One file per month bounds unbounded growth and makes pruning trivial;
    # the report reads every events*.jsonl in the directory.
    log_name = f"events-{now:%Y-%m}.jsonl"
    with open(os.path.join(log_dir, log_name), "a", encoding="utf-8") as f:
        f.write(json.dumps(line, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
