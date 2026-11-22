"""Foresight right-sizing and resize advice (sentinel/analysis/rightsizing.py, resize_advisor.py).

Needs numpy and scipy, which the sentinel image has and the plain test image does not: run with
the sentinel image (skipped elsewhere)."""
import math

import pytest

pytest.importorskip('numpy')
pytest.importorskip('scipy')

import config as sentinel_config                              # noqa: E402
from analysis.resize_advisor import run_resize_advisor        # noqa: E402
from analysis.rightsizing import run_rightsizing              # noqa: E402
from collector.vms import VMTimeSeries, _compute_stats        # noqa: E402


def _vm(values, vcpu=8, uuid='vm-1'):
    v = VMTimeSeries(vm_uuid=uuid, vm_name=uuid, node_uuid='node-1', cpu_values=list(values), num_vcpu=vcpu)
    _compute_stats(v)
    return v


def _run(vm, storage=None, **cfg):
    c = sentinel_config.Config(**cfg)
    rs = run_rightsizing({'vms': {vm.vm_uuid: vm}, 'storage': storage, 'running_vm_uuids': {vm.vm_uuid}}, c)
    return rs, run_resize_advisor(rs, c)


def test_a_vm_idle_by_day_but_flat_out_at_night_is_not_over_provisioned():
    """8 vCPU at 100 % for 90 min a night and 2 % otherwise: avg 8.1 %, p95 100 %. It got '8 -> 1 vCPU, risk LOW'."""
    day = [100.0] * 9 + [2.0] * 135              # 10-minute samples: 90 min of 24 h at 100 %
    rs, advice = _run(_vm(day * 14))
    assert not [r for r in rs if r.sizing_direction == 'over_provisioned']
    assert not [a for a in advice if a.sizing_direction == 'over_provisioned']


def test_over_provisioned_advice_keeps_enough_vcpus_for_the_p95():
    values = [5.0] * 90 + [60.0] * 10             # avg 10.5 %, p95 60 %
    rs, advice = _run(_vm(values, vcpu=8))
    (a,) = [a for a in advice if a.sizing_direction == 'over_provisioned']
    floor = math.ceil(8 * 60.0 / (90.0 * 0.85))
    assert a.recommended_vcpu >= floor, f'{a.recommended_vcpu} vCPU cannot carry a 60 % p95 of 8 vCPU'
    assert a.recommended_vcpu < 8


def test_an_idle_vm_is_at_most_a_warning():
    """Any VM averaging 4 % or less scored CRITICAL and set off the CRITICAL webhook."""
    rs, _ = _run(_vm([1.0] * 100))
    (r,) = rs
    assert r.sizing_direction == 'over_provisioned' and r.severity == 'WARNING'


class _VSD:
    def __init__(self, vm_uuid):
        from datetime import datetime, timedelta, timezone
        t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.vm_uuid, self.vsd_uuid, self.vsd_name = vm_uuid, 'vsd-1', 'disk'
        self.times = [t0 + timedelta(days=i) for i in range(14)]
        self.read_latency_us = [100.0 * (1 + i) for i in range(14)]     # a clean, steep latency trend


class _Storage:
    def __init__(self, vm_uuid):
        self.vsds = {'vsd-1': _VSD(vm_uuid)}


def test_latency_alone_does_not_make_a_vm_under_provisioned():
    """avg 24 %, p95 50 % with a latency trend was flagged under-provisioned and told to shrink 4 -> 3."""
    vm = _vm([20.0] * 90 + [50.0] * 10, vcpu=4)
    rs, advice = _run(vm, storage=_Storage(vm.vm_uuid))
    assert not [r for r in rs if r.sizing_direction == 'under_provisioned']
    assert all(a.recommended_vcpu >= 4 for a in advice if a.sizing_direction == 'under_provisioned')


def test_under_provisioned_advice_follows_the_configured_p95_threshold():
    vm = _vm([70.0] * 90 + [85.0] * 10, vcpu=4)          # p95 85 %: under-provisioned only at a lower threshold
    rs, advice = _run(vm, cpu_under_provisioned_p95_threshold=80.0)
    (a,) = [a for a in advice if a.sizing_direction == 'under_provisioned']
    assert a.suggested_cpu_change_pct == pytest.approx(85.0 / (80.0 * 0.85) - 1.0, abs=1e-3)
    assert a.suggested_cpu_change_pct > 0 and a.recommended_vcpu >= 4
