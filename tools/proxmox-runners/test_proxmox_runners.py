#!/usr/bin/env python3
import importlib.util
import sys
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


if __name__ == "__main__":
    unittest.main()
