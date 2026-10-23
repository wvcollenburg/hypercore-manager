"""
influx_client.py — Read-only InfluxDB client wrapper (G4).

Sentinel NEVER writes to InfluxDB.  This module only exposes query methods.

All queries use Flux and return normalised Python dicts.  Callers receive
plain Python data structures — no InfluxDB SDK types leak out.

Two internal query helpers are provided:

  _query()      — loads the full result set into a list.  Used only for small
                  lookups (latest-value queries returning one row per entity).

  _iter_query() — streams records one at a time using the SDK's query_stream()
                  API, yielding each normalised dict as it arrives.  Used for
                  all large time-series queries so that raw SDK record objects
                  are freed immediately after each row is yielded, rather than
                  the entire result set accumulating in RAM before processing
                  begins.  The HTTP connection is closed automatically when the
                  generator is exhausted or garbage-collected.

All public query_* methods that return time-series data are generators.
Callers iterate with a standard for-loop — behaviour is identical to the
previous list-based API.
"""

import logging
from typing import Any, Generator

from influxdb_client import InfluxDBClient
from influxdb_client.client.exceptions import InfluxDBError

import config as cfg_module

log = logging.getLogger(__name__)


class InfluxReader:
    """
    Thin wrapper around the InfluxDB query API.

    One instance is created per Sentinel run and reused across collection
    cycles.  The underlying HTTP connection is kept alive automatically by
    the SDK.
    """

    def __init__(self, cfg: cfg_module.Config) -> None:
        self._cfg    = cfg
        # every measurement is tagged with the registry cluster_id by the collector;
        # all queries filter on it so several clusters can share one bucket
        self._cluster_id = cfg.cluster_id
        self._client = InfluxDBClient(
            url=cfg.influx_url,
            token=cfg.influx_token,
            org=cfg.influx_org,
            timeout=30_000,       # ms
            enable_gzip=True,
        )
        self._query_api = self._client.query_api()
        log.info('InfluxDB reader initialised', extra={'url': cfg.influx_url, 'org': cfg.influx_org})

    # ------------------------------------------------------------------
    # low-level helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise(record) -> dict[str, Any]:
        """Convert a raw FluxRecord to a normalised dict.

        Queries that end in ``keep(columns: [...])`` without ``_time`` return records
        with no time column; FluxRecord.get_time() raises KeyError on those, so the
        value is read defensively and comes back as None.
        """
        values = record.values
        return {
            'time':        values.get('_time'),
            'measurement': values.get('_measurement'),
            'field':       values.get('_field'),
            'value':       values.get('_value'),
            **{k: v for k, v in values.items()
               if k not in ('_time', '_measurement', '_field', '_value',
                            '_start', '_stop', 'result', 'table')},
        }

    def _query(self, flux: str) -> list[dict[str, Any]]:
        """
        Execute a Flux query and return the full result as a list of dicts.

        Use only for small lookups (e.g. latest-value queries).  For large
        time-series queries use _iter_query() to avoid holding the entire
        result set in memory simultaneously.
        """
        try:
            tables = self._query_api.query(flux, org=self._cfg.influx_org)
        except InfluxDBError as exc:
            log.error('InfluxDB query failed', extra={'error': str(exc)})
            raise

        return [self._normalise(record) for table in tables for record in table.records]

    def _iter_query(self, flux: str) -> Generator[dict[str, Any], None, None]:
        """
        Execute a Flux query and yield one normalised dict per record.

        Uses the SDK's query_stream() so each FluxRecord is yielded to the
        caller and then freed — the full result set never accumulates in RAM.
        The underlying stream is closed automatically when the generator is
        exhausted or garbage-collected.
        """
        try:
            stream = self._query_api.query_stream(flux, org=self._cfg.influx_org)
        except InfluxDBError as exc:
            log.error('InfluxDB stream query failed', extra={'error': str(exc)})
            raise

        try:
            for record in stream:
                yield self._normalise(record)
        finally:
            stream.close()

    def _window(self, days: int) -> str:
        """Return a Flux range string covering the last N days."""
        return f'-{days}d'

    # ------------------------------------------------------------------
    # node_metrics queries  (generators — stream large time-series)
    # ------------------------------------------------------------------

    def query_node_cpu(self, lookback_days: int) -> Generator[dict, None, None]:
        """
        Yield per-node CPU usage samples over the lookback window.
        Fields: time, node_uuid, node_peer_id, value (cpu_usage %)
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: {self._window(lookback_days)})
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "node_metrics" and r._field == "cpu_usage")
  |> keep(columns: ["_time", "_value", "node_uuid", "node_peer_id"])
'''
        for r in self._iter_query(flux):
            yield {'time': r['time'], 'node_uuid': r.get('node_uuid'),
                   'node_peer_id': r.get('node_peer_id'), 'value': r['value']}

    def query_node_ram(self, lookback_days: int) -> Generator[dict, None, None]:
        """
        Yield per-node RAM usage samples (allocation, bytes) over the lookback window.
        HyperCore does not overcommit RAM — this is allocation tracking only.

        The HyperCore collector writes mem_usage_percentage + mem_size rather than a
        pre-computed mem_usage_bytes field.  We pivot both fields onto the same row and
        multiply to produce bytes: mem_usage_percentage * mem_size / 100.

        Fields returned: time, node_uuid, node_peer_id, value (bytes)
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: {self._window(lookback_days)})
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "node_metrics"
       and (r._field == "mem_usage_percentage" or r._field == "mem_size"))
  |> keep(columns: ["_time", "_field", "_value", "node_uuid", "node_peer_id"])
  |> pivot(rowKey: ["_time", "node_uuid", "node_peer_id"],
           columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => exists r.mem_usage_percentage and exists r.mem_size and r.mem_size > 0)
  |> map(fn: (r) => ({{
       _time:         r._time,
       node_uuid:     r.node_uuid,
       node_peer_id:  if exists r.node_peer_id then r.node_peer_id else "",
       _value:        r.mem_usage_percentage * float(v: r.mem_size) / 100.0
     }}))
'''
        for r in self._iter_query(flux):
            yield {'time': r['time'], 'node_uuid': r.get('node_uuid'),
                   'node_peer_id': r.get('node_peer_id'), 'value': r['value']}

    # ------------------------------------------------------------------
    # vm_metrics queries  (generators — stream large time-series)
    # ------------------------------------------------------------------

    def query_vm_cpu(self, lookback_days: int) -> Generator[dict, None, None]:
        """
        Yield per-VM CPU usage samples.
        Fields: time, vm_uuid, vm_name, node_uuid, value (cpu_usage %)
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: {self._window(lookback_days)})
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "vm_metrics" and r._field == "cpu_usage")
  |> keep(columns: ["_time", "_value", "vm_uuid", "vm_name", "node_uuid"])
'''
        for r in self._iter_query(flux):
            yield {'time': r['time'], 'vm_uuid': r.get('vm_uuid'),
                   'vm_name': r.get('vm_name'), 'node_uuid': r.get('node_uuid'),
                   'value': r['value']}

    def query_vm_network(self, lookback_days: int) -> Generator[dict, None, None]:
        """
        Yield per-VM rx and tx bit rate samples.
        Node-level network = rollup of these values (no native node endpoint).
        Fields: time, vm_uuid, vm_name, node_uuid, rx_bit_rate, tx_bit_rate
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: {self._window(lookback_days)})
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "vm_metrics"
       and (r._field == "rx_bit_rate" or r._field == "tx_bit_rate"))
  |> keep(columns: ["_time", "_field", "_value", "vm_uuid", "vm_name", "node_uuid"])
  |> pivot(rowKey: ["_time", "vm_uuid", "vm_name"],
           columnKey: ["_field"], valueColumn: "_value")
'''
        for r in self._iter_query(flux):
            yield {
                'time':        r['time'],
                'vm_uuid':     r.get('vm_uuid'),
                'vm_name':     r.get('vm_name'),
                'node_uuid':   r.get('node_uuid'),
                'rx_bit_rate': r.get('rx_bit_rate', 0.0),
                'tx_bit_rate': r.get('tx_bit_rate', 0.0),
            }

    # ------------------------------------------------------------------
    # vsd_metrics queries  (generators — stream large time-series)
    # ------------------------------------------------------------------

    def query_vsd_latency(self, lookback_days: int) -> Generator[dict, None, None]:
        """
        Yield per-VSD read/write latency samples (microseconds).
        NVRAM, VTPM, IDE_CDROM, and IDE_FLOPPY device types are excluded.
        Fields: time, vm_uuid, vm_name, vsd_uuid, vsd_name, read_latency_us, write_latency_us
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: {self._window(lookback_days)})
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "vsd_metrics"
       and (r._field == "read_latency_us" or r._field == "write_latency_us"))
  |> filter(fn: (r) => r.vsd_type != "NVRAM" and r.vsd_type != "VTPM"
       and r.vsd_type != "IDE_CDROM" and r.vsd_type != "IDE_FLOPPY")
  |> keep(columns: ["_time", "_field", "_value", "vm_uuid", "vm_name", "vsd_uuid", "vsd_name"])
  |> pivot(rowKey: ["_time", "vm_uuid", "vm_name", "vsd_uuid", "vsd_name"],
           columnKey: ["_field"], valueColumn: "_value")
'''
        for r in self._iter_query(flux):
            yield {
                'time':             r['time'],
                'vm_uuid':          r.get('vm_uuid'),
                'vm_name':          r.get('vm_name'),
                'vsd_uuid':         r.get('vsd_uuid'),
                'vsd_name':         r.get('vsd_name'),
                'read_latency_us':  r.get('read_latency_us',  0.0),
                'write_latency_us': r.get('write_latency_us', 0.0),
            }

    def query_vsd_throughput(self, lookback_days: int) -> Generator[dict, None, None]:
        """
        Yield per-VSD read/write throughput samples (kBps).
        NVRAM, VTPM, IDE_CDROM, and IDE_FLOPPY device types are excluded.
        Fields: time, vm_uuid, vsd_uuid, vsd_name, read_kbps, write_kbps
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: {self._window(lookback_days)})
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "vsd_metrics"
       and (r._field == "read_kbps" or r._field == "write_kbps"))
  |> filter(fn: (r) => r.vsd_type != "NVRAM" and r.vsd_type != "VTPM"
       and r.vsd_type != "IDE_CDROM" and r.vsd_type != "IDE_FLOPPY")
  |> keep(columns: ["_time", "_field", "_value", "vm_uuid", "vm_name", "vsd_uuid", "vsd_name"])
  |> pivot(rowKey: ["_time", "vm_uuid", "vm_name", "vsd_uuid", "vsd_name"],
           columnKey: ["_field"], valueColumn: "_value")
'''
        for r in self._iter_query(flux):
            yield {
                'time':       r['time'],
                'vm_uuid':    r.get('vm_uuid'),
                'vm_name':    r.get('vm_name'),
                'vsd_uuid':   r.get('vsd_uuid'),
                'vsd_name':   r.get('vsd_name'),
                'read_kbps':  r.get('read_kbps',  0.0),
                'write_kbps': r.get('write_kbps', 0.0),
            }

    # ------------------------------------------------------------------
    # drive_metrics queries  (generator — stream large time-series)
    # ------------------------------------------------------------------

    def query_drive_capacity(self, lookback_days: int) -> Generator[dict, None, None]:
        """
        Yield per-drive used_bytes samples.
        Fields: time, node_uuid, drive_uuid, drive_slot, value (used_bytes)
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: {self._window(lookback_days)})
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "drive_metrics" and r._field == "used_bytes")
  |> keep(columns: ["_time", "_value", "node_uuid", "drive_uuid", "drive_slot"])
'''
        for r in self._iter_query(flux):
            yield {'time': r['time'], 'node_uuid': r.get('node_uuid'),
                   'drive_uuid': r.get('drive_uuid'), 'drive_slot': r.get('drive_slot'),
                   'value': r['value']}

    # ------------------------------------------------------------------
    # small lookup queries  (lists — result sets are tiny)
    # ------------------------------------------------------------------

    def query_vm_vcpu(self) -> dict[str, int]:
        """
        Return the latest num_vcpu per VM from the vm_inventory measurement.
        Returns a dict of {vm_uuid: num_vcpu}.
        Uses a 24-hour lookback — vm_inventory is written every slow-poll cycle
        so the most recent value is always within that window.
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: -24h)
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "vm_inventory" and r._field == "num_vcpu")
  |> last()
  |> keep(columns: ["_time", "_value", "vm_uuid"])
'''
        rows = self._query(flux)
        return {r['vm_uuid']: int(r['value'] or 0) for r in rows if r.get('vm_uuid')}

    def query_node_mem_capacity(self) -> dict[str, int]:
        """
        Return the latest mem_size (total physical RAM, bytes) per node.
        Returns a dict of {node_uuid: mem_size_bytes}.
        Uses a 24-hour lookback — node_metrics is written every collection cycle.
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: -24h)
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "node_metrics" and r._field == "mem_size")
  |> last()
  |> keep(columns: ["_time", "_value", "node_uuid"])
'''
        rows = self._query(flux)
        return {r['node_uuid']: int(r['value'] or 0) for r in rows if r.get('node_uuid')}

    def query_vm_mem_assigned(self) -> dict[str, int]:
        """
        Return the latest mem_bytes (assigned RAM, bytes) per currently-running VM.
        Powered-off VMs hold no HA reservation and are excluded.
        Returns a dict of {vm_uuid: mem_bytes}.

        The grouping matters here.  Default Flux grouping after `from() filter()`
        includes every tag — vm_state among them.  If we did `last()` first we
        would get the last record *per (vm_uuid, vm_state) combination*, so a
        VM that ran yesterday and is stopped today would still surface its
        yesterday-RUNNING record and inflate the assigned total.  Explicitly
        regrouping to just `vm_uuid` collapses every state into one stream per
        VM, so `last()` gives the genuine most-recent record and the trailing
        state filter keeps only those whose latest state is RUNNING.
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: -24h)
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "vm_inventory" and r._field == "mem_bytes")
  |> group(columns: ["vm_uuid"])
  |> last()
  |> filter(fn: (r) => r.vm_state == "RUNNING")
  |> keep(columns: ["_time", "_value", "vm_uuid"])
'''
        rows = self._query(flux)
        return {r['vm_uuid']: int(r['value'] or 0) for r in rows if r.get('vm_uuid')}

    def query_running_vms(self, window_minutes: int = 30) -> set[str]:
        """
        Return the set of vm_uuids whose latest vm_inventory record reports
        vm_state == "RUNNING".  Used by the analysis layer to skip
        powered-off and deleted VMs.

        See `query_vm_mem_assigned` for the explanation of the grouping
        pattern — same situation: regroup to `vm_uuid` only before `last()`
        so that filtering on `vm_state == "RUNNING"` after the fact reflects
        each VM's *current* state, not "was running at any point in the window".

        The window must be short enough that a deleted VM (collector stops
        writing inventory records) falls out quickly — otherwise the analysis
        loop keeps re-introducing it into right_sizing / resize_recommendations
        for up to the window length, racing against the 15-min prune loop.
        Default 30 min comfortably covers slow-poll cycles (default 5 min)
        plus a few missed polls for safety.

        Returns an empty set on InfluxDB error so callers can fall back to no
        filtering rather than purging everything.
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: -{window_minutes}m)
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "vm_inventory" and r._field == "mem_bytes")
  |> group(columns: ["vm_uuid"])
  |> last()
  |> filter(fn: (r) => r.vm_state == "RUNNING")
  |> keep(columns: ["vm_uuid"])
'''
        try:
            rows = self._query(flux)
            return {r['vm_uuid'] for r in rows if r.get('vm_uuid')}
        except Exception:
            log.exception('query_running_vms failed')
            return set()

    def query_active_vms(self, window_minutes: int) -> dict[str, str]:
        """
        Return VMs seen in the last window_minutes from vm_metrics.
        Returns {vm_uuid: vm_name} for all VMs with recent InfluxDB data.
        Used by the pruning loop to identify stale VM records in PostgreSQL.
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: -{window_minutes}m)
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "vm_metrics" and r._field == "cpu_usage")
  |> group(columns: ["vm_uuid", "vm_name"])
  |> last()
  |> keep(columns: ["vm_uuid", "vm_name"])
'''
        rows = self._query(flux)
        return {
            r['vm_uuid']: r.get('vm_name', '')
            for r in rows
            if r.get('vm_uuid')
        }

    def ping(self) -> int:
        """
        Lightweight connectivity and data-flow check.

        Counts distinct nodes seen in the last 20 minutes — cheap enough to run
        every collection interval without impacting InfluxDB.  Returns the node
        count (may be 0 on a fresh install).  Raises on InfluxDB error so the
        caller can record a collection failure.
        """
        flux = f'''
from(bucket: "{self._cfg.influx_bucket}")
  |> range(start: -20m)
  |> filter(fn: (r) => r.cluster_id == "{self._cluster_id}")
  |> filter(fn: (r) => r._measurement == "node_metrics" and r._field == "cpu_usage")
  |> group(columns: ["node_uuid"])
  |> last()
  |> keep(columns: ["_time", "_value", "node_uuid"])
'''
        rows = self._query(flux)
        return len(rows)

    def close(self) -> None:
        self._client.close()
