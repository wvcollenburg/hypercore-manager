import os
import re
import gc
import time
import warnings
import concurrent.futures
import pandas as pd
from prophet import Prophet
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import logging

# Suppress Prophet's chatty terminal output
warnings.filterwarnings("ignore")
logging.getLogger('cmdstanpy').setLevel(logging.WARNING)

import config_db

# InfluxDB Configuration (configurables — stay in .env)
INFLUX_URL = os.getenv('INFLUX_URL', "http://localhost:8086")
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN', "super-secret-auth-token")
INFLUX_ORG = os.getenv('INFLUX_ORG', "hypercore")
INFLUX_BUCKET = os.getenv('INFLUX_BUCKET', "metrics")

# Tunables are read live from config_db at forecast time (see get_proactive_migrations)


class ProactiveMigration:
    """A simple data object to hold a migration recommendation."""
    def __init__(self, vm_uuid, vm_name, source_node, target_node, reason, peak_time, source_ip, target_ip):
        self.vm_uuid = vm_uuid
        self.vm_name = vm_name
        self.source_node = source_node
        self.target_node = target_node
        self.reason = reason
        self.peak_time = peak_time
        self.source_ip = source_ip
        self.target_ip = target_ip


def _sanitize_uuid(value: str) -> str:
    """Validates that a value looks like a UUID to prevent Flux query injection."""
    if not re.match(r'^[a-fA-F0-9\-]+$', value):
        raise ValueError(f"Invalid UUID format: {value}")
    return value


def _write_run_summary(summary):
    """Persist a per-run summary to InfluxDB (measurement: predictive_runs).
    Best-effort — failures are logged but never raised, since the run already succeeded.
    """
    try:
        with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
            with client.write_api(write_options=SYNCHRONOUS) as writer:
                p = Point("predictive_runs") \
                    .tag("success", str(summary.get('success', True)).lower()) \
                    .field("vms_total",                int(summary.get('vms_total', 0))) \
                    .field("vms_forecast",             int(summary.get('vms_forecast', 0))) \
                    .field("vms_skipped_short_history", int(summary.get('vms_skipped_short_history', 0))) \
                    .field("vms_skipped_other",        int(summary.get('vms_skipped_other', 0))) \
                    .field("nodes_flagged",            int(summary.get('nodes_flagged', 0))) \
                    .field("recommendations",          int(summary.get('recommendations', 0))) \
                    .field("peak_node_load_percent",   float(summary.get('peak_node_load_percent', 0.0))) \
                    .field("threshold_used",           float(summary.get('threshold_used', 0.0))) \
                    .field("lookback_days",            int(summary.get('lookback_days', 0))) \
                    .field("min_history_hours",        int(summary.get('min_history_hours', 0))) \
                    .field("duration_seconds",         float(summary.get('duration_seconds', 0.0))) \
                    .field("peak_node_ip",             str(summary.get('peak_node_ip', ''))) \
                    .field("error",                    str(summary.get('error', '')))
                writer.write(bucket=INFLUX_BUCKET, record=p)
    except Exception as e:
        print(f"[ORACLE] Failed to write run summary: {e}")


def _write_recommendation(rec, predicted_peak_load, vm_predicted_cpu):
    """Persist a single recommendation to InfluxDB (measurement: predictive_recommendations).
    Best-effort — failures are logged but never raised.
    """
    try:
        with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
            with client.write_api(write_options=SYNCHRONOUS) as writer:
                lead_seconds = max(0.0, rec.peak_time - time.time())
                p = Point("predictive_recommendations") \
                    .tag("vm_name",   rec.vm_name) \
                    .tag("source_ip", rec.source_ip) \
                    .tag("target_ip", rec.target_ip) \
                    .field("vm_uuid",              rec.vm_uuid) \
                    .field("reason",               rec.reason) \
                    .field("peak_time_unix",       float(rec.peak_time)) \
                    .field("predicted_peak_load",  float(predicted_peak_load)) \
                    .field("vm_predicted_cpu",     float(vm_predicted_cpu)) \
                    .field("lead_seconds",         float(lead_seconds))
                writer.write(bucket=INFLUX_BUCKET, record=p)
    except Exception as e:
        print(f"[ORACLE] Failed to write recommendation: {e}")


def get_proactive_migrations(vms, nodes, forecast_hours=24):
    # Read tunables live so each Oracle run picks up any dashboard changes
    threshold       = config_db.get('PREDICTIVE_THRESHOLD')       or 80.0
    min_history     = config_db.get('PREDICTIVE_MIN_HISTORY_HOURS') or 336
    lookback_days   = config_db.get('PREDICTIVE_LOOKBACK_DAYS')    or 90
    max_workers     = config_db.get('PREDICTIVE_MAX_WORKERS')      or 8
    print(f"[ORACLE] Waking up. Forecasting the next {forecast_hours} hours across {len(vms)} VMs "
          f"using up to {max_workers} workers (lookback={lookback_days}d, threshold={threshold}%)...")

    started = time.time()
    summary = {
        'vms_total': len(vms),
        'threshold_used': float(threshold),
        'lookback_days': int(lookback_days),
        'min_history_hours': int(min_history),
        'success': True,
    }
    try:
        recs = _run_forecast(vms, nodes, forecast_hours, lookback_days, min_history, max_workers, threshold, summary)
    except Exception as e:
        summary['success'] = False
        summary['error'] = str(e)
        summary['duration_seconds'] = time.time() - started
        _write_run_summary(summary)
        raise
    summary['duration_seconds'] = time.time() - started
    summary['recommendations'] = len(recs)
    _write_run_summary(summary)
    return recs


def _forecast_single_vm(vm, forecast_hours, lookback_days, min_history_hours):
    """Forecast CPU load for a single VM. Creates its own DB connection."""
    vm_uuid = vm.get('uuid')
    vm_name = vm.get('name', 'Unknown')
    current_node = vm.get('nodeUUID')
    vcpus = vm.get('numVCPU', 1)

    if not current_node:
        return {'skipped': 'other', 'name': vm_name}

    try:
        safe_uuid = _sanitize_uuid(vm_uuid)
    except ValueError as e:
        print(f"[ORACLE] Skipping VM with bad UUID: {e}")
        return {'skipped': 'other', 'name': vm_name}

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    try:
        query = f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{lookback_days}d)
          |> filter(fn: (r) => r["_measurement"] == "vm_metrics")
          |> filter(fn: (r) => r["vm_uuid"] == "{safe_uuid}")
          |> filter(fn: (r) => r["_field"] == "cpu_usage")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
          |> yield(name: "mean")
        """
        result = client.query_api().query(org=INFLUX_ORG, query=query)
    except Exception as e:
        print(f"[ORACLE] Failed to fetch data for {vm_name}: {e}")
        return {'skipped': 'other', 'name': vm_name}
    finally:
        client.close()

    records = []
    for table in result:
        for record in table.records:
            dt = record.get_time().replace(tzinfo=None)
            records.append({'ds': dt, 'y': record.get_value()})

    # min_history_hours (default 336 = 2 weeks) gives Prophet enough data for weekly patterns.
    if len(records) < min_history_hours:
        print(f"[ORACLE] Skipping {vm_name}: Not enough historical data ({len(records)} hours). Needs at least {min_history_hours}.")
        return {'skipped': 'short_history', 'name': vm_name}

    df = pd.DataFrame(records)
    del records
    last_date = df['ds'].max()

    m = Prophet(daily_seasonality=15, weekly_seasonality=20, yearly_seasonality=False, seasonality_prior_scale=15.0)
    m.fit(df)
    del df

    future = m.make_future_dataframe(periods=forecast_hours, freq='h')
    forecast = m.predict(future)
    del m, future

    future_forecast = forecast[forecast['ds'] > last_date]
    peak_pred = future_forecast.loc[future_forecast['yhat_upper'].idxmax()]
    max_expected_cpu = min(peak_pred['yhat_upper'], 100.0)
    peak_timestamp = peak_pred['ds'].timestamp()
    del forecast, future_forecast

    return {
        'uuid': vm_uuid,
        'name': vm_name,
        'current_node': current_node,
        'raw_expected_cpu': max_expected_cpu,
        'vcpus': vcpus,
        'peak_timestamp': peak_timestamp
    }


def _run_forecast(vms, nodes, forecast_hours, lookback_days, min_history_hours, max_workers, threshold, summary=None):
    """Core forecasting logic using a thread pool for parallel VM forecasting."""

    node_threads = {n['uuid']: n.get('numThreads', 1) for n in nodes}
    node_ips = {n['uuid']: n.get('lanIP', n['uuid'][:8]) for n in nodes}

    predicted_node_loads = {n['uuid']: 0.0 for n in nodes}
    vm_forecasts = []
    recommendations = []
    skipped_short_history = 0
    skipped_other = 0

    # Anti-affinity lookups — built from current placement, updated as moves are planned
    # vm_anti_by_name: vm_name → set of VM names it must not share a node with
    vm_anti_by_name = {}
    # planned_node_vm_names: node_uuid → set of VM names planned to be there
    planned_node_vm_names = {n['uuid']: set() for n in nodes}
    for vm in vms:
        name      = vm.get('name', '')
        tags_str  = vm.get('tags', '')
        anti      = {t.strip()[5:] for t in tags_str.split(',') if t.strip().startswith('anti_')}
        vm_anti_by_name[name] = anti
        node_uuid = vm.get('nodeUUID')
        if node_uuid and node_uuid in planned_node_vm_names:
            planned_node_vm_names[node_uuid].add(name)

    # Phase 1: Parallel VM forecasting
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_forecast_single_vm, vm, forecast_hours, lookback_days, min_history_hours): vm.get('name', 'Unknown')
            for vm in vms
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                print(f"[ORACLE] Unexpected error forecasting {futures[future]}: {e}")
                skipped_other += 1
                continue
            if not result:
                skipped_other += 1
                continue
            if result.get('skipped') == 'short_history':
                skipped_short_history += 1
                continue
            if result.get('skipped'):
                skipped_other += 1
                continue
            vm_forecasts.append(result)
            current_node_threads = node_threads.get(result['current_node'], 1)
            normalized_impact = result['raw_expected_cpu'] * (result['vcpus'] / current_node_threads)
            if result['current_node'] in predicted_node_loads:
                predicted_node_loads[result['current_node']] += normalized_impact

    # Phase 2: Analyze future node states and generate migration recommendations
    print("[ORACLE] Forecast complete. Analyzing future node capacities...")
    nodes_flagged = 0
    peak_node_load = 0.0
    peak_node_ip = ''
    if predicted_node_loads:
        peak_uuid, peak_node_load = max(predicted_node_loads.items(), key=lambda kv: kv[1])
        peak_node_ip = node_ips.get(peak_uuid, peak_uuid[:8])

    for node_uuid, future_load in predicted_node_loads.items():
        source_ip = node_ips.get(node_uuid, node_uuid[:8])

        if future_load <= threshold:
            continue
        nodes_flagged += 1

        print(f"[ORACLE] WARNING: Node {source_ip} predicted to hit {future_load:.1f}% CPU. "
              f"Queuing as many moves as needed to bring it below {threshold}%.")

        # Sort lightest-first: each candidate is easy to place, making it more likely
        # we find a safe target and can iterate through multiple VMs if needed.
        vms_on_node = sorted(
            [v for v in vm_forecasts if v['current_node'] == node_uuid],
            key=lambda x: x['raw_expected_cpu']
        )

        for candidate_vm in vms_on_node:
            if predicted_node_loads[node_uuid] <= threshold:
                break  # Node is now predicted safe — stop evicting

            # Re-sort targets each iteration so we always pick the least-loaded one
            target_nodes = sorted(
                [n for n in nodes if n['uuid'] != node_uuid],
                key=lambda n: predicted_node_loads.get(n['uuid'], 100.0)
            )

            placed = False
            candidate_name = candidate_vm['name']
            candidate_anti = vm_anti_by_name.get(candidate_name, set())

            for best_target in target_nodes:
                target_ip = node_ips.get(best_target['uuid'], best_target['uuid'][:8])
                target_future_load = predicted_node_loads.get(best_target['uuid'], 100.0)
                target_threads = node_threads.get(best_target['uuid'], 1)
                target_impact = candidate_vm['raw_expected_cpu'] * (candidate_vm['vcpus'] / target_threads)

                # Anti-affinity: skip target if candidate's anti_X tag matches a VM there,
                # or if any VM on the target has anti_<candidate> pointing back at us
                tgt_names = planned_node_vm_names.get(best_target['uuid'], set())
                tgt_anti  = {a for n in tgt_names for a in vm_anti_by_name.get(n, set())}
                if candidate_anti & tgt_names:
                    print(f"[ORACLE] Skipping {target_ip} for {candidate_name}: anti-affinity conflict")
                    continue
                if candidate_name in tgt_anti:
                    print(f"[ORACLE] Skipping {target_ip} for {candidate_name}: anti-affinity conflict (reverse)")
                    continue

                if (target_future_load + target_impact) < threshold:
                    predicted_peak_at_source = predicted_node_loads[node_uuid]
                    reason = (f"Preventing future overload "
                              f"({predicted_peak_at_source:.1f}%) on source node {source_ip}.")
                    rec = ProactiveMigration(
                        vm_uuid=candidate_vm['uuid'],
                        vm_name=candidate_name,
                        source_node=node_uuid,
                        target_node=best_target['uuid'],
                        reason=reason,
                        peak_time=candidate_vm['peak_timestamp'],
                        source_ip=source_ip,
                        target_ip=target_ip
                    )
                    recommendations.append(rec)
                    _write_recommendation(rec, predicted_peak_at_source, candidate_vm['raw_expected_cpu'])

                    source_impact = candidate_vm['raw_expected_cpu'] * (candidate_vm['vcpus'] / node_threads.get(node_uuid, 1))
                    predicted_node_loads[node_uuid]          -= source_impact
                    predicted_node_loads[best_target['uuid']] += target_impact
                    # Update planned placement so subsequent iterations respect this move
                    planned_node_vm_names[node_uuid].discard(candidate_name)
                    planned_node_vm_names[best_target['uuid']].add(candidate_name)
                    print(f"[ORACLE] Queuing: {candidate_name} -> {target_ip} "
                          f"(node load now {predicted_node_loads[node_uuid]:.1f}%)")
                    placed = True
                    break

            if not placed:
                # If we couldn't place the lightest remaining VM, heavier ones won't fit either
                print(f"[ORACLE] No safe target for {candidate_vm['name']} — cluster headroom exhausted. "
                      f"Node {source_ip} remains at {predicted_node_loads[node_uuid]:.1f}% predicted load. "
                      f"Reactive engine will handle the rest.")
                break

        if predicted_node_loads[node_uuid] > threshold:
            print(f"[ORACLE] Node {source_ip}: predicted load still {predicted_node_loads[node_uuid]:.1f}% "
                  f"after all possible moves.")

    if summary is not None:
        summary['vms_forecast'] = len(vm_forecasts)
        summary['vms_skipped_short_history'] = skipped_short_history
        summary['vms_skipped_other'] = skipped_other
        summary['nodes_flagged'] = nodes_flagged
        summary['peak_node_load_percent'] = float(peak_node_load)
        summary['peak_node_ip'] = peak_node_ip

    del vm_forecasts
    gc.collect()

    return recommendations