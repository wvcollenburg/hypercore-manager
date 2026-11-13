"""
alerts.py — the cluster alert rules behind the badges on a cluster's card (backlog #3).

Kept free of Flask and InfluxDB so the rules can be tested on their own; app.py feeds them the
Collector's condition points.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# HyperCore's SeverityLevel (the eight syslog levels) on the four badges, in the order and colours of
# HyperCore's own cluster header. DEBUG is not shown. ERROR counts as critical (a guess, to check
# against HyperCore's header on a lab cluster with an ERROR condition).
ALERT_SEVERITY = {'EMERGENCY': 'critical', 'ALERT': 'critical', 'CRITICAL': 'critical', 'ERROR': 'critical',
                  'WARNING': 'warning', 'NOTICE': 'notice', 'INFO': 'info', 'INFORMATIONAL': 'info'}
ALERT_LEVELS = ('critical', 'warning', 'notice', 'info')


def ms_iso(ms) -> Optional[str]:
    """HyperCore's millisecond timestamps as ISO text; 0 / missing = None."""
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms > 0 else None


def active_items(latest: dict) -> dict[str, list[dict]]:
    """The conditions that are SET right now, per cluster. `latest` maps (cluster, condition name,
    node uuid) to {'fields': {value, is_ok, description, last_set_time}, 'severity', 'node_ip'} from the
    newest point. The spec says `value` must be ignored when isOK is false (expired, last poll failed,
    never initialised), so those are left out rather than shown as stale alarms."""
    out: dict[str, list[dict]] = {}
    for (cid, name, node), row in latest.items():
        fl = row.get('fields') or {}
        raw = str(row.get('severity', '')).upper()
        level = ALERT_SEVERITY.get(raw)
        if not level or not fl.get('value') or not fl.get('is_ok', True):
            continue
        out.setdefault(cid, []).append({
            'key': f'condition:{name}:{node}', 'kind': 'condition', 'severity': level, 'raw_severity': raw,
            'name': name, 'node_uuid': node, 'node_ip': row.get('node_ip', ''), 'description': fl.get('description') or name,
            'since': ms_iso(fl.get('last_set_time'))})
    return out


def condition_episodes(set_times: dict, clear_times: dict, latest: dict, since_ms: int) -> list[dict]:
    """Episodes per (condition, node) from the distinct lastSetTime / lastClearTime values the
    Collector stored. An episode starts at a set time and ends at the first clear time after it.
    Without one it is still active when the latest point is SET (and OK) with that same set time; otherwise
    its end was not seen (the Collector stopped, or the condition stopped being reported).
    Episodes that ended before `since_ms` are left out."""
    rows = []
    for key, sets in set_times.items():
        clears = sorted(c for c in clear_times.get(key, ()) if c > 0)
        cur = latest.get(key) or {}
        for s in sorted(x for x in sets if x > 0):
            end = next((c for c in clears if c > s), None)
            if end is not None and end < since_ms:
                continue
            # the spec: value means nothing while isOK is false, so such a condition is never 'still active'
            active = (end is None and bool(cur.get('value')) and cur.get('is_ok', True) is not False
                      and int(cur.get('last_set_time') or 0) == s)
            rows.append({'key': key, 'start': s, 'end': end, 'active': active})
    return rows
