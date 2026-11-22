"""
media_api.py — the Media tab: a cluster's ISOs and disk images, filtered by tenant (dashboard/media.py).

The backup service talks to HyperCore (list, streaming upload, delete); this module decides who sees,
uploads, assigns and deletes what, keeps config.media, and narrows the media the VM edit and New VM
dialogs offer (and accept) to what the user may see.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from flask import jsonify, request, session

import media


def register(app, requires, h):
    """h: access(), cluster_id(), write_cluster_problem(perm, need), backup_env() -> (url, token), pg(),
    audit(action, target, node_id=None, detail=None)."""

    # ------------------------------------------------------------------------------------------ database
    def rows(cid: str) -> dict:
        with h.pg() as conn, conn.cursor() as cur:
            cur.execute('SELECT kind, hc_uuid, name, owner_node::text AS owner, '
                        'COALESCE(array_to_json(assigned_nodes)::text, \'[]\') AS assigned, uploaded_by, uploaded_at '
                        'FROM config.media WHERE cluster_id = %s', (cid,))
            out = {}
            for r in cur.fetchall():
                r = dict(r)
                r['assigned'] = json.loads(r['assigned'] or '[]')
                out[(r['kind'], r['hc_uuid'])] = r
            return out

    PENDING = 'pending:'          # hc_uuid of a disk image HyperCore is still converting (it gets a new uuid then)

    def adopt(cid: str, disks: list) -> None:
        """A converted disk image takes over the row its upload left under its file name."""
        known = rows(cid)
        for d in disks:
            if d.get('converting') or ('disk', d['uuid']) in known or ('disk', PENDING + d['name']) not in known:
                continue
            with h.pg() as conn, conn.cursor() as cur:
                cur.execute('UPDATE config.media SET hc_uuid = %s WHERE cluster_id = %s AND kind = %s AND hc_uuid = %s',
                            (d['uuid'], cid, 'disk', PENDING + d['name']))

    def prune(cid: str, present: set) -> None:
        """Rows of media that are gone from the cluster (deleted in HyperCore's own UI) go too; a row still
        waiting for its converted image stays a day."""
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        gone = [(k, u) for (k, u), r in rows(cid).items() if (k, u) not in present
                and not (u.startswith(PENDING) and r.get('uploaded_at') and now - r['uploaded_at'] < _dt.timedelta(days=1))]
        if gone:
            with h.pg() as conn, conn.cursor() as cur:
                for k, u in gone:
                    cur.execute('DELETE FROM config.media WHERE cluster_id = %s AND kind = %s AND hc_uuid = %s', (cid, k, u))

    def item_of(row: dict | None) -> dict:
        return {'owner': row['owner'] if row else media.TOP, 'assigned': (row or {}).get('assigned') or []}

    # ------------------------------------------------------------------------------------------ backup
    def backup(method: str, path: str, params: dict, data=None, headers=None, timeout=60):
        url, token = h.backup_env()
        hdrs = dict(headers or {})
        if token:
            hdrs['Authorization'] = f'Bearer {token}'
        rq = urllib.request.Request(f'{url}/api/v1/{path}?{urllib.parse.urlencode(params)}', data=data,
                                    headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(rq, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or 'null')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or 'null')
            except ValueError:
                return e.code, {'error': f'backup service answered {e.code}'}
        except Exception as e:                       # noqa: BLE001 — refused, unresolvable, timed out
            return 503, {'error': f'Backup service unreachable: {e}'}

    def listed(cid: str) -> tuple[int, dict]:
        return backup('GET', 'media', {'cluster': cid})

    # ------------------------------------------------------------------------------------------ views
    def label(acc, node_id) -> str | None:
        return None if node_id is media.TOP else acc.tree.name.get(node_id, node_id)

    def view(acc, cid: str, kind: str, m: dict, row: dict | None) -> dict:
        it = item_of(row)
        manage = media.may_manage(acc, it, cid)
        out = dict(m, kind=kind, owner={'id': it['owner'], 'label': label(acc, it['owner'])},
                   assigned=[{'id': n, 'label': label(acc, n)} for n in it['assigned'] if n in acc.tree.parent],
                   can_manage=manage,
                   assign_targets=[{'id': n, 'label': label(acc, n), 'level': acc.tree.level.get(n), 'assigned': n in it['assigned']}
                                   for n in media.assign_targets(acc, it, cid)],
                   uploaded_by=(row or {}).get('uploaded_by') if manage else None,
                   uploaded_at=str((row or {}).get('uploaded_at') or '') or None)
        return out

    def may_open(acc, cid: str) -> bool:
        return media.is_top(acc) or bool(media.layers(acc, cid, media.USE_PERMS))

    # ------------------------------------------------------------------------------------------ routes
    @app.route('/api/media')
    @requires(None)
    def media_get():
        acc, cid = h.access(), h.cluster_id()
        if not may_open(acc, cid):
            return jsonify({'error': 'Access denied'}), 403
        st, data = listed(cid)
        if st != 200:
            return jsonify(data or {'error': 'backup service unavailable'}), st if st >= 400 else 502
        adopt(cid, data.get('disks') or [])
        present = {('iso', m['uuid']) for m in data.get('isos') or []} | {('disk', m['uuid']) for m in data.get('disks') or []}
        prune(cid, present)
        known = rows(cid)

        def row_of(kind, m):
            if kind == 'disk' and m.get('converting'):
                return known.get(('disk', PENDING + m['name']))        # its owner, from the upload
            return known.get((kind, m['uuid']))
        out = {}
        for kind, key in (('iso', 'isos'), ('disk', 'disks')):
            items = []
            for m in data.get(key) or []:
                row = row_of(kind, m)
                if not media.may_see(acc, item_of(row), cid):
                    continue
                v = view(acc, cid, kind, m, row)
                if m.get('converting'):
                    v['assign_targets'] = []        # given away once converted (it gets a new uuid); deletable meanwhile
                items.append(v)
            out[key] = items
        ok, owner = media.upload_owner(acc, cid)
        out['can_upload'] = ok
        out['upload_owner'] = {'id': owner, 'label': label(acc, owner)} if ok else None
        return jsonify(out)

    @app.route('/api/media/upload', methods=['PUT'])
    @requires('media.manage', 'any')
    def media_upload():
        """?cluster=&kind=iso|disk&name=  body: the file (Content-Length). Streams through the backup service
        into the cluster and records the uploader's layer as the owner."""
        problem = h.write_cluster_problem('media.manage', True)
        if problem is not None:
            return problem
        acc, cid = h.access(), request.args.get('cluster')
        ok, owner = media.upload_owner(acc, cid)
        if not ok:
            return jsonify({'error': 'Access denied', 'permission': 'media.manage'}), 403
        kind = request.args.get('kind')
        try:
            name = media.check_name(kind, request.args.get('name') or '')
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        size = request.content_length or 0
        if size <= 0:
            return jsonify({'error': 'the upload needs a Content-Length'}), 411
        # the body goes on as it arrives (a disk image can be tens of GB): never read into memory here
        st, data = backup('PUT', 'media/upload', {'cluster': cid, 'kind': kind, 'name': name}, data=request.stream,
                          headers={'Content-Length': str(size), 'Content-Type': 'application/octet-stream'},
                          timeout=4 * 3600)
        if st not in (201, 202):
            return jsonify(data or {'error': 'upload failed'}), st if st >= 400 else 502
        user = session.get('username')
        if st == 202:
            # still converting on the cluster: the owner is kept under the file name until the image shows up
            data['uuid'] = PENDING + name
        with h.pg() as conn, conn.cursor() as cur:
            cur.execute('INSERT INTO config.media (cluster_id, kind, hc_uuid, name, owner_node, uploaded_by) '
                        'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (cluster_id, kind, hc_uuid) DO UPDATE '
                        'SET owner_node = EXCLUDED.owner_node, name = EXCLUDED.name, uploaded_by = EXCLUDED.uploaded_by, '
                        'assigned_nodes = \'{}\', uploaded_at = NOW()',
                        (cid, kind, data['uuid'], name, owner, user))
        h.audit('media.upload', name, node_id=owner, detail={'cluster_id': cid, 'kind': kind, 'size': size})
        return jsonify({'ok': True, 'uuid': data['uuid'], 'name': name, 'converting': st == 202,
                        'owner': {'id': owner, 'label': label(acc, owner)}}), st

    def find(cid: str, kind: str, uuid: str):
        """(listing entry, db row) of one item on the cluster, or (None, None)."""
        st, data = listed(cid)
        if st != 200:
            return None, None
        m = next((x for x in data.get('isos' if kind == 'iso' else 'disks') or [] if x.get('uuid') == uuid), None)
        if not m:
            return None, None
        key = ('disk', PENDING + m['name']) if kind == 'disk' and m.get('converting') else (kind, uuid)
        return m, rows(cid).get(key)

    @app.route('/api/media/assign', methods=['POST'])
    @requires('media.manage', 'any')
    def media_assign():
        """?cluster=  {kind, uuid, node_id, assign: bool}: open an item to a layer down the cluster's branch, or
        take that back."""
        problem = h.write_cluster_problem('media.manage', True)
        if problem is not None:
            return problem
        acc, cid = h.access(), request.args.get('cluster')
        b = request.get_json(silent=True) or {}
        kind, uuid, node = b.get('kind'), str(b.get('uuid') or ''), str(b.get('node_id') or '')
        if kind not in media.KINDS:
            return jsonify({'error': "kind must be 'iso' or 'disk'"}), 400
        m, row = find(cid, kind, uuid)
        if m is None:
            return jsonify({'error': 'not found'}), 404
        it = item_of(row)
        if not media.may_see(acc, it, cid):
            return jsonify({'error': 'not found'}), 404
        if node not in media.assign_targets(acc, it, cid):
            return jsonify({'error': 'that layer is not one this item can be given to'}), 403
        nodes = set(it['assigned'])
        nodes.add(node) if b.get('assign', True) else nodes.discard(node)
        with h.pg() as conn, conn.cursor() as cur:
            cur.execute('INSERT INTO config.media (cluster_id, kind, hc_uuid, name, owner_node, assigned_nodes) '
                        'VALUES (%s, %s, %s, %s, %s, %s::uuid[]) ON CONFLICT (cluster_id, kind, hc_uuid) DO UPDATE '
                        'SET assigned_nodes = EXCLUDED.assigned_nodes',
                        (cid, kind, uuid, m.get('name') or uuid, it['owner'], sorted(nodes)))
        h.audit('media.assign' if b.get('assign', True) else 'media.unassign', m.get('name') or uuid, node_id=node,
                detail={'cluster_id': cid, 'kind': kind})
        return jsonify({'ok': True, 'assigned': sorted(nodes)})

    @app.route('/api/media', methods=['DELETE'])
    @requires('media.manage', 'any')
    def media_delete():
        """?cluster=&kind=&uuid=  Deletes the item from the cluster (the owner layer or the top only)."""
        problem = h.write_cluster_problem('media.manage', True)
        if problem is not None:
            return problem
        acc, cid = h.access(), request.args.get('cluster')
        kind, uuid = request.args.get('kind'), request.args.get('uuid') or ''
        if kind not in media.KINDS:
            return jsonify({'error': "kind must be 'iso' or 'disk'"}), 400
        m, row = find(cid, kind, uuid)
        if m is None or not media.may_see(acc, item_of(row), cid):
            return jsonify({'error': 'not found'}), 404
        if not media.may_manage(acc, item_of(row), cid):
            return jsonify({'error': 'only the layer that owns it may delete it'}), 403
        st, data = backup('DELETE', 'media', {'cluster': cid, 'kind': kind, 'uuid': uuid}, timeout=900)
        if st != 200:
            return jsonify(data or {'error': 'delete failed'}), st if st >= 400 else 502
        with h.pg() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM config.media WHERE cluster_id = %s AND kind = %s AND hc_uuid = %s', (cid, kind, uuid))
        h.audit('media.delete', m.get('name') or uuid, node_id=item_of(row)['owner'], detail={'cluster_id': cid, 'kind': kind})
        return jsonify({'ok': True})

    # ------------------------------------------------------------------------------------------ VM dialogs
    def visible_uuids(cid: str) -> dict:
        """{'iso': set, 'disk': set} of the media this user may see on the cluster (from config.media alone:
        an item without a row is the top's)."""
        acc = h.access()
        if media.is_top(acc):
            return {'iso': None, 'disk': None}                  # everything
        out = {'iso': set(), 'disk': set()}
        for (kind, uuid), row in rows(cid).items():
            if media.may_see(acc, item_of(row), cid):
                out[kind].add(uuid)
        return out

    def narrow_vmedit(sub: str, data, cid: str):
        """GET vmedit/vm and vmedit/options: only the ISOs and images the user may see. An ISO already in one of
        the VM's CD-ROM drives stays listed, so the dialog can show what is inserted (and eject it)."""
        if not isinstance(data, dict):
            return data
        vis = visible_uuids(cid)
        if vis['iso'] is None:
            return data
        inserted = set()
        for d in ((data.get('vm') or {}).get('blockDevs') or []):
            if d.get('type') == 'IDE_CDROM' and d.get('path'):
                inserted.add(str(d['path']).rsplit('/', 1)[-1])
        if isinstance(data.get('isos'), list):
            data['isos'] = [i for i in data['isos'] if i.get('uuid') in vis['iso'] or i.get('uuid') in inserted]
        if isinstance(data.get('images'), list):
            data['images'] = [i for i in data['images'] if i.get('uuid') in vis['disk']]
        return data

    def check_vmedit(sub: str, payload: dict, cid: str):
        """POST vmedit/new and vmedit/changes: an ISO or image the user may not see is refused (403)."""
        wanted = {'iso': set(), 'disk': set()}
        if sub == 'vmedit/new':
            spec = payload.get('spec') or {}
            if spec.get('iso_uuid'):
                wanted['iso'].add(str(spec['iso_uuid']))
            for d in spec.get('disks') or []:
                if isinstance(d, dict) and d.get('image_uuid'):
                    wanted['disk'].add(str(d['image_uuid']))
        elif sub == 'vmedit/changes':
            for o in payload.get('ops') or []:
                if isinstance(o, dict) and o.get('op') == 'iso' and o.get('iso_uuid'):
                    wanted['iso'].add(str(o['iso_uuid']))
        if not (wanted['iso'] or wanted['disk']):
            return None
        vis = visible_uuids(cid)
        if vis['iso'] is None:
            return None
        if wanted['iso'] - vis['iso'] or wanted['disk'] - vis['disk']:
            return jsonify({'error': 'that ISO or disk image is not available to you'}), 403
        return None

    return narrow_vmedit, check_vmedit
