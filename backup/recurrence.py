"""
recurrence.py — when a backup schedule may run. An RFC 5545 rule picks the occurrence days; on each
of those days the window opens at `window_open` and the night should be done by `preferred_finish`
(the next day when it is earlier than the opening). Blackouts are weekly periods in which nothing may
start. All times are read in the schedule's own time zone; functions take and return aware datetimes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
_FREQS = {'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'}          # an occurrence is a day: no HOURLY/MINUTELY
_PARTS = {'FREQ', 'INTERVAL', 'BYDAY', 'BYMONTHDAY', 'BYMONTH', 'BYSETPOS', 'BYWEEKNO', 'BYYEARDAY',
          'UNTIL', 'COUNT', 'WKST'}
# a window may stay open at most this long, and never past the next occurrence's opening
MAX_WINDOW = timedelta(hours=24)


class RecurrenceError(ValueError):
    pass


@dataclass(frozen=True)
class Window:
    opens: datetime          # earliest start of this occurrence
    finish: datetime         # the preferred finish
    closes: datetime         # nothing of this occurrence starts after this
    occurrence: date         # the local day the window opened on (identifies the occurrence)


def parse_hhmm(v) -> time:
    try:
        hh, mm = str(v or '').split(':')
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        raise RecurrenceError(f'{v!r} is not a time (HH:MM)') from None


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or 'UTC')
    except Exception:  # noqa: BLE001
        raise RecurrenceError(f'unknown time zone {name!r}') from None


def normalise_rule(rule: str) -> str:
    """Validate and normalise a rule: upper case, no RRULE: prefix, only day-level parts."""
    text = (rule or '').strip().upper()
    if text.startswith('RRULE:'):
        text = text[6:]
    if not text or '\n' in text or 'DTSTART' in text:
        raise RecurrenceError('the rule must be a single RRULE without DTSTART')
    parts = {}
    for item in text.split(';'):
        if not item:
            continue
        k, sep, v = item.partition('=')
        if not sep or k not in _PARTS:
            raise RecurrenceError(f'unsupported rule part {item!r}')
        parts[k] = v
    if parts.get('FREQ') not in _FREQS:
        raise RecurrenceError('FREQ must be DAILY, WEEKLY, MONTHLY or YEARLY')
    text = ';'.join(f'{k}={v}' for k, v in parts.items())
    try:
        rrulestr(text, dtstart=datetime(2026, 1, 1))
    except (ValueError, TypeError) as e:
        raise RecurrenceError(f'invalid rule: {e}') from None
    return text


def normalise_blackouts(items) -> list[dict]:
    out = []
    for b in items or []:
        days = [d for d in DAYS if d in {str(x).lower()[:3] for x in (b.get('days') or [])}]
        if not days:
            raise RecurrenceError('a blackout needs at least one day')
        f, t = parse_hhmm(b.get('from')), parse_hhmm(b.get('to'))
        if f == t:
            raise RecurrenceError('a blackout needs different start and end times')
        out.append({'days': days, 'from': f.strftime('%H:%M'), 'to': t.strftime('%H:%M')})
    return out


def _local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz).replace(tzinfo=None)


def _aware(naive: datetime, tz: ZoneInfo) -> datetime:
    return naive.replace(tzinfo=tz)


def _rule(policy: dict):
    start = policy.get('starts_on') or date(2026, 1, 1)
    if isinstance(start, str):
        start = date.fromisoformat(start[:10])
    return rrulestr(policy['rrule'], dtstart=datetime.combine(start, parse_hhmm(policy['window_open'])))


def occurrences(policy: dict, after: datetime, count: int = 10) -> list[datetime]:
    """The next `count` window openings strictly after `after`."""
    tz, rule = zone(policy.get('timezone')), _rule(policy)
    out, cur = [], _local(after, tz)
    while len(out) < count:
        nxt = rule.after(cur)
        if nxt is None:
            break
        out.append(_aware(nxt, tz))
        cur = nxt
    return out


def window_for(policy: dict, opens: datetime) -> Window:
    tz = zone(policy.get('timezone'))
    lo = _local(opens, tz)
    fin = datetime.combine(lo.date(), parse_hhmm(policy['preferred_finish']))
    if fin <= lo:
        fin += timedelta(days=1)
    closes = lo + MAX_WINDOW
    nxt = _rule(policy).after(lo)
    if nxt is not None and nxt < closes:
        closes = nxt
    return Window(_aware(lo, tz), _aware(fin, tz), _aware(closes, tz), lo.date())


def current_window(policy: dict, now: datetime) -> Window | None:
    """The window `now` falls in (opened and not yet closed), or None."""
    tz = zone(policy.get('timezone'))
    last = _rule(policy).before(_local(now, tz), inc=True)
    if last is None:
        return None
    w = window_for(policy, _aware(last, tz))
    return w if now < w.closes else None


def blackout_intervals(policy: dict, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Blackout periods overlapping [start, end), as aware intervals. A period whose end is not after
    its start runs past midnight into the next day."""
    tz = zone(policy.get('timezone'))
    lo, hi = _local(start, tz), _local(end, tz)
    out = []
    d = lo.date() - timedelta(days=1)
    while d <= hi.date():
        for b in policy.get('blackouts') or []:
            if DAYS[d.weekday()] not in b['days']:
                continue
            s = datetime.combine(d, parse_hhmm(b['from']))
            e = datetime.combine(d, parse_hhmm(b['to']))
            if e <= s:
                e += timedelta(days=1)
            if s < hi and e > lo:
                out.append((_aware(s, tz), _aware(e, tz)))
        d += timedelta(days=1)
    return sorted(out)


def in_blackout(policy: dict, dt: datetime) -> bool:
    return bool(blackout_intervals(policy, dt, dt + timedelta(seconds=1)))


def overlaps_blackout(policy: dict, start: datetime, end: datetime) -> bool:
    return bool(blackout_intervals(policy, start, max(end, start + timedelta(seconds=1))))


def next_allowed(policy: dict, dt: datetime) -> datetime:
    """The first moment at or after `dt` outside every blackout."""
    for _ in range(64):                          # chained periods; bounded so a full-week blackout cannot spin
        hits = blackout_intervals(policy, dt, dt + timedelta(seconds=1))
        if not hits:
            return dt
        dt = max(e for _, e in hits)
    return dt


def preview(policy: dict, now: datetime, count: int = 10) -> list[dict]:
    """The next windows for the schedule form: opening, first allowed start, preferred finish."""
    out = []
    cur = current_window(policy, now)
    opens = ([cur.opens] if cur else []) + occurrences(policy, now, count)
    for o in opens[:count]:
        w = window_for(policy, o)
        start = next_allowed(policy, w.opens)
        out.append({'opens': w.opens.isoformat(), 'first_start': start.isoformat() if start < w.closes else None,
                    'finish': w.finish.isoformat(), 'closes': w.closes.isoformat(), 'occurrence': w.occurrence.isoformat()})
    return out
