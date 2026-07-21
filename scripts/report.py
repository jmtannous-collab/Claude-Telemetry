#!/usr/bin/env python3
"""Daily telemetry report for Claude Code sessions.

Joins the hook-written event log (events-*.jsonl, this project's own stable
format) with Claude Code's session transcripts (internal format — parsed
defensively, only for token counts and usage-limit detection) to report,
per day:

  - number of sessions, user interventions, and usage-limit hits
  - autonomous work time, time blocked waiting on the user, and time lost
    to usage limits (from the limit hit until the limit reset or the next
    user prompt, whichever comes first)
  - autonomous time and token usage broken down per skill

Each day's report includes a terminal line chart of the 24 hours: one step
curve per skill, showing how many minutes of each hour were autonomous work
in that skill (curves rest on the baseline while a skill is idle). Curves
are colored (ANSI) on a terminal and fall back to per-skill line styles
when piped; the same color/style marks the skill in the by-skill table, and
a skill keeps its color across days and reports.

Skill attribution is sticky: from the moment a skill is invoked, all later
autonomous time and tokens in that session belong to it until another skill
is invoked or the session ends.
"""

import argparse
import bisect
import datetime as dt
import glob
import json
import os
import re
import sys
from collections import defaultdict

UNATTRIBUTED = "(unattributed)"

# When a usage-limit hit has no resolvable reset time, assume at most the
# five-hour window rather than writing off the rest of the day.
NO_RESET_CAP = dt.timedelta(hours=5)

LIMIT_TEXT_RE = re.compile(
    r"\b(usage|session|5-hour|five-hour|weekly)\s+limit\b|\blimit reached\b",
    re.IGNORECASE,
)
ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?"
)
# Older error format: "Claude AI usage limit reached|1721318400"
EPOCH_TS_RE = re.compile(r"\|\s*(\d{9,12})\b")
# Current inline format: "…resets 12:40pm (America/Toronto)" — a wall-clock
# time in a named zone, with no date. Groups: hour, optional minute, am/pm,
# zone name.
RESET_LOCAL_RE = re.compile(
    r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


def _parse_local_reset(text, hit_ts):
    """Resolve a "resets H[:MM]am/pm (Zone)" phrase to the next occurrence of
    that local wall-clock time at or after the hit, returned as an aware UTC
    datetime. None if the phrase is absent, malformed, or the zone can't be
    loaded (zoneinfo/tzdata missing)."""
    m = RESET_LOCAL_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        hour += 12
    minute = int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(m.group(4).strip())
    except Exception:
        return None
    local_hit = hit_ts.astimezone(tz)
    cand = local_hit.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= local_hit:
        # The stated time has already passed today, so it's tomorrow's.
        cand += dt.timedelta(days=1)
    return cand.astimezone(dt.timezone.utc)


def default_log_location():
    return os.environ.get("CLAUDE_TELEMETRY_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "session-metrics"
    )


def parse_ts(s):
    ts = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


def local_date(ts):
    return ts.astimezone().date()


def local_day_bounds(day):
    """Aware [start, end) of a local calendar day. `.astimezone()` on the
    naive local midnight attaches that date's own UTC offset, so days across
    a DST change keep the same boundaries as local_date()."""
    start = dt.datetime.combine(day, dt.time.min).astimezone()
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min).astimezone()
    return start, end


def split_by_local_day(a, b):
    """Split an aware interval at local midnights so each piece can be
    bucketed to a single day without cross-midnight distortion."""
    parts = []
    m = local_day_bounds(local_date(a))[1]
    while m < b:
        parts.append((a, m))
        a, m = m, local_day_bounds(local_date(m))[1]
    parts.append((a, b))
    return parts


def is_main_agent(payload):
    # Hooks also fire inside subagents; their timeline events would corrupt
    # the work/blocked state machine. Per the hooks docs, agent_id is set
    # only inside subagent invocations (agent_type also appears for main
    # sessions launched via `claude --agent`, so it can't be the filter).
    return not payload.get("agent_id")


def load_sessions(location):
    """Parse the event log(s) into {session_id: [events sorted by ts]}.

    `location` is the telemetry directory (all events*.jsonl inside it,
    including the legacy undated file) or a single log file.
    """
    if os.path.isdir(location):
        paths = sorted(glob.glob(os.path.join(location, "events*.jsonl")))
    else:
        paths = [location] if os.path.exists(location) else []
    if not paths:
        sys.exit(
            f"No event log found at {location} — have the plugin's hooks run yet?"
        )
    sessions = defaultdict(list)
    for path in paths:
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
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
                sid = e.get("payload", {}).get("session_id")
                if not sid:
                    continue
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
    """True for transcript entries recording a usage-limit exhaustion.

    Requires Claude Code's structured error markers (error/isApiErrorMessage)
    — plain conversation text is never trusted, since users and agents talk
    *about* limits. Transient auto-retried rate limits and unrelated token
    limits are excluded.
    """
    text = _content_text(obj).lower()
    if "retrying" in text or "output token" in text:
        return False
    if obj.get("error") == "rate_limit":
        return True
    if not obj.get("isApiErrorMessage") or obj.get("type") != "assistant":
        return False
    return bool(LIMIT_TEXT_RE.search(text)) or (
        "limit" in text and "reset" in text
    )


class Session:
    def __init__(self, session_id, events):
        self.session_id = session_id
        self.work = []  # (start, end, skill), split at local midnights
        self.blocked = []  # (start, end), split at local midnights
        self.interventions = []  # timestamps of UserPromptSubmit
        self.skill_timeline = []  # (ts, skill), chronological
        self.rate_limit_snapshots = []  # (ts, [reset datetimes])
        self.transcript_path = None
        self.cwd = None
        self.first_event_ts = events[0]["_ts"] if events else None
        self._scanned = False
        self._usage_entries = []  # (ts, dedup key or None, usage dict)
        self._limit_hits = []  # (ts, reset datetime or None)
        self._analyze(events)
        self._skill_keys = [t for t, _ in self.skill_timeline]

    def _analyze(self, events):
        state = None  # None | "working" | "blocked"
        seg_start = None
        skill = UNATTRIBUTED

        for e in events:
            p = e.get("payload", {})
            ts = e["_ts"]
            if is_main_agent(p):
                # Only main-agent events may set these — a subagent event
                # carrying its own transcript_path must not clobber the
                # session's transcript.
                self.transcript_path = (
                    p.get("transcript_path") or self.transcript_path
                )
                self.cwd = p.get("cwd") or self.cwd
            if not is_main_agent(p):
                continue
            # Rate-limit snapshots are read only from main-agent events, for
            # the same reason as above: subagent events must not drive
            # session-level state. Limits are account-wide, so the main
            # timeline still observes every window that matters.
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
            name = e.get("event")

            if name == "SessionStart":
                # Session boundary only. Work starts at the first prompt —
                # a terminal sitting open before that is neither work nor
                # blocked time.
                pass
            elif name == "UserPromptSubmit":
                self.interventions.append(ts)
                if state == "blocked":
                    self.blocked.append((seg_start, ts))
                if state != "working":
                    state, seg_start = "working", ts
            elif name == "Stop":
                if state == "working":
                    self.work.append((seg_start, ts, skill))
                if state != "blocked":
                    # Repeated Stops (compaction turns, re-fires) must not
                    # restart the blocked segment.
                    state, seg_start = "blocked", ts
            elif name == "Notification":
                # A turn can end without a recorded Stop (the hook can miss,
                # or the user walks away mid-turn). An idle_prompt fires when
                # Claude has finished and is waiting on the user, so it marks
                # the autonomous turn as over just like a Stop. Without this,
                # a walk-away — sometimes over an hour — is silently counted
                # as autonomous work. If a Stop already fired, state is
                # "blocked" and this is a no-op. Work resumes on the next
                # UserPromptSubmit.
                #
                # permission_prompt is deliberately NOT handled here: Claude
                # resumes the *same* turn after approval, and with only Skill
                # PreToolUse logged (no PostToolUse) there is no signal for
                # when work resumes — so treating it as blocked would reclas-
                # sify the genuine post-approval work as "blocked on you".
                if state == "working" and (
                    p.get("notification_type") == "idle_prompt"
                ):
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

        # A still-open session: count an open working tail up to the last
        # recorded event. A trailing *blocked* state is intentionally not
        # counted — without a SessionEnd there is no evidence of when (or
        # whether) the user was still waiting.
        if events and state == "working" and seg_start is not None:
            self.work.append((seg_start, events[-1]["_ts"], skill))

        self.work = [
            (a2, b2, sk)
            for a, b, sk in self.work
            for a2, b2 in split_by_local_day(a, b)
        ]
        self.blocked = [
            (a2, b2)
            for a, b in self.blocked
            for a2, b2 in split_by_local_day(a, b)
        ]

    def skill_at(self, ts):
        i = bisect.bisect_right(self._skill_keys, ts)
        return self.skill_timeline[i - 1][1] if i else UNATTRIBUTED

    def _scan_transcript(self):
        """One defensive pass over the session transcript, collecting token
        usage entries and usage-limit hits. Transcript problems only cost
        the token columns and limit detection — the other timing metrics
        come from the event log and are unaffected."""
        if self._scanned:
            return
        self._scanned = True
        if not self.transcript_path:
            return
        try:
            f = open(
                os.path.expanduser(self.transcript_path),
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return
        seen_keys = set()
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
                if obj.get("type") == "assistant":
                    usage = (obj.get("message") or {}).get("usage")
                    if usage:
                        # Transcripts write one line per content block, each
                        # repeating the message's id and usage — count each
                        # API message once.
                        key = (
                            (obj.get("message") or {}).get("id"),
                            obj.get("requestId"),
                        )
                        if key == (None, None):
                            self._usage_entries.append((ts, None, usage))
                        elif key not in seen_keys:
                            seen_keys.add(key)
                            self._usage_entries.append((ts, key, usage))
                if _is_limit_marker(obj):
                    self._limit_hits.append((ts, self._resolve_reset(ts, obj)))

    def _resolve_reset(self, hit_ts, obj):
        """Best-effort reset time for a limit hit: a timestamp in the error
        text (ISO, trailing |epoch, or a "resets H:MMam (Zone)" wall-clock),
        else the rate_limits snapshot in effect at the hit."""
        text = _content_text(obj)
        m = ISO_TS_RE.search(text)
        if m:
            try:
                reset = parse_ts(m.group(0))
                if reset > hit_ts:
                    return reset
            except ValueError:
                pass
        m = EPOCH_TS_RE.search(text)
        if m:
            try:
                reset = dt.datetime.fromtimestamp(
                    int(m.group(1)), dt.timezone.utc
                )
                if reset > hit_ts:
                    return reset
            except (ValueError, OverflowError, OSError):
                pass
        local = _parse_local_reset(text, hit_ts)
        if local and local > hit_ts:
            return local
        return self._snapshot_reset(hit_ts)

    def _snapshot_reset(self, hit_ts):
        """Earliest future reset from the latest snapshot at or before the
        hit; snapshots recorded later (fresh windows) only as a fallback."""
        before, after = None, None
        for snap_ts, resets in self.rate_limit_snapshots:
            if snap_ts <= hit_ts:
                before = resets
            elif after is None:
                after = resets
        for resets in (before, after):
            if resets:
                future = [r for r in resets if r > hit_ts]
                if future:
                    return min(future)
        return None

    def limit_hits(self):
        self._scan_transcript()
        return self._limit_hits

    def token_usage(self, dates):
        """Per-skill token sums, filtered to the given set of local dates.
        Run dedupe_shared_history() first so resumed sessions don't recount
        copied history."""
        self._scan_transcript()
        totals = defaultdict(lambda: defaultdict(int))
        for ts, _key, usage in self._usage_entries:
            if dates and local_date(ts) not in dates:
                continue
            t = totals[self.skill_at(ts)]
            t["in"] += usage.get("input_tokens", 0) or 0
            t["cache_w"] += usage.get("cache_creation_input_tokens", 0) or 0
            t["cache_r"] += usage.get("cache_read_input_tokens", 0) or 0
            t["out"] += usage.get("output_tokens", 0) or 0
        return totals


def dedupe_shared_history(sessions):
    """Resuming a session copies its history into a new transcript under a
    new session id. Walk sessions chronologically so the original session
    keeps its messages and limit hits, and later copies are dropped."""
    far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    seen_usage, seen_hits = set(), set()
    for s in sorted(
        sessions.values(), key=lambda s: s.first_event_ts or far_future
    ):
        s._scan_transcript()
        kept_usage = []
        for ts, key, usage in s._usage_entries:
            if key is not None:
                if key in seen_usage:
                    continue
                seen_usage.add(key)
            kept_usage.append((ts, key, usage))
        s._usage_entries = kept_usage
        kept_hits = []
        for ts, reset in s._limit_hits:
            if ts in seen_hits:
                continue
            seen_hits.add(ts)
            kept_hits.append((ts, reset))
        s._limit_hits = kept_hits


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


def limited_intervals(day, sessions):
    """[start, end) intervals lost to usage limits, clipped to `day`.

    Each interval runs from the limit hit until the limit reset or the next
    user prompt anywhere (whichever comes first); with no known reset, a
    conservative five-hour cap applies. Hits are taken from all sessions so
    an evening hit still surfaces on the following day.
    """
    day_start, day_end = local_day_bounds(day)
    prompts = sorted(t for s in sessions.values() for t in s.interventions)
    intervals = []
    for s in sessions.values():
        for hit_ts, reset in s.limit_hits():
            end = reset if reset else hit_ts + NO_RESET_CAP
            i = bisect.bisect_right(prompts, hit_ts)
            if i < len(prompts):
                end = min(end, prompts[i])
            a, b = max(hit_ts, day_start), min(end, day_end)
            if b > a:
                intervals.append((a, b))
    return merge_intervals(intervals)


# Categorical palette (hex), validated for CVD separation and dark-surface
# contrast. Slots are assigned to skills in order of first-ever invocation
# across the whole log, so a skill keeps its color across days and reports
# as history grows.
GRAPH_COLORS = [
    "#3987e5",  # blue
    "#008300",  # green
    "#d55181",  # magenta
    "#c98500",  # yellow
    "#199e70",  # aqua
    "#d95926",  # orange
    "#9085e9",  # violet
    "#e66767",  # red
]
GRAPH_MUTED = "#898781"  # (unattributed) and any skill past the 8 slots
ANSI_RESET = "\x1b[0m"

# Box-drawing character sets for the step curves. With color every skill
# uses the rounded set (color carries identity); without color the sets
# cycle so overlapping curves stay tellable-apart.
LINE_SETS = [
    {"h": "─", "v": "│", "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯"},
    {"h": "━", "v": "┃", "tl": "┏", "tr": "┓", "bl": "┗", "br": "┛"},
    {"h": "═", "v": "║", "tl": "╔", "tr": "╗", "bl": "╚", "br": "╝"},
    {"h": "┅", "v": "┇", "tl": "┏", "tr": "┓", "bl": "┗", "br": "┛"},
    {"h": "┈", "v": "┊", "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯"},
]
MUTED_SET = {"h": "┄", "v": "┆", "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯"}


def assign_skill_slots(sessions):
    """{skill: slot index or None} by first invocation anywhere in the log.
    None means muted gray. UNATTRIBUTED is always muted so real skills never
    shift color when unattributed time appears."""
    first_seen = {}
    for s in sessions.values():
        for ts, skill in s.skill_timeline:
            if skill not in first_seen or ts < first_seen[skill]:
                first_seen[skill] = ts
    slots = {UNATTRIBUTED: None}
    for i, skill in enumerate(sorted(first_seen, key=first_seen.get)):
        slots[skill] = i if i < len(GRAPH_COLORS) else None
    return slots


def _fmt_minutes(m):
    return "0" if m == 0 else fmt_duration(m * 60)


def color_enabled():
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
        return True
    # Claude Code captures command output through a pipe but renders it in a
    # terminal that understands ANSI, so color is safe there despite no TTY.
    return sys.stdout.isatty() or bool(os.environ.get("CLAUDECODE"))


def _hex_rgb(hex_color):
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def _truecolor_supported():
    """Whether the terminal renders 24-bit color. Apple Terminal.app sets
    COLORTERM but only supports the 256-color palette, so exclude it
    explicitly rather than trusting COLORTERM alone."""
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return False
    return os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


_CUBE = (0, 95, 135, 175, 215, 255)


def _nearest_256(hex_color):
    """Nearest xterm-256 index for a hex color, choosing between the 6x6x6
    color cube and the 24-step gray ramp — universally supported, unlike
    24-bit truecolor."""
    r, g, b = _hex_rgb(hex_color)

    def cube_idx(v):
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))

    ri, gi, bi = cube_idx(r), cube_idx(g), cube_idx(b)
    cube = 16 + 36 * ri + 6 * gi + bi
    cr, cg, cb = _CUBE[ri], _CUBE[gi], _CUBE[bi]

    gray = round((r + g + b) / 3)
    gramp = 232 + max(0, min(23, round((gray - 8) / 10)))
    gv = 8 + 10 * (gramp - 232)

    d_cube = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
    d_gray = (r - gv) ** 2 + (g - gv) ** 2 + (b - gv) ** 2
    return gramp if d_gray < d_cube else cube


def _paint(s, hex_color):
    if _truecolor_supported():
        r, g, b = _hex_rgb(hex_color)
        return f"\x1b[38;2;{r};{g};{b}m{s}{ANSI_RESET}"
    return f"\x1b[38;5;{_nearest_256(hex_color)}m{s}{ANSI_RESET}"


def _skill_hex(skill, slots):
    slot = slots.get(skill)
    return GRAPH_MUTED if slot is None else GRAPH_COLORS[slot]


def _skill_line_set(skill, slots, color):
    slot = slots.get(skill)
    if slot is None:
        return LINE_SETS[0] if color else MUTED_SET
    return LINE_SETS[0] if color else LINE_SETS[slot % len(LINE_SETS)]


def skill_key(skill, slots, color):
    """Two-character visual key for a skill: a colored dot on terminals, a
    sample of the skill's line style otherwise. The same identity marks the
    chart curve and the by-skill table."""
    if color:
        return _paint("● ", _skill_hex(skill, slots))
    return _skill_line_set(skill, slots, color)["h"] * 2


def print_day_chart(day, day_sessions, slots, color, limited=()):
    """One step curve per skill over the day's 24 hours: minutes of that
    hour spent working autonomously in the skill. Curves sit on the
    baseline where a skill was idle, /stats-style. Rate-limited time is
    excluded so the curves match the by-skill autonomous totals."""
    _, day_end = local_day_bounds(day)
    # Hour-bucket edges as local wall-clock hours, made aware with
    # .astimezone() (the local_day_bounds idiom). Across a DST change each
    # bucket then spans its true real duration — a fall-back hour is 2h wide,
    # a spring-forward gap is empty — so the 24 buckets always tile the actual
    # local day and the 0..23 hour labels stay aligned with their data.
    edges = [
        dt.datetime.combine(day, dt.time(hour=h)).astimezone() for h in range(24)
    ] + [day_end]
    series = defaultdict(lambda: [0.0] * 24)  # skill -> minutes per hour
    for s in day_sessions:
        for a, b, skill in s.work:
            if local_date(a) != day:
                continue
            for h in range(24):
                lo, hi = max(a, edges[h]), min(b, edges[h + 1])
                if hi > lo:
                    mins = (
                        (hi - lo).total_seconds()
                        - overlap_seconds((lo, hi), limited)
                    ) / 60
                    if mins > 0:
                        series[skill][h] += mins
    peak = max((max(v) for v in series.values()), default=0)
    if peak <= 0:
        return

    for tick in (5, 10, 15, 30, 60):
        if peak / tick <= 6:
            break
    top = max(tick, int(-(-peak // tick) * tick))
    unit = tick / 2  # minutes per chart row; labels sit on even rows
    nrows = round(top / unit)
    width = 24 * 3

    order = sorted(
        series,
        key=lambda sk: (slots.get(sk) is None, slots.get(sk) or 0, sk),
    )
    # canvas[level][col] = (glyph, skill); level 0 is the baseline row.
    # Curves are drawn in reverse slot order so earlier slots end up on top
    # where they overlap.
    canvas = [[None] * width for _ in range(nrows + 1)]
    for sk in reversed(order):
        chars = _skill_line_set(sk, slots, color)

        def put(level, col, part):
            canvas[level][col] = (chars[part], sk)

        lv = [min(nrows, round(v / unit)) for v in series[sk]]
        for h in range(24):
            c, y = h * 3, lv[h]
            put(y, c, "h")
            put(y, c + 1, "h")
            nxt = lv[h + 1] if h < 23 else y
            if nxt == y:
                put(y, c + 2, "h")
            elif nxt > y:  # rising step
                put(y, c + 2, "br")
                for yy in range(y + 1, nxt):
                    put(yy, c + 2, "v")
                put(nxt, c + 2, "tl")
            else:  # falling step
                put(y, c + 2, "tr")
                for yy in range(nxt + 1, y):
                    put(yy, c + 2, "v")
                put(nxt, c + 2, "bl")

    def cell(entry):
        glyph, sk = entry
        return _paint(glyph, _skill_hex(sk, slots)) if color else glyph

    print("  by hour:")
    for r in range(nrows, 0, -1):
        label = _fmt_minutes(int(r * unit)) if r % 2 == 0 else ""
        row = "".join(cell(c) if c else " " for c in canvas[r])
        print(f"  {label:>5} │{row.rstrip()}")
    base = "".join(
        cell(c) if c else (_paint("─", GRAPH_MUTED) if color else "─")
        for c in canvas[0]
    )
    print(f"  {'0':>5} └{base}")
    axis = [" "] * width
    for h in range(0, 24, 3):
        for i, ch in enumerate(str(h)):
            axis[h * 3 + i] = ch
    print("         " + "".join(axis).rstrip() + "   (hour of day)")


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
    # `total_in` sums input across every turn, so in a long agentic session it
    # is dominated by cache reads — the same context re-billed each turn. To
    # keep that from reading as "150M of information came in", also show the
    # first-seen volume (uncached input + cache writes) and the cache hit rate
    # (share of input served from cache). The two reconcile: new ~= total_in x
    # (1 - hit).
    total_in = t["in"] + t["cache_w"] + t["cache_r"]
    new_in = t["in"] + t["cache_w"]
    s = f"in {fmt_tokens(total_in)}"
    # Only cache *reads* inflate total_in above the first-seen volume, so the
    # breakdown is shown only when there are reads (which also guarantees
    # total_in > 0). Without reads, new_in == total_in and the clause is noise.
    if t["cache_r"]:
        hit = round(100 * t["cache_r"] / total_in)
        s += f" (new {fmt_tokens(new_in)} · {hit}% cached)"
    return s + f" / out {fmt_tokens(t['out'])}"


def report_day(day, sessions, show_sessions, slots):
    day_sessions = []
    for s in sessions.values():
        active = (
            any(local_date(t) == day for t in s.interventions)
            or any(local_date(a) == day for a, _, _ in s.work)
            or any(local_date(a) == day for a, _ in s.blocked)
        )
        if active:
            day_sessions.append(s)

    limited = limited_intervals(day, sessions)
    limit_hits = sum(
        1
        for s in sessions.values()
        for hit_ts, _ in s.limit_hits()
        if local_date(hit_ts) == day
    )
    if not day_sessions and not limited and not limit_hits:
        return False

    interventions = sum(
        1 for s in day_sessions for t in s.interventions if local_date(t) == day
    )
    work_by_skill = defaultdict(float)
    blocked_total = 0.0
    tokens_by_skill = defaultdict(lambda: defaultdict(int))
    for s in day_sessions:
        for a, b, skill in s.work:
            if local_date(a) == day:
                # Time spent rate-limited is reported separately; a limit hit
                # mid-turn (no Stop until reset) would otherwise count the
                # stall as both autonomous work and rate-limited.
                work_by_skill[skill] += max(
                    0.0,
                    (b - a).total_seconds() - overlap_seconds((a, b), limited),
                )
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
    color = color_enabled()
    if skills:
        print("  by skill:")
        width = max(len(sk) for sk in skills) + 2
        for sk in skills:
            line = (
                f"    {skill_key(sk, slots, color)} "
                f"{sk:<{width}} {fmt_duration(work_by_skill[sk]):>7}"
            )
            if sk in tokens_by_skill:
                line += f"   {fmt_usage(tokens_by_skill[sk])}"
            print(line)
        print_day_chart(day, day_sessions, slots, color, limited)

    if show_sessions and day_sessions:
        print("  sessions:")
        far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)

        def day_start_key(s):
            starts = [a for a, _, _ in s.work if local_date(a) == day]
            return min(starts) if starts else far_future

        for s in sorted(day_sessions, key=day_start_key):
            w = sum(
                max(
                    0.0,
                    (b - a).total_seconds() - overlap_seconds((a, b), limited),
                )
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
                f"    · {label}: {iv} intervention{'s' if iv != 1 else ''}, "
                f"autonomous {fmt_duration(w)}, blocked {fmt_duration(bl)}"
            )

    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "day",
        nargs="?",
        help='day to report: "today", "yesterday", or YYYY-MM-DD '
        "(default today); same as --date",
    )
    ap.add_argument(
        "--date", help='report a specific day: "today", "yesterday", or YYYY-MM-DD'
    )
    ap.add_argument(
        "--days", type=int, default=1, help="report the last N days (default 1)"
    )
    ap.add_argument(
        "--sessions", action="store_true", help="include per-session detail"
    )
    ap.add_argument(
        "--log",
        default=default_log_location(),
        help="event log file, or directory containing events*.jsonl",
    )
    args = ap.parse_args()

    def resolve_day(token):
        t = token.strip().lower()
        if t == "today":
            return dt.date.today()
        if t == "yesterday":
            return dt.date.today() - dt.timedelta(days=1)
        try:
            return dt.date.fromisoformat(token.strip())
        except ValueError:
            ap.error(
                f'invalid day {token!r}: use "today", "yesterday", or YYYY-MM-DD'
            )

    # The positional day and --date name the same thing; accept either, and
    # reject only when both are given and resolve to different dates.
    target = resolve_day(args.date) if args.date else None
    if args.day:
        d = resolve_day(args.day)
        if target is not None and d != target:
            ap.error("positional day and --date disagree; pass only one")
        target = d
    if target is not None and args.days != 1:
        ap.error("a specific day and --days are mutually exclusive")

    sessions = {
        sid: Session(sid, events)
        for sid, events in load_sessions(args.log).items()
    }
    dedupe_shared_history(sessions)

    slots = assign_skill_slots(sessions)

    if target is not None:
        days = [target]
    else:
        today = dt.date.today()
        days = [today - dt.timedelta(days=i) for i in range(args.days)]
        days.reverse()

    printed = False
    for i, day in enumerate(days):
        if report_day(day, sessions, args.sessions, slots):
            printed = True
            if i < len(days) - 1:
                print()
    if not printed:
        print("No recorded activity for the requested period.")


if __name__ == "__main__":
    main()
