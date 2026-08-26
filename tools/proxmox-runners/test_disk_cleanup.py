#!/usr/bin/env python3
"""Tests for the idle runner disk-cleanup script and guest-setup bundle."""

from __future__ import annotations

import os
import pwd
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent
CLEANUP_DIR = TOOL / "disk-cleanup"
SCRIPT = CLEANUP_DIR / "actions-runner-disk-cleanup"
GUEST_SETUP = TOOL / "guest-setup.sh"
PACKER = (
    TOOL.parent.parent
    / "images"
    / "ubuntu"
    / "templates-proxmox"
    / "build.ubuntu-26_04-proxmox.pkr.hcl"
)
DOCS = TOOL.parent.parent / "docs" / "create-image-and-proxmox-resources.md"


def extract_heredoc(text: str, token: str) -> str:
    marker = f"<<'{token}'\n"
    start = text.index(marker) + len(marker)
    end = text.index(f"{token}\n", start)
    return text[start:end]


def write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class BundleSyncTests(unittest.TestCase):
    def test_guest_setup_embeds_standalone_files(self):
        guest = GUEST_SETUP.read_text(encoding="utf-8")
        mapping = {
            "actions-runner-disk-cleanup": "ACTIONS_RUNNER_DISK_CLEANUP_SCRIPT",
            "actions-runner-disk-cleanup.service": "ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_SERVICE",
            "actions-runner-disk-cleanup.timer": "ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_TIMER",
            "actions-runner-disk-cleanup-pressure.service": "ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_PRESSURE_SERVICE",
            "actions-runner-disk-cleanup-pressure.timer": "ACTIONS_RUNNER_DISK_CLEANUP_ACTIONS_RUNNER_DISK_CLEANUP_PRESSURE_TIMER",
            "install.sh": "ACTIONS_RUNNER_DISK_CLEANUP_INSTALL_SH",
        }
        for name, token in mapping.items():
            standalone = (CLEANUP_DIR / name).read_text(encoding="utf-8")
            self.assertEqual(extract_heredoc(guest, token), standalone, name)

    def test_job_started_hook_still_present(self):
        guest = GUEST_SETUP.read_text(encoding="utf-8")
        self.assertIn("ACTIONS_RUNNER_HOOK_JOB_STARTED", guest)
        self.assertIn("actions-runner-chown-work", guest)
        self.assertIn("job-started.sh", guest)
        self.assertIn("install_runner_disk_cleanup", guest)

    def test_packer_installs_before_deprovision(self):
        hcl = PACKER.read_text(encoding="utf-8")
        cleanup_at = hcl.index("tools/proxmox-runners/disk-cleanup")
        install_at = hcl.index("START_TIMERS=0")
        deprovision_at = hcl.index("deprovision-proxmox.sh")
        self.assertLess(cleanup_at, deprovision_at)
        self.assertLess(install_at, deprovision_at)
        self.assertIn("actions-runner-disk-cleanup.timer", hcl)

    def test_docs_cover_timer_and_hook(self):
        docs = DOCS.read_text(encoding="utf-8")
        self.assertIn("Automatic disk cleanup", docs)
        self.assertIn("Runner.Worker", docs)
        self.assertIn("actions-runner-disk-cleanup.timer", docs)
        self.assertIn("ACTIONS_RUNNER_HOOK_JOB_STARTED", docs)
        self.assertIn("70%", docs)


class CleanupScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.runner = self.root / "opt" / "actions-runner"
        self.work = self.runner / "_work"
        self.diag = self.runner / "_diag"
        self.lock = self.root / "run" / "cleanup.lock"
        self.systemctl_log = self.root / "systemctl.log"
        self.docker_log = self.root / "docker.log"
        self.bin.mkdir()
        self.runner.mkdir(parents=True)
        (self.runner / "bin").mkdir()
        (self.runner / "bin" / "Runner.Listener").write_text("listener")
        (self.runner / ".runner").write_text('{"agentId":1}')
        (self.runner / "svc.sh").write_text("#!/bin/sh\n")
        (self.runner / ".service").write_text("actions.runner.example.svc")
        self.diag.mkdir()
        (self.diag / "Runner_old.log").write_text("diag")
        self.work.mkdir()
        (self.work / "some-repo").mkdir()
        (self.work / "some-repo" / "checkout.txt").write_text("leftover")
        (self.work / "_temp").mkdir()
        (self.work / "_temp" / "tmp.txt").write_text("tmp")
        (self.work / "_actions").mkdir()
        (self.work / "_tool").mkdir()
        (self.work / "_tool" / "keep.txt").write_text("cache")
        (self.work / "_update").mkdir()
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.bin}:{self.env.get('PATH', '')}"
        self.env["RUNNER_DIR"] = str(self.runner)
        self.env["RUNNER_USER"] = self.user
        self.env["LOCK_FILE"] = str(self.lock)
        self.env["DOCKER_DATA_ROOT"] = str(self.root / "var" / "lib" / "docker")
        (self.root / "var" / "lib" / "docker").mkdir(parents=True)
        self._write_pgrep(job_running=False)
        self._write_df(pct=40)
        self._write_systemctl(units=["actions.runner.example.gh-runner-01.service"])
        self._write_docker(reachable=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_pgrep(self, job_running: bool) -> None:
        write_exec(
            self.bin / "pgrep",
            "#!/bin/sh\nexit %d\n" % (0 if job_running else 1),
        )

    def _write_df(self, pct: int) -> None:
        write_exec(
            self.bin / "df",
            "\n".join(
                [
                    "#!/bin/sh",
                    'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"',
                    f'echo "/dev/sda1 100000 70000 30000 {pct}% /"',
                    "",
                ]
            ),
        )

    def _write_systemctl(self, units: list[str]) -> None:
        listed = "\\n".join(f"{u} loaded active running" for u in units)
        write_exec(
            self.bin / "systemctl",
            "\n".join(
                [
                    "#!/bin/sh",
                    f'echo "$@" >> "{self.systemctl_log}"',
                    'if [ "$1" = "list-units" ]; then',
                    f'  printf "{listed}\\n"',
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
        )

    def _write_docker(self, reachable: bool) -> None:
        write_exec(
            self.bin / "docker",
            "\n".join(
                [
                    "#!/bin/sh",
                    f'echo "$@" >> "{self.docker_log}"',
                    'if [ "$1" = "info" ]; then',
                    f"  exit {0 if reachable else 1}",
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
        )

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=self.env,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_skips_when_job_running(self):
        self._write_pgrep(job_running=True)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("job running", result.stdout)
        self.assertTrue((self.work / "some-repo" / "checkout.txt").is_file())
        self.assertTrue((self.diag / "Runner_old.log").is_file())
        self.assertFalse(self.systemctl_log.exists())

    def test_pressure_skips_when_disk_low(self):
        self._write_df(pct=40)
        result = self._run("--if-pressure")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("below 70%", result.stdout)
        self.assertTrue((self.work / "some-repo" / "checkout.txt").is_file())
        self.assertFalse(self.systemctl_log.exists())

    def test_pressure_cleans_when_disk_high_and_idle(self):
        self._write_df(pct=81)
        result = self._run("--if-pressure")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("81% full", result.stdout)
        self.assertFalse((self.work / "some-repo").exists())
        self.assertFalse((self.work / "_temp").exists())
        self.assertFalse((self.work / "_actions").exists())
        self.assertFalse((self.diag / "Runner_old.log").exists())
        self.assertTrue((self.work / "_tool" / "keep.txt").is_file())
        self.assertTrue((self.work / "_update").is_dir())

    def test_scheduled_cleans_when_idle_even_if_disk_low(self):
        self._write_df(pct=10)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse((self.work / "some-repo").exists())
        self.assertTrue((self.runner / ".runner").is_file())
        self.assertTrue((self.runner / "bin" / "Runner.Listener").is_file())
        self.assertTrue((self.runner / "svc.sh").is_file())
        self.assertTrue((self.runner / ".service").is_file())

    def test_trap_stops_then_starts_runner_unit(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        log = self.systemctl_log.read_text()
        self.assertIn("stop actions.runner.example.gh-runner-01.service", log)
        self.assertIn("start actions.runner.example.gh-runner-01.service", log)
        self.assertLess(
            log.index("stop actions.runner.example.gh-runner-01.service"),
            log.index("start actions.runner.example.gh-runner-01.service"),
        )

    def test_trap_starts_even_if_cleanup_step_fails(self):
        # Make leftover removal fail: _work/some-repo is not writable as a tree
        # root by replacing the parent with a file after creating the mock.
        # Instead, point docker at a failing prune after cordon.
        write_exec(
            self.bin / "docker",
            "\n".join(
                [
                    "#!/bin/sh",
                    f'echo "$@" >> "{self.docker_log}"',
                    'if [ "$1" = "info" ]; then exit 0; fi',
                    "exit 1",
                    "",
                ]
            ),
        )
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        log = self.systemctl_log.read_text()
        self.assertIn("start actions.runner.example.gh-runner-01.service", log)
        self.assertIn("docker", result.stdout)

    def test_docker_prune_invoked_when_daemon_exists(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        docker_log = self.docker_log.read_text()
        self.assertIn("container prune -f", docker_log)
        self.assertIn("builder prune -af", docker_log)
        self.assertIn("image prune -af", docker_log)

    def test_does_not_delete_runner_config(self):
        self._run()
        for rel in (".runner", "bin/Runner.Listener", "svc.sh", ".service"):
            self.assertTrue((self.runner / rel).exists(), rel)


class InstallScriptTests(unittest.TestCase):
    def test_install_respects_destdir_and_skips_systemctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "root"
            result = subprocess.run(
                ["bash", str(CLEANUP_DIR / "install.sh")],
                env={**os.environ, "DESTDIR": str(dest), "START_TIMERS": "1"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            script = dest / "usr" / "local" / "sbin" / "actions-runner-disk-cleanup"
            self.assertTrue(script.is_file())
            self.assertTrue(os.access(script, os.X_OK))
            self.assertTrue((dest / "etc" / "systemd" / "system" / "actions-runner-disk-cleanup.timer").is_file())
            self.assertTrue((dest / "etc" / "systemd" / "system" / "actions-runner-disk-cleanup-pressure.timer").is_file())
            defaults = (dest / "etc" / "default" / "actions-runner-disk-cleanup").read_text()
            self.assertIn("RUNNER_DIR=/opt/actions-runner", defaults)
            self.assertIn("skipped systemctl", result.stdout)

    def test_install_starts_timers_only_when_requested(self):
        source = (CLEANUP_DIR / "install.sh").read_text(encoding="utf-8")
        self.assertIn('if [ "$START_TIMERS" = 1 ]; then', source)
        self.assertIn("systemctl enable actions-runner-disk-cleanup.timer", source)
        self.assertIn("systemctl start actions-runner-disk-cleanup.timer", source)
        self.assertIn('[ -n "$DESTDIR" ]', source)

    def test_scripts_parse(self):
        for path in (SCRIPT, CLEANUP_DIR / "install.sh", GUEST_SETUP):
            result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
