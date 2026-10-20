"""sentinel/influx_client.py — Flux record normalisation."""
from datetime import datetime, timezone

from influx_client import InfluxReader


class FakeRecord:
    """Mimics influxdb_client FluxRecord: .values dict, get_time() raising on a missing _time."""
    def __init__(self, values):
        self.values = values

    def get_time(self):
        return self.values['_time']


def test_normalise_full_record():
    t = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    rec = FakeRecord({'_time': t, '_measurement': 'vm_metrics', '_field': 'cpu_usage', '_value': 12.5,
                      '_start': 0, '_stop': 1, 'result': '_result', 'table': 0,
                      'vm_uuid': 'u1', 'vm_name': 'web01'})
    out = InfluxReader._normalise(rec)
    assert out == {'time': t, 'measurement': 'vm_metrics', 'field': 'cpu_usage', 'value': 12.5,
                   'vm_uuid': 'u1', 'vm_name': 'web01'}


def test_normalise_record_without_time_column():
    """keep(columns: ["vm_uuid", "vm_name"]) drops _time; the pruning loop crashed on this
    (KeyError from FluxRecord.get_time) every cycle since April 2026."""
    rec = FakeRecord({'result': '_result', 'table': 3, 'vm_uuid': 'u1', 'vm_name': 'web01'})
    out = InfluxReader._normalise(rec)
    assert out['time'] is None
    assert out['vm_uuid'] == 'u1' and out['vm_name'] == 'web01'
    assert out['measurement'] is None and out['field'] is None and out['value'] is None
