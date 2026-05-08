/* HyperCore Manager — Français (French) traductions */
window.LANG = {
  // header
  'header.connecting':    'Connexion...',
  'header.connected':     'Connect\u00e9',
  'header.disconnected':  'D\u00e9connect\u00e9',
  'header.refresh':       '\u21bb Actualiser',

  // nav tabs
  'nav.modules':          'Modules',
  'nav.settings':         '\u2699 Param\u00e8tres',

  // Infraviewer sub-tabs
  'iv.nodes_drives':      'N\u0153uds & Disques',
  'iv.vms_storage':       'MV & Stockage',

  // Clusterbalancer sub-tabs
  'cb.miglog':            'Journal de migration',
  'cb.balancer_settings': 'Param\u00e8tres du r\u00e9partiteur',

  // Sentinel sub-tabs
  'sentinel.tab.overview':    'Aper\u00e7u',
  'sentinel.tab.forecasts':   'Pr\u00e9visions',
  'sentinel.tab.rightsizing': 'Redimensionnement',
  'sentinel.tab.exhaustion':  '\u00c9puisement',
  'sentinel.tab.reports':     'Rapports',
  'sentinel.tab.whatif':      'Simulation',
  'sentinel.tab.settings':    'Param\u00e8tres',
  'sentinel.tab.datasharing': 'Partage de donn\u00e9es',

  // time range / grouping controls
  'time.range':           'Plage\u00a0:',
  'vm.group_by':          'Grouper par\u00a0:',
  'vm.group_none':        'Aucun',
  'vm.group_no_tag':      '(sans tag)',
  'vm.group_label.tag':   'Tag',
  'vm.group_label.node':  'N\u0153ud',
  'vm.group_label.vlan':  'VLAN',
  'vm.group_label.peer':  'Pair',
  'vm.performance_details': 'D\u00e9tails de performance',

  // chart titles
  'chart.node_cpu':       'Utilisation CPU du n\u0153ud au fil du temps',
  'chart.cluster_iops':   'IOPS totaux du cluster au fil du temps',
  'chart.cluster_ram':    'Utilisation RAM du cluster',
  'chart.cluster_storage':'Utilisation du stockage du cluster',
  'chart.vm_cpu':         'Utilisation CPU des MV (top 5)',
  'chart.vsd_latency':    "Latence VSD (top 5 par latence d'\u00e9criture)",
  'chart.vm_net':         'Utilisation r\u00e9seau des MV \u2014 top 5 (RX + TX)',
  'chart.miglog':         "\u00c9v\u00e9nements de migration",

  // section / panel headings
  'section.capacity_forecasts': 'Pr\u00e9visions de capacit\u00e9',
  'section.resize_advice':      'Recommandations de redimensionnement',
  'section.exhaustion_alerts':  'Alertes d\u2019\u00e9puisement des ressources',
  'section.whatif':             'Mod\u00e9lisation de sc\u00e9nario What-If',

  // IOPS stat labels
  'iops.p95':   '95e percentile\u00a0:',
  'iops.min':   'Min\u00a0:',
  'iops.max':   'Max\u00a0:',
  'iops.iops':  'IOPS',
  'iops.insufficient': '95e percentile\u00a0: donn\u00e9es insuffisantes',

  // loading states
  'loading.nodes':      'Chargement des m\u00e9triques de n\u0153uds...',
  'loading.vms':        'Chargement des m\u00e9triques de MV...',
  'loading.migrations': 'Chargement des \u00e9v\u00e9nements de migration...',
  'loading.settings':   'Chargement des param\u00e8tres...',
  'loading.capacity':   "Chargement de l'intelligence de capacit\u00e9...",
  'loading.forecasts':  'Chargement des pr\u00e9visions...',
  'loading.resize':     'Chargement des conseils de redimensionnement...',
  'loading.exhaustion': "Chargement des donn\u00e9es d'\u00e9puisement...",
  'loading.simulation': 'Simulation en cours...',
  'loading.modules':    'D\u00e9couverte des modules...',

  // empty / error states
  'empty.no_api':               "Impossible de se connecter \u00e0 l'API",
  'empty.no_nodes':             'Aucune donn\u00e9e de n\u0153ud. Le collecteur fonctionne-t-il\u00a0?',
  'empty.no_vms':               'Aucune donn\u00e9e de MV.',
  'empty.no_migrations':        'Aucun \u00e9v\u00e9nement de migration enregistr\u00e9.',
  'empty.no_measurements':      'Aucune mesure trouv\u00e9e.',
  'empty.no_forecasts':         'Aucune donn\u00e9e de pr\u00e9vision disponible.',
  'empty.no_resize':            'Aucun conseil de redimensionnement concret.',
  'empty.no_exhaustion':        "Aucune alerte d'\u00e9puisement des ressources.",
  'empty.sentinel_unavailable': 'Donn\u00e9es SC//Foresight indisponibles \u2014 Sentinel fonctionne-t-il\u00a0?',
  'empty.settings_failed':      'Impossible de charger les param\u00e8tres.',
  'empty.sentinel_settings_unavail': 'Sentinel indisponible \u2014 les param\u00e8tres ne peuvent pas \u00eatre charg\u00e9s.',
  'empty.settings_load_failed': 'Chargement des param\u00e8tres \u00e9chou\u00e9\u00a0: {msg}',

  // metric labels
  'metric.cpu_usage':   'Utilisation CPU',
  'metric.memory_used': 'M\u00e9moire utilis\u00e9e',
  'metric.vcpu_ratio':  'Ratio vCPU',
  'metric.data_points': 'Points de donn\u00e9es',
  'metric.network_rx':  'R\u00e9seau RX',
  'metric.network_tx':  'R\u00e9seau TX',

  // physical drive sub-items
  'drive.physical_drives': '\u25c9 Disques physiques',
  'drive.slot':      'Emplacement',
  'drive.healthy':   'Sain',
  'drive.degraded':  'D\u00e9grad\u00e9',
  'drive.used':      'Utilis\u00e9',
  'drive.temperature': 'Temp\u00e9rature',
  'drive.errors':    'Erreurs',

  // virtual storage device sub-items
  'vsd.devices':       '\u25c8 Dispositifs de stockage virtuels',
  'vsd.read':          'Lecture',
  'vsd.write':         '\u00c9criture',
  'vsd.read_latency':  'Latence de lecture',
  'vsd.write_latency': "Latence d'\u00e9criture",

  // migration log table
  'mig.time':      'Heure',
  'mig.vm':        'MV',
  'mig.migration': 'Migration',
  'mig.engine':    'Moteur',
  'mig.mode':      'Mode',
  'mig.reason':    'Raison',

  // VM detail modal
  'vm.modal.cpu':          'Utilisation CPU',
  'vm.modal.network':      'R\u00e9seau (RX / TX)',
  'vm.modal.disk_iops':    'IOPS disque (par VSD)',
  'vm.modal.disk_latency': 'Latence disque (par VSD)',

  // settings controls
  'settings.apply':      'Appliquer',
  'settings.unsaved':    '\u25cf non sauvegard\u00e9',
  'settings.applied_n':  '{n} modification(s) appliqu\u00e9e(s)',
  'settings.save_failed':'Échec de la sauvegarde\u00a0: {msg}',
  'settings.restart_required': 'Param\u00e8tres n\u00e9cessitant un red\u00e9marrage',
  'settings.restart_note': "Param\u00e8tres n\u00e9cessitant un red\u00e9marrage \u2014 intervalle de collecte, intervalle de tendance, intervalle de v\u00e9rification des r\u00e9sultats, ports, identifiants InfluxDB / PostgreSQL / HyperCore, URL webhook, secret et param\u00e8tres de livraison doivent \u00eatre d\u00e9finis via des variables d'environnement dans docker-compose et n\u00e9cessitent un red\u00e9marrage du conteneur.",

  // info popup
  'info.what':     'Ce que \u00e7a fait',
  'info.benefits': 'Avantages',
  'info.careful':  'Soyez prudent',

  // settings group labels
  'group.master':      'Contr\u00f4les principaux',
  'group.reactive':    'R\u00e9partiteur r\u00e9actif',
  'group.predictive':  "Moteur pr\u00e9dictif  \u00b7  L'Oracle",
  'group.collector':   'Collecteur',
  'group.dashboard':   'Tableau de bord',
  'group.rightsizing': 'Seuils de redimensionnement',
  'group.forecast':    'Pr\u00e9vision & Analyse',
  'group.maintenance': 'Maintenance',

  // Sentinel overview
  'sentinel.cluster_health':    'Sant\u00e9 du cluster',
  'sentinel.severity':          'Gravit\u00e9',
  'sentinel.bottleneck':        "Ressource goulot d'\u00e9tranglement",
  'sentinel.efficiency':        "Indice d'efficacit\u00e9",
  'sentinel.as_of':             'Au',
  'sentinel.health_breakdown':       'D\u00e9tail de la sant\u00e9 par composant',
  'sentinel.health_breakdown_hint':  "Chaque dimension est not\u00e9e de 0 \u00e0 100. Les scores faibles font baisser la sant\u00e9 globale du cluster \u2014 concentrez-vous sur la dimension identifi\u00e9e comme goulot d'\u00e9tranglement ci-dessus.",
  'sentinel.based_on':               'bas\u00e9 sur :',
  'reason.ha_headroom':              "Marge HA N+1 : {pct}% \u2014 survit \u00e0 la perte d'un n\u0153ud / mise \u00e0 jour progressive",
  'reason.ha_broken':                "HA rompue \u2014 la RAM assign\u00e9e d\u00e9passe la capacit\u00e9 N+1 de {pct}% (le cluster ne peut pas survivre \u00e0 la perte du plus grand n\u0153ud)",
  'reason.forecast':                 'Bas\u00e9 sur la gravit\u00e9 pr\u00e9vue : {severity}',
  'sentinel.analysis_run_now':       "Relancer l'analyse maintenant",
  'sentinel.analysis_run_hint':      "Les param\u00e8tres prennent effet au prochain cycle d'analyse planifi\u00e9 (intervalle par d\u00e9faut : 1 heure). Cliquez pour lancer un cycle imm\u00e9diatement et voir le changement dans la vue d'ensemble sans attendre.",
  'sentinel.analysis_triggering':    'D\u00e9clenchement\u2026',
  'sentinel.analysis_triggered':     "Cycle d'analyse d\u00e9clench\u00e9. Actualisez la vue d'ensemble dans environ 30 s pour voir les r\u00e9sultats.",
  'sentinel.analysis_trigger_failed':"\u00c9chec du d\u00e9clenchement de l'analyse : {msg}",
  'sentinel.active_forecasts':  'Pr\u00e9visions actives',
  'sentinel.over_provisioned':  'MV sur-provisonn\u00e9es',
  'sentinel.under_provisioned': 'MV sous-provisonn\u00e9es',
  'sentinel.worst_exhaustion':  'Pire \u00e9puisement',
  'sentinel.days':              'jours',
  'sentinel.already_exhausted': 'd\u00e9j\u00e0 \u00e9puis\u00e9',
  'sentinel.exhaustion_in':     '\u00e9puisement dans',
  'sentinel.warming_title':     'SC//Foresight se pr\u00e9pare',
  'sentinel.warming_running':   'Sentinel fonctionne et collecte des m\u00e9triques. La premi\u00e8re analyse de capacit\u00e9 produira bient\u00f4t des recommandations.',
  'sentinel.warming_first_run': 'Premi\u00e8re analyse en cours\u2026',
  'sentinel.warming_first_in':  'Premi\u00e8re analyse dans',
  'sentinel.warming_started':   'D\u00e9marr\u00e9',

  // forecast table
  'forecast.dimension':    'Dimension',
  'forecast.horizon':      'Horizon',
  'forecast.node_entity':  'N\u0153ud / Entit\u00e9',
  'forecast.current':      'Actuel',
  'forecast.forecast':     'Pr\u00e9vision',
  'forecast.confidence':   'Confiance',
  'forecast.exhaustion':   '\u00c9puisement',
  'forecast.severity':     'Gravit\u00e9',
  'forecast.showing':      'Affichage de {n} sur {total} pr\u00e9visions \u2014 lignes INFO \u00e0 faible confiance masqu\u00e9es.',
  'forecast.no_significant': 'Aucune pr\u00e9vision significative pour ce filtre (tous les r\u00e9sultats ont une faible confiance ou une gravit\u00e9 INFO).',
  'forecast.all_dimensions': 'Toutes les dimensions',
  'forecast.all_horizons':   'Tous les horizons',

  // right-sizing table
  'rs.vm_name':           'Nom MV',
  'rs.direction':         'Direction',
  'rs.current_vcpu':      'vCPU actuel',
  'rs.recommended':       'Recommand\u00e9',
  'rs.change_pct':        'Changement%',
  'rs.risk':              'Risque',
  'rs.severity':          'Gravit\u00e9',
  'rs.confidence':        'Confiance',
  'rs.reclaim':           'R\u00e9cup\u00e9ration%',
  'rs.reduce':            '\u2193 R\u00e9duire',
  'rs.increase':          '\u2191 Augmenter',
  'rs.act_now':           'Agir maintenant',
  'rs.recommended_action':'Recommand\u00e9',
  'rs.low_priority':      'Faible priorit\u00e9',

  // exhaustion table
  'ex.dimension':      'Dimension',
  'ex.node':           'N\u0153ud',
  'ex.days_left':      'Jours restants',
  'ex.exhaustion_date':"Date d'\u00e9puisement",
  'ex.growth_rate':    'Taux de croissance/jour',
  'ex.severity':       'Gravit\u00e9',

  // dimension labels
  'dim.cpu':     'CPU',
  'dim.ram':     'RAM',
  'dim.storage': 'Stockage',
  'dim.latency': 'Latence',
  'dim.network': 'R\u00e9seau',

  // what-if panel
  'whatif.scenario':         'Sc\u00e9nario',
  'whatif.add_nodes':        'Ajouter des n\u0153uds',
  'whatif.add_vms':          'Ajouter des MV',
  'whatif.add_vms_like':     'Ajouter des MV similaires\u2026',
  'whatif.count':            'Nombre',
  'whatif.storage_per_node': 'Stockage / n\u0153ud (To, optionnel)',
  'whatif.ram_per_node':     'RAM / n\u0153ud (Go, optionnel)',
  'whatif.template_vm':      'MV mod\u00e8le',
  'whatif.run_simulation':   '\u25b6 Lancer la simulation',
  'whatif.exhaustion_changes': "CHANGEMENTS DE DATE D'\u00c9PUISEMENT",
  'whatif.baseline':         'BASE (actuelle)',
  'whatif.simulated':        'SIMUL\u00c9',
  'whatif.nodes':            'n\u0153ud(s)',
  'whatif.vms':              'MV(s)',
  'whatif.vms_like':         'MV(s) similaire(s) \u00e0',
  'whatif.bottleneck_changed': '(modifi\u00e9)',
  'whatif.dimension':        'DIMENSION',
  'whatif.baseline_col':     'BASE',
  'whatif.simulated_col':    'SIMUL\u00c9',
  'whatif.gained':           'GAGN\u00c9',
  'whatif.unchanged':        'inchang\u00e9',
  'whatif.no_exhaustion':    "Aucune donn\u00e9e d'\u00e9puisement disponible.",
  'whatif.assumptions':      'Hypoth\u00e8ses\u00a0:',
  'whatif.sim_failed':       '\u00c9chec de la simulation\u00a0:',
  'whatif.enter_template_vm':'Veuillez entrer un nom de MV mod\u00e8le.',
  'whatif.nodes_label':      'n\u0153uds',
  'whatif.vms_label':        'MV(s)',
  'whatif.bottleneck':       "Goulot d'\u00e9tranglement\u00a0:",
  'whatif.none':             'aucun',

  // reports / PDF
  'reports.branding_title':      'Image de marque du rapport',
  'reports.branding_desc':       'Personnalisez la page de couverture du rapport PDF. Les param\u00e8tres sont sauvegard\u00e9s par cluster et appliqu\u00e9s \u00e0 chaque rapport g\u00e9n\u00e9r\u00e9.',
  'reports.company_name':        "Nom de l'entreprise",
  'reports.accent_colour':       "Couleur d'accentuation",
  'reports.logo':                'Logo (PNG ou JPG, max 500\u00a0Ko)',
  'reports.logo_remove':         '\u2715 Supprimer',
  'reports.logo_too_large':      'Le logo doit faire moins de 500\u00a0Ko.',
  'reports.save_branding':       "Sauvegarder l'image de marque",
  'reports.saved':               '\u2713 Sauvegard\u00e9',
  'reports.pdf_title':           'Rapports SC//Foresight',
  'reports.pdf_desc':            "G\u00e9n\u00e9rez un rapport PDF complet d'intelligence de capacit\u00e9 \u00e0 partir des derni\u00e8res donn\u00e9es d'analyse. Le rapport comprend des pr\u00e9visions, des recommandations de redimensionnement, des alertes d'\u00e9puisement et des conseils de redimensionnement.",
  'reports.download_pdf':        '\u2193 T\u00e9l\u00e9charger le rapport PDF',
  'reports.generating':          'G\u00e9n\u00e9ration\u2026',
  'reports.building':            'Construction du rapport, veuillez patienter\u2026',
  'reports.contents':            'Contenu du rapport',
  'reports.contents_summary':    '\u25c6 R\u00e9sum\u00e9 ex\u00e9cutif avec score de sant\u00e9 du cluster',
  'reports.contents_exhaustion': "Alertes d'\u00e9puisement des ressources (class\u00e9es par urgence)",
  'reports.contents_forecasts':  '\u25c6 Pr\u00e9visions de capacit\u00e9 \u2014 horizons 30j, 90j, 180j, 365j',
  'reports.contents_rightsizing':'\u25c6 Recommandations de redimensionnement des MV',
  'reports.contents_resize':     '\u25c6 Conseils concrets de redimensionnement avec classification des risques',

  // data sharing panel
  'sharing.connection_title':   'Connexion',
  'sharing.connection_desc':    'Configurez le point de terminaison du visualiseur SC//Foresight-Central pour activer les envois de snapshots planifi\u00e9s.',
  'sharing.enabled':            'Partage activ\u00e9',
  'sharing.viewer_url':         'URL du visualiseur',
  'sharing.bearer_token':       'Token Bearer',
  'sharing.save_connection':    'Sauvegarder la connexion',
  'sharing.anon_title':         'Anonymisation',
  'sharing.anon_desc':          'L\u2019anonymisation utilise des pseudonymes stables \u2014 les tendances restent intactes.',
  'sharing.anon_vm_names':      'Anonymiser les noms de MV',
  'sharing.anon_node_ids':      'Anonymiser les identifiants de n\u0153uds',
  'sharing.anon_cluster':       'Anonymiser le nom du cluster',
  'sharing.save_anon':          "Sauvegarder l'anonymisation",
  'sharing.manual_title':       'Actions manuelles',
  'sharing.test_push':          'Test Push',
  'sharing.download_hcb':       'T\u00e9l\u00e9charger le snapshot (.hcb)',
  'sharing.preview_snapshot':   'Aper\u00e7u du snapshot (JSON)',
  'sharing.last_sync_never':    'Derni\u00e8re sync\u00a0: jamais',
  'sharing.last_sync':          'Derni\u00e8re sync\u00a0:',
  'sharing.token_hint_saved':   'Token enregistr\u00e9. Entrez une nouvelle valeur pour le remplacer.',
  'sharing.token_placeholder_saved': '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022  (enregistr\u00e9 \u2014 laisser vide pour conserver)',
  'sharing.connection_saved':   'Param\u00e8tres de connexion sauvegard\u00e9s',
  'sharing.anon_saved':         "Param\u00e8tres d'anonymisation sauvegard\u00e9s",
  'sharing.load_failed':        'Impossible de charger les param\u00e8tres de partage',
  'sharing.test_push_sending':  'Envoi du test push\u2026',
  'sharing.test_push_ok':       'Test push r\u00e9ussi (HTTP {code})',
  'sharing.test_push_failed':   'Test push \u00e9chou\u00e9\u00a0:',
  'sharing.test_push_error':    'Erreur de test push\u00a0:',

  // HCB export modal
  'hcb.title':     'Exporter le snapshot (.hcb)',
  'hcb.desc':      "D\u00e9finissez une phrase secr\u00e8te pour chiffrer ce fichier. Vous devrez entrer la m\u00eame phrase secr\u00e8te lors de l'importation dans SC//Foresight-Central.",
  'hcb.passphrase':'Phrase secr\u00e8te',
  'hcb.confirm':   'Confirmer la phrase secr\u00e8te',
  'hcb.cancel':    'Annuler',
  'hcb.download':  'T\u00e9l\u00e9charger',
  'hcb.required':  'La phrase secr\u00e8te est obligatoire.',
  'hcb.mismatch':  'Les phrases secr\u00e8tes ne correspondent pas.',

  // modules tab
  'modules.tools':           'Outils',
  'modules.grafana_desc':    'Exploration de donn\u00e9es avanc\u00e9e et tableaux de bord personnalis\u00e9s',
  'modules.open_grafana':    'Ouvrir Grafana',
  'modules.grafana_note':    "Grafana n'est pas d\u00e9marr\u00e9 par d\u00e9faut. Pour le lancer, ex\u00e9cutez\u00a0:",
  'modules.discovered_title':'Modules de donn\u00e9es d\u00e9couverts',
  'modules.auto_appear':     'Chaque conteneur qui \u00e9crit dans InfluxDB appara\u00eet ici automatiquement.',

  // Grafana status
  'grafana.running':    "\u25cf En cours d'ex\u00e9cution",
  'grafana.not_running':"\u25cb Pas en cours d'ex\u00e9cution",
  'grafana.checking':   'V\u00e9rification...',

  // auto-discovered module fallback desc
  'module.auto_desc': 'Source de donn\u00e9es d\u00e9couverte automatiquement',

  // ─── info popup body text (ⓘ) ───────────────
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
