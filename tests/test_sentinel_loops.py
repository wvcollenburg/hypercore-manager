"""sentinel/sentinel.py — the outcome and webhook loops follow the live settings (Foresight)."""
import dataclasses
import importlib.util
import sys
import types
from pathlib import Path

import pytest

import config as sentinel_config
import tunables


def _import_sentinel_main():
    """sentinel/sentinel.py imports 'log', a name every service has: with another service's directory first on
    sys.path (the console tests put theirs there) it got console/log.py. Import it with sentinel/ first and the
    clashing name cleared, then put sys.modules back as it was."""
    sentinel_dir = str(Path(__file__).resolve().parents[1] / 'sentinel')
    saved = sys.modules.pop('log', None)
    sys.path.insert(0, sentinel_dir)
    try:
        spec = importlib.util.spec_from_file_location('sentinel_main', Path(sentinel_dir) / 'sentinel.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(sentinel_dir)
        sys.modules.pop('log', None)
        if saved is not None:
            sys.modules['log'] = saved


sentinel_main = _import_sentinel_main()


class _Stop:
    """A stop event whose wait() returns at once and that is set after `passes` waits."""
    def __init__(self, passes): self.passes, self.waits = passes, 0
    def is_set(self): return self.waits >= self.passes
    def wait(self, seconds): self.waits += 1


def _live(monkeypatch, values):
    """apply_live returns the next of `values` (dicts of cfg fields) on every call."""
    seq = iter(values)
    monkeypatch.setattr(tunables, 'apply_live', lambda cfg: dataclasses.replace(cfg, **next(seq)))


def test_the_outcome_tracker_switch_and_delay_are_read_on_every_pass(monkeypatch):
    """The loop used the start-up config: switching the Outcome Tracker on or off, or changing the
    check delay, in the UI did nothing; started disabled, the loop had returned for good."""
    runs = []
    monkeypatch.setitem(sys.modules, 'outcome_tracker', types.SimpleNamespace(run_once=runs.append))
    _live(monkeypatch, [{'outcome_tracker_enabled': True, 'outcome_check_delay_hours': 48},
                        {'outcome_tracker_enabled': False},
                        {'outcome_tracker_enabled': True, 'outcome_check_delay_hours': 2}])
    cfg = sentinel_config.Config(outcome_tracker_enabled=False)
    sentinel_main.outcome_loop(cfg, _Stop(4))
    assert [c.outcome_check_delay_hours for c in runs] == [48, 2]


def test_the_webhook_switch_is_read_on_every_pass(monkeypatch):
    sent = []
    monkeypatch.setitem(sys.modules, 'webhook', types.SimpleNamespace(dispatch_pending=sent.append))
    _live(monkeypatch, [{'webhook_enabled': False}, {'webhook_enabled': True}, {'webhook_enabled': False}])
    cfg = sentinel_config.Config(webhook_enabled=False, webhook_url='https://hook.example/x')
    sentinel_main.webhook_loop(cfg, _Stop(3))
    assert len(sent) == 1 and sent[0].webhook_enabled


def test_without_a_webhook_url_the_loop_does_not_run(monkeypatch):
    monkeypatch.setattr(tunables, 'apply_live', lambda cfg: pytest.fail('read settings without a URL'))
    sentinel_main.webhook_loop(sentinel_config.Config(webhook_url=''), _Stop(3))
