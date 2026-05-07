/* HyperCore Manager — English (American) translations */
window.LANG = {
  // Header
  'header.connecting':    'Connecting...',
  'header.connected':     'Connected',
  'header.disconnected':  'Disconnected',
  'header.refresh':       '↻ Refresh',

  // Nav tabs
  'nav.modules':          'Modules',
  'nav.settings':         '⚙ Settings',

  // Infraviewer sub-tabs
  'iv.nodes_drives':      'Nodes & Drives',
  'iv.vms_storage':       'VMs & Storage',

  // Clusterbalancer sub-tabs
  'cb.miglog':            'Migration Log',
  'cb.balancer_settings': 'Balancer Settings',

  // Sentinel sub-tabs
  'sentinel.tab.overview':    'Overview',
  'sentinel.tab.forecasts':   'Forecasts',
  'sentinel.tab.rightsizing': 'Right-Sizing',
  'sentinel.tab.exhaustion':  'Exhaustion',
  'sentinel.tab.reports':     'Reports',
  'sentinel.tab.whatif':      'What-If',
  'sentinel.tab.settings':    'Settings',
  'sentinel.tab.datasharing': 'Data Sharing',

  // Time range / grouping controls
  'time.range':           'Range:',
  'vm.group_by':          'Group by:',
  'vm.group_none':        'None',
  'vm.group_no_tag':      '(no tag)',
  'vm.group_label.tag':   'Tag',
  'vm.group_label.node':  'Node',
  'vm.group_label.vlan':  'VLAN',
  'vm.group_label.peer':  'Peer',
  'vm.performance_details': 'Performance Details',

  // Chart titles
  'chart.node_cpu':       'Node CPU Usage Over Time',
  'chart.cluster_iops':   'Total Cluster IOPS Over Time',
  'chart.cluster_ram':    'Cluster RAM Usage',
  'chart.cluster_storage':'Cluster Storage Usage',
  'chart.vm_cpu':         'VM CPU Usage (top 5)',
  'chart.vsd_latency':    'VSD Latency (top 5 by write latency)',
  'chart.vm_net':         'VM Network Usage — top 5 (RX + TX)',
  'chart.miglog':         'Migration Event Log',

  // Section / panel headings
  'section.capacity_forecasts': 'Capacity Forecasts',
  'section.resize_advice':      'Concrete Resize Advice',
  'section.exhaustion_alerts':  'Resource Exhaustion Alerts',
  'section.whatif':             'What-If Scenario Modelling',

  // IOPS stat labels
  'iops.p95':   '95th Percentile:',
  'iops.min':   'Min:',
  'iops.max':   'Max:',
  'iops.iops':  'IOPS',
  'iops.insufficient': '95th Percentile: insufficient data',

  // Loading states
  'loading.nodes':      'Loading node metrics...',
  'loading.vms':        'Loading VM metrics...',
  'loading.migrations': 'Loading migration events...',
  'loading.settings':   'Loading settings...',
  'loading.capacity':   'Loading capacity intelligence...',
  'loading.forecasts':  'Loading forecasts...',
  'loading.resize':     'Loading resize advice...',
  'loading.exhaustion': 'Loading exhaustion data...',
  'loading.simulation': 'Running simulation...',
  'loading.modules':    'Discovering modules...',

  // Empty / error states
  'empty.no_api':               'Could not connect to API',
  'empty.no_nodes':             'No node data yet. Is the collector running?',
  'empty.no_vms':               'No VM data yet.',
  'empty.no_migrations':        'No migration events recorded yet.',
  'empty.no_measurements':      'No measurements found.',
  'empty.no_forecasts':         'No forecast data available.',
  'empty.no_resize':            'No concrete resize advice yet.',
  'empty.no_exhaustion':        'No resource exhaustion alerts.',
  'empty.sentinel_unavailable': 'SC//Foresight data unavailable — is Sentinel running?',
  'empty.settings_failed':      'Could not load settings.',
  'empty.sentinel_settings_unavail': 'Sentinel unavailable — settings cannot be loaded.',
  'empty.settings_load_failed': 'Failed to load settings: {msg}',

  // Metric labels (node / VM cards)
  'metric.cpu_usage':   'CPU Usage',
  'metric.memory_used': 'Memory Used',
  'metric.vcpu_ratio':  'vCPU Ratio',
  'metric.data_points': 'Data Points',
  'metric.network_rx':  'Network RX',
  'metric.network_tx':  'Network TX',

  // Physical drive sub-items
  'drive.physical_drives': '◉ Physical Drives',
  'drive.slot':      'Slot',
  'drive.healthy':   'Healthy',
  'drive.degraded':  'Degraded',
  'drive.used':      'Used',
  'drive.temperature': 'Temperature',
  'drive.errors':    'Errors',

  // Virtual storage device sub-items
  'vsd.devices':       '◈ Virtual Storage Devices',
  'vsd.read':          'Read',
  'vsd.write':         'Write',
  'vsd.read_latency':  'Read Latency',
  'vsd.write_latency': 'Write Latency',

  // Migration log table
  'mig.time':      'Time',
  'mig.vm':        'VM',
  'mig.migration': 'Migration',
  'mig.engine':    'Engine',
  'mig.mode':      'Mode',
  'mig.reason':    'Reason',

  // VM detail modal chart labels
  'vm.modal.cpu':          'CPU Usage',
  'vm.modal.network':      'Network (RX / TX)',
  'vm.modal.disk_iops':    'Disk IOPS (per VSD)',
  'vm.modal.disk_latency': 'Disk Latency (per VSD)',

  // Settings controls
  'settings.apply':      'Apply',
  'settings.unsaved':    '● unsaved',
  'settings.applied_n':  'Applied {n} change(s)',
  'settings.save_failed':'Save failed: {msg}',
  'settings.restart_required': 'Restart-required settings',
  'settings.restart_note': 'Restart-required settings — collection interval, trend interval, outcome check interval, ports, InfluxDB / PostgreSQL / HyperCore credentials, webhook URL, secret and delivery parameters must be set via environment variables in docker-compose and require a container restart.',

  // Info popup section labels
  'info.what':     'What it does',
  'info.benefits': 'Benefits',
  'info.careful':  'Be careful',

  // Settings group labels
  'group.master':      'Master Controls',
  'group.reactive':    'Reactive Balancer',
  'group.predictive':  'Predictive Engine  ·  The Oracle',
  'group.collector':   'Collector',
  'group.dashboard':   'Dashboard',
  'group.rightsizing': 'Right-Sizing Thresholds',
  'group.forecast':    'Forecast & Analysis',
  'group.maintenance': 'Maintenance',

  // Sentinel overview
  'sentinel.cluster_health':    'Cluster health',
  'sentinel.severity':          'Severity',
  'sentinel.bottleneck':        'Bottleneck resource',
  'sentinel.efficiency':        'Efficiency index',
  'sentinel.as_of':             'As of',
  'sentinel.health_breakdown':       'Component Health Breakdown',
  'sentinel.health_breakdown_hint':  'Each dimension scores 0–100. Lower scores drag the overall cluster health down — focus on the dimension flagged as the bottleneck above.',
  'sentinel.based_on':               'based on:',
  'sentinel.active_forecasts':  'Active Forecasts',
  'sentinel.over_provisioned':  'Over-Provisioned VMs',
  'sentinel.under_provisioned': 'Under-Provisioned VMs',
  'sentinel.worst_exhaustion':  'Worst Exhaustion',
  'sentinel.days':              'days',
  'sentinel.already_exhausted': 'already exhausted',
  'sentinel.exhaustion_in':     'exhaustion in',
  'sentinel.warming_title':     'SC//Foresight is warming up',
  'sentinel.warming_running':   'Sentinel is running and collecting metrics. The first capacity analysis will produce recommendations shortly.',
  'sentinel.warming_first_run': 'First analysis is running now\u2026',
  'sentinel.warming_first_in':  'First analysis in',
  'sentinel.warming_started':   'Started',

  // Forecast table
  'forecast.dimension':    'Dimension',
  'forecast.horizon':      'Horizon',
  'forecast.node_entity':  'Node / Entity',
  'forecast.current':      'Current',
  'forecast.forecast':     'Forecast',
  'forecast.confidence':   'Confidence',
  'forecast.exhaustion':   'Exhaustion',
  'forecast.severity':     'Severity',
  'forecast.showing':      'Showing {n} of {total} forecasts \u2014 low-confidence INFO rows hidden.',
  'forecast.no_significant': 'No significant forecasts for this filter (all results have low confidence or INFO severity).',
  'forecast.all_dimensions': 'All dimensions',
  'forecast.all_horizons':   'All horizons',

  // Right-sizing table
  'rs.vm_name':           'VM Name',
  'rs.direction':         'Direction',
  'rs.current_vcpu':      'Current vCPU',
  'rs.recommended':       'Recommended',
  'rs.change_pct':        'Change%',
  'rs.risk':              'Risk',
  'rs.severity':          'Severity',
  'rs.confidence':        'Confidence',
  'rs.reclaim':           'Reclaim%',
  'rs.reduce':            '\u2193 Reduce',
  'rs.increase':          '\u2191 Increase',
  'rs.act_now':           'Act Now',
  'rs.recommended_action':'Recommended',
  'rs.low_priority':      'Low Priority',

  // Exhaustion table
  'ex.dimension':      'Dimension',
  'ex.node':           'Node',
  'ex.days_left':      'Days Left',
  'ex.exhaustion_date':'Exhaustion Date',
  'ex.growth_rate':    'Growth Rate/day',
  'ex.severity':       'Severity',

  // Dimension labels
  'dim.cpu':     'CPU',
  'dim.ram':     'RAM',
  'dim.storage': 'Storage',
  'dim.latency': 'Latency',
  'dim.network': 'Network',

  // What-If panel
  'whatif.scenario':         'Scenario',
  'whatif.add_nodes':        'Add Nodes',
  'whatif.add_vms':          'Add VMs',
  'whatif.add_vms_like':     'Add VMs Like\u2026',
  'whatif.count':            'Count',
  'whatif.storage_per_node': 'Storage / node (TB, optional)',
  'whatif.ram_per_node':     'RAM / node (GB, optional)',
  'whatif.template_vm':      'Template VM',
  'whatif.run_simulation':   '\u25b6 Run Simulation',
  'whatif.exhaustion_changes': 'EXHAUSTION DATE CHANGES',
  'whatif.baseline':         'BASELINE (current)',
  'whatif.simulated':        'SIMULATED',
  'whatif.nodes':            'node(s)',
  'whatif.vms':              'VM(s)',
  'whatif.vms_like':         'VM(s) like',
  'whatif.bottleneck_changed': '(changed)',
  'whatif.dimension':        'DIMENSION',
  'whatif.baseline_col':     'BASELINE',
  'whatif.simulated_col':    'SIMULATED',
  'whatif.gained':           'GAINED',
  'whatif.unchanged':        'unchanged',
  'whatif.no_exhaustion':    'No exhaustion data available.',
  'whatif.assumptions':      'Assumptions:',
  'whatif.sim_failed':       'Simulation failed:',
  'whatif.enter_template_vm':'Please enter a template VM name.',
  'whatif.nodes_label':      'nodes',
  'whatif.vms_label':        'VMs',
  'whatif.bottleneck':       'Bottleneck:',
  'whatif.none':             'none',

  // Reports / PDF
  'reports.branding_title':      'Report Branding',
  'reports.branding_desc':       'Customise the PDF report cover page. Settings are saved per-cluster and applied to every generated report.',
  'reports.company_name':        'Company Name',
  'reports.accent_colour':       'Accent Colour',
  'reports.logo':                'Logo (PNG or JPG, max 500 KB)',
  'reports.logo_remove':         '\u2715 Remove',
  'reports.logo_too_large':      'Logo must be under 500 KB.',
  'reports.save_branding':       'Save Branding',
  'reports.saved':               '\u2713 Saved',
  'reports.pdf_title':           'SC//Foresight Reports',
  'reports.pdf_desc':            'Generate a full capacity intelligence PDF report from the latest analysis data. The report includes forecasts, right-sizing recommendations, exhaustion alerts, and resize advice.',
  'reports.download_pdf':        '\u2193 Download PDF Report',
  'reports.generating':          'Generating\u2026',
  'reports.building':            'Building report, please wait\u2026',
  'reports.contents':            'Report Contents',
  'reports.contents_summary':    '\u25c6 Executive summary with cluster health score',
  'reports.contents_exhaustion': '\u25c6 Resource exhaustion alerts (ordered by urgency)',
  'reports.contents_forecasts':  '\u25c6 Capacity forecasts \u2014 30d, 90d, 180d, 365d horizons',
  'reports.contents_rightsizing':'\u25c6 VM right-sizing recommendations',
  'reports.contents_resize':     '\u25c6 Concrete resize advice with risk classification',

  // Data Sharing panel
  'sharing.connection_title':   'Connection',
  'sharing.connection_desc':    'Configure the SC//Foresight-Central viewer endpoint to enable scheduled snapshot pushes.',
  'sharing.enabled':            'Sharing enabled',
  'sharing.viewer_url':         'Viewer URL',
  'sharing.bearer_token':       'Bearer Token',
  'sharing.save_connection':    'Save Connection',
  'sharing.anon_title':         'Anonymisation',
  'sharing.anon_desc':          'Anonymisation uses stable pseudonyms \u2014 trends remain intact.',
  'sharing.anon_vm_names':      'Anonymise VM names',
  'sharing.anon_node_ids':      'Anonymise node identifiers',
  'sharing.anon_cluster':       'Anonymise cluster name',
  'sharing.save_anon':          'Save Anonymisation',
  'sharing.manual_title':       'Manual Actions',
  'sharing.test_push':          'Test Push',
  'sharing.download_hcb':       'Download Snapshot (.hcb)',
  'sharing.preview_snapshot':   'Preview Snapshot (JSON)',
  'sharing.last_sync_never':    'Last sync: never',
  'sharing.last_sync':          'Last sync:',
  'sharing.token_hint_saved':   'Token is stored. Enter a new value to replace it.',
  'sharing.token_placeholder_saved': '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022  (saved \u2014 leave blank to keep)',
  'sharing.connection_saved':   'Connection settings saved',
  'sharing.anon_saved':         'Anonymisation settings saved',
  'sharing.load_failed':        'Could not load sharing settings',
  'sharing.test_push_sending':  'Sending test push\u2026',
  'sharing.test_push_ok':       'Test push succeeded (HTTP {code})',
  'sharing.test_push_failed':   'Test push failed:',
  'sharing.test_push_error':    'Test push error:',

  // HCB export modal
  'hcb.title':     'Export Snapshot (.hcb)',
  'hcb.desc':      'Set a passphrase to encrypt this file. You will need to enter the same passphrase when importing into SC//Foresight-Central.',
  'hcb.passphrase':'Passphrase',
  'hcb.confirm':   'Confirm passphrase',
  'hcb.cancel':    'Cancel',
  'hcb.download':  'Download',
  'hcb.required':  'Passphrase is required.',
  'hcb.mismatch':  'Passphrases do not match.',

  // Modules tab
  'modules.tools':           'Tools',
  'modules.grafana_desc':    'Advanced data exploration and custom dashboards',
  'modules.open_grafana':    'Open Grafana',
  'modules.grafana_note':    'Grafana is not started by default. To launch it, run:',
  'modules.discovered_title':'Discovered Data Modules',
  'modules.auto_appear':     'Every container that writes to InfluxDB appears here automatically.',

  // Grafana status
  'grafana.running':   '\u25cf Running',
  'grafana.not_running':'\u25cb Not running',
  'grafana.checking':  'Checking...',

  // Auto-discovered module fallback desc
  'module.auto_desc': 'Auto-discovered data source',

  // ─── Info popup body text (ⓘ) — Balancer & Collector settings ───────────
  'info.DRY_RUN.what':      'The balancer analyses the cluster and decides which VMs to migrate, but stops short of actually executing any moves. All decision logic runs normally — you see the outcomes in the logs.',
  'info.DRY_RUN.benefits':  'Essential safety net when first deploying or after changing thresholds significantly. Lets you validate behaviour before committing to live migrations.',
  'info.DRY_RUN.careful':   'While enabled, real resource imbalances are NOT corrected. A cluster under heavy load will stay imbalanced for as long as Dry Run is on.',

  'info.VERBOSITY.what':      'Controls how much detail appears in the balancer container logs. 0 = errors only \u00b7 1 = decisions \u00b7 2 = scoring detail \u00b7 3 = full trace.',
  'info.VERBOSITY.benefits':  'Higher levels are invaluable for diagnosing why a migration did or did not trigger. Lower levels keep logs clean in production.',
  'info.VERBOSITY.careful':   'Level 3 on a busy cluster with many VMs and nodes produces very large log volumes. Use it temporarily for debugging, then dial back.',

  'info.CPU_UPPER_THRESHOLD_PERCENT.what':      'CPU utilisation percentage above which a node is considered overloaded and becomes a migration source candidate.',
  'info.CPU_UPPER_THRESHOLD_PERCENT.benefits':  'Lowering the threshold makes balancing more proactive, keeping nodes cooler. Raising it tolerates higher utilisation before acting.',
  'info.CPU_UPPER_THRESHOLD_PERCENT.careful':   'Set too low (e.g. 30%) and the balancer churns VMs constantly based on normal usage spikes. Always pair with AVG_WINDOW to smooth out short bursts before deciding.',

  'info.RAM_UPPER_THRESHOLD_PERCENT.what':      'RAM utilisation percentage above which a node is flagged as memory-pressured and may have VMs migrated away from it.',
  'info.RAM_UPPER_THRESHOLD_PERCENT.benefits':  'Prevents nodes from reaching dangerously high RAM usage by triggering migrations early.',
  'info.RAM_UPPER_THRESHOLD_PERCENT.careful':   'Must always be set below RAM_HARD_LIMIT. Setting it too close to the hard limit leaves little reaction window before a node becomes critical.',

  'info.RAM_LIMIT_PERCENT.what':      'Hard RAM ceiling \u2014 nodes at or above this percentage cannot receive incoming VM migrations regardless of any other condition.',
  'info.RAM_LIMIT_PERCENT.benefits':  'Absolute guard against out-of-memory events. Even if the balancer wants to place a VM somewhere, this limit is never violated.',
  'info.RAM_LIMIT_PERCENT.careful':   'Must be higher than RAM_UPPER_THRESHOLD. If set equal to or below the threshold, every node is simultaneously a migration source and unable to accept \u2014 migrations become impossible.',

  'info.MAX_VCPU_RATIO.what':      'Maximum ratio of vCPUs allocated to VMs vs physical CPU cores on a node (e.g. 2.0\u00d7 means 2 vCPUs per physical core).',
  'info.MAX_VCPU_RATIO.benefits':  'Higher values allow more VMs per node through CPU overcommit, improving density. Lower values reserve physical capacity for latency-sensitive workloads.',
  'info.MAX_VCPU_RATIO.careful':   'Excessive overcommit (above 4\u20136\u00d7) noticeably degrades VM CPU performance under real load. The right value depends on your workload mix \u2014 idle VMs tolerate high ratios; compute-heavy ones do not.',

  'info.SAMPLE_INTERVAL_SECONDS.what':      'How often (in seconds) the balancer reads current node CPU and RAM usage to update its rolling averages.',
  'info.SAMPLE_INTERVAL_SECONDS.benefits':  'Shorter intervals give finer-grained averages. Longer intervals reduce API polling frequency on the HyperCore cluster.',
  'info.SAMPLE_INTERVAL_SECONDS.careful':   'Very short intervals produce noisy readings from brief spikes. Always pair with an appropriate AVG_WINDOW_MINUTES to smooth the data before making a decision.',

  'info.AVG_WINDOW_MINUTES.what':      'Length of the rolling window (in minutes) used to average CPU and RAM samples before any balancing decision is made.',
  'info.AVG_WINDOW_MINUTES.benefits':  'A longer window ignores transient spikes and bases decisions on sustained load. A shorter window reacts faster to genuine load changes.',
  'info.AVG_WINDOW_MINUTES.careful':   'The window must contain at least 2 samples. Minimum meaningful value: (2 \u00d7 SAMPLE_INTERVAL_SECONDS) / 60 minutes. If the window is shorter than one sample interval, decisions are based on a single reading.',

  'info.VM_MOVE_COOLDOWN_MINUTES.what':      'After a VM is migrated, it cannot be selected for migration again for this many minutes.',
  'info.VM_MOVE_COOLDOWN_MINUTES.benefits':  'Prevents a single VM from being shuffled repeatedly in quick succession, giving the cluster time to stabilise after each move.',
  'info.VM_MOVE_COOLDOWN_MINUTES.careful':   'Set too high and a VM that genuinely needs to move again must wait a long time. Setting to 0 removes all per-VM protection against migration storms.',

  'info.MIGRATION_COOLDOWN_MINUTES.what':      'Global lock \u2014 after any VM migration completes, no further migrations are initiated for this many minutes cluster-wide.',
  'info.MIGRATION_COOLDOWN_MINUTES.benefits':  'Prevents cascading migrations. One move changes the load landscape; this window lets the cluster settle before the next decision.',
  'info.MIGRATION_COOLDOWN_MINUTES.careful':   'Too high stalls balancing entirely on a heavily loaded cluster. Too low (or 0) can trigger rapid sequential migrations that destabilise rather than help.',

  'info.CPU_TARGET_TOLERANCE_PERCENT.what':      'When ranking target nodes in CPU_FOCUS mode, nodes whose average CPU falls within this percentage band are treated as equal on CPU, and the one with lower RAM usage is preferred.',
  'info.CPU_TARGET_TOLERANCE_PERCENT.benefits':  'Prevents a trivially lower CPU reading from sending a VM to a node that is already more RAM-constrained. Produces more balanced placement overall.',
  'info.CPU_TARGET_TOLERANCE_PERCENT.careful':   'Set too wide and significantly more-loaded nodes can be chosen over lighter ones. The default of 5% is a safe starting point for most clusters.',

  'info.EXCLUDE_NODE_IPS.what':      'Comma-separated list of node IP addresses that the balancer will completely ignore \u2014 it will never migrate VMs to or from these nodes.',
  'info.EXCLUDE_NODE_IPS.benefits':  'Protects nodes undergoing maintenance, dedicated workload hosts, or nodes under investigation from being touched by the balancer.',
  'info.EXCLUDE_NODE_IPS.careful':   'Excluding nodes reduces the balancer\'s available headroom. Excluded nodes still accumulate load and can become overloaded without the balancer intervening.',

  'info.PREDICTIVE_BALANCING_ENABLED.what':      'Enables "The Oracle" \u2014 a Prophet-based forecasting engine that analyses historical trends and proactively migrates VMs before predicted resource exhaustion.',
  'info.PREDICTIVE_BALANCING_ENABLED.benefits':  'Prevents overload before it happens rather than reacting after the fact. Especially valuable for workloads with predictable daily or weekly patterns.',
  'info.PREDICTIVE_BALANCING_ENABLED.careful':   'Requires at least MIN_HISTORY_HOURS of historical data per VM in InfluxDB. Running with insufficient history causes VMs to be silently skipped. Each run is CPU-intensive \u2014 ensure the container has adequate resources.',

  'info.PREDICTIVE_THRESHOLD.what':      'Predicted future resource usage percentage that, if forecast to be exceeded within the LEAD_TIME window, triggers a proactive migration.',
  'info.PREDICTIVE_THRESHOLD.benefits':  'Lower values are more aggressive, acting on smaller predicted exceedances. Higher values reserve action for near-certain overloads only.',
  'info.PREDICTIVE_THRESHOLD.careful':   'Set too low and normal daily usage peaks trigger unnecessary migrations. Set too high and genuine overload events may not be caught in time.',

  'info.PREDICTIVE_LEAD_TIME_HOURS.what':      'How far ahead (in hours) The Oracle looks when deciding whether a VM needs to be moved before a predicted resource spike.',
  'info.PREDICTIVE_LEAD_TIME_HOURS.benefits':  'Larger lead times give more flexibility for scheduling migrations during quiet periods, particularly useful for workloads with sharp, well-predicted peaks.',
  'info.PREDICTIVE_LEAD_TIME_HOURS.careful':   'Prophet\'s confidence intervals widen significantly beyond 24\u201348h, making very long lead times less reliable. Long lead times may also trigger migrations too far in advance of actual risk.',

  'info.PREDICTIVE_MIN_HISTORY_HOURS.what':      'Minimum hours of historical data a VM must have in InfluxDB before The Oracle will attempt to forecast it.',
  'info.PREDICTIVE_MIN_HISTORY_HOURS.benefits':  'Prevents garbage forecasts from newly provisioned VMs with insufficient history to reveal usage patterns.',
  'info.PREDICTIVE_MIN_HISTORY_HOURS.careful':   'Set too high and recently created VMs are never protected. The default 336h (14 days) captures at least two full weekly cycles \u2014 the recommended minimum for Prophet to detect patterns reliably.',

  'info.PREDICTIVE_LOOKBACK_DAYS.what':      'How many days of historical metric data are fed to Prophet when training each forecast. Longer lookbacks capture longer-term seasonal patterns.',
  'info.PREDICTIVE_LOOKBACK_DAYS.benefits':  'More data allows Prophet to detect weekly and monthly patterns more reliably, improving forecast accuracy.',
  'info.PREDICTIVE_LOOKBACK_DAYS.careful':   'More data means longer training time per VM. With many VMs and limited MAX_WORKERS, very long lookbacks can make forecast runs take hours. Also, InfluxDB\'s long-term bucket retention must cover this entire window.',

  'info.PREDICTIVE_MAX_WORKERS.what':      'Number of parallel Python processes used to train Prophet forecasts simultaneously across multiple VMs.',
  'info.PREDICTIVE_MAX_WORKERS.benefits':  'Higher worker counts reduce total forecast run time proportionally, which matters on large clusters with many VMs.',
  'info.PREDICTIVE_MAX_WORKERS.careful':   'Should not exceed the container\'s CPU limit (default 2.0 cores). More workers than cores causes them to compete and actually slows total throughput. Match this value to the balancer\'s CPU limit in docker-compose.',

  'info.PREDICTIVE_INTERVAL_SECONDS.what':      'How frequently The Oracle runs a full forecast cycle across all VMs, in seconds. Default 43200 = every 12 hours.',
  'info.PREDICTIVE_INTERVAL_SECONDS.benefits':  'More frequent runs keep predictions fresh and catch emerging trends sooner. Useful if your workload changes rapidly throughout the day.',
  'info.PREDICTIVE_INTERVAL_SECONDS.careful':   'Each full run is CPU-intensive, especially with many VMs. Intervals shorter than 3600s on large clusters risk back-to-back runs without recovery time. Monitor container CPU usage when reducing this value.',

  'info.PREDICTIVE_RESERVATION_MINUTES.what':      'When the Oracle decides a node is heading for overload and queues migrations away from it, that node is immediately locked as a migration target until this many minutes after the predicted peak.',
  'info.PREDICTIVE_RESERVATION_MINUTES.benefits':  'Prevents the reactive engine from routing other VMs back onto a node the Oracle just flagged, which would undo the proactive work. Also stops the reactive engine from making the problem worse while the predictive migrations are executing one by one.',
  'info.PREDICTIVE_RESERVATION_MINUTES.careful':   'Set too high and a node that recovers quickly stays blocked from receiving VMs for longer than necessary, reducing balancing headroom cluster-wide. Set to 0 to disable reservations entirely \u2014 not recommended when predictive balancing is active.',

  'info.POLL_INTERVAL.what':      'How often (in seconds) the Collector fetches node and VM metrics from the HyperCore API and writes them to InfluxDB.',
  'info.POLL_INTERVAL.benefits':  'Shorter intervals produce more granular historical data for both the dashboard graphs and The Oracle\'s training dataset.',
  'info.POLL_INTERVAL.careful':   'Too short increases API load on the HyperCore cluster and InfluxDB write pressure. Below 15s risks rate-limiting or degraded cluster API performance. 30s is the recommended minimum.',

  'info.SLOW_POLL_INTERVAL.what':      'Extended collection interval used for less time-sensitive data such as static node properties and disk metadata.',
  'info.SLOW_POLL_INTERVAL.benefits':  'Reduces unnecessary API calls for information that changes rarely, keeping overall HyperCore API load low.',
  'info.SLOW_POLL_INTERVAL.careful':   'Too long can allow stale metadata to persist in InfluxDB. For most deployments 300s (5 min) is a sensible balance.',

  'info.INFLUX_WRITE_RETRIES.what':      'How many times the Collector retries a failed InfluxDB write before giving up and logging an error.',
  'info.INFLUX_WRITE_RETRIES.benefits':  'Handles brief InfluxDB restarts, container restarts, or transient network blips without dropping metric data.',
  'info.INFLUX_WRITE_RETRIES.careful':   'Very high values can stall the collection loop if InfluxDB is genuinely unreachable. Total blocking time = WRITE_RETRIES \u00d7 RETRY_DELAY \u2014 keep the product well below POLL_INTERVAL to avoid skipping a collection cycle.',

  'info.INFLUX_RETRY_DELAY.what':      'Seconds to wait between successive InfluxDB write retry attempts after a failed write.',
  'info.INFLUX_RETRY_DELAY.benefits':  'Gives InfluxDB time to recover between retries rather than hammering it with immediate successive requests.',
  'info.INFLUX_RETRY_DELAY.careful':   'Total blocking time = WRITE_RETRIES \u00d7 RETRY_DELAY. For example, 3 retries \u00d7 10s = 30s blocked \u2014 ensure this product is below POLL_INTERVAL to avoid collection gaps.',

  'info.STALE_SECONDS.what':      'How many seconds can elapse since the last successful Collector write before the dashboard marks a node\'s data as stale and shows a warning indicator.',
  'info.STALE_SECONDS.benefits':  'Quickly surfaces collection failures, network issues, or InfluxDB problems before they silently affect dashboard accuracy.',
  'info.STALE_SECONDS.careful':   'Set too low (below POLL_INTERVAL) and nodes will always appear stale. Should be at least 2\u20133\u00d7 POLL_INTERVAL to account for normal write timing variance.',

  // ─── Info popup body text (ⓘ) — Sentinel / SC//Foresight settings ────────
  'info.SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD.what':      'VMs consistently using less than this % of allocated CPU over the analysis window are flagged as over-provisioned.',
  'info.SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD.benefits':  'Identifies VMs with too many vCPUs allocated, freeing up capacity that can be reassigned to under-provisioned VMs or allow higher VM density on the same hardware.',
  'info.SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD.careful':   'Set too high and intentionally reserved VMs (standby servers, batch jobs with variable load) get incorrectly flagged. Consider your workload mix before lowering below 20%.',

  'info.SENTINEL_CPU_UNDER_PROVISIONED_AVG_THRESHOLD.what':      'Average CPU usage above this % over the analysis window triggers Signal 1 of the under-provisioned model. Either avg or P95 threshold can fire independently.',
  'info.SENTINEL_CPU_UNDER_PROVISIONED_AVG_THRESHOLD.benefits':  'Catches VMs that are consistently near capacity \u2014 sustained high utilisation is the strongest indicator that a VM needs more vCPUs.',
  'info.SENTINEL_CPU_UNDER_PROVISIONED_AVG_THRESHOLD.careful':   'Set too low and VMs with occasional spikes but healthy averages will be missed. This signal works best alongside the P95 threshold, which catches burst patterns the average smooths out.',

  'info.SENTINEL_CPU_UNDER_PROVISIONED_P95_THRESHOLD.what':      'P95 CPU usage above this % triggers burst-pattern under-provisioning (Signal 1). Catches VMs that spike to near-capacity even when their average looks healthy.',
  'info.SENTINEL_CPU_UNDER_PROVISIONED_P95_THRESHOLD.benefits':  'Surfaces VMs that look fine on average but regularly starve for CPU during busy periods \u2014 a common source of end-user performance complaints that average metrics hide.',
  'info.SENTINEL_CPU_UNDER_PROVISIONED_P95_THRESHOLD.careful':   'P95 at 90% is aggressive. Brief spikes are normal on shared HyperCore clusters. Raise this threshold if you see too many false positives on workloads with short, predictable bursts.',

  'info.SENTINEL_ANALYSIS_WINDOW_DAYS.what':      'How many days of InfluxDB metric history are used for right-sizing analysis and recurring spike detection. Minimum 7 days.',
  'info.SENTINEL_ANALYSIS_WINDOW_DAYS.benefits':  'A longer window captures more complete usage patterns including weekday/weekend variation. The default 14 days covers two full weekly cycles \u2014 the recommended minimum for pattern detection.',
  'info.SENTINEL_ANALYSIS_WINDOW_DAYS.careful':   'Shortening below 7 days risks recommendations based on atypical data (e.g. a single busy week). Extending beyond 30 days means the analysis is slower to react to genuine workload changes.',

  'info.SENTINEL_RECURRING_SPIKE_MIN_OCCURRENCES.what':      'Minimum number of times a CPU spike must recur at the same weekday and hour bucket before Sentinel flags it as a recurring pattern.',
  'info.SENTINEL_RECURRING_SPIKE_MIN_OCCURRENCES.benefits':  'Prevents a one-off incident from triggering a right-sizing recommendation. Requiring 3 or more occurrences means the pattern is genuinely repeating, not a fluke.',
  'info.SENTINEL_RECURRING_SPIKE_MIN_OCCURRENCES.careful':   'Raising too high means real daily patterns need many days to appear (at 5, a daily spike needs 5 days of data). Lowering to 1 means any single spike at an hour triggers the signal.',

  'info.SENTINEL_ANOMALY_FILTER_ENABLED.what':      'Applies an IQR-fence outlier removal pass to each time series before fitting the linear regression forecast model. Outliers beyond 1.5\u00d7 IQR are excluded from the regression.',
  'info.SENTINEL_ANOMALY_FILTER_ENABLED.benefits':  'Prevents a brief outage, maintenance window, or traffic spike from skewing the regression line and producing an inaccurate long-term forecast.',
  'info.SENTINEL_ANOMALY_FILTER_ENABLED.careful':   'Disabling makes forecasts more sensitive to anomalies. Useful only if you specifically want unusual events to influence predictions. In most production environments, leave this on.',

  'info.SENTINEL_FORECAST_MIN_R2_FOR_EXHAUSTION.what':      'Minimum r\u00b2 (fraction of variance explained by the linear trend) required before a positive slope is allowed to produce an exhaustion date. Linear fits below this threshold are treated as noise rather than a real trend.',
  'info.SENTINEL_FORECAST_MIN_R2_FOR_EXHAUSTION.benefits':  'Suppresses spurious exhaustion alerts on near-flat series. A fit with r\u00b2 of 0.2 explains only 20% of variance \u2014 extrapolating it forward produces dates that wander cycle to cycle. The default 0.5 keeps alerts focused on meaningful trends.',
  'info.SENTINEL_FORECAST_MIN_R2_FOR_EXHAUSTION.careful':   'Raising too high (e.g. 0.9) hides real but gradual trends \u2014 you may miss a genuine exhaustion runway. Setting to 0 disables the gate so any positive slope produces an exhaustion date regardless of fit quality.',

  'info.SENTINEL_FORECAST_DAILY_AGGREGATE.what':      'How sub-day samples roll up into the daily data point used by cluster CPU / RAM / storage forecasts. mean = time-weighted average across the day. p95 = the 95th-percentile sample (roughly the busiest 1.2 hours). max = single highest sample of the day.',
  'info.SENTINEL_FORECAST_DAILY_AGGREGATE.benefits':  'mean suits steady workloads where load is roughly constant. p95 captures sustained peak \u2014 best for VDI, batch-jobs, or any environment with a strong day/night pattern; the daily mean understates peak in those cases. max gives worst-case visibility but is sensitive to brief spikes (backup jobs, deploy/teardown) so use it deliberately.',
  'info.SENTINEL_FORECAST_DAILY_AGGREGATE.careful':   'Switching to p95 or max makes growth rates look higher because daytime peaks count more than off-hours idle \u2014 that is the correct signal for capacity planning, but expect the absolute numbers to shift after the change. The choice does not affect per-VM right-sizing or per-VSD latency forecasts; it only changes how the cluster aggregates are built.',

  'info.SENTINEL_FORECAST_30D_ENABLED.what':      'Enables the 30-day short-term capacity forecast for all tracked resource dimensions: CPU, RAM, storage capacity, storage latency, and network.',
  'info.SENTINEL_FORECAST_30D_ENABLED.benefits':  'Tactical view for immediate capacity decisions \u2014 are we about to run out of something in the next month? Drives the most urgent exhaustion alerts.',
  'info.SENTINEL_FORECAST_30D_ENABLED.careful':   'Disabling suppresses all 30-day exhaustion alerts across the dashboard and API. Only turn off if you are deliberately reducing analysis scope or troubleshooting unexpected behaviour.',

  'info.SENTINEL_FORECAST_90D_ENABLED.what':      'Enables the 90-day quarterly capacity forecast. Aligned with typical IT planning and budget cycles.',
  'info.SENTINEL_FORECAST_90D_ENABLED.benefits':  'Answers \'do we need to act this quarter?\' \u2014 the primary horizon for procurement planning and workload-growth conversations.',
  'info.SENTINEL_FORECAST_90D_ENABLED.careful':   'Disabling removes all 90-day exhaustion alerts. Only disable if you want to reduce PostgreSQL write volume and are confident 30-day coverage is sufficient.',

  'info.SENTINEL_FORECAST_180D_ENABLED.what':      'Enables the 6-month strategic capacity forecast.',
  'info.SENTINEL_FORECAST_180D_ENABLED.benefits':  'Identifies the next hardware purchase window before it becomes urgent. Reveals growth trends that are invisible at the 30 and 90-day horizons.',
  'info.SENTINEL_FORECAST_180D_ENABLED.careful':   'Confidence scores for 180-day forecasts are typically lower \u2014 less data relative to the horizon. The dashboard automatically suppresses forecasts with confidence below 10%.',

  'info.SENTINEL_FORECAST_365D_ENABLED.what':      'Enables the 12-month strategic capacity forecast. The longest supported horizon.',
  'info.SENTINEL_FORECAST_365D_ENABLED.benefits':  'Used for annual capacity planning and pre-sales growth conversations. Shows the full year trajectory for each resource dimension.',
  'info.SENTINEL_FORECAST_365D_ENABLED.careful':   'Forecast confidence at 365 days is the lowest of all horizons and should be treated as directional only. The data-coverage confidence penalty suppresses very uncertain forecasts automatically.',

  'info.SENTINEL_RETENTION_MONTHS.what':      'How many months forecast snapshots and right-sizing records are kept before the automated cleanup job removes them. Audit records (recommendation_events) are kept indefinitely regardless.',
  'info.SENTINEL_RETENTION_MONTHS.benefits':  'Prevents the PostgreSQL database from growing unboundedly. 12 months is sufficient for trend analysis and historical report generation.',
  'info.SENTINEL_RETENTION_MONTHS.careful':   'Lowering aggressively (e.g. 1\u20132 months) removes older recommendations from the dashboard and API, limiting historical comparison. Raising it increases database storage proportionally.',

  'info.SENTINEL_OUTCOME_TRACKER_ENABLED.what':      'When enabled, Sentinel polls HyperCore every 6 hours to detect whether VMs that received a right-sizing recommendation were subsequently resized. Results are written back to the audit table.',
  'info.SENTINEL_OUTCOME_TRACKER_ENABLED.benefits':  'Closes the feedback loop on recommendations \u2014 shows whether advice was acted on and what the before/after spec looks like. Powers the \'Outcomes\' view in the API.',
  'info.SENTINEL_OUTCOME_TRACKER_ENABLED.careful':   'Requires SC_HOST, SC_USERNAME, and SC_PASSWORD to be set (HyperCore API credentials \u2014 env vars, restart required). Disable here if those are not configured or if HyperCore API polling is a concern.',

  'info.SENTINEL_OUTCOME_CHECK_DELAY_HOURS.what':      'How many hours after a right-sizing recommendation is issued before Sentinel first checks whether the VM was actually resized. Default 48 hours.',
  'info.SENTINEL_OUTCOME_CHECK_DELAY_HOURS.benefits':  'Gives operators enough time to act on a recommendation before the check runs, avoiding premature \'no_action\' outcomes being logged.',
  'info.SENTINEL_OUTCOME_CHECK_DELAY_HOURS.careful':   'Setting too low (e.g. 1 hour) means most checks log \'no_action\' before the operator has had time to act. Setting very high delays outcome visibility in the history view.',

  'info.SENTINEL_WEBHOOK_ENABLED.what':      'Master switch for the outbound webhook dispatcher. When enabled, Sentinel fires signed HTTP POST alerts to SENTINEL_WEBHOOK_URL for new CRITICAL and EMERGENCY events.',
  'info.SENTINEL_WEBHOOK_ENABLED.benefits':  'Pushes critical capacity alerts to external systems (Slack, PagerDuty, ticketing tools) in real time without polling the API. Each request is signed with HMAC-SHA256 for verification.',
  'info.SENTINEL_WEBHOOK_ENABLED.careful':   'Has no effect unless SENTINEL_WEBHOOK_URL is also set (environment variable \u2014 restart required). Toggling this off suppresses all outbound alerts without a restart.',
};
