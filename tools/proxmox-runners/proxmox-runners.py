#!/usr/bin/env python3
"""Deploy GitHub Actions runner VMs across a Proxmox cluster.

Phase A copies the Ubuntu 26.04 Packer template to each local-storage node
(once). Phase B full-clones runner VMs from that node's replica in parallel.
Phase C starts guests and registers actions/runner. Runners are never cloned
on the template node and migrated.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent
GUEST_SETUP = TOOL_DIR / "guest-setup.sh"
DISK_CLEANUP_DIR = TOOL_DIR / "disk-cleanup"
DEFAULT_LABELS = ["self-hosted", "linux", "x64", "ubuntu-26.04"]
RUNNER_REPO = "actions/runner"
AGENT_PING_HTTP_TIMEOUT = 8
AGENT_WAIT_TIMEOUT = 1800
AGENT_WAIT_HEARTBEAT = 30
AGENT_ERROR_LIMIT = 160
_LOG_LOCK = threading.Lock()


def log(message: str) -> None:
    with _LOG_LOCK:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def fatal(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.is_file():
        fatal(f"config file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    elif path.suffix == ".toml":
        import tomllib

        data = tomllib.loads(text)
    else:
        fatal(f"config must be .toml or .json: {path}")
    if not isinstance(data, dict):
        fatal("config root must be a table/object")
    return data


def cfg_get(config: dict[str, Any], section: str, key: str, default: Any = "") -> Any:
    block = config.get(section, {})
    if not isinstance(block, dict):
        return default
    return block.get(key, default)


def expand_user_path(value: str) -> str:
    return os.path.expanduser(value) if value else value


def parse_github_target(url: str) -> tuple[str, str]:
    """Return ('repo', 'owner/repo') or ('org', 'org')."""
    raw = url.strip().rstrip("/")
    if raw.startswith("https://github.com/"):
        raw = raw[len("https://github.com/") :]
    elif raw.startswith("http://github.com/"):
        raw = raw[len("http://github.com/") :]
    raw = raw.split("?")[0].strip("/")
    if not raw:
        raise ValueError("GitHub url is empty")
    parts = [p for p in raw.split("/") if p]
    if len(parts) >= 2:
        return "repo", f"{parts[0]}/{parts[1]}"
    return "org", parts[0]


def default_labels(extra: list[str] | None) -> list[str]:
    labels = list(DEFAULT_LABELS)
    for item in extra or []:
        if item and item not in labels:
            labels.append(item)
    return labels


def assign_nodes(nodes: list[str], count: int) -> list[str]:
    if not nodes:
        raise ValueError("no target nodes")
    if count < 1:
        raise ValueError("count must be >= 1")
    return [nodes[i % len(nodes)] for i in range(count)]


def runner_name(prefix: str, index: int) -> str:
    return f"{prefix}-{index:02d}"


def replica_template_name(template_name: str, node: str) -> str:
    """Per-node template replica on local storage, e.g. ubuntu-2604-runner-pve2."""
    return f"{template_name}-{node}"


def replica_delete_refusal(
    row: dict[str, Any],
    node: str,
    *,
    template_name: str,
    name_prefix: str,
    source: dict[str, Any] | None = None,
) -> str | None:
    """Why this VM must not be deleted as a per-node replica, or None if it is safe."""
    name = row.get("name") or ""
    vmid = row.get("vmid")
    wanted = replica_template_name(template_name, node)
    if name == template_name:
        return (
            f"refusing to delete {name!r} (VMID {vmid}): that is the source Packer template "
            f"(name has no -{node} suffix)"
        )
    if name_prefix and (name == name_prefix or name.startswith(name_prefix + "-")):
        return f"refusing to delete {name!r} (VMID {vmid}): name looks like a runner ({name_prefix}-*)"
    if not row.get("template"):
        return f"refusing to delete {name!r} (VMID {vmid}): not a template"
    if name != wanted:
        return f"refusing to delete {name!r} (VMID {vmid}): expected replica name {wanted!r}"
    if source is not None and vmid is not None and int(vmid) == int(source.get("vmid") or 0):
        return f"refusing to delete the source Packer template (VMID {vmid}) on {source.get('node')}"
    return None


def render_ipconfig(template: str, index: int, ip_start: int) -> str:
    if "{n}" not in template:
        return template
    return template.replace("{n}", str(ip_start + index - 1))


def parse_storage_id(disk_value: str) -> str:
    """Parse 'local-lvm:vm-100-disk-0,size=75G' -> local-lvm."""
    head = disk_value.split(",", 1)[0]
    return head.split(":", 1)[0]


def form_encode(params: dict[str, Any]) -> bytes:
    """Encode PVE form fields. Arrays become repeated keys (command=a&command=b)."""
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                pairs.append((key, str(item)))
        elif value is None:
            continue
        else:
            pairs.append((key, str(value)))
    return urllib.parse.urlencode(pairs).encode()


def proxmox_not_implemented(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    if isinstance(message, str) and "not implemented" in message.lower():
        return message
    return None


def ping_succeeded(data: Any) -> bool:
    """POST ping returns {result: {}}. GET returns data=null plus a 'not implemented' message."""
    if data is None:
        return False
    if proxmox_not_implemented(data):
        return False
    return True


def next_unused_vmid(used: set[int], start: int) -> int:
    """Return the smallest VMID >= start that is not already in the cluster."""
    if start < 100:
        start = 100
    candidate = start
    while candidate in used:
        candidate += 1
    return candidate


def is_vmid_conflict(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "file exists" in text or "already exists" in text


def trim_error(exc: BaseException, limit: int = AGENT_ERROR_LIMIT) -> str:
    text = " ".join(str(exc).split())
    if not text:
        text = exc.__class__.__name__
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


class ProxmoxAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    text = str(exc)
    for code in (401, 403, 404, 405, 500, 501):
        if f"HTTP {code}" in text:
            return code
    return None


def is_absent_vm_error(exc: BaseException) -> bool:
    """True when Proxmox says this VMID is already gone (404 or missing config)."""
    status = http_status(exc)
    text = str(exc).lower()
    if status == 404:
        return True
    return "does not exist" in text or "no such vm" in text or "no such guest" in text


def is_agent_forbidden(exc: BaseException) -> bool:
    if http_status(exc) in {401, 403}:
        return True
    text = str(exc).lower()
    return "permission check failed" in text or "permission denied" in text


def guest_agent_acl_hint(token_id: str) -> str:
    ident = token_id or "user@realm!tokenid"
    return (
        f"Proxmox API token {ident} cannot use qemu-guest-agent (HTTP 401/403). "
        "`qm agent <vmid> ping` can succeed because it runs as root on the node, not as this token. "
        "Grant VM.GuestAgent.Audit + FileRead + FileWrite + Unrestricted (PVE 9), or VM.Monitor (PVE 8). "
        "Example:\n"
        '  pveum role add RunnerFleet -privs "VM.GuestAgent.Audit,VM.GuestAgent.FileRead,'
        'VM.GuestAgent.FileWrite,VM.GuestAgent.Unrestricted"\n'
        f"  pveum acl modify / --token '{ident}' --role RunnerFleet"
    )


class HttpClient:
    def __init__(self, insecure: bool = False) -> None:
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._ctx = ctx

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: int = 120,
    ) -> Any:
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProxmoxAPIError(
                f"{method} {url} failed: HTTP {exc.code}: {detail}",
                status=exc.code,
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError(f"{method} {url} timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise RuntimeError(f"{method} {url} timed out after {timeout}s") from exc
            raise RuntimeError(f"{method} {url} failed: {exc}") from exc
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return body.decode("utf-8", errors="replace")


class Proxmox:
    def __init__(self, url: str, username: str, token: str, password: str, insecure: bool) -> None:
        self.base = url.rstrip("/")
        if not self.base.endswith("/api2/json"):
            if self.base.endswith("/api2"):
                self.base += "/json"
            else:
                self.base += "/api2/json"
        self.http = HttpClient(insecure=insecure)
        self.headers = {"Accept": "application/json"}
        if token:
            self.headers["Authorization"] = f"PVEAPIToken={username}={token}"
        elif password:
            self._login(username, password)
        else:
            fatal("Proxmox token or password is required (PROXMOX_TOKEN preferred)")

    def _login(self, username: str, password: str) -> None:
        payload = urllib.parse.urlencode({"username": username, "password": password}).encode()
        data = self.http.request(
            "POST",
            f"{self.base}/access/ticket",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
        )
        ticket = data["data"]["ticket"]
        csrf = data["data"]["CSRFPreventionToken"]
        self.headers["Cookie"] = f"PVEAuthCookie={ticket}"
        self.headers["CSRFPreventionToken"] = csrf

    def call(self, method: str, path: str, params: dict[str, Any] | None = None, timeout: int = 120) -> Any:
        url = f"{self.base}/{path.lstrip('/')}"
        data = None
        headers = dict(self.headers)
        if method in {"GET", "HEAD", "DELETE"}:
            if params:
                url += "?" + form_encode(params).decode()
        else:
            data = form_encode(params or {})
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        result = self.http.request(method, url, headers=headers, data=data, timeout=timeout)
        if isinstance(result, dict):
            note = proxmox_not_implemented(result)
            if note:
                raise ProxmoxAPIError(f"{method} {path} not implemented: {note}", status=501)
            if "data" in result:
                return result["data"]
        return result

    def wait_task(self, upid: str, timeout: int) -> None:
        if not upid or not str(upid).startswith("UPID:"):
            return
        node = str(upid).split(":")[1]
        encoded = urllib.parse.quote(str(upid), safe="")
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.call("GET", f"nodes/{node}/tasks/{encoded}/status")
            if status and status.get("status") != "running":
                exitstatus = status.get("exitstatus", "")
                if exitstatus in {"OK", "WARNINGS: 0", None, ""}:
                    return
                if isinstance(exitstatus, str) and exitstatus.startswith("OK"):
                    return
                raise RuntimeError(f"Proxmox task failed: {exitstatus}")
            time.sleep(3)
        raise TimeoutError(f"timed out waiting for task {upid}")

    def request_task(self, method: str, path: str, params: dict[str, Any], timeout: int) -> Any:
        """POST/DELETE that returns a UPID: wait until that task finishes."""
        upid = self.call(method, path, params, timeout=timeout)
        if isinstance(upid, str):
            self.wait_task(upid, timeout)
        return upid

    def post_task(self, path: str, params: dict[str, Any], timeout: int) -> Any:
        return self.request_task("POST", path, params, timeout)

    def delete_task(self, path: str, params: dict[str, Any], timeout: int) -> Any:
        return self.request_task("DELETE", path, params, timeout)


class GitHub:
    def __init__(self, token: str, insecure: bool = False) -> None:
        if not token:
            fatal("GITHUB_TOKEN or GH_TOKEN is required to mint runner registration tokens")
        self.token = token
        self.http = HttpClient(insecure=insecure)
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "proxmox-runners",
        }

    def _api(self, method: str, path: str) -> Any:
        return self.http.request(method, f"https://api.github.com{path}", headers=self.headers)

    def registration_token(self, kind: str, target: str) -> str:
        if kind == "repo":
            path = f"/repos/{target}/actions/runners/registration-token"
        else:
            path = f"/orgs/{target}/actions/runners/registration-token"
        data = self._api("POST", path)
        token = (data or {}).get("token")
        if not token:
            fatal(f"GitHub did not return a registration token for {kind} {target}")
        return token

    def list_runners(self, kind: str, target: str) -> list[dict[str, Any]]:
        runners: list[dict[str, Any]] = []
        page = 1
        while True:
            if kind == "repo":
                path = f"/repos/{target}/actions/runners?per_page=100&page={page}"
            else:
                path = f"/orgs/{target}/actions/runners?per_page=100&page={page}"
            data = self._api("GET", path) or {}
            batch = data.get("runners") or []
            runners.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return runners

    def delete_runner(self, kind: str, target: str, runner_id: int) -> None:
        if kind == "repo":
            path = f"/repos/{target}/actions/runners/{runner_id}"
        else:
            path = f"/orgs/{target}/actions/runners/{runner_id}"
        self._api("DELETE", path)

    def latest_runner_tarball(self, version: str) -> tuple[str, str]:
        if version:
            tag = version if version.startswith("v") else f"v{version}"
            bare = tag.lstrip("v")
            url = f"https://github.com/{RUNNER_REPO}/releases/download/{tag}/actions-runner-linux-x64-{bare}.tar.gz"
            return bare, url
        data = self._api("GET", f"/repos/{RUNNER_REPO}/releases/latest")
        tag = (data or {}).get("tag_name") or ""
        bare = tag.lstrip("v")
        for asset in data.get("assets") or []:
            name = asset.get("name") or ""
            if name.startswith("actions-runner-linux-x64-") and name.endswith(".tar.gz"):
                return bare, asset["browser_download_url"]
        if bare:
            url = f"https://github.com/{RUNNER_REPO}/releases/download/{tag}/actions-runner-linux-x64-{bare}.tar.gz"
            return bare, url
        fatal("could not resolve the latest actions/runner linux-x64 release")
        return "", ""


@dataclass
class Settings:
    proxmox_url: str
    proxmox_username: str
    proxmox_token: str
    proxmox_password: str
    insecure: bool
    node_allowlist: list[str]
    template_name: str
    template_vmid: int | None
    storage: str
    recreate_templates: bool
    task_timeout: int
    name_prefix: str
    vmid_start: int | None
    cores: int | None
    memory_mb: int | None
    ciuser: str
    ssh_public_key: str
    ipconfig: str
    ip_start: int
    nameserver: str
    searchdomain: str
    github_url: str
    github_token: str
    labels: list[str]
    runner_version: str
    runner_dir: str
    github_kind: str = ""
    github_target: str = ""
    runner_tarball_url: str = ""

    @classmethod
    def from_sources(cls, config: dict[str, Any], args: argparse.Namespace) -> Settings:
        extra = cfg_get(config, "github", "extra_labels", []) or []
        if isinstance(extra, str):
            extra = [part.strip() for part in extra.split(",") if part.strip()]
        configured = cfg_get(config, "github", "labels", None)
        if configured is None:
            labels = default_labels(extra)
        else:
            labels = list(configured)
            for item in extra:
                if item not in labels:
                    labels.append(item)
        if getattr(args, "labels", None):
            labels = [part.strip() for part in args.labels.split(",") if part.strip()]

        nodes = cfg_get(config, "proxmox", "nodes", []) or []
        if isinstance(nodes, str):
            nodes = [part.strip() for part in nodes.split(",") if part.strip()]
        if getattr(args, "nodes", None):
            nodes = [part.strip() for part in args.nodes.split(",") if part.strip()]

        ssh_file = expand_user_path(
            getattr(args, "ssh_public_key_file", "") or cfg_get(config, "vm", "ssh_public_key_file", "")
        )
        ssh_key = ""
        if ssh_file:
            ssh_path = Path(ssh_file)
            if not ssh_path.is_file():
                fatal(f"ssh public key file not found: {ssh_path}")
            ssh_key = ssh_path.read_text(encoding="utf-8").strip() + "\n"

        template_vmid = cfg_get(config, "proxmox", "template_vmid", None)
        if getattr(args, "template_vmid", None):
            template_vmid = args.template_vmid
        if template_vmid in ("", None):
            template_vmid = None
        else:
            template_vmid = int(template_vmid)

        vmid_start = cfg_get(config, "vm", "vmid_start", None)
        if vmid_start in ("", None):
            vmid_start = None
        else:
            vmid_start = int(vmid_start)

        github_url = getattr(args, "github_url", "") or cfg_get(config, "github", "url", "")
        settings = cls(
            proxmox_url=getattr(args, "proxmox_url", "")
            or env_first("PROXMOX_URL")
            or cfg_get(config, "proxmox", "url"),
            proxmox_username=getattr(args, "proxmox_username", "")
            or env_first("PROXMOX_USERNAME")
            or cfg_get(config, "proxmox", "username"),
            proxmox_token=env_first("PROXMOX_TOKEN") or cfg_get(config, "proxmox", "token"),
            proxmox_password=env_first("PROXMOX_PASSWORD") or cfg_get(config, "proxmox", "password"),
            insecure=bool(
                getattr(args, "insecure", False)
                or cfg_get(config, "proxmox", "insecure_skip_tls_verify", False)
            ),
            node_allowlist=list(nodes),
            template_name=getattr(args, "template_name", "")
            or cfg_get(config, "proxmox", "template_name", "ubuntu-2604-runner"),
            template_vmid=template_vmid,
            storage=getattr(args, "storage", "") or cfg_get(config, "proxmox", "storage", "local-lvm"),
            recreate_templates=bool(
                getattr(args, "recreate_templates", False)
                or cfg_get(config, "proxmox", "recreate_templates", False)
            ),
            task_timeout=int(cfg_get(config, "proxmox", "task_timeout_seconds", 7200) or 7200),
            name_prefix=getattr(args, "name_prefix", "") or cfg_get(config, "vm", "name_prefix", "gh-runner"),
            vmid_start=vmid_start,
            cores=int(cfg_get(config, "vm", "cores", 0) or 0) or None,
            memory_mb=int(cfg_get(config, "vm", "memory_mb", 0) or 0) or None,
            ciuser=cfg_get(config, "vm", "ciuser", "runner") or "runner",
            ssh_public_key=ssh_key,
            ipconfig=cfg_get(config, "vm", "ipconfig", "ip=dhcp") or "ip=dhcp",
            ip_start=int(cfg_get(config, "vm", "ip_start", 50) or 50),
            nameserver=cfg_get(config, "vm", "nameserver", ""),
            searchdomain=cfg_get(config, "vm", "searchdomain", ""),
            github_url=github_url,
            github_token=env_first("GITHUB_TOKEN", "GH_TOKEN") or cfg_get(config, "github", "token"),
            labels=labels,
            runner_version=str(cfg_get(config, "github", "runner_version", "") or ""),
            runner_dir=cfg_get(config, "github", "runner_dir", "/opt/actions-runner"),
        )
        if not settings.proxmox_url:
            fatal("Proxmox URL is required")
        if not settings.proxmox_username:
            fatal("Proxmox username is required (user@realm or user@realm!tokenid)")
        if settings.github_url:
            settings.github_kind, settings.github_target = parse_github_target(settings.github_url)
        return settings


@dataclass
class PlannedVM:
    name: str
    node: str
    index: int
    vmid: int | None = None
    existed: bool = False


class Fleet:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pve = Proxmox(
            settings.proxmox_url,
            settings.proxmox_username,
            settings.proxmox_token,
            settings.proxmox_password,
            settings.insecure,
        )
        self._gh: GitHub | None = None
        self._used_vmids: set[int] = set()
        self._vmid_lock = threading.Lock()
        self._resources_cache: list[dict[str, Any]] | None = None
        self._skip_reuse_nodes: set[str] = set()
        self._skip_reuse_vmids: set[int] = set()
        self._skip_reuse_names: set[str] = set()

    @property
    def gh(self) -> GitHub:
        if self._gh is None:
            self._gh = GitHub(self.settings.github_token)
        return self._gh

    def online_nodes(self) -> list[str]:
        rows = self.pve.call("GET", "nodes") or []
        online = []
        for row in rows:
            name = row.get("node") or row.get("id")
            status = row.get("status")
            if name and status == "online":
                online.append(name)
        online.sort()
        if self.settings.node_allowlist:
            wanted = list(self.settings.node_allowlist)
            missing = [name for name in wanted if name not in online]
            if missing:
                fatal(
                    "allowlisted nodes are missing or offline: "
                    + ", ".join(missing)
                    + f" (online: {', '.join(online) or 'none'})"
                )
            return wanted
        if not online:
            fatal("GET /nodes returned no online nodes")
        return online

    def invalidate_resources(self) -> None:
        """Drop any cached cluster/resources snapshot after mutations."""
        self._resources_cache = None

    def resources(self, rtype: str | None = None, *, refresh: bool = True) -> list[dict[str, Any]]:
        """Cluster inventory. Default refresh=True so deletes/clones are not served stale."""
        if refresh:
            params = {"type": rtype} if rtype else None
            rows = self.pve.call("GET", "cluster/resources", params) or []
            if rtype is None:
                self._resources_cache = rows
            else:
                self._resources_cache = None
            return rows
        if self._resources_cache is None:
            self._resources_cache = self.pve.call("GET", "cluster/resources") or []
        if rtype:
            return [row for row in self._resources_cache if row.get("type") == rtype]
        return list(self._resources_cache)

    def find_vm(self, name: str) -> dict[str, Any] | None:
        for row in self.resources("vm"):
            if row.get("name") == name:
                return row
        return None

    def find_template(self, node: str | None = None) -> dict[str, Any]:
        matches = []
        for row in self.resources("vm"):
            if not row.get("template"):
                continue
            if self.settings.template_vmid and int(row.get("vmid", 0)) == self.settings.template_vmid:
                matches.append(row)
            elif row.get("name") == self.settings.template_name or (
                row.get("name") or ""
            ).startswith(self.settings.template_name + "-"):
                matches.append(row)
        if not matches:
            ident = (
                f"vmid {self.settings.template_vmid}"
                if self.settings.template_vmid
                else f"name {self.settings.template_name}"
            )
            fatal(f"Packer template not found ({ident}). Build it first.")
        if node:
            on_node = [row for row in matches if row.get("node") == node]
            if on_node:
                exact = [row for row in on_node if row.get("name") == self.settings.template_name]
                return exact[0] if exact else on_node[0]
        exact = [row for row in matches if row.get("name") == self.settings.template_name]
        return exact[0] if exact else matches[0]

    def find_template_on_node(self, node: str) -> dict[str, Any] | None:
        """Return a template already stored on node, or None (no other-node fallback)."""
        replica = replica_template_name(self.settings.template_name, node)
        fallback: dict[str, Any] | None = None
        for row in self.resources("vm"):
            if not row.get("template") or row.get("node") != node:
                continue
            name = row.get("name") or ""
            vmid = int(row.get("vmid", 0) or 0)
            if self.settings.template_vmid and vmid == self.settings.template_vmid:
                return row
            if name == self.settings.template_name:
                return row
            if name == replica:
                fallback = row
            elif name.startswith(self.settings.template_name + "-") and fallback is None:
                fallback = row
        return fallback

    def find_replica_vm(self, node: str) -> dict[str, Any] | None:
        """Return ubuntu-2604-runner-<node> on that node even if it is not a template yet."""
        wanted = replica_template_name(self.settings.template_name, node)
        for row in self.resources("vm"):
            if row.get("name") == wanted and row.get("node") == node:
                return row
        return None

    def storage_info(self, storage: str) -> dict[str, Any]:
        for row in self.pve.call("GET", "storage") or []:
            if row.get("storage") == storage:
                return row
        # Fall back to cluster resources.
        for row in self.resources("storage"):
            if (row.get("storage") or row.get("id", "").split("/")[-1]) == storage:
                return row
        return {}

    def storage_is_shared(self, storage: str) -> bool:
        info = self.storage_info(storage)
        shared = info.get("shared")
        return bool(int(shared)) if shared not in (None, "") else False

    def node_has_image_storage(self, node: str, storage: str) -> bool:
        rows = self.pve.call("GET", f"nodes/{node}/storage") or []
        for row in rows:
            if row.get("storage") != storage:
                continue
            if row.get("active") in (0, "0", False):
                return False
            content = str(row.get("content") or "")
            return "images" in content.split(",")
        return False

    def require_storage_on_node(self, node: str, storage: str) -> None:
        if self.node_has_image_storage(node, storage):
            return
        available = []
        for row in self.pve.call("GET", f"nodes/{node}/storage") or []:
            content = str(row.get("content") or "")
            if "images" in content.split(","):
                available.append(row.get("storage"))
        hint = f" Storages with images on {node}: {', '.join(available) or 'none'}."
        if self.storage_is_shared(storage):
            fatal(f"node {node} cannot use shared storage {storage!r}.{hint}")
        fatal(
            f"node {node} cannot receive a disk on {storage!r}. "
            f"{storage} is treated as node-local (typical home-lab local-lvm). "
            f"Each target node needs a storage id {storage!r} with content=images.{hint}"
        )

    def next_vmid(self) -> int:
        value = self.pve.call("GET", "cluster/nextid")
        return int(value)

    def cluster_vmids(self) -> set[int]:
        used: set[int] = set()
        for row in self.resources("vm"):
            if row.get("vmid") is not None:
                used.add(int(row["vmid"]))
        return used

    def allocate_vmid(self, used: set[int] | None = None) -> int:
        """Cluster-safe VMID allocation. Concurrent callers never share an id."""
        with self._vmid_lock:
            if used is None:
                used = self._used_vmids
            return self._allocate_vmid_unlocked(used)

    def _allocate_vmid_unlocked(self, used: set[int]) -> int:
        if self.settings.vmid_start is not None:
            vmid = next_unused_vmid(used, self.settings.vmid_start)
            used.add(vmid)
            return vmid
        for _ in range(32):
            candidate = self.next_vmid()
            if candidate not in used:
                used.add(candidate)
                return candidate
            vmid = next_unused_vmid(used, candidate + 1)
            used.add(vmid)
            return vmid
        fatal("could not allocate a free Proxmox VMID")
        return 0

    def _reserve_clone_vmid(self, planned: PlannedVM, reuse_planned: bool) -> int:
        with self._vmid_lock:
            if reuse_planned and planned.vmid is not None:
                vmid = planned.vmid
                self._used_vmids.add(vmid)
            else:
                vmid = self._allocate_vmid_unlocked(self._used_vmids)
            planned.vmid = vmid
            return vmid

    def allocate_replica_vmid(self) -> int:
        """Template replicas use cluster nextid so [vm].vmid_start stays for runners."""
        with self._vmid_lock:
            for _ in range(32):
                candidate = self.next_vmid()
                if candidate not in self._used_vmids:
                    self._used_vmids.add(candidate)
                    return candidate
                vmid = next_unused_vmid(self._used_vmids, candidate + 1)
                self._used_vmids.add(vmid)
                return vmid
            fatal("could not allocate a free Proxmox VMID for a template replica")
            return 0

    def vm_config(self, node: str, vmid: int) -> dict[str, Any]:
        return self.pve.call("GET", f"nodes/{node}/qemu/{vmid}/config") or {}

    def template_storage(self, template: dict[str, Any]) -> str:
        if self.settings.storage:
            return self.settings.storage
        cfg = self.vm_config(template["node"], int(template["vmid"]))
        for key, value in cfg.items():
            if key.startswith(("scsi", "virtio", "sata", "ide")) and isinstance(value, str) and ":" in value:
                if "cloudinit" in value or "media=cdrom" in value:
                    continue
                return parse_storage_id(value)
        fatal("could not determine template disk storage; set [proxmox].storage")
        return ""

    def _convert_to_template(self, row: dict[str, Any]) -> dict[str, Any]:
        node = row["node"]
        vmid = int(row["vmid"])
        if row.get("template"):
            return row
        log(f"converting {row.get('name')} (VMID {vmid}) to template on {node}")
        self.pve.post_task(f"nodes/{node}/qemu/{vmid}/template", {}, self.settings.task_timeout)
        updated = dict(row)
        updated["template"] = 1
        return updated

    def _reuse_local_template(self, node: str) -> dict[str, Any] | None:
        existing = self.find_template_on_node(node)
        if existing is not None:
            return existing
        leftover = self.find_replica_vm(node)
        if leftover is None:
            return None
        return self._convert_to_template(leftover)

    def ensure_template_on_node(self, node: str, storage: str) -> dict[str, Any]:
        existing = self._reuse_local_template(node)
        if existing is not None:
            return existing
        source = self.find_template()
        if self.storage_is_shared(storage):
            log(f"template {source['vmid']} is on shared storage {storage}; clones can target {node}")
            return source
        return self._copy_template_to_node(source, node, storage)

    def _copy_template_to_node(self, source: dict[str, Any], node: str, storage: str) -> dict[str, Any]:
        """Clone the source template on its node, migrate the replica, convert to template.

        This is the only migrate on the local-storage path. Runner VMs are never migrated.
        """
        self.require_storage_on_node(node, storage)
        replica_vmid = self.allocate_replica_vmid()
        replica_name = replica_template_name(self.settings.template_name, node)
        source_node = source["node"]
        log(
            f"Phase A: cloning template {source['vmid']} on {source_node} "
            f"to {replica_vmid} ({replica_name}) then migrating to {node}"
        )
        self.pve.post_task(
            f"nodes/{source_node}/qemu/{source['vmid']}/clone",
            {
                "newid": replica_vmid,
                "name": replica_name,
                "full": 1,
                "storage": storage,
            },
            self.settings.task_timeout,
        )
        if source_node != node:
            self.pve.post_task(
                f"nodes/{source_node}/qemu/{replica_vmid}/migrate",
                {
                    "target": node,
                    "online": 0,
                    "with-local-disks": 1,
                    "targetstorage": storage,
                },
                self.settings.task_timeout,
            )
        self.pve.post_task(f"nodes/{node}/qemu/{replica_vmid}/template", {}, self.settings.task_timeout)
        log(f"created template replica {replica_vmid} ({replica_name}) on {node}")
        return {"node": node, "vmid": replica_vmid, "name": replica_name, "template": 1}

    def _qemu_vm_exists(self, node: str, vmid: int) -> bool:
        try:
            data = self.pve.call("GET", f"nodes/{node}/qemu/{vmid}/status/current")
            return data is not None
        except (ProxmoxAPIError, RuntimeError) as exc:
            if is_absent_vm_error(exc):
                return False
            raise

    def _wait_replica_absent(self, node: str, vmid: int, name: str) -> None:
        """Poll qemu + refreshed cluster/resources until this replica is gone."""
        deadline = time.time() + self.settings.task_timeout
        while time.time() < deadline:
            self.invalidate_resources()
            in_inventory = False
            for row in self.resources("vm", refresh=True):
                if int(row.get("vmid") or 0) == vmid:
                    in_inventory = True
                    break
                if name and row.get("name") == name and row.get("node") == node:
                    in_inventory = True
                    break
            on_node = self._qemu_vm_exists(node, vmid)
            if not on_node:
                if in_inventory:
                    log(
                        f"Phase A: replica {name} (VMID {vmid}) is gone on {node}; "
                        "cluster/resources still lists it (stale), will not reuse"
                    )
                return
            time.sleep(2)
        fatal(
            f"replica {name} (VMID {vmid}) on {node} is still present after delete; "
            "refusing to reuse it"
        )

    def delete_replica_template(self, row: dict[str, Any], node: str, source: dict[str, Any]) -> None:
        reason = replica_delete_refusal(
            row,
            node,
            template_name=self.settings.template_name,
            name_prefix=self.settings.name_prefix,
            source=source,
        )
        if reason:
            fatal(reason)
        vmid = int(row["vmid"])
        name = row.get("name") or ""
        source_vmid = int(source.get("vmid") or 0)
        if source_vmid and vmid == source_vmid:
            fatal(f"refusing to delete the source Packer template (VMID {vmid}) on {source.get('node')}")
        if name == self.settings.template_name:
            fatal(
                f"refusing to delete {name!r} (VMID {vmid}): that is the source Packer template"
            )
        log(f"Phase A: deleting replica template {name} (VMID {vmid}) on {node}")
        try:
            delete_task = getattr(self.pve, "delete_task", None)
            if delete_task is not None:
                delete_task(
                    f"nodes/{node}/qemu/{vmid}",
                    {"purge": 1, "destroy-unreferenced-disks": 1},
                    self.settings.task_timeout,
                )
            else:
                upid = self.pve.call(
                    "DELETE",
                    f"nodes/{node}/qemu/{vmid}",
                    {"purge": 1, "destroy-unreferenced-disks": 1},
                    timeout=self.settings.task_timeout,
                )
                if isinstance(upid, str):
                    self.pve.wait_task(upid, self.settings.task_timeout)
        except SystemExit:
            raise
        except Exception as exc:
            if is_absent_vm_error(exc):
                log(f"Phase A: replica {name} (VMID {vmid}) on {node} already gone")
            else:
                fatal(
                    f"failed to delete replica template {name} (VMID {vmid}) on {node}: {exc}"
                )
        self.invalidate_resources()
        self._wait_replica_absent(node, vmid, name)
        # Keep the VMID reserved this run so a stale inventory cannot reallocate it
        # as a clone source or nextid.
        self._used_vmids.add(vmid)
        self._skip_reuse_nodes.add(node)
        self._skip_reuse_vmids.add(vmid)
        if name:
            self._skip_reuse_names.add(name)

    def remove_node_replicas(self, nodes: list[str], source: dict[str, Any]) -> list[tuple[str, int, str]]:
        """Delete {template}-{node} replicas so Phase A can copy the current source again.

        Never deletes the source Packer template. Failed deletes are fatal.
        Returns (node, vmid, name) for each replica that was removed.
        """
        removed: list[tuple[str, int, str]] = []
        source_vmid = int(source.get("vmid") or 0)
        for node in nodes:
            replica = self.find_replica_vm(node)
            if replica is None:
                continue
            replica_vmid = int(replica.get("vmid") or 0)
            if source_vmid and replica_vmid == source_vmid:
                log(
                    f"Phase A: leaving source template {source_vmid} "
                    f"({replica.get('name')}) on {node} in place"
                )
                continue
            self.delete_replica_template(replica, node, source)
            removed.append((node, replica_vmid, replica.get("name") or ""))
        self.invalidate_resources()
        self.resources("vm", refresh=True)
        return removed

    def _should_skip_reuse(self, node: str, existing: dict[str, Any] | None = None) -> bool:
        if node in self._skip_reuse_nodes:
            return True
        if existing is None:
            return False
        vmid = int(existing.get("vmid") or 0)
        name = existing.get("name") or ""
        return vmid in self._skip_reuse_vmids or name in self._skip_reuse_names

    def place_templates(self, nodes: list[str], storage: str) -> dict[str, dict[str, Any]]:
        """Phase A: a template replica on every local-storage node. Shared storage: no replica."""
        source = self.find_template()
        if self.storage_is_shared(storage):
            if self.settings.recreate_templates:
                log("Phase A: --recreate-templates is ignored on shared storage (no per-node replica)")
            log(
                f"Phase A: template {source['vmid']} is on shared storage {storage}; "
                "no per-node replica"
            )
            return {node: source for node in nodes}

        if self.settings.recreate_templates:
            log(
                "Phase A: --recreate-templates: replacing "
                f"{self.settings.template_name}-<node> replicas from source "
                f"{source['vmid']} on {source['node']}"
            )
            self.remove_node_replicas(nodes, source)
            self.invalidate_resources()
            self.resources("vm", refresh=True)

        placed: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for node in nodes:
            if self._should_skip_reuse(node):
                log(
                    f"Phase A: not reusing a just-deleted replica on {node}; "
                    f"will copy from source {source['vmid']}"
                )
                missing.append(node)
                continue
            existing = self._reuse_local_template(node)
            if existing is not None and self._should_skip_reuse(node, existing):
                log(
                    f"Phase A: not reusing deleted {existing.get('name')} "
                    f"(VMID {existing.get('vmid')}) on {node}"
                )
                missing.append(node)
                continue
            if existing is not None:
                log(f"Phase A: reusing template {existing['vmid']} ({existing.get('name')}) on {node}")
                placed[node] = existing
            else:
                missing.append(node)
        if not missing:
            return placed

        self._used_vmids |= self.cluster_vmids()
        log(
            f"Phase A: copying template {source['vmid']} from {source['node']} "
            f"to {', '.join(missing)} (parallel replica copies)"
        )

        def copy_one(node: str) -> dict[str, Any]:
            return self._copy_template_to_node(source, node, storage)

        results = self._run_parallel(missing, copy_one, "template placement")
        for node, replica in zip(missing, results):
            placed[node] = replica
        return placed

    def _run_parallel(self, items: list[Any], fn: Any, label: str) -> list[Any]:
        """Run fn(item) for each item. One item stays on this thread; several use a pool."""
        if not items:
            return []
        if len(items) == 1:
            return [fn(items[0])]
        results: list[Any] = [None] * len(items)
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            futs = {pool.submit(fn, item): index for index, item in enumerate(items)}
            for fut in as_completed(futs):
                index = futs[fut]
                item = items[index]
                ident = getattr(item, "name", None) or str(item)
                try:
                    results[index] = fut.result()
                except SystemExit as exc:
                    errors.append(f"{ident}: {exc}")
                except Exception as exc:
                    errors.append(f"{ident}: {exc}")
        if errors:
            fatal(f"{label} failed: " + "; ".join(errors))
        return results

    def clone_runner(self, template: dict[str, Any], planned: PlannedVM, storage: str) -> int:
        source_node = template["node"]
        last_error: BaseException | None = None
        for attempt in range(2):
            vmid = self._reserve_clone_vmid(planned, reuse_planned=(attempt == 0))
            params: dict[str, Any] = {
                "newid": vmid,
                "name": planned.name,
                "full": 1,
                "storage": storage,
            }
            if self.storage_is_shared(storage) and planned.node != source_node:
                params["target"] = planned.node
                log(f"cloning {template['vmid']} -> {vmid} ({planned.name}) on {planned.node} (shared {storage})")
            else:
                if planned.node != source_node:
                    fatal(
                        f"internal error: template for {planned.node} is still on {source_node}. "
                        "Local disks need a per-node template replica."
                    )
                log(f"cloning {template['vmid']} -> {vmid} ({planned.name}) locally on {planned.node}")
            try:
                self.pve.post_task(
                    f"nodes/{source_node}/qemu/{template['vmid']}/clone",
                    params,
                    self.settings.task_timeout,
                )
                return vmid
            except Exception as exc:
                last_error = exc
                if attempt == 0 and is_vmid_conflict(exc):
                    log(f"VMID {vmid} already exists in the cluster; retrying {planned.name} with a new id")
                    planned.vmid = None
                    continue
                raise
        raise RuntimeError(f"clone {planned.name} failed: {last_error}")

    def clone_planned_runners(
        self,
        planned: list[PlannedVM],
        templates_by_node: dict[str, dict[str, Any]],
        storage: str,
    ) -> None:
        """Phase B: full-clone every new runner from the local replica. No runner migrate."""
        new_items = [item for item in planned if not item.existed]
        if not new_items:
            log("Phase B: no new runner VMs to clone")
            return
        log(
            f"Phase B: full-cloning {len(new_items)} runner VM(s) in parallel "
            f"from per-node templates (local disks, no runner migrate)"
        )

        def clone_one(item: PlannedVM) -> int:
            template = templates_by_node[item.node]
            vmid = self.clone_runner(template, item, storage)
            item.vmid = vmid
            return vmid

        self._run_parallel(new_items, clone_one, "parallel clone")

    def apply_cloudinit(self, node: str, vmid: int, index: int) -> None:
        # Proxmox Cloud-Init has no cigroups field for ciuser. docker
        # membership is applied in guest-setup.sh before svc.sh starts.
        params: dict[str, Any] = {
            "ciuser": self.settings.ciuser,
            "ipconfig0": render_ipconfig(self.settings.ipconfig, index, self.settings.ip_start),
            "agent": "enabled=1",
        }
        if self.settings.ssh_public_key:
            params["sshkeys"] = self.settings.ssh_public_key
        if self.settings.nameserver:
            params["nameserver"] = self.settings.nameserver
        if self.settings.searchdomain:
            params["searchdomain"] = self.settings.searchdomain
        if self.settings.cores:
            params["cores"] = self.settings.cores
        if self.settings.memory_mb:
            params["memory"] = self.settings.memory_mb
        self.pve.call("PUT", f"nodes/{node}/qemu/{vmid}/config", params)

    def start_vm(self, node: str, vmid: int) -> None:
        status = self.pve.call("GET", f"nodes/{node}/qemu/{vmid}/status/current") or {}
        if status.get("status") == "running":
            log(f"VM {vmid} is already running on {node}; waiting for qemu-guest-agent")
            return
        log(f"starting VM {vmid} on {node}")
        self.pve.post_task(f"nodes/{node}/qemu/{vmid}/status/start", {}, 300)

    def _fail_if_agent_forbidden(self, exc: BaseException) -> None:
        if is_agent_forbidden(exc):
            fatal(guest_agent_acl_hint(self.settings.proxmox_username))

    def ping_agent(self, node: str, vmid: int) -> Any:
        path = f"nodes/{node}/qemu/{vmid}/agent/ping"
        try:
            data = self.pve.call("POST", path, timeout=AGENT_PING_HTTP_TIMEOUT)
        except Exception as exc:
            self._fail_if_agent_forbidden(exc)
            text = str(exc)
            if http_status(exc) == 501 or "not implemented" in text.lower():
                fatal(
                    f"agent/ping must be POST; GET is not implemented on this PVE "
                    f"and must not be treated as success. Last error: {trim_error(exc)}"
                )
            raise
        if not ping_succeeded(data):
            raise RuntimeError(
                "agent/ping returned empty or not-implemented data "
                "(GET on this PVE returns HTTP 200 with data=null; POST is required)"
            )
        return data

    def wait_agent(self, node: str, vmid: int, timeout: int = AGENT_WAIT_TIMEOUT) -> None:
        started = time.time()
        deadline = started + timeout
        last_error = "qemu-guest-agent not responding"
        last_heartbeat = started
        log(f"waiting for qemu-guest-agent on VM {vmid} / {node} (up to {timeout}s)")
        while time.time() < deadline:
            try:
                self.ping_agent(node, vmid)
                elapsed = int(time.time() - started)
                log(f"qemu-guest-agent is up on VM {vmid} / {node} after {elapsed}s")
                return
            except SystemExit:
                raise
            except Exception as exc:
                self._fail_if_agent_forbidden(exc)
                last_error = trim_error(exc)
            now = time.time()
            if now - last_heartbeat >= AGENT_WAIT_HEARTBEAT:
                elapsed = int(now - started)
                log(
                    f"still waiting for qemu-guest-agent on VM {vmid} / {node} "
                    f"({elapsed}s elapsed): {last_error}"
                )
                last_heartbeat = now
            time.sleep(2)
        fatal(
            f"VM {vmid} on {node} did not get a qemu-guest-agent ping within {timeout}s. "
            f"Check agent=enabled, Cloud-Init, and DHCP. "
            f"On the node: qm agent {vmid} ping. In the guest: systemctl status qemu-guest-agent. "
            f"If qm ping works but this CLI fails, the API token needs guest-agent privileges "
            f"(see docs). Last error: {last_error}"
        )

    def agent_exec(self, node: str, vmid: int, command: list[str], timeout: int = 1800) -> tuple[int, str, str]:
        log(f"guest-exec on VM {vmid} / {node}: {' '.join(command)}")
        result = self.pve.call(
            "POST",
            f"nodes/{node}/qemu/{vmid}/agent/exec",
            {"command": command},
            timeout=60,
        )
        pid = (result or {}).get("pid")
        if pid is None:
            raise RuntimeError(f"guest-exec returned no pid: {result}")
        log(f"guest-exec pid {pid} started; waiting up to {timeout}s (output when it exits)")
        started = time.time()
        deadline = started + timeout
        last_heartbeat = started
        while time.time() < deadline:
            status = self.pve.call(
                "GET",
                f"nodes/{node}/qemu/{vmid}/agent/exec-status",
                {"pid": pid},
            ) or {}
            if status.get("exited"):
                return (
                    int(status.get("exitcode") or 0),
                    status.get("out-data") or "",
                    status.get("err-data") or "",
                )
            now = time.time()
            if now - last_heartbeat >= AGENT_WAIT_HEARTBEAT:
                log(f"guest-exec pid {pid} still running ({int(now - started)}s elapsed)")
                last_heartbeat = now
            time.sleep(3)
        raise TimeoutError(f"guest-exec pid {pid} timed out")

    def agent_write(self, node: str, vmid: int, path: str, content: str) -> None:
        self.pve.call(
            "POST",
            f"nodes/{node}/qemu/{vmid}/agent/file-write",
            {"file": path, "content": content},
        )

    def write_disk_cleanup_assets(self, node: str, vmid: int) -> None:
        if not DISK_CLEANUP_DIR.is_dir():
            fatal(f"missing {DISK_CLEANUP_DIR}")
        remote = "/tmp/actions-runner-disk-cleanup"
        self.agent_exec(node, vmid, ["mkdir", "-p", remote], timeout=60)
        for path in sorted(DISK_CLEANUP_DIR.iterdir()):
            if not path.is_file():
                continue
            self.agent_write(node, vmid, f"{remote}/{path.name}", path.read_text(encoding="utf-8"))
        log(f"wrote disk-cleanup assets to VM {vmid} {remote}")

    def setup_guest(self, node: str, vmid: int, name: str) -> None:
        if not GUEST_SETUP.is_file():
            fatal(f"missing {GUEST_SETUP}")
        log(f"minting GitHub registration token for {name}")
        token = self.gh.registration_token(self.settings.github_kind, self.settings.github_target)
        env_text = (
            f"RUNNER_URL={self.settings.github_url}\n"
            f"RUNNER_TOKEN={token}\n"
            f"RUNNER_NAME={name}\n"
            f"RUNNER_LABELS={','.join(self.settings.labels)}\n"
            f"RUNNER_USER={self.settings.ciuser}\n"
            f"RUNNER_DIR={self.settings.runner_dir}\n"
            f"RUNNER_TARBALL_URL={self.settings.runner_tarball_url}\n"
        )
        log(f"writing guest-setup files to VM {vmid} / {node} via qemu-guest-agent")
        self.agent_write(node, vmid, "/tmp/proxmox-runner-fleet.env", env_text)
        self.agent_write(node, vmid, "/tmp/proxmox-runner-fleet.sh", GUEST_SETUP.read_text(encoding="utf-8"))
        self.write_disk_cleanup_assets(node, vmid)
        log(
            f"running guest-setup on {name} (cloud-init check, post-gen, runner download). "
            "This can take several minutes; guest stdout is printed when exec finishes."
        )
        code, out, err = self.agent_exec(
            node,
            vmid,
            ["bash", "/tmp/proxmox-runner-fleet.sh"],
            timeout=1800,
        )
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print(err.rstrip(), file=sys.stderr)
        if code != 0:
            fatal(f"guest setup failed on {name} (VM {vmid}, node {node}) with exit {code}")
        log(f"guest-setup finished on {name}; removing temp files")
        self.agent_exec(
            node,
            vmid,
            [
                "rm",
                "-rf",
                "/tmp/proxmox-runner-fleet.env",
                "/tmp/proxmox-runner-fleet.sh",
                "/tmp/actions-runner-disk-cleanup",
            ],
        )

    def plan(self, count: int, nodes: list[str]) -> list[PlannedVM]:
        assignment = assign_nodes(nodes, count)
        used = self.cluster_vmids() | self._used_vmids
        self._used_vmids = used
        planned: list[PlannedVM] = []
        for index, node in enumerate(assignment, start=1):
            name = runner_name(self.settings.name_prefix, index)
            existing = self.find_vm(name)
            item = PlannedVM(name=name, node=node, index=index)
            if existing:
                item.existed = True
                item.vmid = int(existing["vmid"])
                item.node = existing["node"]
                used.add(item.vmid)
                log(f"reusing existing VM {item.vmid} ({name}) on {item.node}")
            else:
                item.vmid = self.allocate_vmid(used)
                log(f"planning {name} on {node} as VMID {item.vmid}")
            planned.append(item)
        return planned

    def boot_planned_runners(self, planned: list[PlannedVM]) -> None:
        """Phase C start/wait: Cloud-Init + start + qemu-guest-agent, in parallel."""
        log(
            f"Phase C: Cloud-Init, start, and wait for qemu-guest-agent "
            f"({len(planned)} VM(s), parallel)"
        )

        def boot_one(item: PlannedVM) -> None:
            assert item.vmid is not None
            self.apply_cloudinit(item.node, item.vmid, item.index)
            self.start_vm(item.node, item.vmid)
            self.wait_agent(item.node, item.vmid)

        self._run_parallel(planned, boot_one, "start/wait-agent")

    def register_planned_runners(
        self,
        planned: list[PlannedVM],
        registered: dict[str, Any],
    ) -> None:
        """Phase C register: guest-setup stays sequential (GitHub token + long exec)."""
        for item in planned:
            assert item.vmid is not None
            already = registered.get(item.name)
            if already and already.get("status") == "online":
                log(f"{item.name} is already registered and online; running guest setup only if needed")
            self.setup_guest(item.node, item.vmid, item.name)
            log(f"{item.name} is ready on {item.node} (VMID {item.vmid})")

    def deploy(self, count: int) -> None:
        nodes = self.online_nodes()
        log(f"online target nodes: {', '.join(nodes)}")
        template = self.find_template()
        storage = self.template_storage(template)
        shared = self.storage_is_shared(storage)
        log(
            f"template {template['vmid']} ({template.get('name')}) on {template['node']}; "
            f"storage {storage} shared={shared}"
        )
        for node in nodes:
            self.require_storage_on_node(node, storage)

        version, tarball = self.gh.latest_runner_tarball(self.settings.runner_version)
        self.settings.runner_tarball_url = tarball
        log(f"actions/runner linux-x64 {version}: {tarball}")

        self._used_vmids = self.cluster_vmids()
        templates_by_node = self.place_templates(nodes, storage)

        planned = self.plan(count, nodes)
        registered = {
            row.get("name"): row
            for row in self.gh.list_runners(self.settings.github_kind, self.settings.github_target)
        }

        # Phase B must finish all clones before Phase C guest-setup.
        self.clone_planned_runners(planned, templates_by_node, storage)
        self.boot_planned_runners(planned)
        self.register_planned_runners(planned, registered)
        self.print_status(planned)

    def destroy(self, count: int | None, remove_github: bool) -> None:
        nodes = self.online_nodes()
        if count:
            names = {runner_name(self.settings.name_prefix, i) for i in range(1, count + 1)}
        else:
            names = None
        victims = []
        for row in self.resources("vm"):
            name = row.get("name") or ""
            if row.get("template"):
                continue
            if names is not None and name not in names:
                continue
            if names is None and not name.startswith(self.settings.name_prefix + "-"):
                continue
            victims.append(row)
        if not victims:
            log("no matching runner VMs to destroy")
        for row in victims:
            node, vmid, name = row["node"], int(row["vmid"]), row.get("name")
            log(f"destroying {name} (VMID {vmid}) on {node}")
            try:
                status = self.pve.call("GET", f"nodes/{node}/qemu/{vmid}/status/current") or {}
                if status.get("status") == "running":
                    self.pve.post_task(f"nodes/{node}/qemu/{vmid}/status/stop", {}, 300)
            except RuntimeError as exc:
                log(f"stop {vmid}: {exc}")
            self.pve.call(
                "DELETE",
                f"nodes/{node}/qemu/{vmid}",
                {"purge": 1, "destroy-unreferenced-disks": 1},
            )
        if remove_github:
            for runner in self.gh.list_runners(self.settings.github_kind, self.settings.github_target):
                name = runner.get("name") or ""
                if names is not None and name not in names:
                    continue
                if names is None and not name.startswith(self.settings.name_prefix + "-"):
                    continue
                log(f"removing GitHub runner {name} (id {runner['id']})")
                self.gh.delete_runner(self.settings.github_kind, self.settings.github_target, int(runner["id"]))
        log(f"online nodes (unchanged): {', '.join(nodes)}")

    def print_status(self, planned: list[PlannedVM] | None = None) -> None:
        runners = {
            row.get("name"): row
            for row in self.gh.list_runners(self.settings.github_kind, self.settings.github_target)
        }
        rows = []
        if planned is None:
            for row in self.resources("vm"):
                name = row.get("name") or ""
                if row.get("template"):
                    continue
                if name.startswith(self.settings.name_prefix + "-"):
                    rows.append(row)
        else:
            for item in planned:
                found = self.find_vm(item.name)
                if found:
                    rows.append(found)
        print("name\tvmid\tnode\tvm\tgithub")
        for row in sorted(rows, key=lambda item: item.get("name") or ""):
            gh = runners.get(row.get("name"), {})
            print(
                f"{row.get('name')}\t{row.get('vmid')}\t{row.get('node')}\t"
                f"{row.get('status')}\t{gh.get('status', '-')}"
            )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="fleet.toml or fleet.json (gitignored copy of the example)")
    parser.add_argument("--proxmox-url")
    parser.add_argument("--proxmox-username")
    parser.add_argument("--template-name")
    parser.add_argument("--template-vmid", type=int)
    parser.add_argument("--storage")
    parser.add_argument("--nodes", help="comma-separated node allowlist (default: all online nodes)")
    parser.add_argument("--name-prefix")
    parser.add_argument("--github-url")
    parser.add_argument("--labels", help="comma-separated runner labels")
    parser.add_argument("--insecure", action="store_true", help="skip Proxmox TLS verification")
    parser.add_argument("--ssh-public-key-file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    deploy = sub.add_parser("deploy", help="clone, boot, post-gen, and register runners")
    add_common_args(deploy)
    deploy.add_argument("--count", type=int, help="total runner VMs (placed round-robin)")
    deploy.add_argument("--per-node", type=int, help="runners per online target node")
    deploy.add_argument(
        "--recreate-templates",
        action="store_true",
        help=(
            "delete per-node {template}-{node} replica templates and copy the "
            "source Packer template to each node again (default: reuse replicas)"
        ),
    )

    destroy = sub.add_parser("destroy", help="stop and delete fleet VMs")
    add_common_args(destroy)
    destroy.add_argument("--count", type=int, help="only destroy gh-runner-01..N")
    destroy.add_argument("--yes", action="store_true", help="do not prompt")
    destroy.add_argument("--keep-github", action="store_true", help="do not delete GitHub runner registrations")

    status = sub.add_parser("status", help="list fleet VMs and GitHub runner state")
    add_common_args(status)

    nodes = sub.add_parser("nodes", help="list online Proxmox nodes")
    add_common_args(nodes)
    return parser


def resolve_count(args: argparse.Namespace, node_count: int) -> int:
    if getattr(args, "count", None) and getattr(args, "per_node", None):
        expected = args.per_node * node_count
        if args.count != expected:
            fatal(f"--count {args.count} does not match --per-node {args.per_node} x {node_count} nodes ({expected})")
        return args.count
    if getattr(args, "count", None):
        return args.count
    if getattr(args, "per_node", None):
        return args.per_node * node_count
    fatal("deploy requires --count or --per-node")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    settings = Settings.from_sources(config, args)
    fleet = Fleet(settings)

    if args.command == "nodes":
        print("\n".join(fleet.online_nodes()))
        return 0
    if args.command == "status":
        if not settings.github_url:
            fatal("GitHub url is required (repo or org)")
        fleet.print_status()
        return 0
    if args.command == "destroy":
        if not args.yes:
            fatal("refusing to destroy without --yes")
        if not args.keep_github and not settings.github_url:
            fatal("GitHub url is required unless --keep-github is set")
        fleet.destroy(args.count, remove_github=not args.keep_github)
        return 0
    if args.command == "deploy":
        if not settings.github_url:
            fatal("GitHub url is required (repo or org)")
        nodes = fleet.online_nodes()
        count = resolve_count(args, len(nodes))
        fleet.deploy(count)
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
