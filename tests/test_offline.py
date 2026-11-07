"""MockSysfs failure cases; never access or alter Host memory sysfs."""
import copy
import errno
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

GUEST = Path(__file__).resolve().parents[1] / "guest"
sys.path.insert(0, str(GUEST))
spec = importlib.util.spec_from_file_location("cxl_offline", GUEST / "cxl_offline.py")
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


def fixture():
    start, block_size = 0x150000000, 128 * 1024 * 1024
    blocks = [{"name": f"memory{42+i}", "start": start+i*block_size, "size": block_size,
               "node": 1, "node_links": ["node1"], "state": "online", "valid_zones": "Movable",
               "zone": "Movable"} for i in range(8)]
    header = {"schema_version": 1, "run_id": "test", "scenario": "offline",
              "evidence_basis": "guest_kernel_measured"}
    topology = dict(header, scenario="migrate", status="PASS", fast_node=0, slow_node=1,
                    region="region0", daxdev="dax0.0", resource_start=start,
                    resource_size=8*block_size, blocks=copy.deepcopy(blocks))
    snapshot = {"auto_online_blocks": "offline", "region": {"name": "region0", "mode": "ram",
                "resource": start, "size": 8*block_size, "interleave_ways": 1},
                "dax": {"name": "dax0.0", "resource": start, "size": 8*block_size,
                        "target_node": 1, "driver": "kmem"}, "blocks": blocks}
    iomem = (f"{start:x}-{start+8*block_size-1:x} : dax0.0\n"
             f"  {start:x}-{start+8*block_size-1:x} : System RAM (kmem)\n")
    drain = dict(header, status="PASS", lifecycle_state="DRAINING", fast_node=0, slow_node=1,
                 final_slow_pages=0, integrity_failures=0, query_missing_count=0,
                 mapping_released=True, workload_stopped=True, managed_bytes=128*1024*1024,
                 page_size=4096, page_count=32768, initial_slow_pages=32768,
                 capacity={"passed": True, "slow_bytes": 128*1024*1024,
                           "safety_margin_bytes": 256*1024*1024, "required_bytes": 384*1024*1024,
                           "node_free_bytes": 2*1024**3, "mem_available_bytes": 2*1024**3})
    return topology, snapshot, iomem, drain, header


class MockSysfs:
    def __init__(self, snapshot, iomem):
        self.snapshot_data, self.iomem = snapshot, iomem
        self.states = {b["name"]: "online" for b in snapshot["blocks"]}
        self.writes, self.commands = [], []
        self.fail_offline = None
        self.fail_rollback = None
        self.timeout_offline = None
        self.identity_error = False
        self.mode, self.region_exists, self.dax_exists, self.driver_bound = "system-ram", True, True, True

    def identity(self):
        if self.identity_error:
            raise ValueError("Not the designated Guest")

    def snapshot(self, topology):
        return copy.deepcopy(self.snapshot_data), self.iomem

    def state(self, name):
        return self.states[name]

    def write(self, name, value):
        self.writes.append((name, value))
        if value == "offline" and name == self.timeout_offline:
            raise TimeoutError(errno.ETIMEDOUT, "Unknown in-flight kernel operation")
        if (value == "offline" and name == self.fail_offline) or (value == "online_movable" and name == self.fail_rollback):
            raise OSError(errno.EBUSY, "Injected busy block")
        self.states[name] = "offline" if value == "offline" else "online"

    def command(self, argv):
        self.commands.append(argv)
        stdout = ""
        if argv[-1] == "--version":
            stdout = "81\n"
        elif argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 1, "", "man viewer unavailable")
        elif argv[:2] == ["daxctl", "reconfigure-device"]:
            if any(state == "online" for state in self.states.values()):
                raise AssertionError("Teardown attempted with online memory")
            self.mode = "devdax"
            self.states = {name: "absent" for name in self.states}
        elif argv == ["daxctl", "list", "-D"]:
            stdout = json.dumps([{"chardev": "dax0.0", "mode": self.mode}] if self.dax_exists else [])
        elif argv[:2] == ["cxl", "disable-region"]:
            self.driver_bound = False
        elif argv[:2] == ["cxl", "destroy-region"]:
            self.region_exists = self.dax_exists = False
        elif argv == ["cxl", "list", "-R"]:
            stdout = json.dumps([{"region": "region0"}] if self.region_exists else [])
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def manual(self, tool, command):
        return f"{tool} {command} <region> --mode devdax"

    def exists(self, kind, name):
        return self.region_exists if kind == "cxl" else self.dax_exists

    def bound(self, region):
        return self.driver_bound


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.topology, self.snapshot, self.iomem, self.drain, self.config = fixture()
        self.io = MockSysfs(self.snapshot, self.iomem)

    def operation(self):
        for name, data in (("topology.json", self.topology), ("drain.json", self.drain), ("config.json", self.config)):
            (self.output / name).write_text(json.dumps(data))
        return offline.Offline(self.output, "test", self.io)

    def events(self):
        return [json.loads(line) for line in (self.output / "offline.jsonl").read_text().splitlines()]

    def test_success_offlines_only_whitelist_then_nonforce_teardown(self):
        op = self.operation()
        report = op.execute()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["lifecycle_state"], "OFFLINE")
        self.assertEqual(self.io.writes, [(b["name"], "offline") for b in self.topology["blocks"]])
        teardown = [event["argv"] for event in self.events() if event["kind"] == "command" and event["purpose"] == "teardown"]
        self.assertEqual(teardown, [["daxctl", "reconfigure-device", "--mode=devdax", "dax0.0"],
                                    ["cxl", "disable-region", "region0"], ["cxl", "destroy-region", "region0"]])
        stages = [event["stage"] for event in self.events() if event["kind"] == "all_offline"]
        self.assertEqual(stages, ["before_devdax", "before_disable", "before_destroy"])
        self.assertEqual(self.events()[-1]["region_exists"], False)

    def test_kth_failure_snapshots_before_reverse_rollback(self):
        self.io.fail_offline = "memory44"
        op = self.operation()
        with self.assertRaises(OSError):
            op.execute()
        self.assertEqual(self.io.writes, [("memory42", "offline"), ("memory43", "offline"),
                                         ("memory44", "offline"), ("memory43", "online_movable"),
                                         ("memory42", "online_movable")])
        self.assertTrue(op.report["rollback_complete"])
        events = self.events()
        failed = next(i for i, event in enumerate(events) if event["kind"] == "failure_snapshot")
        rollback = next(i for i, event in enumerate(events) if event.get("requested") == "online_movable")
        self.assertLess(failed, rollback)
        self.assertEqual(events[failed]["blocks"][0]["state"], "offline")
        self.assertFalse(any(event.get("purpose") == "teardown" for event in events))

    def test_rollback_failure_remains_failed_and_never_destroys(self):
        self.io.fail_offline, self.io.fail_rollback = "memory44", "memory43"
        op = self.operation()
        with self.assertRaises(OSError):
            op.execute()
        self.assertFalse(op.report["rollback_complete"])
        self.assertEqual(op.report["lifecycle_state"], "FAILED")
        self.assertEqual(self.io.states["memory43"], "offline")
        self.assertEqual(self.io.states["memory42"], "online")
        self.assertFalse(any(event.get("purpose") == "teardown" for event in self.events()))

    def test_timeout_never_rolls_back_or_retries_unknown_kernel_write(self):
        self.io.timeout_offline = "memory44"
        op = self.operation()
        with self.assertRaises(TimeoutError):
            op.execute()
        self.assertEqual(len(self.io.writes), 3)
        self.assertFalse(op.report["rollback_attempted"])
        self.assertTrue(self.events()[-1]["unknown_inflight"])
        self.assertFalse(any(event.get("purpose") == "teardown" for event in self.events()))

    def test_failed_write_that_changes_state_does_not_claim_full_rollback(self):
        original = self.io.write
        def changed_then_failed(name, value):
            original(name, value)
            if name == "memory44" and value == "offline":
                raise OSError(errno.EBUSY, "Failed after changing state")
        self.io.write = changed_then_failed
        op = self.operation()
        with self.assertRaises(OSError):
            op.execute()
        self.assertEqual(self.io.writes[-2:], [("memory43", "online_movable"), ("memory42", "online_movable")])
        self.assertEqual(self.io.states["memory44"], "offline")
        self.assertFalse(op.report["rollback_complete"])
        self.assertEqual(self.events()[-1]["blocks"][2]["state"], "offline")

    def test_readback_failure_preserves_snapshot_errors_and_known_successes(self):
        original = self.io.state
        def unreadable_after_write(name):
            if name == "memory44" and (name, "offline") in self.io.writes:
                raise OSError(errno.EIO, "Injected unreadable memory state")
            return original(name)
        self.io.state = unreadable_after_write
        op = self.operation()
        with self.assertRaises(OSError):
            op.execute()
        events = self.events()
        failed = next(event for event in events if event["kind"] == "failure_snapshot")
        self.assertEqual(failed["blocks"][2]["state"], "unreadable")
        self.assertEqual(failed["blocks"][2]["errno"], errno.EIO)
        self.assertIn("Injected unreadable", failed["blocks"][2]["error"])
        self.assertEqual(self.io.writes[-2:], [("memory43", "online_movable"), ("memory42", "online_movable")])
        self.assertNotIn(("memory44", "online_movable"), self.io.writes)
        self.assertFalse(op.report["rollback_complete"])
        self.assertFalse(any(event.get("purpose") == "teardown" for event in events))

    def test_readback_mismatch_does_not_restore_unknown_state_or_claim_complete(self):
        original = self.io.state
        def transitional_after_write(name):
            if name == "memory44" and (name, "offline") in self.io.writes:
                return "going-offline"
            return original(name)
        self.io.state = transitional_after_write
        op = self.operation()
        with self.assertRaisesRegex(ValueError, "readback mismatch"):
            op.execute()
        self.assertNotIn(("memory44", "online_movable"), self.io.writes)
        self.assertFalse(op.report["rollback_complete"])
        self.assertEqual(self.events()[-1]["blocks"][2]["state"], "going-offline")

    def test_rollback_skips_unreadable_success_and_restores_other_known_success(self):
        self.io.fail_offline = "memory44"
        original = self.io.state
        def unreadable_rollback_target(name):
            if name == "memory43" and ("memory44", "offline") in self.io.writes:
                raise OSError(errno.EIO, "Rollback target state unavailable")
            return original(name)
        self.io.state = unreadable_rollback_target
        op = self.operation()
        with self.assertRaises(OSError):
            op.execute()
        self.assertNotIn(("memory43", "online_movable"), self.io.writes)
        self.assertEqual(self.io.writes[-1], ("memory42", "online_movable"))
        self.assertFalse(op.report["rollback_complete"])
        self.assertEqual(self.events()[-1]["blocks"][1]["state"], "unreadable")

    def test_capacity_refusal_precedes_any_write(self):
        self.drain["capacity"]["passed"] = False
        with self.assertRaises(ValueError):
            self.operation().execute()
        self.assertEqual(self.io.writes, [])
        self.assertEqual(self.io.commands, [])

    def test_false_capacity_pass_claim_cannot_authorize_offline(self):
        self.drain["capacity"]["node_free_bytes"] = 128*1024*1024
        with self.assertRaisesRegex(ValueError, "Insufficient drain capacity"):
            self.operation().execute()
        self.assertEqual(self.io.writes, [])
        self.assertEqual(self.io.commands, [])

    def test_mapping_must_be_released_and_worker_stopped(self):
        for key in ("mapping_released", "workload_stopped"):
            with self.subTest(key=key):
                self.drain[key] = False
                with self.assertRaises(ValueError):
                    self.operation().execute()
                self.drain[key] = True
        self.assertEqual(self.io.writes, [])

    def test_wrong_run_or_kernel_cannot_authorize_writes(self):
        self.drain["run_id"] = "other"
        with self.assertRaises(ValueError):
            self.operation().execute()
        self.drain["run_id"] = "test"
        self.io.identity_error = True
        with self.assertRaises(ValueError):
            self.operation().execute()
        self.assertEqual(self.io.writes, [])
        self.assertEqual(self.io.commands, [])

    def test_ownership_nid_and_zone_are_rechecked(self):
        for field, value in (("node_links", ["node0"]), ("valid_zones", "Normal"), ("state", "offline")):
            with self.subTest(field=field):
                original = self.io.snapshot_data["blocks"][0][field]
                self.io.snapshot_data["blocks"][0][field] = value
                with self.assertRaises(ValueError):
                    self.operation().execute()
                self.io.snapshot_data["blocks"][0][field] = original
        self.io.iomem = self.iomem.replace("dax0.0", "dax9.9")
        with self.assertRaises(ValueError):
            self.operation().execute()
        self.assertEqual(self.io.writes, [])

    def test_partial_range_and_holes_refuse_before_writes(self):
        self.io.snapshot_data["blocks"][0]["size"] -= 4096
        with self.assertRaises(ValueError):
            self.operation().execute()
        self.assertEqual(self.io.writes, [])

    def test_teardown_guard_refuses_reappearing_online_block(self):
        original = self.io.state
        def racing_state(name):
            if len(self.io.writes) == 8 and name == "memory42":
                return "online"
            return original(name)
        self.io.state = racing_state
        with self.assertRaises(ValueError):
            self.operation().execute()
        self.assertFalse(any(event.get("purpose") == "teardown" for event in self.events()))


if __name__ == "__main__":
    unittest.main()
