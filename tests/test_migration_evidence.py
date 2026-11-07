"""Synthetic M1 evidence fixtures exercise rejection; they are not Guest evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "migration_validator", Path(__file__).resolve().parents[1] / "scripts/validate_migration.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class MigrationEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.header = {"schema_version": 1, "run_id": "synthetic-unit-fixture", "scenario": "migrate"}
        start, size = 8 * 1024**3, 1024**3
        blocks = [{"name": f"memory{i}", "start": start + i * size // 8,
                   "size": size // 8, "node": 1, "state": "online", "zone": "Movable"}
                  for i in range(8)]
        cls.topology = {**cls.header, "status": "PASS", "fast_node": 0, "slow_node": 1,
                        "region_type": "ram", "interleave_ways": 1,
                        "resource_start": start, "resource_size": size, "memdev": "mem0",
                        "region": "region0", "daxdev": "dax0.0", "root_decoder": "decoder0.0",
                        "memdev_volatile_bytes": size,
                        "blocks": blocks, "raw_files": {k: k + ".json" for k in
                         ("memdevs", "decoders", "regions", "dax_devices", "sysfs_before", "sysfs_after")}}
        cls.topology["raw_files"]["iomem"] = "iomem.txt"
        raw_blocks = [{**b, "node_links": ["node1"], "valid_zones": "Movable"} for b in blocks]
        cls.raw = {
            "memdevs": [{"memdev": "mem0", "ram_size": size, "pmem_size": 0}],
            "decoders": [{"decoder": "decoder0.0", "resource": start, "size": 4 * size,
                          "interleave_ways": 1, "volatile_capable": True}],
            "regions": [{"region": "region0", "type": "ram", "interleave_ways": 1,
                         "resource": start, "size": size, "decode_state": "commit"}],
            "dax_devices": [{"chardev": "dax0.0", "mode": "system-ram", "target_node": 1}],
            "sysfs_before": {"blocks": []},
            "sysfs_after": {"dax": {"name": "dax0.0", "resource": start, "size": size, "target_node": 1, "driver": "kmem"},
                            "region": {"name": "region0", "resource": start, "size": size,
                                       "mode": "ram", "interleave_ways": 1},
                            "blocks": raw_blocks,
                            "nodes": [{"node": 0, "cpulist": "0-3", "meminfo": "Node 0 MemTotal: 3145728 kB"}]},
        }
        cls.events = []

        def event(kind, **values):
            cls.events.append({**cls.header, "kind": kind,
                               "evidence_basis": "guest_kernel_measured", **values})

        def snapshot(stage, node):
            event("snapshot", stage=stage, nodes=[node] * validator.PAGES,
                  query_rc=0, query_errno=0, integrity_failures=0)

        def pattern(stage, generation):
            event("pattern", stage=stage, generation=generation,
                  checked_bytes=validator.BYTES, integrity_failures=0)

        def batches(leg, source, target):
            for batch in range(validator.PAGES // 256):
                pages = [{"page_id": batch * 256 + i, "query_before_node": source,
                          "move_status": target, "move_status_valid": True, "query_after_node": target}
                         for i in range(256)]
                event("batch", leg=leg, batch_index=batch, source_node=source, target_node=target,
                      syscall_rc=0, syscall_errno=0, before_query_rc=0, before_query_errno=0,
                      after_query_rc=0, after_query_errno=0, pages=pages)

        pattern("initial", 0)
        snapshot("initial", 0)
        batches(0, 0, 1)
        snapshot("slow", 1)
        pattern("slow_preserved", 0)
        pattern("slow_rewrite", 1)
        batches(1, 1, 0)
        snapshot("final", 0)
        pattern("final", 1)
        controls = [{"name": name, "passed": True,
                     "raw": {"syscall_rc": -1, "syscall_errno": 22,
                             "move_status": validator.SENTINEL, "move_status_valid": False,
                             "query_before_node": 0 if name == "invalid_target" else -14,
                             "query_after_node": 0 if name == "invalid_target" else -14,
                             "query_before_rc": 0, "query_before_errno": 0,
                             "query_after_rc": 0, "query_after_errno": 0,
                             "requested_node": (1 << 31) - 1 if name == "invalid_target" else 0}}
                    for name in ("invalid_target", "invalid_va", "unmapped_va")]
        for control in controls:
            event("negative", **control)
        cls.report = {**cls.header, "status": "PASS", "managed_bytes": validator.BYTES,
                      "page_size": 4096, "fast_node": 0, "slow_node": 1,
                      "integrity_failures": 0, "query_missing_count": 0,
                      "initial_node_counts": {"0": validator.PAGES},
                      "slow_node_counts": {"1": validator.PAGES},
                      "final_node_counts": {"0": validator.PAGES},
                      "legs": [{"source_node": s, "target_node": t, "eligible_pages": validator.PAGES,
                                "attempted_pages": validator.PAGES, "succeeded_pages": validator.PAGES,
                                "success_ratio": 1.0} for s, t in ((0, 1), (1, 0))],
                      "negative_controls": controls, "raw_file": "migration.jsonl"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-m1-")
        self.addCleanup(self.temp.cleanup)
        self.shared = Path(self.temp.name)
        self.top = copy.deepcopy(self.topology)
        self.data = copy.deepcopy(self.raw)
        self.summary = copy.deepcopy(self.report)
        self.trace = copy.deepcopy(self.events)
        self.iomem = "200000000-23fffffff : dax0.0\n  200000000-23fffffff : System RAM (kmem)\n"

    def validate(self):
        for name, value in (("topology.json", self.top), ("migration.json", self.summary)):
            (self.shared / name).write_text(json.dumps(value))
        for key, data in self.data.items():
            (self.shared / (key + ".json")).write_text(json.dumps({**self.header, "data": data}))
        (self.shared / "iomem.txt").write_text(self.iomem)
        (self.shared / "migration.jsonl").write_text("\n".join(json.dumps(e) for e in self.trace) + "\n")
        return validator.validate_migration(self.shared, self.header["run_id"])

    def test_complete_synthetic_fixture_can_be_recomputed(self):
        result = self.validate()
        self.assertEqual(result["legs"][0]["succeeded_pages"], validator.PAGES)

    def test_failure_cannot_be_hidden_by_removing_it_from_denominator(self):
        self.summary["legs"][0]["eligible_pages"] -= 1
        with self.assertRaisesRegex(ValueError, "eligibility"):
            self.validate()

    def test_unknown_or_wrong_snapshot_is_rejected(self):
        next(e for e in self.trace if e.get("stage") == "slow" and e["kind"] == "snapshot")["nodes"][0] = -14
        with self.assertRaisesRegex(ValueError, "residency"):
            self.validate()

    def test_missing_raw_batch_is_not_a_pass(self):
        self.trace = [e for e in self.trace if not (e["kind"] == "batch" and e["leg"] == 0 and e["batch_index"] == 63)]
        with self.assertRaisesRegex(ValueError, "counts"):
            self.validate()

    def test_wrong_raw_run_is_rejected(self):
        self.trace[0]["run_id"] = "other-run"
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.validate()

    def test_missing_raw_file_is_rejected(self):
        self.validate()
        (self.shared / "migration.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "evidence path"):
            validator.validate_migration(self.shared, self.header["run_id"])

    def test_duplicate_page_attempt_is_rejected(self):
        batch = next(e for e in self.trace if e["kind"] == "batch")
        batch["pages"][1]["page_id"] = batch["pages"][0]["page_id"]
        with self.assertRaisesRegex(ValueError, "Repeated"):
            self.validate()

    def test_sentinal_cannot_be_claimed_valid(self):
        page = next(e for e in self.trace if e["kind"] == "batch")["pages"][0]
        page["move_status"] = validator.SENTINEL
        with self.assertRaisesRegex(ValueError, "validity"):
            self.validate()

    def test_false_eligible_ratio_is_rejected(self):
        self.summary["legs"][0]["success_ratio"] = .95
        with self.assertRaisesRegex(ValueError, "ratio"):
            self.validate()

    def test_block_outside_resource_or_nonmovable_is_rejected(self):
        for field, value in (("start", self.top["resource_start"] - 1), ("zone", "Normal")):
            with self.subTest(field=field):
                previous = self.top["blocks"][0][field]
                self.top["blocks"][0][field] = value
                with self.assertRaises(ValueError):
                    self.validate()
                self.top["blocks"][0][field] = previous

    def test_dax_must_be_owned_by_the_observed_ram_region(self):
        self.data["sysfs_after"]["region"]["size"] //= 2
        with self.assertRaisesRegex(ValueError, "owned"):
            self.validate()

    def test_raw_region_range_and_commit_must_match_sysfs(self):
        for field, value in (("resource", 0), ("size", 4096), ("decode_state", "reset")):
            with self.subTest(field=field):
                original = self.data["regions"][0][field]
                self.data["regions"][0][field] = value
                with self.assertRaisesRegex(ValueError, "Raw region"):
                    self.validate()
                self.data["regions"][0][field] = original

    def test_root_decoder_must_contain_the_ram_range(self):
        for field, value in (("resource", 0), ("size", 4096), ("volatile_capable", False)):
            with self.subTest(field=field):
                original = self.data["decoders"][0][field]
                self.data["decoders"][0][field] = value
                with self.assertRaisesRegex(ValueError, "Root decoder"):
                    self.validate()
                self.data["decoders"][0][field] = original

    def test_iomem_requires_exact_owner_and_complete_kmem_coverage(self):
        for iomem in ("200000000-23fffffff : System RAM\n",
                      self.iomem.replace("dax0.0", "dax1.0"),
                      self.iomem.replace("23fffffff", "23ffffffe"),
                      "200000000-23fffffff : dax0.0\n  200000001-23fffffff : System RAM (kmem)\n",
                      self.iomem + "200000000-200000fff : System RAM (kmem)\n"):
            with self.subTest(iomem=iomem):
                self.iomem = iomem
                with self.assertRaisesRegex(ValueError, "[Ii]omem"):
                    self.validate()

    def test_iomem_allows_contiguous_kmem_segments(self):
        self.iomem = ("200000000-23fffffff : dax0.0\n"
                      "  200000000-21fffffff : System RAM (kmem)\n"
                      "  220000000-23fffffff : System RAM (kmem)\n")
        self.assertEqual(self.validate()["status"], "PASS")

    def test_wrong_schema_and_malformed_shape_are_rejected(self):
        self.summary["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "schema"):
            self.validate()
        self.summary["schema_version"] = 1
        self.top["raw_files"] = None
        with self.assertRaises(ValueError):
            self.validate()

    def test_negative_pass_without_an_error_is_rejected(self):
        control = self.summary["negative_controls"][0]
        control["raw"].update(syscall_rc=0, syscall_errno=0, move_status=0, move_status_valid=True)
        event = next(e for e in self.trace if e["kind"] == "negative")
        event["raw"] = copy.deepcopy(control["raw"])
        with self.assertRaisesRegex(ValueError, "actual error"):
            self.validate()

    def test_negative_error_must_preserve_managed_placement(self):
        control = self.summary["negative_controls"][0]
        control["raw"]["query_after_node"] = 1
        next(e for e in self.trace if e["kind"] == "negative")["raw"] = copy.deepcopy(control["raw"])
        with self.assertRaisesRegex(ValueError, "placement"):
            self.validate()

    def test_cxl_write_cannot_be_reordered_after_return_to_fast(self):
        event = next(e for e in self.trace if e.get("stage") == "slow_rewrite")
        self.trace.remove(event)
        self.trace.append(event)
        with self.assertRaisesRegex(ValueError, "order"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
