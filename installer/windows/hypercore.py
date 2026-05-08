"""Minimal HyperCore (Scale Computing) REST client.

Stdlib only (urllib + http.cookiejar + ssl) so the Windows wizard remains a
single-file PyInstaller bundle with no extra Python dependencies.

Reference: Scale Computing HyperCore API v2.0.10 (openAPI.json).

Operations covered:
  * login (cookie-based session)
  * create VM with embedded cloud-init userData/metaData
  * upload a virtual disk (qcow2) and attach it to a VM
  * create a NIC on a VM
  * set boot order
  * power on
  * poll a TaskTag to completion

Anything more exotic (snapshots, replication, etc.) is intentionally out of
scope — this module is a thin shim for what the deploy wizard needs.
"""

from __future__ import annotations

import base64
import json
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, BinaryIO, Callable, Optional


class HyperCoreError(Exception):
    """Raised for any non-2xx response or task failure."""


def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop case-duplicate header keys, keeping the last spelling we saw.
    HTTP forbids duplicate Content-Length and nginx 400s on it."""
    seen: dict[str, str] = {}  # lower-case key -> canonical key
    out: dict[str, str] = {}
    for k, v in headers.items():
        canon = seen.get(k.lower())
        if canon is not None:
            out.pop(canon, None)
        seen[k.lower()] = k
        out[k] = v
    return out


class _ProgressReader:
    """File-like wrapper that counts bytes read and fires a callback. Used
    to feed urllib a streaming body while reporting upload progress."""

    def __init__(
        self,
        fileobj: BinaryIO,
        total: int,
        callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        self._f = fileobj
        self._total = total
        self._cb = callback
        self._sent = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._f.read(size)
        if chunk:
            self._sent += len(chunk)
            if self._cb:
                self._cb(self._sent, self._total)
        return chunk

    def __len__(self) -> int:
        # urllib uses len(data) to set Content-Length when not already set
        return self._total


@dataclass
class TaskResult:
    task_tag: Optional[str]
    created_uuid: Optional[str]


class HyperCoreClient:
    """One client = one cluster session. Not thread-safe.

    Args:
        host:        REST URL like 'https://10.0.0.5/rest/v1'.
        verify_tls:  False for self-signed clusters (the common case).
        timeout:     per-request socket timeout, seconds.
    """

    def __init__(self, host: str, verify_tls: bool = False, timeout: float = 30.0) -> None:
        self.host = host.rstrip("/")
        self.verify_tls = verify_tls
        self.timeout = timeout

        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._ssl_ctx = ctx
        self._cookies = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(self._cookies),
        )

    # ------------------------------------------------------------------ auth

    def login(self, username: str, password: str) -> None:
        """Authenticate and stash the session cookie. Raises on failure."""
        self.request("POST", "/login", json_body={"username": username, "password": password})

    # --------------------------------------------------------------- request

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        raw_body: Optional[bytes | BinaryIO] = None,
        query: Optional[dict[str, Any]] = None,
        content_type: Optional[str] = None,
        extra_headers: Optional[dict[str, str]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
        body_size: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Generic request. Returns parsed JSON, or {} for empty bodies.

        Pass exactly one of json_body / raw_body. For raw_body, body_size is
        required (used for Content-Length and progress callback). progress is
        called as `progress(bytes_sent, total)`.

        timeout overrides self.timeout for this single call — useful for
        long-running uploads where the server response can take minutes to
        arrive after the body has been sent.
        """
        url = self.host + path
        if query:
            url += "?" + urllib.parse.urlencode(query)

        headers: dict[str, str] = {"Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)

        body_data: Any = None
        if json_body is not None:
            body_data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
            if isinstance(raw_body, (bytes, bytearray)):
                body_data = bytes(raw_body)
                body_size = len(body_data)
            else:
                if body_size is None:
                    raise ValueError("body_size required when raw_body is a stream")
                # wrap so http.client can read it AND we can report progress
                body_data = _ProgressReader(raw_body, body_size, progress)
            headers["Content-Length"] = str(body_size)

        # sanity: collapse any case-duplicate headers a caller might have
        # introduced via extra_headers (HTTP forbids duplicate Content-Length;
        # nginx rejects requests that have it twice with a 400)
        headers = _normalize_headers(headers)

        req = urllib.request.Request(url, data=body_data, headers=headers, method=method)
        effective_timeout = timeout if timeout is not None else self.timeout
        try:
            with self._opener.open(req, timeout=effective_timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            self._raise(e.code, raw, f"{method} {path}")
        except urllib.error.URLError as e:
            raise HyperCoreError(f"{method} {path}: connection failed: {e.reason}") from e

        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"_raw": raw}

    @staticmethod
    def _raise(code: int, raw: bytes, ctx: str) -> None:
        msg = raw.decode("utf-8", errors="replace") if raw else f"HTTP {code}"
        try:
            payload = json.loads(msg)
            if isinstance(payload, dict):
                msg = payload.get("error") or payload.get("message") or msg
        except (json.JSONDecodeError, TypeError):
            pass
        raise HyperCoreError(f"{ctx} -> HTTP {code}: {msg}")

    # ----------------------------------------------------------------- tasks

    def wait_task(
        self,
        task_tag: str,
        *,
        timeout: float = 600.0,
        poll: float = 2.0,
        progress: Optional[Callable[[int, str], None]] = None,
    ) -> dict:
        """Poll /TaskTag/{tag} until COMPLETE/ERROR or timeout. Returns final
        TaskTagStatus. progress(percent, formatted_description) is called each poll."""
        deadline = time.monotonic() + timeout
        last_pct = -1
        while True:
            data = self.request("GET", f"/TaskTag/{task_tag}")
            # /TaskTag returns a list with a single entry per the spec; tolerate both shapes
            if isinstance(data, list):
                data = data[0] if data else {}
            state = data.get("state") or "UNINITIALIZED"
            pct = int(data.get("progressPercent", 0) or 0)
            if progress and pct != last_pct:
                progress(pct, data.get("formattedDescription", "") or "")
                last_pct = pct
            if state == "COMPLETE":
                return data
            if state == "ERROR":
                msg = data.get("formattedMessage") or "task failed"
                raise HyperCoreError(f"task {task_tag} ERROR: {msg}")
            if time.monotonic() > deadline:
                raise HyperCoreError(f"task {task_tag} did not finish within {timeout:.0f}s (state={state})")
            time.sleep(poll)

    @staticmethod
    def _result(payload: Any) -> TaskResult:
        """Normalize a Result-shaped response."""
        if not isinstance(payload, dict):
            return TaskResult(None, None)
        return TaskResult(
            task_tag=payload.get("taskTag"),
            created_uuid=payload.get("createdUUID"),
        )

    # -------------------------------------------------------------- VirtualDisk

    def upload_virtual_disk(
        self,
        name: str,
        stream: BinaryIO,
        size: int,
        *,
        progress: Optional[Callable[[int, int], None]] = None,
        upload_timeout: float = 1800.0,
    ) -> str:
        """Upload a qcow2 (or raw) image. `name` must include the file extension.
        Returns the VirtualDisk UUID.

        upload_timeout (default 30 min) bounds how long we wait for the cluster's
        response AFTER the body has been sent. HyperCore ingests/converts the
        disk before responding, which can take several minutes for large qcow2s.
        """
        payload = self.request(
            "PUT",
            "/VirtualDisk/upload",
            raw_body=stream,
            body_size=size,
            content_type="application/octet-stream",
            query={"filename": name, "filesize": size},
            progress=progress,
            timeout=upload_timeout,
        )
        result = self._result(payload)
        if result.task_tag:
            done = self.wait_task(result.task_tag, timeout=3600)  # uploads can be slow
            if not result.created_uuid:
                result = TaskResult(result.task_tag, done.get("objectUUID"))
        if not result.created_uuid:
            raise HyperCoreError(f"upload of {name}: no createdUUID in response")
        return result.created_uuid

    def attach_virtual_disk(
        self,
        virtual_disk_uuid: str,
        vm_uuid: str,
        *,
        type_: str = "VIRTIO_DISK",
        capacity_bytes: Optional[int] = None,
        cache_mode: str = "WRITETHROUGH",
        regenerate_disk_id: bool = True,
    ) -> str:
        """Clone the uploaded VirtualDisk onto the VM as a real BlockDevice.
        Returns the new VirDomainBlockDevice UUID."""
        template: dict[str, Any] = {
            "virDomainUUID": vm_uuid,
            "type": type_,
            "cacheMode": cache_mode,
        }
        if capacity_bytes:
            template["capacity"] = capacity_bytes
        body = {
            "options": {"regenerateDiskID": regenerate_disk_id, "readOnly": False},
            "template": template,
        }
        payload = self.request("POST", f"/VirtualDisk/{virtual_disk_uuid}/attach", json_body=body)
        result = self._result(payload)
        if result.task_tag:
            done = self.wait_task(result.task_tag)
            if not result.created_uuid:
                result = TaskResult(result.task_tag, done.get("createdUUID") or done.get("objectUUID"))
        if not result.created_uuid:
            raise HyperCoreError("attach: no createdUUID in response")
        return result.created_uuid

    # ------------------------------------------------------------------ VM

    def create_vm(
        self,
        *,
        name: str,
        description: str = "",
        vcpus: int,
        mem_bytes: int,
        cloudinit_user_data: str,
        cloudinit_meta_data: str = "",
        operating_system: str = "os_other",
        machine_type_keyword: str = "uefi",
        attach_guest_tools_iso: bool = False,
        tags: str = "",
    ) -> str:
        """Create a VM with cloud-init data attached as a NoCloud ISO.
        Returns the new VirDomain UUID."""
        dom: dict[str, Any] = {
            "name": name,
            "description": description,
            "operatingSystem": operating_system,
            "mem": int(mem_bytes),
            "numVCPU": int(vcpus),
            "blockDevs": [],
            "netDevs": [],
            "tags": tags,
            "cloudInitData": {
                "userData": base64.b64encode(cloudinit_user_data.encode("utf-8")).decode("ascii"),
                "metaData": base64.b64encode((cloudinit_meta_data or "").encode("utf-8")).decode("ascii"),
            },
        }
        body = {
            "dom": dom,
            "options": {
                "attachGuestToolsISO": attach_guest_tools_iso,
                "machineTypeKeyword": machine_type_keyword,
            },
        }
        payload = self.request("POST", "/VirDomain", json_body=body)
        result = self._result(payload)
        if result.task_tag:
            done = self.wait_task(result.task_tag)
            if not result.created_uuid:
                result = TaskResult(result.task_tag, done.get("createdUUID") or done.get("objectUUID"))
        if not result.created_uuid:
            raise HyperCoreError("create_vm: no createdUUID in response")
        return result.created_uuid

    def update_vm(self, vm_uuid: str, patch: dict[str, Any]) -> None:
        """PATCH a VM (e.g. to set bootDevices after disk attach)."""
        payload = self.request("PATCH", f"/VirDomain/{vm_uuid}", json_body=patch)
        result = self._result(payload)
        if result.task_tag:
            self.wait_task(result.task_tag)

    def get_vm(self, vm_uuid: str) -> dict:
        data = self.request("GET", f"/VirDomain/{vm_uuid}")
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    # ----------------------------------------------------------------- NetDev

    def create_nic(
        self,
        vm_uuid: str,
        *,
        type_: str = "VIRTIO",
        vlan: int = 0,
        mac_address: str = "",
        connected: bool = True,
    ) -> str:
        body: dict[str, Any] = {
            "virDomainUUID": vm_uuid,
            "type": type_,
            "vlan": vlan,
            "connected": connected,
        }
        if mac_address:
            body["macAddress"] = mac_address
        payload = self.request("POST", "/VirDomainNetDevice", json_body=body)
        result = self._result(payload)
        if result.task_tag:
            done = self.wait_task(result.task_tag)
            if not result.created_uuid:
                result = TaskResult(result.task_tag, done.get("createdUUID") or done.get("objectUUID"))
        if not result.created_uuid:
            raise HyperCoreError("create_nic: no createdUUID in response")
        return result.created_uuid

    # ----------------------------------------------------------------- action

    def action(self, vm_uuid: str, action_type: str, *, node_uuid: str = "") -> None:
        """Send a VM lifecycle action (START/SHUTDOWN/STOP/REBOOT/RESET/...)."""
        body = [{"virDomainUUID": vm_uuid, "actionType": action_type}]
        if node_uuid:
            body[0]["nodeUUID"] = node_uuid
        payload = self.request("POST", "/VirDomain/action", json_body=body)
        result = self._result(payload)
        if result.task_tag:
            self.wait_task(result.task_tag)

    def power_on(self, vm_uuid: str, *, node_uuid: str = "") -> None:
        self.action(vm_uuid, "START", node_uuid=node_uuid)

    # -------------------------------------------------------------- live IPs

    def vm_ipv4(self, vm_uuid: str) -> list[str]:
        """Best-effort: return any IPv4 addresses the cluster has observed
        on this VM's NICs. Empty until cloud-init has finished networking."""
        vm = self.get_vm(vm_uuid)
        ips: list[str] = []
        for nd in vm.get("netDevs", []) or []:
            for ip in nd.get("ipv4Addresses", []) or []:
                if ip:
                    ips.append(ip)
        return ips
