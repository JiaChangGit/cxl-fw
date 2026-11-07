"""Synthetic G5 evidence tests validator rejection; these are not Guest runs."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_trace as validator
import test_migration_evidence as topology_fixtures


class TraceEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        topology_fixtures.MigrationEvidenceTests.setUpClass()
        source = topology_fixtures.MigrationEvidenceTests
        cls.topology, cls.topology_raw = copy.deepcopy(source.topology), copy.deepcopy(source.raw)
        cls.header = {"schema_version": 1, "run_id": source.header["run_id"], "scenario": "trace",
                      "evidence_basis": "guest_kernel_measured"}
        cls.iomem = "200000000-23fffffff : dax0.0\n  200000000-23fffffff : System RAM (kmem)\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-g5-")
        self.addCleanup(self.temp.cleanup)
        self.shared = Path(self.temp.name)
        self.pid, self.base = 4321, 0x70000000
        self.report = {**self.header, "status": "PASS", "raw_file": "trace_batches.jsonl",
                       "passed_cases": 5, "managed_bytes": validator.BYTES, "page_count": 64,
                       "page_size": 4096, "fast_node": 0, "slow_node": 1,
                       "integrity_failures": 0, "query_missing_count": 0}
        scan = {"checked_bytes": validator.BYTES, "integrity_failures": 0, "mapping_live": True}
        self.events = [{**self.header, "kind": "environment", "pid": self.pid,
                        "mapping_base": hex(self.base), "page_count": 64, "page_size": 4096,
                        "generation": 11, "fast_node": 0, "slow_node": 1, **scan}]
        nodes = [0] * 64
        for index, name in enumerate(validator.CASES):
            ids = list(range(64 if index < 2 else 32)) + ([None] if index == 2 else [])
            target = 1 if index in (0, 2) else 0 if index in (1, 3) else (1 << 31) - 1
            before = nodes[:]
            if index < 4:
                for page in ids:
                    if page is not None:
                        nodes[page] = target
            self.events.append({**self.header, "kind": "batch", "id": index, "name": name,
                "passed": True, "marker_error": "",
                "requested_node": target, "request_page_ids": ids,
                "request_vas": [hex(self.base + page * 4096) if page is not None else "0x1000" for page in ids],
                "query_before_rc": 0, "query_before_errno": 0, "query_before_nodes": before,
                "query_after_rc": 0, "query_after_errno": 0, "query_after_nodes": nodes[:],
                "syscall_rc": 0 if index < 4 else -1, "syscall_errno": 0 if index < 4 else 19,
                "move_status": ([target] * (len(ids) - (index == 2)) + ([-14] if index == 2 else []))
                               if index < 4 else [validator.SENTINEL] * len(ids),
                "move_status_valid": [index < 4] * len(ids), **scan})
        self.events.append({**self.header, "kind": "complete", **scan})
        self.trace_lines = []

        def trace(event, body):
            self.trace_lines.append(f"         tierlab-{self.pid} [000] .... {100 + len(self.trace_lines) / 1000:.6f}: {event}: {body}\n")

        for index, count in enumerate((64, 64, 32, 32, 0)):
            trace("tracing_mark_write", f"tierlab {self.header['run_id']} {index} begin")
            if count:
                trace("mm_migrate_pages_start", "mode=MIGRATE_SYNC reason=syscall_or_cpuset")
                trace("mm_migrate_pages", f"nr_succeeded={count} nr_failed=0 nr_thp_succeeded=0 nr_thp_failed=0 nr_thp_split=0 nr_split=0 mode=MIGRATE_SYNC reason=syscall_or_cpuset")
            trace("tracing_mark_write", f"tierlab {self.header['run_id']} {index} end")
        stats = "entries: 0\noverrun: 0\ncommit overrun: 0\nbytes: 0\ndropped events: 0\nread events: 18\n"
        self.collector = {**self.header, "status": "PASS", "pid": self.pid, "clock": "mono",
                          "enabled_events": validator.EVENTS, "stats_before": {"cpu0": stats},
                          "stats_after": {"cpu0": stats}, "eof_drained": True,
                          "target_exit_code": 0, "cleanup_success": True}
        self.formats = {event.split(":")[1]: f"name: {event.split(':')[1]}\nfield:int common_pid;\nfield:enum migrate_mode mode;\nfield:int reason;\n"
                        for event in validator.EVENTS}
        self.config = "CONFIG_MIGRATION=y\nCONFIG_TRACEPOINTS=y\nCONFIG_EVENT_TRACING=y\nCONFIG_FTRACE=y\n"
        self.upstream_tap = "TAP version 13\n1..1\n# Starting 1 tests from 1 test cases.\nok 1 migration.private_anon\n# PASSED: 1 / 1 tests passed.\n"
        self.own_tap = "TAP version 13\n1..5\n" + "".join(f"ok {i + 1} {name}\n" for i, name in enumerate(validator.CASES))
        self.binary = b"synthetic upstream binary fixture, not executable\n"
        self.upstream = {**self.header, "status": "PASS", "testcase": "migration.private_anon",
                         "argv": ["/mnt/host/upstream-migration", "-r", "migration.private_anon"],
                         "exit_code": 0, "binary_sha256": hashlib.sha256(self.binary).hexdigest(),
                         "results": {"planned": 1, "passed": 1, "failed": 0, "skipped": 0}}
        self.build = {"status": "BUILT", "kernel_version": "6.18.56", "source_sha256": validator.SOURCE_SHA,
                      "binary_sha256": self.upstream["binary_sha256"],
                      "inputs_sha256": {"tools/testing/selftests/mm/migration.c": "a" * 64}}

    def write(self):
        (self.shared / "topology.json").write_text(json.dumps(self.topology))
        for key, value in self.topology_raw.items():
            (self.shared / (key + ".json")).write_text(json.dumps({**self.topology, "data": value}))
        (self.shared / "iomem.txt").write_text(self.iomem)
        (self.shared / "trace_batches.jsonl").write_text("".join(json.dumps(event) + "\n" for event in self.events))
        trace = "".join(self.trace_lines)
        self.collector.update(trace_sha256=validator.digest(trace), trace_bytes=len(trace.encode()),
                              kernel_config_sha256=validator.digest(self.config),
                              format_sha256={key: validator.digest(value) for key, value in self.formats.items()})
        self.upstream["tap_sha256"] = validator.digest(self.upstream_tap)
        for name, value in (("trace_batches.json", self.report), ("collector.json", self.collector),
                            ("upstream-selftest.json", self.upstream), ("upstream-build.json", self.build)):
            (self.shared / name).write_text(json.dumps(value))
        for name, value in (("trace.log", trace), ("kernel.config", self.config),
                            ("trace-selftest.tap", self.own_tap), ("upstream-selftest.tap", self.upstream_tap)):
            (self.shared / name).write_text(value)
        (self.shared / "upstream-migration").write_bytes(self.binary)
        (self.shared / "trace_formats").mkdir(exist_ok=True)
        for event, value in self.formats.items():
            (self.shared / "trace_formats" / (event + ".format")).write_text(value)

    def validate(self):
        self.write()
        return validator.validate_trace(self.shared, self.header["run_id"])

    def test_complete_synthetic_chain_recomputes_aggregate_counts(self):
        result = self.validate()
        self.assertEqual(result["gates"], {"G5": "PASS"})
        self.assertEqual([batch["trace_succeeded_pages"] for batch in result["batches"]], [64, 64, 32, 32, 0])

    def test_wrong_batch_marker_is_rejected(self):
        self.trace_lines[0] = self.trace_lines[0].replace("0 begin", "1 begin")
        with self.assertRaisesRegex(ValueError, "batch marker"):
            self.validate()

    def test_missing_query_is_rejected(self):
        del self.events[1]["query_after_nodes"]
        with self.assertRaisesRegex(ValueError, "query residency"):
            self.validate()

    def test_partial_query_cannot_shrink_denominator(self):
        self.events[1]["query_after_nodes"].pop()
        with self.assertRaisesRegex(ValueError, "query residency"):
            self.validate()

    def test_truncated_trace_is_rejected_even_with_recomputed_hash(self):
        self.trace_lines[-1] = self.trace_lines[-1].rstrip("\n")
        with self.assertRaisesRegex(ValueError, "truncated trace"):
            self.validate()

    def test_deleted_success_trace_is_rejected_with_recomputed_hash(self):
        del self.trace_lines[2]
        with self.assertRaisesRegex(ValueError, "Unpaired"):
            self.validate()

    def test_missing_entire_migration_call_is_detected_from_query(self):
        del self.trace_lines[1:3]
        with self.assertRaisesRegex(ValueError, "Missing or contradictory"):
            self.validate()

    def test_wrong_pid_is_rejected(self):
        self.trace_lines[2] = self.trace_lines[2].replace("tierlab-4321", "tierlab-4322")
        with self.assertRaisesRegex(ValueError, "PID"):
            self.validate()

    def test_wrong_migration_reason_is_rejected(self):
        self.trace_lines[1] = self.trace_lines[1].replace("syscall_or_cpuset", "memory_hotplug")
        with self.assertRaisesRegex(ValueError, "reason"):
            self.validate()

    def test_trace_counter_cannot_replace_kernel_query_truth(self):
        self.events[1]["query_after_nodes"][0] = 0
        with self.assertRaisesRegex(ValueError, "actual placement"):
            self.validate()

    def test_broken_query_chain_is_rejected(self):
        self.events[2]["query_before_nodes"][0] = 0
        with self.assertRaisesRegex(ValueError, "query residency chain"):
            self.validate()

    def test_buffer_overrun_is_rejected(self):
        self.collector["stats_after"]["cpu0"] = self.collector["stats_after"]["cpu0"].replace("overrun: 0", "overrun: 1", 1)
        with self.assertRaisesRegex(ValueError, "overrun"):
            self.validate()

    def test_dropped_events_are_rejected(self):
        self.collector["stats_after"]["cpu0"] = self.collector["stats_after"]["cpu0"].replace("dropped events: 0", "dropped events: 1")
        with self.assertRaisesRegex(ValueError, "dropped"):
            self.validate()

    def test_missing_loss_counter_is_rejected(self):
        self.collector["stats_after"]["cpu0"] = "overrun: 0\n"
        with self.assertRaisesRegex(ValueError, "loss counter"):
            self.validate()

    def test_collector_not_fully_drained_is_rejected(self):
        self.collector["eof_drained"] = False
        with self.assertRaisesRegex(ValueError, "collection"):
            self.validate()

    def test_collector_cleanup_failure_is_rejected(self):
        self.collector["cleanup_success"] = False
        with self.assertRaisesRegex(ValueError, "cleanup"):
            self.validate()

    def test_invalid_node_must_not_have_migration_events(self):
        self.trace_lines.insert(-1, self.trace_lines[1].replace("100.001000", "100.016500"))
        with self.assertRaises(ValueError):
            self.validate()

    def test_mixed_invalid_va_requires_real_per_page_error(self):
        self.events[3]["move_status"][-1] = 1
        with self.assertRaisesRegex(ValueError, "mixed-VA"):
            self.validate()

    def test_invalid_syscall_status_cannot_be_claimed_valid(self):
        self.events[5]["move_status_valid"][0] = True
        with self.assertRaisesRegex(ValueError, "status validity"):
            self.validate()

    def test_full_payload_scan_is_required(self):
        self.events[2]["checked_bytes"] = 4096
        with self.assertRaisesRegex(ValueError, "incomplete batch scan"):
            self.validate()

    def test_raw_failed_batch_cannot_be_claimed_pass(self):
        self.events[1]["passed"] = False
        with self.assertRaisesRegex(ValueError, "Batch failure"):
            self.validate()

    def test_marker_write_error_cannot_be_claimed_pass(self):
        self.events[1]["marker_error"] = "Trace marker write failed"
        with self.assertRaisesRegex(ValueError, "marker error"):
            self.validate()

    def test_mapping_must_remain_live(self):
        self.events[-1]["mapping_live"] = False
        with self.assertRaisesRegex(ValueError, "lifetime"):
            self.validate()

    def test_skipped_upstream_selftest_is_not_pass(self):
        self.upstream_tap = self.upstream_tap.replace("migration.private_anon\n", "migration.private_anon # SKIP unavailable\n")
        with self.assertRaisesRegex(ValueError, "skipped"):
            self.validate()

    def test_wrong_upstream_binary_is_rejected(self):
        self.build["binary_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.validate()

    def test_trace_hash_detects_post_collection_changes(self):
        self.write()
        with (self.shared / "trace.log").open("a") as stream:
            stream.write("changed\n")
        with self.assertRaisesRegex(ValueError, "Trace hash"):
            validator.validate_trace(self.shared, self.header["run_id"])

    def test_wrong_format_file_is_rejected(self):
        self.write()
        (self.shared / "trace_formats/mm_migrate_pages.format").write_text("different format\n")
        with self.assertRaisesRegex(ValueError, "format hash"):
            validator.validate_trace(self.shared, self.header["run_id"])

    def test_kernel_trace_support_is_required(self):
        self.config = self.config.replace("CONFIG_EVENT_TRACING=y", "# CONFIG_EVENT_TRACING is not set")
        with self.assertRaisesRegex(ValueError, "kernel configuration"):
            self.validate()

    def test_negative_controls_reject_copies_and_preserve_original(self):
        verified = self.validate()
        path = self.shared / "trace.log"
        original = path.read_bytes()
        controls = validator.trace_negative_controls(self.shared, self.header["run_id"], verified)
        self.assertEqual([value["name"] for value in controls],
                         ["missing_migration_event", "wrong_batch_marker", "truncated_trace"])
        self.assertTrue(all(value["status"] == "REJECTED" and value["evidence_basis"] == "modeled"
                            for value in controls))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(validator.validate_trace(self.shared, self.header["run_id"])["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
