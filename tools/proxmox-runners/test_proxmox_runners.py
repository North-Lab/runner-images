#!/usr/bin/env python3
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "proxmox_runners", Path(__file__).with_name("proxmox-runners.py")
)
pr = importlib.util.module_from_spec(_SPEC)
sys.modules["proxmox_runners"] = pr
assert _SPEC.loader is not None
_SPEC.loader.exec_module(pr)

assign_nodes = pr.assign_nodes
default_labels = pr.default_labels
parse_github_target = pr.parse_github_target
parse_storage_id = pr.parse_storage_id
render_ipconfig = pr.render_ipconfig
resolve_count = pr.resolve_count
runner_name = pr.runner_name
replica_template_name = pr.replica_template_name
replica_delete_refusal = pr.replica_delete_refusal
trim_error = pr.trim_error
http_status = pr.http_status
is_agent_forbidden = pr.is_agent_forbidden
guest_agent_acl_hint = pr.guest_agent_acl_hint
ProxmoxAPIError = pr.ProxmoxAPIError
next_unused_vmid = pr.next_unused_vmid
is_vmid_conflict = pr.is_vmid_conflict
form_encode = pr.form_encode
proxmox_not_implemented = pr.proxmox_not_implemented
ping_succeeded = pr.ping_succeeded
DISK_CLEANUP_DIR = pr.DISK_CLEANUP_DIR
PlannedVM = pr.PlannedVM


class ParseGitHubTargetTests(unittest.TestCase):
    def test_repo_url(self):
        self.assertEqual(
            parse_github_target("https://github.com/JoeNorth/runner-images"),
            ("repo", "JoeNorth/runner-images"),
        )

    def test_org_url(self):
        self.assertEqual(parse_github_target("https://github.com/JoeNorth"), ("org", "JoeNorth"))

    def test_repo_slug(self):
        self.assertEqual(parse_github_target("JoeNorth/runner-images"), ("repo", "JoeNorth/runner-images"))

    def test_trailing_slash(self):
        self.assertEqual(parse_github_target("https://github.com/acme/app/"), ("repo", "acme/app"))


class PlacementTests(unittest.TestCase):
    def test_round_robin(self):
        self.assertEqual(
            assign_nodes(["pve1", "pve2", "pve3"], 6),
            ["pve1", "pve2", "pve3", "pve1", "pve2", "pve3"],
        )

    def test_names(self):
        self.assertEqual(runner_name("gh-runner", 1), "gh-runner-01")
        self.assertEqual(runner_name("gh-runner", 12), "gh-runner-12")

    def test_count_and_per_node_agree(self):
        args = type("A", (), {"count": 6, "per_node": 2})()
        self.assertEqual(resolve_count(args, 3), 6)

    def test_per_node_only(self):
        args = type("A", (), {"count": None, "per_node": 2})()
        self.assertEqual(resolve_count(args, 3), 6)

    def test_skip_used_vmids(self):
        self.assertEqual(next_unused_vmid({701}, 701), 702)
        self.assertEqual(next_unused_vmid({701, 702}, 701), 703)
        self.assertEqual(next_unused_vmid({100}, 701), 701)
        self.assertTrue(is_vmid_conflict(RuntimeError("close (rename) atomic file '.../701.conf' failed: File exists")))
        self.assertFalse(is_vmid_conflict(RuntimeError("guest agent is not running")))


class MiscTests(unittest.TestCase):
    def test_ipconfig_placeholder(self):
        self.assertEqual(
            render_ipconfig("ip=192.168.1.{n}/24,gw=192.168.1.1", 1, 50),
            "ip=192.168.1.50/24,gw=192.168.1.1",
        )
        self.assertEqual(render_ipconfig("ip=dhcp", 3, 50), "ip=dhcp")

    def test_storage_id(self):
        self.assertEqual(parse_storage_id("local-lvm:vm-9000-disk-0,size=75G"), "local-lvm")

    def test_default_labels(self):
        self.assertIn("ubuntu-26.04", default_labels(["home-lab"]))
        self.assertIn("home-lab", default_labels(["home-lab"]))

    def test_trim_error(self):
        self.assertEqual(trim_error(RuntimeError("guest agent is not running")), "guest agent is not running")
        long = RuntimeError("x" * 200)
        trimmed = trim_error(long, limit=40)
        self.assertEqual(len(trimmed), 40)
        self.assertTrue(trimmed.endswith("..."))
        self.assertEqual(trim_error(TimeoutError("")), "TimeoutError")

    def test_agent_forbidden(self):
        forbidden = ProxmoxAPIError("POST ... failed: HTTP 403: Permission denied", status=403)
        self.assertEqual(http_status(forbidden), 403)
        self.assertTrue(is_agent_forbidden(forbidden))
        self.assertTrue(is_agent_forbidden(RuntimeError("HTTP 401: authentication failed")))
        self.assertTrue(is_agent_forbidden(RuntimeError("Permission check failed")))
        self.assertFalse(is_agent_forbidden(RuntimeError("guest agent is not running")))
        self.assertEqual(http_status(ProxmoxAPIError("GET ... HTTP 501: not implemented", status=501)), 501)
        hint = guest_agent_acl_hint("packer@pve!imagegen")
        self.assertIn("pveum role add RunnerFleet", hint)
        self.assertIn("VM.GuestAgent.Audit", hint)
        self.assertIn("packer@pve!imagegen", hint)

    def test_form_encode_command_array(self):
        encoded = form_encode({"command": ["bash", "/tmp/proxmox-runner-fleet.sh"]}).decode()
        self.assertEqual(encoded, "command=bash&command=%2Ftmp%2Fproxmox-runner-fleet.sh")
        self.assertNotIn("[", encoded)

    def test_ping_not_implemented_is_failure(self):
        get_body = {
            "data": None,
            "message": "Method 'GET /nodes/pve1/qemu/701/agent/ping' not implemented",
        }
        self.assertIsNotNone(proxmox_not_implemented(get_body))
        self.assertFalse(ping_succeeded(None))
        self.assertFalse(ping_succeeded(get_body))
        self.assertTrue(ping_succeeded({"result": {}}))


class DiskCleanupAssetTests(unittest.TestCase):
    def test_disk_cleanup_dir_is_shipped(self):
        self.assertTrue(DISK_CLEANUP_DIR.is_dir())
        self.assertTrue((DISK_CLEANUP_DIR / "install.sh").is_file())
        self.assertTrue((DISK_CLEANUP_DIR / "actions-runner-disk-cleanup.timer").is_file())
        source = Path(__file__).with_name("proxmox-runners.py").read_text(encoding="utf-8")
        self.assertIn("write_disk_cleanup_assets", source)
        self.assertIn("/tmp/actions-runner-disk-cleanup", source)


class FakeGitHub:
    def latest_runner_tarball(self, version):
        return "2.328.0", "https://example.test/actions-runner-linux-x64-2.328.0.tar.gz"

    def list_runners(self, kind, target):
        return []

    def registration_token(self, kind, target):
        return "regtok"


class FakePVE:
    def __init__(self, *, shared=False, nodes=None, vms=None, next_id=8000, clone_sleep=0.0):
        self.shared = shared
        self.nodes = list(nodes or ["pve1", "pve2", "pve3"])
        self.vms = [dict(row) for row in (vms or [])]
        self.next_id = next_id
        self.tasks = []
        self.calls = []
        self.clone_sleep = clone_sleep
        self.in_flight_runner_clones = 0
        self.max_in_flight_runner_clones = 0
        self._lock = threading.Lock()

    def call(self, method, path, params=None, timeout=120):
        self.calls.append((method, path, params))
        if method == "GET" and path == "nodes":
            return [{"node": name, "status": "online"} for name in self.nodes]
        if method == "GET" and path == "cluster/resources":
            rtype = (params or {}).get("type")
            rows = list(self.vms)
            if rtype == "vm":
                return rows
            if rtype == "storage":
                return [
                    {
                        "storage": "local-lvm",
                        "id": f"storage/{self.nodes[0]}/local-lvm",
                        "shared": 1 if self.shared else 0,
                    }
                ]
            return rows
        if method == "GET" and path == "cluster/nextid":
            with self._lock:
                value = self.next_id
                self.next_id += 1
                return value
        if method == "GET" and path == "storage":
            return [{"storage": "local-lvm", "shared": 1 if self.shared else 0}]
        if method == "GET" and path.endswith("/storage") and path.startswith("nodes/"):
            return [{"storage": "local-lvm", "active": 1, "content": "images,iso"}]
        if method == "GET" and "/status/current" in path:
            vmid = int(path.split("/")[3])
            for row in self.vms:
                if int(row["vmid"]) == vmid:
                    return {"status": row.get("status") or "stopped"}
            return {"status": "stopped"}
        if method == "PUT" and path.endswith("/config"):
            return None
        if method == "POST" and path.endswith("/agent/ping"):
            return {"result": {}}
        if method == "DELETE" and "/qemu/" in path:
            vmid = int(path.split("/")[3])
            with self._lock:
                self.vms = [row for row in self.vms if int(row["vmid"]) != vmid]
            return None
        return None

    def post_task(self, path, params, timeout):
        rec = {"path": path, "params": dict(params or {})}
        with self._lock:
            self.tasks.append(rec)
        parts = path.split("/")
        if path.endswith("/clone"):
            source_node = parts[1]
            newid = int(params["newid"])
            name = params.get("name") or ""
            target = params.get("target") or source_node
            is_runner = name.startswith("gh-runner-")
            if is_runner:
                with self._lock:
                    self.in_flight_runner_clones += 1
                    self.max_in_flight_runner_clones = max(
                        self.max_in_flight_runner_clones, self.in_flight_runner_clones
                    )
                if self.clone_sleep:
                    time.sleep(self.clone_sleep)
            with self._lock:
                self.vms.append(
                    {
                        "vmid": newid,
                        "name": name,
                        "node": target,
                        "template": 0,
                        "type": "vm",
                        "status": "stopped",
                    }
                )
                if is_runner:
                    self.in_flight_runner_clones -= 1
            return f"UPID:{source_node}:clone:{newid}"
        if "/migrate" in path:
            vmid = int(parts[3])
            target = params["target"]
            with self._lock:
                for row in self.vms:
                    if int(row["vmid"]) == vmid:
                        row["node"] = target
            return f"UPID:{parts[1]}:migrate:{vmid}"
        if path.endswith("/template"):
            vmid = int(parts[3])
            with self._lock:
                for row in self.vms:
                    if int(row["vmid"]) == vmid:
                        row["template"] = 1
            return f"UPID:{parts[1]}:template:{vmid}"
        if path.endswith("/status/start"):
            vmid = int(parts[3])
            with self._lock:
                for row in self.vms:
                    if int(row["vmid"]) == vmid:
                        row["status"] = "running"
            return f"UPID:{parts[1]}:start:{vmid}"
        return "UPID:fake:ok"

    def wait_task(self, upid, timeout):
        return None


def make_settings(**overrides):
    values = dict(
        proxmox_url="https://pve.example:8006/api2/json",
        proxmox_username="packer@pve!imagegen",
        proxmox_token="secret",
        proxmox_password="",
        insecure=True,
        node_allowlist=["pve1", "pve2", "pve3"],
        template_name="ubuntu-2604-runner",
        template_vmid=None,
        storage="local-lvm",
        recreate_templates=False,
        task_timeout=30,
        name_prefix="gh-runner",
        vmid_start=701,
        cores=None,
        memory_mb=None,
        ciuser="runner",
        ssh_public_key="",
        ipconfig="ip=dhcp",
        ip_start=50,
        nameserver="",
        searchdomain="",
        github_url="https://github.com/example/repo",
        github_token="ghp_test",
        labels=["self-hosted", "linux", "x64", "ubuntu-26.04"],
        runner_version="",
        runner_dir="/opt/actions-runner",
        github_kind="repo",
        github_target="example/repo",
    )
    values.update(overrides)
    return pr.Settings(**values)


def make_fleet(pve, **overrides):
    fleet = pr.Fleet(make_settings(**overrides))
    fleet.pve = pve
    fleet._gh = FakeGitHub()
    return fleet


def packer_template(node="pve1", vmid=9000):
    return {
        "vmid": vmid,
        "name": "ubuntu-2604-runner",
        "node": node,
        "template": 1,
        "type": "vm",
        "status": "stopped",
    }


def replica_on(node, vmid):
    return {
        "vmid": vmid,
        "name": replica_template_name("ubuntu-2604-runner", node),
        "node": node,
        "template": 1,
        "type": "vm",
        "status": "stopped",
    }


def task_ops(pve):
    ops = []
    for rec in pve.tasks:
        path = rec["path"]
        params = rec["params"]
        if path.endswith("/clone"):
            ops.append(("clone", path, params))
        elif "/migrate" in path:
            ops.append(("migrate", path, params))
        elif path.endswith("/template"):
            ops.append(("template", path, params))
        elif path.endswith("/status/start"):
            ops.append(("start", path, params))
    return ops


class ReplicaNameTests(unittest.TestCase):
    def test_replica_name(self):
        self.assertEqual(replica_template_name("ubuntu-2604-runner", "pve2"), "ubuntu-2604-runner-pve2")


class TemplatePlacementTests(unittest.TestCase):
    def test_reuses_existing_node_replica(self):
        pve = FakePVE(
            vms=[
                packer_template("pve1", 9000),
                replica_on("pve2", 9001),
            ]
        )
        fleet = make_fleet(pve)
        placed = fleet.place_templates(["pve1", "pve2", "pve3"], "local-lvm")
        self.assertEqual(placed["pve1"]["vmid"], 9000)
        self.assertEqual(placed["pve2"]["vmid"], 9001)
        self.assertEqual(placed["pve2"]["name"], "ubuntu-2604-runner-pve2")
        self.assertEqual(placed["pve3"]["node"], "pve3")
        self.assertEqual(placed["pve3"]["name"], "ubuntu-2604-runner-pve3")
        clones = [op for op in task_ops(pve) if op[0] == "clone"]
        migrates = [op for op in task_ops(pve) if op[0] == "migrate"]
        self.assertEqual(len(clones), 1)
        self.assertEqual(clones[0][2]["name"], "ubuntu-2604-runner-pve3")
        self.assertEqual(len(migrates), 1)
        self.assertEqual(migrates[0][2]["target"], "pve3")
        self.assertNotIn("gh-runner", clones[0][2]["name"])

    def test_shared_storage_skips_replicas(self):
        pve = FakePVE(shared=True, vms=[packer_template("pve1", 9000)])
        fleet = make_fleet(pve)
        placed = fleet.place_templates(["pve1", "pve2", "pve3"], "local-lvm")
        self.assertEqual({placed[n]["vmid"] for n in placed}, {9000})
        self.assertEqual(task_ops(pve), [])

    def test_converts_leftover_replica_vm_to_template(self):
        leftover = replica_on("pve2", 9001)
        leftover["template"] = 0
        pve = FakePVE(vms=[packer_template("pve1", 9000), leftover])
        fleet = make_fleet(pve)
        placed = fleet.place_templates(["pve1", "pve2"], "local-lvm")
        self.assertEqual(placed["pve2"]["vmid"], 9001)
        templates = [op for op in task_ops(pve) if op[0] == "template"]
        self.assertEqual(len(templates), 1)
        self.assertIn("/qemu/9001/template", templates[0][1])
        self.assertEqual([op for op in task_ops(pve) if op[0] == "migrate"], [])


class ParallelLocalCloneTests(unittest.TestCase):
    def test_full_deploy_copies_template_once_then_clones_locally(self):
        pve = FakePVE(vms=[packer_template("pve1", 9000)], clone_sleep=0.12)
        fleet = make_fleet(pve)
        fleet.setup_guest = lambda node, vmid, name: None
        fleet.deploy(6)

        clones = [op for op in task_ops(pve) if op[0] == "clone"]
        migrates = [op for op in task_ops(pve) if op[0] == "migrate"]
        replica_clones = [op for op in clones if op[2]["name"].startswith("ubuntu-2604-runner-")]
        runner_clones = [op for op in clones if op[2]["name"].startswith("gh-runner-")]
        self.assertEqual({op[2]["name"] for op in replica_clones}, {"ubuntu-2604-runner-pve2", "ubuntu-2604-runner-pve3"})
        self.assertEqual(len(runner_clones), 6)
        self.assertEqual(len(migrates), 2)
        self.assertEqual({op[2]["target"] for op in migrates}, {"pve2", "pve3"})
        migrated_vmids = {int(op[1].split("/")[3]) for op in migrates}
        replica_vmids = {int(op[2]["newid"]) for op in replica_clones}
        self.assertEqual(migrated_vmids, replica_vmids)
        runner_vmids = {int(op[2]["newid"]) for op in runner_clones}
        self.assertFalse(migrated_vmids & runner_vmids, "a runner VMID must never appear in migrate")
        for _kind, path, params in runner_clones:
            self.assertNotIn("target", params)
            node = {"gh-runner-01": "pve1", "gh-runner-02": "pve2", "gh-runner-03": "pve3",
                    "gh-runner-04": "pve1", "gh-runner-05": "pve2", "gh-runner-06": "pve3"}[params["name"]]
            self.assertTrue(path.startswith(f"nodes/{node}/qemu/"), path)
        self.assertGreater(pve.max_in_flight_runner_clones, 1)

    def test_clones_are_local_to_node_not_migrated(self):
        pve = FakePVE(
            vms=[
                packer_template("pve1", 9000),
                replica_on("pve2", 9001),
                replica_on("pve3", 9002),
            ],
            clone_sleep=0.15,
        )
        fleet = make_fleet(pve)
        setup_order = []

        def setup(node, vmid, name):
            setup_order.append(("setup", name, node, vmid))

        fleet.setup_guest = setup
        fleet.deploy(6)

        clones = [op for op in task_ops(pve) if op[0] == "clone"]
        migrates = [op for op in task_ops(pve) if op[0] == "migrate"]
        runner_clones = [op for op in clones if op[2]["name"].startswith("gh-runner-")]
        self.assertEqual(len(runner_clones), 6)
        self.assertEqual(migrates, [], "runner VMs must not be migrated; only template replicas move")
        expected_node = {
            "gh-runner-01": "pve1",
            "gh-runner-02": "pve2",
            "gh-runner-03": "pve3",
            "gh-runner-04": "pve1",
            "gh-runner-05": "pve2",
            "gh-runner-06": "pve3",
        }
        expected_template = {"pve1": 9000, "pve2": 9001, "pve3": 9002}
        for _kind, path, params in runner_clones:
            name = params["name"]
            node = expected_node[name]
            self.assertNotIn("target", params, f"{name} must clone locally, not via target=")
            self.assertTrue(path.startswith(f"nodes/{node}/qemu/"))
            template_vmid = int(path.split("/")[3])
            self.assertEqual(template_vmid, expected_template[node], f"{name} must clone from {node}'s replica")
        self.assertGreater(
            pve.max_in_flight_runner_clones,
            1,
            "runner clones must overlap in time (parallel), not run one-by-one",
        )
        self.assertEqual([row[0] for row in setup_order], ["setup"] * 6)
        self.assertEqual([row[1] for row in setup_order], [f"gh-runner-{i:02d}" for i in range(1, 7)])

    def test_phase_b_finishes_before_guest_setup(self):
        pve = FakePVE(
            vms=[
                packer_template("pve1", 9000),
                replica_on("pve2", 9001),
                replica_on("pve3", 9002),
            ]
        )
        fleet = make_fleet(pve)
        order = []
        orig_clone = fleet.clone_runner

        def tracking_clone(template, planned, storage):
            order.append(("clone", planned.name))
            return orig_clone(template, planned, storage)

        def tracking_setup(node, vmid, name):
            order.append(("setup", name))

        fleet.clone_runner = tracking_clone
        fleet.setup_guest = tracking_setup
        fleet.deploy(3)
        kinds = [step[0] for step in order]
        last_clone = max(i for i, kind in enumerate(kinds) if kind == "clone")
        first_setup = min(i for i, kind in enumerate(kinds) if kind == "setup")
        self.assertLess(last_clone, first_setup)

    def test_reuse_by_name_skips_clone(self):
        existing = {
            "vmid": 701,
            "name": "gh-runner-01",
            "node": "pve1",
            "template": 0,
            "type": "vm",
            "status": "running",
        }
        pve = FakePVE(vms=[packer_template("pve1", 9000), existing])
        fleet = make_fleet(pve, node_allowlist=["pve1"])
        fleet.setup_guest = lambda node, vmid, name: None
        fleet.deploy(1)
        runner_clones = [
            op for op in task_ops(pve) if op[0] == "clone" and op[2]["name"].startswith("gh-runner-")
        ]
        self.assertEqual(runner_clones, [])

    def test_vmid_conflict_retries_without_migrate(self):
        pve = FakePVE(vms=[packer_template("pve1", 9000)])
        real_post = pve.post_task

        def flaky_post(path, params, timeout):
            if path.endswith("/clone") and params.get("name") == "gh-runner-01" and params.get("newid") == 701:
                raise RuntimeError("close (rename) atomic file '.../701.conf' failed: File exists")
            return real_post(path, params, timeout)

        pve.post_task = flaky_post
        fleet = make_fleet(pve, node_allowlist=["pve1"])
        planned = PlannedVM(name="gh-runner-01", node="pve1", index=1, vmid=701)
        fleet._used_vmids = {701}
        vmid = fleet.clone_runner(packer_template("pve1", 9000), planned, "local-lvm")
        self.assertEqual(vmid, 702)
        self.assertEqual(planned.vmid, 702)
        self.assertEqual([op for op in task_ops(pve) if op[0] == "migrate"], [])

    def test_allocate_vmid_parallel_unique(self):
        fleet = make_fleet(FakePVE(vms=[packer_template()]))
        fleet._used_vmids = set()
        ids = []
        lock = threading.Lock()

        def alloc():
            vmid = fleet.allocate_vmid(fleet._used_vmids)
            with lock:
                ids.append(vmid)

        threads = [threading.Thread(target=alloc) for _ in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(ids), 24)
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(sorted(ids), list(range(701, 725)))

    def test_clone_runner_rejects_cross_node_local_template(self):
        fleet = make_fleet(FakePVE(vms=[packer_template("pve1", 9000)]))
        planned = PlannedVM(name="gh-runner-01", node="pve2", index=1, vmid=701)
        with self.assertRaises(SystemExit):
            fleet.clone_runner(packer_template("pve1", 9000), planned, "local-lvm")

    def test_shared_storage_clone_uses_target_not_migrate(self):
        pve = FakePVE(shared=True, vms=[packer_template("pve1", 9000)])
        fleet = make_fleet(pve)
        fleet.setup_guest = lambda node, vmid, name: None
        fleet.deploy(3)
        runner_clones = [
            op for op in task_ops(pve) if op[0] == "clone" and op[2]["name"].startswith("gh-runner-")
        ]
        self.assertEqual(len(runner_clones), 3)
        self.assertEqual([op for op in task_ops(pve) if op[0] == "migrate"], [])
        by_name = {op[2]["name"]: op[2] for op in runner_clones}
        self.assertNotIn("target", by_name["gh-runner-01"])
        self.assertEqual(by_name["gh-runner-02"]["target"], "pve2")
        self.assertEqual(by_name["gh-runner-03"]["target"], "pve3")

    def test_docs_describe_template_copy_then_parallel_clone(self):
        docs = Path(__file__).resolve().parents[2] / "docs" / "create-image-and-proxmox-resources.md"
        text = docs.read_text(encoding="utf-8")
        self.assertIn("Phase A — template placement", text)
        self.assertIn("Phase B — parallel clones", text)
        self.assertIn("does **not** clone a runner on the template node and migrate that runner", text)
        self.assertIn("--recreate-templates", text)
        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        self.assertIn("full-clones runner VMs from that node's replica in parallel", readme)
        self.assertIn("--recreate-templates", readme)


class RecreateTemplatesTests(unittest.TestCase):
    def test_refusal_rules(self):
        source = packer_template("pve1", 9000)
        self.assertIn(
            "source Packer template",
            replica_delete_refusal(
                source, "pve1", template_name="ubuntu-2604-runner", name_prefix="gh-runner"
            ),
        )
        self.assertIn(
            "looks like a runner",
            replica_delete_refusal(
                {"vmid": 701, "name": "gh-runner-01", "template": 1, "node": "pve2"},
                "pve2",
                template_name="ubuntu-2604-runner",
                name_prefix="gh-runner",
            ),
        )
        leftover = replica_on("pve2", 9001)
        leftover["template"] = 0
        self.assertIn(
            "not a template",
            replica_delete_refusal(
                leftover, "pve2", template_name="ubuntu-2604-runner", name_prefix="gh-runner"
            ),
        )
        self.assertIsNone(
            replica_delete_refusal(
                replica_on("pve2", 9001),
                "pve2",
                template_name="ubuntu-2604-runner",
                name_prefix="gh-runner",
                source=source,
            )
        )
        same_as_source = dict(source)
        same_as_source["name"] = "ubuntu-2604-runner-pve1"
        self.assertIn(
            "source Packer template",
            replica_delete_refusal(
                same_as_source,
                "pve1",
                template_name="ubuntu-2604-runner",
                name_prefix="gh-runner",
                source=source,
            ),
        )

    def test_recreate_deletes_replicas_not_source_or_runners(self):
        runner = {
            "vmid": 701,
            "name": "gh-runner-01",
            "node": "pve1",
            "template": 0,
            "type": "vm",
            "status": "running",
        }
        pve = FakePVE(
            vms=[
                packer_template("pve1", 9000),
                replica_on("pve2", 9001),
                replica_on("pve3", 9002),
                runner,
            ]
        )
        fleet = make_fleet(pve, recreate_templates=True)
        fleet.setup_guest = lambda node, vmid, name: None
        fleet.deploy(1)

        deletes = [path for method, path, _params in pve.calls if method == "DELETE"]
        self.assertEqual(
            set(deletes),
            {"nodes/pve2/qemu/9001", "nodes/pve3/qemu/9002"},
        )
        self.assertTrue(any(int(row["vmid"]) == 9000 for row in pve.vms))
        self.assertTrue(any(row.get("name") == "gh-runner-01" for row in pve.vms))
        replica_clones = [
            op for op in task_ops(pve) if op[0] == "clone" and op[2]["name"].startswith("ubuntu-2604-runner-")
        ]
        self.assertEqual(
            {op[2]["name"] for op in replica_clones},
            {"ubuntu-2604-runner-pve2", "ubuntu-2604-runner-pve3"},
        )
        runner_clones = [
            op for op in task_ops(pve) if op[0] == "clone" and op[2]["name"].startswith("gh-runner-")
        ]
        self.assertEqual(runner_clones, [])
        names = {row.get("name") for row in pve.vms}
        self.assertIn("ubuntu-2604-runner", names)
        self.assertIn("ubuntu-2604-runner-pve2", names)
        self.assertIn("ubuntu-2604-runner-pve3", names)
        self.assertNotIn(9001, {int(row["vmid"]) for row in pve.vms})
        self.assertNotIn(9002, {int(row["vmid"]) for row in pve.vms})

    def test_default_off_reuses_replicas(self):
        pve = FakePVE(vms=[packer_template("pve1", 9000), replica_on("pve2", 9001)])
        fleet = make_fleet(pve)
        self.assertFalse(fleet.settings.recreate_templates)
        placed = fleet.place_templates(["pve1", "pve2"], "local-lvm")
        self.assertEqual(placed["pve2"]["vmid"], 9001)
        self.assertEqual([path for method, path, _ in pve.calls if method == "DELETE"], [])

    def test_refuses_leftover_non_template(self):
        leftover = replica_on("pve2", 9001)
        leftover["template"] = 0
        pve = FakePVE(vms=[packer_template("pve1", 9000), leftover])
        fleet = make_fleet(pve, recreate_templates=True)
        with self.assertRaises(SystemExit):
            fleet.place_templates(["pve1", "pve2"], "local-lvm")
        self.assertTrue(any(int(row["vmid"]) == 9001 for row in pve.vms))
        self.assertTrue(any(int(row["vmid"]) == 9000 for row in pve.vms))

    def test_shared_storage_ignores_recreate(self):
        pve = FakePVE(shared=True, vms=[packer_template("pve1", 9000), replica_on("pve2", 9001)])
        fleet = make_fleet(pve, recreate_templates=True)
        placed = fleet.place_templates(["pve1", "pve2", "pve3"], "local-lvm")
        self.assertEqual({placed[n]["vmid"] for n in placed}, {9000})
        self.assertEqual([path for method, path, _ in pve.calls if method == "DELETE"], [])

    def test_cli_and_toml_enable_flag(self):
        parser = pr.build_parser()
        common = [
            "deploy",
            "--count",
            "1",
            "--proxmox-url",
            "https://pve.example:8006",
            "--proxmox-username",
            "u@pve!t",
        ]
        on = parser.parse_args([*common, "--recreate-templates"])
        self.assertTrue(on.recreate_templates)
        self.assertTrue(pr.Settings.from_sources({}, on).recreate_templates)
        off = parser.parse_args(common)
        self.assertFalse(off.recreate_templates)
        self.assertFalse(pr.Settings.from_sources({}, off).recreate_templates)
        self.assertTrue(
            pr.Settings.from_sources({"proxmox": {"recreate_templates": True}}, off).recreate_templates
        )


if __name__ == "__main__":
    unittest.main()
