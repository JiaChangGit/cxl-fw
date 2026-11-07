"""Focused M0–M2 runner controls; no QEMU or Guest is started by these tests."""
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

spec = importlib.util.spec_from_file_location(
    "runner", Path(__file__).resolve().parents[1] / "scripts/run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class QMPTests(unittest.TestCase):
    def setUp(self):
        self.client, self.server = socket.socketpair()
        self.addCleanup(self.client.close)
        self.addCleanup(self.server.close)
        self.log = io.StringIO()

    def test_event_and_other_response_do_not_satisfy_request(self):
        def serve():
            request = json.loads(self.server.recv(4096))
            payload = [
                {"event": "RESET", "data": {}},
                {"id": "unrelated", "return": {"status": "wrong"}},
                {"id": request["id"], "return": {"status": "running"}},
            ]
            self.server.sendall(b"".join(json.dumps(p).encode() + b"\r\n" for p in payload))
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        client = runner.QMP(self.client, self.log)
        self.assertEqual(client.command("query-status", time.monotonic() + 1), {"status": "running"})
        self.assertEqual(client.events[0]["event"], "RESET")
        self.assertIn("unrelated", client.pending)
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    def test_qmp_command_error_is_preserved(self):
        self.server.sendall(b'{"id":"request-1","error":{"class":"CommandNotFound","desc":"bad"}}\n')
        with self.assertRaisesRegex(RuntimeError, "CommandNotFound"):
            runner.QMP(self.client, self.log).command("unknown", time.monotonic() + 1)

    def test_response_deadline(self):
        with self.assertRaises(TimeoutError):
            runner.QMP(self.client, self.log).receive(time.monotonic() + 0.02)

    def test_non_object_message_is_rejected(self):
        self.server.sendall(b"[]\n")
        with self.assertRaisesRegex(ValueError, "object"):
            runner.QMP(self.client, self.log).receive(time.monotonic() + 1)

    def test_missing_greeting_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="qmp-test-") as temp:
            path = Path(temp) / "qmp.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(path))
            listener.listen()
            def serve():
                conn, _ = listener.accept()
                with conn:
                    conn.sendall(b'{"return":{}}\n')
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            process = mock.Mock()
            process.poll.return_value = None
            with self.assertRaisesRegex(ValueError, "greeting"):
                runner.QMP.connect(path, self.log, time.monotonic() + 1, process)
            thread.join(timeout=1)


class GuestEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.shared = Path(self.temp.name)
        self.run_id = "run-123"
        self.payload = {
            "schema_version": 1, "run_id": self.run_id, "scenario": "boot",
            "status": "PASS", "kernel_release": "6.18.56", "boot_id": str(uuid.uuid4()),
            "tools": {n: {"path": f"/usr/bin/{n}", "version": "v1"}
                      for n in ("cxl", "daxctl", "ndctl")},
            "cmdline": "root=/dev/vda transparent_hugepage=never numa_balancing=disable memhp_default_state=offline",
        }
        (self.shared / "dmesg.log").write_text("[0] Booting Linux\n")
        self.console = f"[ 2.0] tierlab-run[1]: TIERLAB_DONE {self.run_id} PASS\n"

    def verify(self):
        (self.shared / "guest.json").write_text(json.dumps(self.payload))
        return runner.validate_guest(self.shared, self.console, self.run_id, "6.18.56")

    def test_correlated_guest_report_and_marker(self):
        self.assertEqual(self.verify()["boot_id"], self.payload["boot_id"])

    def test_wrong_run_kernel_or_missing_tools_are_rejected(self):
        for key, value in (("run_id", "other"), ("kernel_release", "6.18.57"), ("tools", {})):
            with self.subTest(key=key):
                original = self.payload[key]
                self.payload[key] = value
                with self.assertRaises(ValueError):
                    self.verify()
                self.payload[key] = original

    def test_missing_guest_report_is_not_pass(self):
        with self.assertRaises(FileNotFoundError):
            runner.validate_guest(self.shared, self.console, self.run_id, "6.18.56")

    def test_missing_marker_and_health_failures_are_rejected(self):
        for suffix in ("", "Kernel panic - not syncing", "Oops: kernel problem", "WARNING: CPU: 0 at drivers/cxl/core/region.c:235"):
            with self.subTest(console=suffix):
                self.console = suffix
                with self.assertRaises(ValueError):
                    self.verify()

    def test_kernel_cmdline_is_not_mistaken_for_oops(self):
        self.console += "Kernel command line: panic=1 oops=panic\n"
        self.verify()

    def test_debug_prefix_is_not_mistaken_for_a_kernel_bug(self):
        self.console += "DEBUG: normal diagnostic output\\n"
        self.verify()

    def test_dmesg_failure_is_independently_rejected(self):
        (self.shared / "dmesg.log").write_text("BUG: kernel failure\n")
        with self.assertRaisesRegex(ValueError, "dmesg"):
            self.verify()

    def test_non_object_report_is_rejected(self):
        self.payload = []
        with self.assertRaisesRegex(ValueError, "object"):
            self.verify()

    def test_correlated_migration_block_keeps_blocked_status(self):
        self.payload.update(scenario="migrate", status="BLOCKED", reason="cxl_setup_failed")
        (self.shared / "guest.json").write_text(json.dumps(self.payload))
        console = f"TIERLAB_DONE {self.run_id} BLOCKED\n"
        with self.assertRaisesRegex(runner.GuestBlocked, "cxl_setup_failed"):
            runner.validate_guest(self.shared, console, self.run_id, "6.18.56", "migrate")

    def test_migration_cannot_reuse_boot_report(self):
        (self.shared / "guest.json").write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(ValueError, "scenario"):
            runner.validate_guest(self.shared, self.console, self.run_id, "6.18.56", "migrate")

    def test_policy_report_requires_its_own_scenario(self):
        self.payload["scenario"] = "migrate"
        (self.shared / "guest.json").write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(ValueError, "scenario"):
            runner.validate_guest(self.shared, self.console, self.run_id, "6.18.56", "policy")
        self.payload["scenario"] = "policy"
        (self.shared / "guest.json").write_text(json.dumps(self.payload))
        self.assertEqual(runner.validate_guest(
            self.shared, self.console, self.run_id, "6.18.56", "policy")["scenario"], "policy")

    def test_policy_prerequisite_block_remains_blocked(self):
        self.payload.update(scenario="policy", status="BLOCKED", reason="runtime_unavailable")
        (self.shared / "guest.json").write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(runner.GuestBlocked, "runtime_unavailable"):
            runner.validate_guest(self.shared, f"TIERLAB_DONE {self.run_id} BLOCKED\n",
                                  self.run_id, "6.18.56", "policy")


class LifecycleTests(unittest.TestCase):
    def test_cleanup_escalates_only_owned_process(self):
        process, client = mock.Mock(), mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("qemu", 1),
            subprocess.TimeoutExpired("qemu", 1),
            subprocess.TimeoutExpired("qemu", 1), 0]
        runner.stop_owned_process(process, client)
        self.assertEqual([c.args[0] for c in client.command.call_args_list],
                         ["system_powerdown", "quit"])
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_auto_fallback_records_permission_reason(self):
        with mock.patch.object(runner.os, "open", side_effect=PermissionError(13, "denied")):
            accel, reason = runner.choose_accel("auto")
            self.assertEqual(accel, "tcg")
            self.assertIn("EACCES", reason)
            with self.assertRaises(OSError):
                runner.choose_accel("kvm")

    def test_qmp_socket_limit_is_checked_before_launch(self):
        path = Path("/" + "x" * 110)
        with self.assertRaisesRegex(ValueError, "socket path"):
            runner.qemu_argv(path, path, path, path, path, path, "kvm", "")

    def check_series(self, repeats, boot_ids):
        with tempfile.TemporaryDirectory(prefix="boot-series-") as temp:
            root = Path(temp)
            results = [{"run_id": f"r{i}", "status": "PASS", "boot_id": b}
                       for i, b in enumerate(boot_ids)]
            with mock.patch.object(runner, "ROOT", root), \
                    mock.patch.object(runner.os, "geteuid", return_value=1000), \
                    mock.patch.object(runner, "choose_accel", return_value=("kvm", "fixture")), \
                    mock.patch.object(runner, "load_artifacts", return_value={"hashes": {}}), \
                    mock.patch.object(runner, "run_once", side_effect=results), \
                    mock.patch.object(sys, "argv", ["run.py", "boot", "--repeat", str(repeats)]):
                code = runner.main()
            return code, json.loads((root / "results/gates/G0.json").read_text())

    def test_one_good_boot_does_not_pass_g0(self):
        code, result = self.check_series(1, ["b1"])
        self.assertEqual(code, 0)
        self.assertEqual(result["gates"]["G0"], "NOT_RUN")

    def test_three_fresh_boots_pass_g0(self):
        code, result = self.check_series(3, ["b1", "b2", "b3"])
        self.assertEqual(code, 0)
        self.assertEqual(result["gates"]["G0"], "PASS")

    def test_reused_boot_id_is_rejected(self):
        code, result = self.check_series(3, ["b1", "b1", "b3"])
        self.assertEqual(code, 1)
        self.assertEqual(result["gates"]["G0"], "FAIL")

    def check_scenario_gate(self, scenario, milestone, gate_id, timeout):
        for status, expected_code in (("PASS", 0), ("FAIL", 1), ("BLOCKED", 78)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                result = {"status": status, "boot_id": "new-boot", "run_id": f"{scenario}-run"}
                with mock.patch.object(runner, "ROOT", root), \
                        mock.patch.object(runner.os, "geteuid", return_value=1000), \
                        mock.patch.object(runner, "choose_accel", return_value=("kvm", "fixture")), \
                        mock.patch.object(runner, "load_artifacts", return_value={"hashes": {}}) as load, \
                        mock.patch.object(runner, "run_once", return_value=result) as run, \
                        mock.patch.object(sys, "argv", ["run.py", scenario]):
                    code = runner.main()
                gate = json.loads((root / f"results/gates/{milestone}.json").read_text())
                self.assertEqual(code, expected_code)
                self.assertEqual(gate["milestone"], milestone)
                self.assertEqual(gate["gates"], {gate_id: status})
                self.assertFalse((root / "results/gates/M1.json").exists())
                load.assert_called_once_with(root, scenario)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[-2:], (timeout, scenario))


    def test_policy_gate_and_default_runtime_budget(self):
        self.check_scenario_gate("policy", "M2", "G3", 900)

    def test_trace_gate_and_default_runtime_budget(self):
        self.check_scenario_gate("trace", "M4", "G5", 180)

    def test_offline_gate_and_default_runtime_budget(self):
        self.check_scenario_gate("offline", "M3", "G4", 300)



class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="artifacts-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        contents = {
            "install/qemu/bin/qemu-system-x86_64": "binary",
            "build/kernel/arch/x86/boot/bzImage": "kernel",
            "build/kernel/.config": "CONFIG_NUMA=y",
            "images/rootfs.ext4": "image",
            "images/rootfs.packages.tsv": "systemd 1",
            "configs/cmdline.txt": "root=/dev/vda",
            "guest/run.sh": "guest wrapper",
            "guest/cxl_setup.py": "topology helper",
            "guest/cxl_offline.py": "offline helper",
            "guest/trace.py": "trace collector",
            "guest/tierlab.service": "guest unit",
        }
        for name, data in contents.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        self.lock = {
            "qemu": {"version": "11.1.0", "source_sha256": "a" * 64},
            "linux": {"version": "6.18.56", "source_sha256": "b" * 64},
        }
        (self.root / "configs/versions.lock.json").write_text(json.dumps(self.lock))
        self.meta = {
            "image_sha256": runner.sha256(self.root / "images/rootfs.ext4"),
            "kernel_release": "6.18.56",
            "kernel_config_sha256": runner.sha256(self.root / "build/kernel/.config"),
            "package_manifest_sha256": runner.sha256(self.root / "images/rootfs.packages.tsv"),
            "guest_inputs": {name: runner.sha256(self.root / name) for name in
                             ("guest/run.sh", "guest/cxl_setup.py", "guest/cxl_offline.py", "guest/trace.py", "guest/tierlab.service")},
        }
        self.meta_path = self.root / "images/rootfs.meta.json"
        self.meta_path.write_text(json.dumps(self.meta))

    def load(self, scenario="boot"):
        version = mock.Mock(stdout="QEMU emulator version 11.1.0\n")
        with mock.patch.object(runner.subprocess, "run", return_value=version), \
                mock.patch.object(runner.shutil, "which", return_value="/usr/bin/qemu-img"):
            return runner.load_artifacts(self.root, scenario)

    def test_source_image_and_config_provenance(self):
        artifacts = self.load()
        self.assertIn("build/kernel/.config", artifacts["hashes"])
        self.assertIn("images/rootfs.packages.tsv", artifacts["hashes"])

    def test_changed_image_or_config_cannot_reuse_rootfs(self):
        for name in ("images/rootfs.ext4", "build/kernel/.config"):
            with self.subTest(artifact=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "provenance"):
                    self.load()
                path.write_bytes(original)

    def test_missing_source_pin_is_blocked(self):
        self.lock["qemu"]["source_sha256"] = None
        (self.root / "configs/versions.lock.json").write_text(json.dumps(self.lock))
        with self.assertRaisesRegex(ValueError, "source pin"):
            self.load()

    def test_changed_guest_source_requires_image_rebuild(self):
        for name in ("guest/cxl_setup.py", "guest/cxl_offline.py", "guest/trace.py"):
            with self.subTest(helper=name):
                path = self.root / name
                original = path.read_text()
                path.write_text("changed helper")
                with self.assertRaisesRegex(ValueError, "rebuild rootfs"):
                    self.load()
                path.write_text(original)

    def test_migration_and_policy_require_and_hash_guest_binary(self):
        binary = self.root / "build/tierlab/tierlab"
        for scenario in ("migrate", "policy", "offline"):
            with self.subTest(scenario=scenario), self.assertRaisesRegex(OSError, "Guest binary"):
                self.load(scenario)
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"guest-ELF-fixture")
        for scenario in ("migrate", "policy", "offline"):
            with self.subTest(scenario=scenario):
                artifacts = self.load(scenario)
                self.assertEqual(artifacts["tierlab"], binary)
                self.assertEqual(artifacts["hashes"]["build/tierlab/tierlab"], runner.sha256(binary))

    def prepare_trace(self):
        binary = self.root / "build/tierlab/tierlab"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"tierlab fixture")
        baseline = self.root / "build/selftests/mm/migration"
        baseline.parent.mkdir(parents=True)
        baseline.write_bytes(b"upstream fixture")
        names = ["tools/testing/selftests/mm/" + n for n in (
            "migration.c", "vm_util.c", "vm_util.h", "thp_settings.c", "thp_settings.h", "Makefile", "check_config.sh")]
        names += ["tools/testing/selftests/" + n for n in ("kselftest.h", "kselftest_harness.h")]
        inputs = {}
        for name in names:
            path = self.root / "build/sources/linux-6.18.56" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("source fixture")
            inputs[name] = runner.sha256(path)
        metadata = {"status": "BUILT", "kernel_version": "6.18.56",
                    "source_sha256": "b" * 64, "binary_sha256": runner.sha256(baseline),
                    "inputs_sha256": inputs}
        path = baseline.parent / "build-meta.json"
        path.write_text(json.dumps(metadata))
        return baseline, path, metadata

    def test_trace_baseline_is_hashed_and_changed_source_rejected(self):
        baseline, _, _ = self.prepare_trace()
        self.assertEqual(self.load("trace")["baseline"], baseline)
        (self.root / "build/sources/linux-6.18.56/tools/testing/selftests/mm/migration.c").write_text("changed")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.load("trace")

    def test_trace_rejects_corrupt_binary_and_incomplete_manifest(self):
        baseline, path, data = self.prepare_trace()
        original = baseline.read_bytes()
        baseline.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.load("trace")
        baseline.write_bytes(original)
        data["inputs_sha256"].pop("tools/testing/selftests/mm/migration.c")
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.load("trace")


class DemoTests(unittest.TestCase):
    def test_demo_rejects_old_inputs_before_replaying(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gates = root / "results/gates"
            gates.mkdir(parents=True)
            (gates / "G0.json").write_text(json.dumps({
                "status": "PASS", "gates": {"G0": "PASS"},
                "input_sha256": {"kernel": "old"}, "runs": []}))
            with mock.patch.object(runner, "load_artifacts", return_value={"hashes": {"kernel": "new"}}):
                with self.assertRaisesRegex(ValueError, "stale"):
                    runner.show_demo(root)

if __name__ == "__main__":
    unittest.main()
