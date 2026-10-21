"""Minimal HyperCore REST API client for exports and imports.

Uses HTTP Basic auth (supported globally per the HyperCore OpenAPI spec), which
keeps the client stateless: no session cookie bookkeeping across long-running
export polls. Credentials come from the cluster registry.
"""
from __future__ import annotations

import requests
import urllib3

import cluster_registry


class HyperCoreError(Exception):
    pass


def first_tag(tags: str) -> str:
    """The first tag of HyperCore's comma-separated tag string ('' if none)."""
    return (tags or '').split(',')[0].strip()


def _vm_sort_key(vm: dict):
    tag = first_tag(vm.get('tags', '')).lower()
    name = (vm.get('name') or '').lower()
    # tag == "" -> True sorts after False, so untagged VMs land last
    return (tag == '', tag, name)


class HyperCoreClient:
    def __init__(self, host: str, username: str, password: str, verify_tls: bool = True, timeout: int = 30):
        self.base = cluster_registry.base_url(host)
        self.auth = (username, password)
        self.verify = verify_tls
        self.timeout = timeout
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request(self, method: str, path: str, **kwargs):
        url = f'{self.base}{path}'
        try:
            resp = requests.request(method, url, auth=self.auth, verify=self.verify, timeout=self.timeout, **kwargs)
        except requests.exceptions.SSLError as e:
            raise HyperCoreError(f'TLS error talking to {url}: {e}') from e
        except requests.exceptions.ConnectionError as e:
            raise HyperCoreError(f'Cannot reach {url}: {e}') from e
        except requests.exceptions.Timeout as e:
            raise HyperCoreError(f'Timeout talking to {url}') from e
        if resp.status_code == 401:
            raise HyperCoreError('Authentication failed (401): check the cluster credentials')
        if resp.status_code == 403:
            raise HyperCoreError('Permission denied (403): the cluster account lacks rights for this action')
        if not resp.ok:
            try:
                detail = resp.json().get('error', '')
            except Exception:
                detail = resp.text[:200]
            raise HyperCoreError(f'HyperCore API {resp.status_code} on {path}: {detail}')
        return resp.json() if resp.text else None

    def ping(self) -> bool:
        self._request('GET', '/ping')
        return True

    def list_vms(self) -> list[dict]:
        vms = self._request('GET', '/VirDomain') or []
        slim = [{
            'uuid': vm.get('uuid'), 'name': vm.get('name'), 'description': vm.get('description', ''),
            'state': vm.get('state', ''), 'mem': vm.get('mem', 0), 'numVCPU': vm.get('numVCPU', 0),
            'tags': vm.get('tags', ''),
        } for vm in vms]
        return sorted(slim, key=_vm_sort_key)

    def export_vm(self, vm_uuid: str, path_uri: str, compress: bool = False) -> str:
        """Start a VM export. HyperCore creates the basename directory of path_uri on the
        target, which is how each run gets its own timestamped folder. Returns the taskTag."""
        body = {'target': {'pathURI': path_uri}}
        if compress:
            body['target']['compress'] = True
        result = self._request('POST', f'/VirDomain/{vm_uuid}/export', json=body)
        task_tag = (result or {}).get('taskTag')
        if not task_tag:
            raise HyperCoreError(f'Export accepted but no taskTag returned: {result}')
        return task_tag

    def import_vm(self, source_uri: str, name: str | None = None, description: str | None = None,
                  tags: str | None = None, definition_file: str | None = None) -> tuple[str, str]:
        """Import a VM from a previously exported folder. Returns (taskTag, createdUUID)."""
        body: dict = {'source': {'pathURI': source_uri}}
        if definition_file:
            body['source']['definitionFileName'] = definition_file
        template = {}
        if name:
            template['name'] = name
        if description is not None:
            template['description'] = description
        if tags is not None:
            template['tags'] = tags
        if template:
            body['template'] = template
        result = self._request('POST', '/VirDomain/import', json=body) or {}
        task_tag = result.get('taskTag')
        if not task_tag:
            raise HyperCoreError(f'Import accepted but no taskTag returned: {result}')
        return task_tag, result.get('createdUUID', '')

    def task_status(self, task_tag: str) -> dict:
        """{'state': QUEUED|RUNNING|COMPLETE|ERROR|UNINITIALIZED, 'progressPercent': int, ...}"""
        data = self._request('GET', f'/TaskTag/{task_tag}') or []
        if isinstance(data, list) and data:
            return data[0]
        return {'state': 'UNINITIALIZED', 'progressPercent': 0}
