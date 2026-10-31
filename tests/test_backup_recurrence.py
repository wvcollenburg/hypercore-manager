"""backup/recurrence.py — occurrence days, windows, blackouts and time zones of backup schedules."""
import importlib
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
sys.path.insert(0, _BACKUP)
try:
    rec = importlib.import_module('recurrence')
finally:
    sys.path.remove(_BACKUP)

AMS = ZoneInfo('Europe/Amsterdam')


def _pol(**kw):
    p = {'rrule': 'FREQ=DAILY', 'starts_on': date(2026, 10, 1), 'window_open': '22:00', 'preferred_finish': '06:00',
         'buffer_minutes': 60, 'blackouts': [], 'timezone': 'Europe/Amsterdam'}
    p.update(kw)
    return p


@pytest.mark.parametrize('rule, expected', [
    ('freq=weekly;byday=mo,we', 'FREQ=WEEKLY;BYDAY=MO,WE'),
    ('RRULE:FREQ=MONTHLY;BYDAY=-1SU', 'FREQ=MONTHLY;BYDAY=-1SU'),
    ('FREQ=WEEKLY;INTERVAL=2;BYDAY=SA', 'FREQ=WEEKLY;INTERVAL=2;BYDAY=SA'),
])
def test_rules_are_normalised(rule, expected):
    assert rec.normalise_rule(rule) == expected


@pytest.mark.parametrize('rule', ['', 'FREQ=MINUTELY', 'FREQ=HOURLY;INTERVAL=0', 'FREQ=HOURLY;INTERVAL=169', 'FREQ=DAILY;BYHOUR=3', 'DTSTART:20260101T000000\nFREQ=DAILY',
                                  'FREQ=WEEKLY;BYDAY=XX', 'BYDAY=MO'])
def test_bad_rules_are_refused(rule):
    with pytest.raises(rec.RecurrenceError):
        rec.normalise_rule(rule)


def test_every_second_saturday_and_last_sunday():
    p = _pol(rrule='FREQ=WEEKLY;INTERVAL=2;BYDAY=SA', starts_on=date(2026, 10, 3))     # a Saturday
    got = [o.date() for o in rec.occurrences(p, datetime(2026, 10, 1, tzinfo=AMS), 3)]
    assert got == [date(2026, 10, 3), date(2026, 10, 17), date(2026, 10, 31)]
    p = _pol(rrule='FREQ=MONTHLY;BYDAY=-1SU')
    assert [o.date() for o in rec.occurrences(p, datetime(2026, 10, 1, tzinfo=AMS), 2)] == [date(2026, 10, 25), date(2026, 11, 29)]


def test_window_runs_past_midnight_and_closes_at_the_next_opening():
    p = _pol()
    w = rec.current_window(p, datetime(2026, 10, 20, 23, 30, tzinfo=AMS))
    assert w.opens == datetime(2026, 10, 20, 22, 0, tzinfo=AMS) and w.occurrence == w.opens and w.day == date(2026, 10, 20)
    assert w.finish == datetime(2026, 10, 21, 6, 0, tzinfo=AMS)
    assert w.closes == datetime(2026, 10, 21, 22, 0, tzinfo=AMS), 'daily: open until the next opening'
    # weekly: closed 24 h after opening
    p = _pol(rrule='FREQ=WEEKLY;BYDAY=TU')
    assert rec.current_window(p, datetime(2026, 10, 21, 23, 0, tzinfo=AMS)) is None
    assert rec.current_window(p, datetime(2026, 10, 21, 21, 0, tzinfo=AMS)).day == date(2026, 10, 20)


def test_times_are_read_in_the_schedule_zone_across_dst():
    """Europe/Amsterdam leaves summer time on 25 Oct 2026: 22:00 local is 20:00 UTC before, 21:00 after."""
    p = _pol()
    o = rec.occurrences(p, datetime(2026, 10, 24, 12, tzinfo=AMS), 2)
    assert [x.astimezone(timezone.utc).hour for x in o] == [20, 21]
    p = _pol(timezone='US/Eastern', window_open='03:00')
    o = rec.occurrences(p, datetime(2026, 10, 24, 0, tzinfo=timezone.utc), 1)[0]
    assert o.astimezone(timezone.utc) == datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)


def test_blackouts_including_one_across_midnight():
    p = _pol(blackouts=rec.normalise_blackouts([{'days': ['Mon', 'tue'], 'from': '23:00', 'to': '01:30'}]))
    assert rec.in_blackout(p, datetime(2026, 10, 19, 23, 30, tzinfo=AMS))      # Monday night
    assert rec.in_blackout(p, datetime(2026, 10, 20, 1, 0, tzinfo=AMS))        # still Monday's period
    assert not rec.in_blackout(p, datetime(2026, 10, 20, 2, 0, tzinfo=AMS))
    assert not rec.in_blackout(p, datetime(2026, 10, 21, 23, 30, tzinfo=AMS))  # Wednesday
    assert rec.next_allowed(p, datetime(2026, 10, 19, 23, 10, tzinfo=AMS)) == datetime(2026, 10, 20, 1, 30, tzinfo=AMS)
    assert rec.overlaps_blackout(p, datetime(2026, 10, 19, 22, 0, tzinfo=AMS), datetime(2026, 10, 19, 23, 5, tzinfo=AMS))
    assert not rec.overlaps_blackout(p, datetime(2026, 10, 19, 22, 0, tzinfo=AMS), datetime(2026, 10, 19, 22, 59, tzinfo=AMS))


@pytest.mark.parametrize('items', [[{'days': [], 'from': '07:00', 'to': '19:00'}], [{'days': ['mon'], 'from': '07:00', 'to': '07:00'}],
                                   [{'days': ['mon'], 'from': 'x', 'to': '07:00'}]])
def test_bad_blackouts_are_refused(items):
    with pytest.raises(rec.RecurrenceError):
        rec.normalise_blackouts(items)


def test_preview_lists_the_current_window_first_and_the_first_allowed_start():
    p = _pol(blackouts=[{'days': list(rec.DAYS), 'from': '21:00', 'to': '23:00'}])
    pv = rec.preview(p, datetime(2026, 10, 20, 22, 30, tzinfo=AMS), 3)
    assert [x['day'] for x in pv] == ['2026-10-20', '2026-10-21', '2026-10-22']
    assert pv[0]['first_start'] == datetime(2026, 10, 20, 23, 0, tzinfo=AMS).isoformat()


def test_full_mode_per_occurrence():
    p = _pol(kind='incremental', full_rrule='FREQ=MONTHLY;BYDAY=-1SU', full_kind='real')
    assert rec.full_mode(p, datetime(2026, 10, 25, 22, tzinfo=AMS)) == 'real' and rec.full_mode(p, datetime(2026, 10, 24, 22, tzinfo=AMS)) == 'none'
    assert rec.full_mode(dict(p, full_rrule=None), datetime(2026, 10, 25, 22, tzinfo=AMS)) is None
    assert rec.full_mode(dict(p, kind='export'), datetime(2026, 10, 25, 22, tzinfo=AMS)) is None
    pv = rec.preview(dict(p, full_kind='synthetic'), datetime(2026, 10, 24, 12, tzinfo=AMS), 3)
    assert [(x['day'], x['full']) for x in pv] == [('2026-10-23', False), ('2026-10-24', False), ('2026-10-25', True)]


# ------------------------------------------------------------------- hourly
def test_an_hourly_rule_counts_from_midnight_and_each_window_lasts_until_the_next():
    p = _pol(rrule='FREQ=HOURLY;INTERVAL=3')
    opens = rec.occurrences(p, datetime(2026, 10, 20, 10, 30, tzinfo=AMS), 3)
    assert [o.hour for o in opens] == [12, 15, 18], 'every third hour from 00:00, whatever window_open says'
    w = rec.current_window(p, datetime(2026, 10, 20, 13, 0, tzinfo=AMS))
    assert w.opens == datetime(2026, 10, 20, 12, 0, tzinfo=AMS)
    assert w.finish == w.closes == datetime(2026, 10, 20, 15, 0, tzinfo=AMS), 'no preferred finish: the next occurrence'
    assert w.occurrence == w.opens and w.day == date(2026, 10, 20)
    assert rec.normalise_rule('freq=hourly;interval=3') == 'FREQ=HOURLY;INTERVAL=3'


def test_an_hourly_schedule_takes_its_full_in_the_first_occurrence_of_a_full_day():
    p = _pol(rrule='FREQ=HOURLY;INTERVAL=6', kind='incremental', full_rrule='FREQ=WEEKLY;BYDAY=SU', full_kind='synthetic')
    sun = [datetime(2026, 10, 25, h, tzinfo=AMS) for h in (0, 6, 12, 18)]
    assert [rec.full_mode(p, o) for o in sun] == ['synthetic', 'none', 'none', 'none']
    assert rec.full_mode(p, datetime(2026, 10, 24, 0, tzinfo=AMS)) == 'none'


def test_a_manual_window_is_open_for_a_day_from_the_request():
    at = datetime(2026, 10, 31, 14, 7, tzinfo=timezone.utc)
    w = rec.manual_window(at)
    assert w.opens == w.occurrence == at and w.closes == at + timedelta(hours=24)
