"""
H2: Anonymisation engine.

Produces stable HMAC-based pseudonyms for sensitive fields so that
timeseries coherence is preserved in the SC//Foresight-Central viewer even
when customer names are not transmitted in the clear.

Customer-selectable per-field via config.sharing_prefs.
"""

import copy
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

import db
import config as cfg_module

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnonConfig:
    """Current anonymisation settings loaded from config.sharing_prefs."""
    enabled: bool           # overall sharing enabled
    anon_vm_names: bool
    anon_node_uuids: bool
    anon_cluster_name: bool
    hmac_key: str           # hex string; generated on first enable


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def load_anon_config(cfg: cfg_module.Config) -> AnonConfig:
    """
    Read sharing_prefs from the DB and return an AnonConfig.

    If anon_hmac_key is NULL, a new key is generated and persisted before
    returning.  If the config.sharing_prefs table does not yet exist (e.g.
    migration not yet applied), logs a warning and returns a disabled config.
    """
    _disabled = AnonConfig(
        enabled=False,
        anon_vm_names=False,
        anon_node_uuids=False,
        anon_cluster_name=False,
        hmac_key='',
    )

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    SELECT sharing_enabled, anon_vm_names, anon_node_uuids,
                           anon_cluster_name, anon_hmac_key
                    FROM config.sharing_prefs
                    WHERE cluster_id = 'default'
                    LIMIT 1
                    ''',
                )
                row = cur.fetchone()

        if row is None:
            log.warning('anonymiser: no sharing_prefs row found; anonymisation disabled')
            return _disabled

        sharing_enabled, anon_vm, anon_node, anon_cluster, hmac_key = row

        # generate and persist an HMAC key if one has not been set yet
        if not hmac_key:
            hmac_key = secrets.token_hex(32)
            try:
                with db.connect(cfg) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            '''
                            UPDATE config.sharing_prefs
                               SET anon_hmac_key = %s, updated_at = NOW()
                             WHERE cluster_id = 'default'
                            ''',
                            [hmac_key],
                        )
            except Exception:
                log.exception('anonymiser: failed to persist generated HMAC key')

        return AnonConfig(
            enabled=bool(anon_vm or anon_node or anon_cluster),
            anon_vm_names=bool(anon_vm),
            anon_node_uuids=bool(anon_node),
            anon_cluster_name=bool(anon_cluster),
            hmac_key=hmac_key,
        )

    except Exception as exc:
        log.warning(
            'anonymiser: could not load sharing_prefs (table may not exist yet): %s', exc
        )
        return _disabled


# ---------------------------------------------------------------------------
# core pseudonym generator
# ---------------------------------------------------------------------------

def _pseudonym(hmac_key: str, prefix: str, value: str) -> str:
    """
    Deterministic pseudonym for *value*.

    Uses HMAC-SHA256(key, prefix + ":" + value) and returns the first 8 hex
    characters with a human-readable prefix so the output remains meaningful
    in dashboards and log files.

    Examples::

        vm_name   "DC02"         -> "vm-3a7f2c1b"
        node_uuid "abc-123-..."  -> "node-d4e8f091"
        cluster   "prod-cluster" -> "cluster-99a1b2c3"
    """
    digest = hmac.new(
        hmac_key.encode(),
        f"{prefix}:{value}".encode(),
        hashlib.sha256,
    ).hexdigest()[:8]
    return f"{prefix}-{digest}"


# ---------------------------------------------------------------------------
# public snapshot anonymiser
# ---------------------------------------------------------------------------

def anonymise_snapshot(snapshot: dict, cfg: AnonConfig) -> dict:
    """
    Apply anonymisation to a snapshot dict and return a deep copy.

    The snapshot structure (produced by exporter.py) contains top-level keys:
    ``metadata``, ``forecasts``, ``rightsizing``, ``resize_recommendations``,
    ``cluster_health``, and ``exhaustion``.  Each section is a list of record
    dicts that may contain ``vm_name``, ``node_uuid``, and ``entity_label``
    fields.

    Rules applied when the corresponding toggle is enabled:

    - ``anon_vm_names``:
        Replace ``vm_name`` fields with ``_pseudonym(key, 'vm', value)``.
        Replace ``entity_label`` values that are plain VM names (not
        ``"Node N"`` format and not ``"X / Y"`` VSD labels) with the VM
        pseudonym.  For ``"vm_name / vsd_name"`` labels, only the VM
        part is replaced; the VSD name is kept as-is.

    - ``anon_node_uuids``:
        Replace ``node_uuid`` fields with ``_pseudonym(key, 'node', value)``.
        For ``entity_label`` values that match ``"Node <peer_id>"`` format,
        keep the ``"Node"`` prefix and hash the peer ID portion.

    - ``anon_cluster_name``:
        Replace ``cluster_name`` in the ``metadata`` block.

    ``None`` values are skipped gracefully (no anonymisation applied).
    """
    result = copy.deepcopy(snapshot)
    key = cfg.hmac_key

    # metadata block
    if cfg.anon_cluster_name and result.get('metadata'):
        meta = result['metadata']
        if meta.get('cluster_name'):
            meta['cluster_name'] = _pseudonym(key, 'cluster', meta['cluster_name'])

    # per-section record lists
    for section in ('forecasts', 'rightsizing', 'resize_recommendations', 'cluster_health', 'exhaustion'):
        for record in result.get(section, []):
            _anonymise_record(record, key, cfg)

    return result


def _anonymise_record(record: dict, key: str, cfg: AnonConfig) -> None:
    """Mutate a single record dict in-place applying the configured rules."""
    if cfg.anon_vm_names:
        if record.get('vm_name'):
            record['vm_name'] = _pseudonym(key, 'vm', record['vm_name'])

        label = record.get('entity_label')
        if label:
            if label.startswith('Node '):
                # node label — handled below by anon_node_uuids block;
                # leave untouched here so we don't double-process
                pass
            elif ' / ' in label:
                # "vm_name / vsd_name" — anonymise VM part only; keep VSD name
                parts = label.split(' / ', 1)
                record['entity_label'] = f"{_pseudonym(key, 'vm', parts[0])} / {parts[1]}"
            else:
                # plain VM name entity label
                record['entity_label'] = _pseudonym(key, 'vm', label)

    if cfg.anon_node_uuids:
        if record.get('node_uuid'):
            record['node_uuid'] = _pseudonym(key, 'node', record['node_uuid'])

        label = record.get('entity_label')
        if label and label.startswith('Node '):
            # "Node <peer_id>" — keep the "Node" prefix, hash the peer ID part
            peer_id = label[5:]  # everything after "Node "
            record['entity_label'] = f"Node-{_pseudonym(key, 'node', peer_id)}"
