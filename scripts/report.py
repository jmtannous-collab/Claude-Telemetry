#!/usr/bin/env python3
"""Daily telemetry report for Claude Code sessions.

Joins the hook-written event log (events.jsonl, this project's own stable
format) with Claude Code's session transcripts (internal format — parsed
defensively, only for token counts and usage-limit detection) to report,
per day:

  - number of sessions, user interventions, and usage-limit hits
  - autonomous work time, time blocked waiting on the user, and time lost
    to usage limits (from the limit hit until the limit reset or the next
    user prompt, whichever comes first)
  - autonomous time and token usage broken down per skill

Skill attribution is sticky: from the moment a skill is invoked, all later
autonomous time and tokens in that session belong to it until another skill
is invoked or the session ends.
"""

import argparse
import bisect
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict

UNATTRIBUTED = "(unattributed)"

# Matches the user-facing text of a usage-limit API error, across the
# phrasings Claude Code has used ("usage limit reached", "session limit
# reached", "5-hour limit reached", ...).
LIMIT_TEXT_RE = re.compile(
    r"\b(usage|session|5-hour|five-hour|weekly)\s+limit\b|\blimit reached\b",
    re.IGNORECASE,
)
ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?"
)


def default_log_path():
    log_dir = os.environ.get("CLAUDE_TELEMETRY_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "session-metrics"
    )
    return os.path.join(log_dir, "events.jsonl")


def parse_ts(s):
    ts = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


def local_date(ts):
    return ts.astimezone().date()


def is_main_agent(payload):
    # Hooks also fire inside subagents; their timeline events would corrupt
    # the work/blocked state machine, so only the main agent's events drive it.
    return payload.get("agent_type") in (None, "", "main")


def load_sessions(log_path):
    """Parse events.jsonl into {session_id: [events sorted by ts]}."""
    sessions = defaultdict(list)
    try:
        f = open(log_path)
    except FileNotFoundError:
        sys.exit(
            f"No event log at {log_path} — have the plugin's hooks run yet?"
        )
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                e["_ts"] = parse_ts(e["ts"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            sid = e.get("payload", {}).get("session_id") or "unknown"
            sessions[sid].append(e)
    for events in sessions.values():
        events.sort(key=lambda e: e["_ts"])
    return sessions


def _content_text(obj):
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def _is_limit_marker(obj):
    if obj.get("error") == "rate_limit":
        return True
    text = _content_text(obj)
    if not text:
        return False
    if obj.get("isApiErrorMessage") and "limit" in text.lower():
        return True
    # Fallback for Claude Code versions without error markers: a short
    # message naming a limit AND a reset. Ordinary conversation *about*
    # limits is long-form and lacks the reset phrasing, so it won't match.
    return (
        len(text) < 300
        and bool(LIMIT_TEXT_RE.search(text))
        and "reset" in text.lower()
    )


class Session:
    def __init__(self, session_id, events):
        self.session_id = session_id
        self.work = []  # (start, end, skill)
        self.blocked = []  # (start, end)
        self.interventions = []  # timestamps of UserPromptSubmit
        self.skill_timeline = []  # (ts, skill), chronological
        self.rate_limit_snapshots = []  # (ts, [reset datetimes])
        self.transcript_path = None
        self.cwd = None
        self.last_event_ts = events[-1]["_ts"] if events else None
        self._scanned = False
        self._usage_entries = []  # (ts, usage dict)
        self._limit_hits = []  # (ts, reset datetime or None)
        self._analyze(events)

    def _analyze(self, events):
        state = None  # None | "working" | "blocked"
        seg_start = None
        skill = UNATTRIBUTED

        for e in events:
            p = e.get("payload", {})
            ts = e["_ts"]
            self.transcript_path = p.get("transcript_path") or self.transcript_path
            self.cwd = p.get("cwd") or self.cwd
            limits = p.get("rate_limits")
            if isinstance(limits, dict):
                resets = []
                for window in limits.values():
                    if isinstance(window, dict) and window.get("resets_at"):
                        try:
                            resets.append(
                                dt.datetime.fromtimestamp(
                                    float(window["resets_at"]), dt.timezone.utc
                                )
                            )
                        except (TypeError, ValueError, OverflowError):
                            pass
                if resets:
                    self.rate_limit_snapshots.append((ts, resets))
            if not is_main_agent(p):
                continue
            name = e.get("event")

            if name == "SessionStart":
                if state is None:
                    state, seg_start = "working", ts
            elif name == "UserPromptSubmit":
                self.interventions.append(ts)
                if state == "blocked":
                    self.blocked.append((seg_start, ts))
                if state != "working":
                    state, seg_start = "working", ts
            elif name == "Stop":
                if state == "working":
                    self.work.append((seg_start, ts, skill))
                state, seg_start = "blocked", ts
            elif name == "SessionEnd":
                if state == "working":
                    self.work.append((seg_start, ts, skill))
                elif state == "blocked":
                    self.blocked.append((seg_start, ts))
                state, seg_start = None, None
            elif name == "PreToolUse" and p.get("tool_name") == "Skill":
                new_skill = (p.get("tool_input") or {}).get("skill")
                if new_skill and new_skill != skill:
                    if state == "working":
                        # Split the running segment at the skill boundary.
                        self.work.append((seg_start, ts, skill))
                        seg_start = ts
                    skill = new_skill
                    self.skill_timeline.append((ts, skill))

        # A still-open session: count up to its last recorded event.
        if events and state == "working" and seg_start is not None:
            self.work.append((seg_start, events[-1]["_ts"], skill))

    def skill_at(self, ts):
        keys = [t for t, _ in self.skill_timeline]
        i = bisect.bisect_right(keys, ts)
        return self.skill_timeline[i - 1][1] if i else UNATTRIBUTED

    def _scan_transcript(self):
        """One defensive pass over the session transcript, collecting token
        usage entries and usage-limit hits. Transcript problems only cost the
        token columns and limit detection — timing metrics are unaffected."""
        if self._scanned:
            return
        self._scanned = True
        if not self.transcript_path:
            return
        try:
            f = open(os.path.expanduser(self.transcript_path))
        except OSError:
            return
        with f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_s = obj.get("timestamp")
                if not ts_s:
                    continue
                try:
                    ts = parse_ts(ts_s)
                except ValueError:
                    continue
                if _is_limit_marker(obj):
                    self._limit_hits.append((ts, self._resolve_reset(ts, obj)))
                    continue
                if obj.get("type") != "assistant":
                    continue
                usage = (obj.get("message") or {}).get("usage")
                if usage:
                    self._usage_entries.append((ts, usage))

    def _resolve_reset(self, hit_ts, obj):
        """Best-effort reset time for a limit hit: an ISO timestamp in the
        error text, else the nearest logged rate_limits snapshot."""
        m = ISO_TS_RE.search(_content_text(obj))
        if m:
            try:
                reset = parse_ts(m.group(0))
                if reset > hit_ts:
                    return reset
            except ValueError:
                pass
        best = None
        for snap_ts, resets in self.rate_limit_snapshots:
            for reset in resets:
                if reset > hit_ts and (best is None or reset < best):
                    best = reset
        return best

    def limit_hits(self):
        self._scan_transcript()
        return self._limit_hits

    def token_usage(self, dates):
        """Per-skill token sums, filtered to the given set of local dates."""
        self._scan_transcript()
        totals = defaultdict(lambda: defaultdict(int))
        for ts, usage in self._usage_entries:
            if dates and local_date(ts) not in dates:
                continue
            t = totals[self.skill_at(ts)]
            t["in"] += usage.get("input_tokens", 0) or 0
            t["cache_w"] += usage.get("cache_creation_input_tokens", 0) or 0
            t["cache_r"] += usage.get("cache_read_input_tokens", 0) or 0
            t["out"] += usage.get("output_tokens", 0) or 0
        return totals


def merge_intervals(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def overlap_seconds(seg, intervals):
    a, b = seg
    total = 0.0
    for x, y in intervals:
        lo, hi = max(a, x), min(b, y)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def limited_intervals(day, day_sessions, all_sessions):
    """[start, end) intervals lost to usage limits on `day`.

    Each interval runs from the limit hit until the limit reset or the next
    user prompt anywhere (whichever comes first), clipped to the day.
    """
    tz = dt.datetime.now().astimezone().tzinfo
    day_start = dt.datetime.combine(day, dt.time.min).replace(tzinfo=tz)
    day_end = day_start + dt.timedelta(days=1)
    prompts = sorted(
        t for s in all_sessions.values() for t in s.interventions
    )
    intervals = []
    for s in day_sessions:
        for hit_ts, reset in s.limit_hits():
            if local_date(hit_ts) != day:
                continue
            candidates = [day_end]
            if reset:
                candidates.append(reset)
            i = bisect.bisect_right(prompts, hit_ts)
            if i < len(prompts):
                candidates.append(prompts[i])
            if reset is None and i >= len(prompts):
                # No reset time and the user never came back: count up to
                # the session's last recorded event.
                candidates.append(max(hit_ts, s.last_event_ts))
            end = min(candidates)
            if end > hit_ts:
                intervals.append((hit_ts, min(end, day_end)))
    return merge_intervals(intervals)


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m"
    return f"{seconds}s"


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_usage(t):
    total_in = t["in"] + t["cache_w"] + t["cache_r"]
    cached = t["cache_w"] + t["cache_r"]
    s = f"in {fmt_tokens(total_in)}"
    if cached:
        s += f" (cache {fmt_tokens(cached)})"
    return s + f" / out {fmt_tokens(t['out'])}"


def report_day(day, sessions, show_sessions):
    day_sessions = []
    for s in sessions.values():
        active = (
            any(local_date(t) == day for t in s.interventions)
            or any(local_date(a) == day for a, _, _ in s.work)
            or any(local_date(a) == day for a, _ in s.blocked)
        )
        if active:
            day_sessions.append(s)
    if not day_sessions:
        return False

    limited = limited_intervals(day, day_sessions, sessions)
    limit_hits = sum(
        1
        for s in day_sessions
        for hit_ts, _ in s.limit_hits()
        if local_date(hit_ts) == day
    )

    interventions = sum(
        1 for s in day_sessions for t in s.interventions if local_date(t) == day
    )
    work_by_skill = defaultdict(float)
    blocked_total = 0.0
    tokens_by_skill = defaultdict(lambda: defaultdict(int))
    for s in day_sessions:
        for a, b, skill in s.work:
            if local_date(a) == day:
                work_by_skill[skill] += (b - a).total_seconds()
        for a, b in s.blocked:
            if local_date(a) == day:
                # Time spent rate-limited is reported separately, not as
                # "blocked on you".
                blocked_total += (b - a).total_seconds() - overlap_seconds(
                    (a, b), limited
                )
        for skill, t in s.token_usage({day}).items():
            for k, v in t.items():
                tokens_by_skill[skill][k] += v
    blocked_total = max(0.0, blocked_total)
    limited_total = sum((b - a).total_seconds() for a, b in limited)

    n = len(day_sessions)
    header = (
        f"{day} — {n} session{'s' if n != 1 else ''}, "
        f"{interventions} intervention{'s' if interventions != 1 else ''}"
    )
    if limit_hits:
        header += f", {limit_hits} limit hit{'s' if limit_hits != 1 else ''}"
    print(header)
    line = (
        f"  autonomous {fmt_duration(sum(work_by_skill.values()))}"
        f" · blocked on you {fmt_duration(blocked_total)}"
    )
    if limited_total:
        line += f" · rate-limited {fmt_duration(limited_total)}"
    print(line)
    skills = sorted(work_by_skill, key=work_by_skill.get, reverse=True)
    if skills:
        print("  by skill:")
        width = max(len(sk) for sk in skills) + 2
        for sk in skills:
            line = f"    {sk:<{width}} {fmt_duration(work_by_skill[sk]):>7}"
            if sk in tokens_by_skill:
                line += f"   {fmt_usage(tokens_by_skill[sk])}"
            print(line)

    if show_sessions:
        print("  sessions:")
        far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        for s in sorted(
            day_sessions, key=lambda s: s.work[0][0] if s.work else far_future
        ):
            w = sum(
                (b - a).total_seconds()
                for a, b, _ in s.work
                if local_date(a) == day
            )
            bl = sum(
                max(
                    0.0,
                    (b - a).total_seconds() - overlap_seconds((a, b), limited),
                )
                for a, b in s.blocked
                if local_date(a) == day
            )
            iv = sum(1 for t in s.interventions if local_date(t) == day)
            label = os.path.basename(s.cwd) if s.cwd else s.session_id[:8]
            print(
                f"    · {label}: {iv} interventions, "
                f"autonomous {fmt_duration(w)}, blocked {fmt_duration(bl)}"
            )
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="report a specific day (YYYY-MM-DD)")
    ap.add_argument(
        "--days", type=int, default=1, help="report the last N days (default 1)"
    )
    ap.add_argument(
        "--sessions", action="store_true", help="include per-session detail"
    )
    ap.add_argument("--log", default=default_log_path(), help="event log path")
    args = ap.parse_args()

    sessions = {
        sid: Session(sid, events)
        for sid, events in load_sessions(args.log).items()
    }

    if args.date:
        days = [dt.date.fromisoformat(args.date)]
    else:
        today = dt.date.today()
        days = [today - dt.timedelta(days=i) for i in range(args.days)]
        days.reverse()

    printed = False
    for i, day in enumerate(days):
        if report_day(day, sessions, args.sessions):
            printed = True
            if i < len(days) - 1:
                print()
    if not printed:
        print("No recorded activity for the requested period.")


if __name__ == "__main__":
    main()
