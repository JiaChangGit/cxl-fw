"""Synthetic M3 evidence exercises rejection; it is never Guest execution evidence."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_offline as validator
import test_migration_evidence as topology_fixtures


class OfflineEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        topology_fixtures.MigrationEvidenceTests.setUpClass()
        fixtures = topology_fixtures.MigrationEvidenceTests
        cls.header = {"schema_version": 1, "run_id": fixtures.header["run_id"],
                      "scenario": "offline", "evidence_basis": "guest_kernel_measured"}
        cls.topology = copy.deepcopy(fixtures.topology)
        cls.topology_raw = copy.deepcopy(fixtures.raw)
        cls.iomem = "200000000-23fffffff : dax0.0\n  200000000-23fffffff : System RAM (kmem)\n"
        cls.cap = {"node_free_bytes": 2 * 1024**3, "mem_available_bytes": 2 * 1024**3,
                   "slow_bytes": validator.BYTES, "safety_margin_bytes": validator.MARGIN,
                   "required_bytes": validator.BYTES + validator.MARGIN, "passed": True}
        cls.drain_report = {**cls.header, "status": "PASS", "lifecycle_state": "DRAINING",
                            "managed_bytes": validator.BYTES, "page_size": 4096,
                            "page_count": validator.PAGES, "fast_node": 0, "slow_node": 1,
                            "capacity": cls.cap, "initial_slow_pages": validator.PAGES,
                            "final_slow_pages": 0, "attempted_pages": validator.PAGES,
                            "succeeded_pages": validator.PAGES, "integrity_failures": 0,
                            "query_missing_count": 0, "workload_stopped": True,
                            "mapping_released": True, "raw_file": "drain.jsonl",
                            "deadline_ms": 60000, "max_attempts_per_page": 3, "elapsed_ms": 200}

        def event(kind, **values):
            return {**cls.header, "kind": kind, **values}

        def pattern(stage):
            return event("pattern", stage=stage, generation=7, checked_bytes=validator.BYTES,
                         integrity_failures=0)

        def snapshot(stage, node):
            return event("snapshot", stage=stage, query_rc=0, query_errno=0,
                         nodes=[node] * validator.PAGES)

        cls.drain_events = [pattern("initial"), snapshot("initial", 1),
                            event("capacity", node_meminfo="Node 0 MemFree: 2097152 kB\n",
                                  meminfo="MemAvailable: 2097152 kB\n", **cls.cap)]
        for index in range(validator.PAGES // 256):
            cls.drain_events.append(event("batch", batch_index=index, target_node=0, source_node=1,
                syscall_rc=0, syscall_errno=0, before_query_rc=0, before_query_errno=0,
                after_query_rc=0, after_query_errno=0,
                pages=[{"page_id": index * 256 + offset, "query_before_node": 1,
                        "query_after_node": 0, "move_status": 0, "move_status_valid": True}
                       for offset in range(256)]))
        cls.drain_events += [snapshot("final", 0), pattern("final"),
                            event("release", workload_stopped=True, mapping_released=True,
                                  managed_slow_pages=0, lifecycle_state="DRAINING", elapsed_ms=200)]
        cls.names = [block["name"] for block in cls.topology["blocks"]]
        cls.offline_report = {**cls.header, "status": "PASS", "lifecycle_state": "OFFLINE",
                              **{key: cls.topology[key] for key in
                                 ("fast_node", "slow_node", "region", "daxdev", "resource_start", "resource_size")},
                              "blocks": cls.names, "offlined_blocks": cls.names,
                              "rollback_attempted": False, "rollback_complete": None,
                              "region_removed": True, "dax_removed": True, "raw_file": "offline.jsonl"}
        cls.offline_events = [event("plan", snapshot=cls.topology_raw["sysfs_after"], iomem=cls.iomem,
                                     blocks=cls.names, drain={"mapping_released": True,
                                     "workload_stopped": True, "final_slow_pages": 0, "capacity_passed": True})]
        for tool in ("cxl", "daxctl"):
            cls.offline_events.append(event("command", purpose="probe", argv=[tool, "--version"],
                                             exit_code=0, stdout="81\n", stderr=""))
        cls.manuals = {}
        for tool, sub in (("daxctl", "reconfigure-device"), ("cxl", "disable-region"),
                          ("cxl", "destroy-region")):
            text = f"{tool} {sub} --mode devdax\n"
            name = f"{tool}-{sub}.manual.txt"
            cls.manuals[name] = text
            cls.offline_events.append(event("command", purpose="probe", argv=[tool, sub, "--help"],
                                             exit_code=1, stdout="", stderr="No man viewer"))
            cls.offline_events.append(event("manual", tool=tool, command=sub, required=[sub],
                                             file=name, sha256=hashlib.sha256(text.encode()).hexdigest()))
        for name in cls.names:
            cls.offline_events.append(event("write", block=name, before_state="online", requested="offline",
                                             after_state="offline", rc=0, errno=0, error=None))

        def guard(stage, absent=False):
            return event("all_offline", stage=stage,
                         blocks=[{"name": name, "state": "absent" if absent else "offline"} for name in cls.names])

        def command(argv, purpose="teardown", stdout=""):
            return event("command", purpose=purpose, argv=argv, exit_code=0, stdout=stdout, stderr="")

        devdax = [{"chardev": "dax0.0", "mode": "devdax"}]
        cls.offline_events += [guard("before_devdax"),
            command(["daxctl", "reconfigure-device", "--mode=devdax", "dax0.0"]),
            command(["daxctl", "list", "-D"], "verify", json.dumps(devdax)), event("devdax", data=devdax),
            guard("before_disable", True), command(["cxl", "disable-region", "region0"]),
            event("disabled", region_exists=True, driver_bound=False), guard("before_destroy", True),
            command(["cxl", "destroy-region", "region0"]), command(["cxl", "list", "-R"], "verify", "[]"),
            command(["daxctl", "list", "-D"], "verify", "[]"),
            event("final", blocks=[{"name": name, "state": "absent"} for name in cls.names],
                  region_exists=False, dax_exists=False, regions=[], dax_devices=[])]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-m3-")
        self.addCleanup(self.temp.cleanup)
        self.shared = Path(self.temp.name)
        self.drain = copy.deepcopy(self.drain_report)
        self.moves = copy.deepcopy(self.drain_events)
        self.offline = copy.deepcopy(self.offline_report)
        self.lifecycle = copy.deepcopy(self.offline_events)

    def write(self):
        for name, data in (("topology.json", self.topology), ("drain.json", self.drain),
                           ("offline.json", self.offline)):
            (self.shared / name).write_text(json.dumps(data))
        for key, value in self.topology_raw.items():
            (self.shared / (key + ".json")).write_text(json.dumps({**self.topology, "data": value}))
        (self.shared / "iomem.txt").write_text(self.iomem)
        for name, text in self.manuals.items():
            (self.shared / name).write_text(text)
        for name, raw in (("drain.jsonl", self.moves), ("offline.jsonl", self.lifecycle)):
            (self.shared / name).write_text("".join(json.dumps(event) + "\n" for event in raw))

    def validate(self):
        self.write()
        return validator.validate_offline(self.shared, self.header["run_id"])

    def life_event(self, kind):
        return next(event for event in self.lifecycle if event["kind"] == kind)

    def batch(self):
        return next(event for event in self.moves if event["kind"] == "batch")

    def test_complete_synthetic_chain_can_be_recomputed(self):
        result = self.validate()
        self.assertEqual(result["gates"], {"G4": "PASS"})
        self.assertEqual(result["drain"]["succeeded_pages"], validator.PAGES)

    def test_capacities_are_parsed_from_raw_guest_meminfo(self):
        self.moves[2]["meminfo"] = "MemAvailable: 1024 kB\n"
        with self.assertRaisesRegex(ValueError, "Capacity"):
            self.validate()

    def test_insufficient_capacity_cannot_be_claimed_pass(self):
        raw = self.moves[2]
        raw["meminfo"] = "MemAvailable: 1024 kB\n"
        raw["mem_available_bytes"] = 1024**2
        self.drain["capacity"]["mem_available_bytes"] = 1024**2
        with self.assertRaisesRegex(ValueError, "Insufficient"):
            self.validate()

    def test_capacity_must_precede_all_migration(self):
        self.moves[2], self.moves[3] = self.moves[3], self.moves[2]
        with self.assertRaisesRegex(ValueError, "Capacity guard"):
            self.validate()

    def test_missing_batch_cannot_reduce_fixed_denominator(self):
        del self.moves[3]
        with self.assertRaises(ValueError):
            self.validate()

    def test_duplicate_page_in_batch_is_rejected(self):
        self.batch()["pages"][1]["page_id"] = self.batch()["pages"][0]["page_id"]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.validate()

    def test_invalid_query_is_not_node_zero(self):
        self.batch()["pages"][0]["query_after_node"] = validator.SENTINEL
        with self.assertRaisesRegex(ValueError, "committed placement"):
            self.validate()

    def test_syscall_failure_status_cannot_be_claimed_valid(self):
        self.batch().update(syscall_rc=-1, syscall_errno=16)
        with self.assertRaisesRegex(ValueError, "valid status"):
            self.validate()

    def test_query_after_is_truth_despite_whole_batch_error(self):
        batch = self.batch()
        batch.update(syscall_rc=-1, syscall_errno=16)
        for page in batch["pages"]:
            page.update(move_status=validator.SENTINEL, move_status_valid=False)
        self.assertEqual(self.validate()["drain"]["succeeded_pages"], validator.PAGES)

    def test_retry_bound_cannot_be_disabled_in_summary(self):
        self.drain["max_attempts_per_page"] = 100
        with self.assertRaisesRegex(ValueError, "fixed profile"):
            self.validate()

    def test_per_page_retry_bound_is_enforced_from_raw_attempts(self):
        nodes, attempts = [1] * validator.PAGES, [0] * validator.PAGES
        event = copy.deepcopy(self.batch())
        event["pages"] = event["pages"][:1]
        event["pages"][0].update(query_after_node=1, move_status=-16)
        for index in range(3):
            event["batch_index"] = index
            self.assertEqual(validator.drain_batch(event, index, nodes, attempts, 0, 1), (1, 0))
        event["batch_index"] = 3
        with self.assertRaisesRegex(ValueError, "bounded per-page"):
            validator.drain_batch(event, 3, nodes, attempts, 0, 1)

    def test_no_slow_admission_while_draining(self):
        self.batch()["target_node"] = 1
        with self.assertRaisesRegex(ValueError, "target"):
            self.validate()

    def test_payload_corruption_fails_gate(self):
        self.moves[-2]["integrity_failures"] = 1
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.validate()

    def test_unreleased_mapping_cannot_reach_offline(self):
        self.moves[-1]["mapping_released"] = False
        with self.assertRaisesRegex(ValueError, "release"):
            self.validate()

    def test_deadline_is_bounded(self):
        self.moves[-1]["elapsed_ms"] = 60001
        with self.assertRaisesRegex(ValueError, "deadline"):
            self.validate()

    def test_wrong_run_is_rejected(self):
        self.lifecycle[-1]["run_id"] = "other-run"
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.validate()

    def test_foreign_block_cannot_enter_offline_plan(self):
        self.life_event("plan")["snapshot"]["blocks"][0]["node"] = 0
        with self.assertRaisesRegex(ValueError, "foreign"):
            self.validate()

    def test_any_online_block_blocks_teardown(self):
        self.life_event("all_offline")["blocks"][0]["state"] = "online"
        with self.assertRaisesRegex(ValueError, "Teardown guard"):
            self.validate()

    def test_missing_offline_readback_is_rejected(self):
        self.life_event("write")["after_state"] = "online"
        with self.assertRaisesRegex(ValueError, "readback"):
            self.validate()

    def test_force_destroy_is_not_an_accepted_command(self):
        command = next(event for event in self.lifecycle if event.get("argv") == ["cxl", "destroy-region", "region0"])
        command["argv"].append("--force")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.validate()

    def test_region_that_survives_teardown_is_rejected(self):
        self.life_event("final")["region_exists"] = True
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.validate()

    def test_final_raw_listing_must_agree(self):
        event = next(event for event in reversed(self.lifecycle) if event.get("argv") == ["cxl", "list", "-R"])
        event["stdout"] = '[{"region":"region0"}]'
        with self.assertRaisesRegex(ValueError, "contradicts"):
            self.validate()

    def test_rollback_evidence_never_passes_as_normal_teardown(self):
        self.offline["rollback_attempted"] = True
        with self.assertRaisesRegex(ValueError, "status"):
            self.validate()

    def test_truncated_raw_file_is_rejected(self):
        self.write()
        path = self.shared / "offline.jsonl"
        path.write_text(path.read_text().rstrip("\n"))
        with self.assertRaisesRegex(ValueError, "truncated"):
            validator.validate_offline(self.shared, self.header["run_id"])

    def test_manual_copy_must_match_recorded_hash(self):
        self.write()
        (self.shared / next(iter(self.manuals))).write_text("changed manual")
        with self.assertRaisesRegex(ValueError, "manual proof"):
            validator.validate_offline(self.shared, self.header["run_id"])


if __name__ == "__main__":
    unittest.main()
