"""Synthetic evaluator fixtures test rejection; none are Guest execution evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_policy as validator


class PolicyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.current = [1] * validator.PAGES
        self.hot = (list(range(validator.HOT_PAGES)),
                    list(range(validator.HOT_PAGES, 2 * validator.HOT_PAGES)))
        self.counts = [0] * validator.PAGES
        self.counts[0] = validator.OPERATIONS
        snapshot = {"rc": 0, "errno": 0, "nodes": self.current.copy()}
        counters = dict.fromkeys(validator.WINDOW_COUNTERS, 0)
        self.event = {"kind": "epoch", "epoch": 0, "counts": self.counts.copy(),
                      "before": copy.deepcopy(snapshot), "window_end": copy.deepcopy(snapshot),
                      "after": copy.deepcopy(snapshot), "batches": [],
                      "trace_hash": "synthetic-trace", "access_integrity_failures": 0,
                      "scan_bytes": 0, "scan_integrity_failures": 0,
                      "window_counters_before": counters.copy(),
                      "window_counters_end": counters.copy(),
                      "awfr_during_epoch": 0.0, "ready_hfr": 0.0,
                      "attempted_pages": 0, "succeeded_pages": 0}

    @staticmethod
    def move(page=0, source=1, target=0):
        return {"target_node": target, "direction": "promote" if target == 0 else "demote",
                "syscall_rc": 0, "syscall_errno": 0,
                "before_query_rc": 0, "before_query_errno": 0,
                "after_query_rc": 0, "after_query_errno": 0,
                "pages": [{"page_id": page, "query_before_node": source,
                           "move_status": target, "move_status_valid": True,
                           "query_after_node": target}]}

    def validate(self):
        return validator.validate_epoch(self.event, self.current, 0, 1, "B2", 0,
                                         self.hot, (self.counts, "synthetic-trace"))

    def add_promotion(self):
        self.event["batches"] = [self.move()]
        self.event["after"]["nodes"][0] = 0
        self.event["ready_hfr"] = 1 / validator.HOT_PAGES
        self.event["attempted_pages"] = self.event["succeeded_pages"] = 1

    def test_after_promotion_does_not_rewrite_past_accesses(self):
        self.add_promotion()
        hfr, awfr, attempted, succeeded = self.validate()
        self.assertEqual((awfr, attempted, succeeded), (0, 1, 1))
        self.assertEqual(hfr, 1 / validator.HOT_PAGES)

    def test_after_based_awfr_is_rejected(self):
        self.add_promotion()
        self.event["awfr_during_epoch"] = 1.0
        with self.assertRaisesRegex(ValueError, "AWFR"):
            self.validate()

    def test_residency_cache_cannot_replace_kernel_after(self):
        self.add_promotion()
        self.event["after"]["nodes"][0] = 1
        with self.assertRaisesRegex(ValueError, "After-query"):
            self.validate()

    def test_missing_count_is_not_zero(self):
        self.event["counts"].pop()
        with self.assertRaisesRegex(ValueError, "counts"):
            self.validate()

    def test_counts_must_match_replayed_trace(self):
        self.event["counts"][0] -= 1
        self.event["counts"][1] += 1
        with self.assertRaisesRegex(ValueError, "deterministic trace"):
            self.validate()

    def test_failed_query_cannot_be_hidden_by_plausible_nodes(self):
        self.event["before"]["rc"] = -1
        self.event["before"]["errno"] = 1
        with self.assertRaisesRegex(ValueError, "Failed kernel"):
            self.validate()

    def test_unknown_page_placement_is_rejected(self):
        self.event["window_end"]["nodes"][17] = validator.SENTINEL
        with self.assertRaisesRegex(ValueError, "residency query"):
            self.validate()

    def test_equal_endpoints_do_not_hide_observed_interference(self):
        self.event["window_counters_end"]["pgmigrate_success"] += 1
        with self.assertRaisesRegex(ValueError, "INVALID_RESIDENCY_WINDOW"):
            self.validate()

    def test_changed_window_end_is_invalid_even_if_after_restored(self):
        self.event["window_end"]["nodes"][3] = 0
        with self.assertRaisesRegex(ValueError, "INVALID_RESIDENCY_WINDOW"):
            self.validate()

    def test_payload_corruption_is_not_a_pass(self):
        self.event["access_integrity_failures"] = 1
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.validate()

    def test_full_scan_cannot_be_skipped_at_phase_boundary(self):
        self.event["epoch"] = 29
        with self.assertRaisesRegex(ValueError, "full payload scan"):
            validator.validate_epoch(self.event, self.current, 0, 1, "B2", 29,
                                       self.hot, (self.counts, "synthetic-trace"))

    def test_sentinel_cannot_be_labeled_valid(self):
        move = self.move()
        move["pages"][0]["move_status"] = validator.SENTINEL
        with self.assertRaisesRegex(ValueError, "Sentinel"):
            validator.batch(move, self.current, 0, 1)

    def test_whole_syscall_failure_still_reconciles_query_truth(self):
        move = self.move()
        move.update(syscall_rc=-1, syscall_errno=1)
        move["pages"][0].update(move_status=validator.SENTINEL, move_status_valid=False,
                                query_after_node=1)
        ids, succeeded = validator.batch(move, self.current, 0, 1)
        self.assertEqual((ids, succeeded, self.current[0]), ({0}, 0, 1))

    def test_failed_demotion_does_not_free_fast_capacity(self):
        self.current[:validator.FAST_CAP] = [0] * validator.FAST_CAP
        failed = self.move(page=0, source=0, target=1)
        failed["pages"][0].update(move_status=-16, query_after_node=0)
        move = self.move(page=validator.FAST_CAP)
        with self.assertRaisesRegex(ValueError, "fast capacity"):
            validator.apply_batches([failed, move], self.current, 0, 1, "B2", 0, self.hot[0])

    def test_shared_attempt_budget_counts_failed_attempts(self):
        pages = [self.move(page=index)["pages"][0]
                 for index in range(validator.ATTEMPT_BUDGET + 1)]
        for page in pages:
            page.update(move_status=-16, query_after_node=1)
        moves = []
        for index in range(0, len(pages), 256):
            move = self.move()
            move["pages"] = pages[index:index + 256]
            moves.append(move)
        with self.assertRaisesRegex(ValueError, "shared budget"):
            validator.apply_batches(moves, self.current, 0, 1, "B2", 0, self.hot[0])

    def test_duplicate_attempts_cannot_inflate_success(self):
        move = self.move()
        with self.assertRaisesRegex(ValueError, "actual placement|Repeated"):
            validator.apply_batches([move, move], self.current, 0, 1, "B2", 0, self.hot[0])

    def test_demote_after_promote_is_rejected(self):
        moves = [self.move(), self.move(0, 0, 1)]
        with self.assertRaisesRegex(ValueError, "Demotion follows"):
            validator.apply_batches(moves, self.current, 0, 1, "B2", 0, self.hot[0])

    def test_static_baseline_cannot_adapt_after_phase_shift(self):
        with self.assertRaisesRegex(ValueError, "Static baseline"):
            validator.apply_batches([self.move()], self.current, 0, 1, "B1", 30, self.hot[0])

    def test_environment_must_rule_out_automatic_migration_and_swap(self):
        environment = {"kind": "environment", "numa_balancing": "unavailable",
                       "ksm_run": "0", "demotion_enabled": "false", "swap_total_kb": 0}
        validator.check_environment(environment)
        environment["swap_total_kb"] = 1024
        with self.assertRaisesRegex(ValueError, "invalidates"):
            validator.check_environment(environment)

    def test_splitmix_unsigned_sequence_is_stable(self):
        rng = validator.Splitmix64(0)
        self.assertEqual([rng.next() for _ in range(3)],
                         [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4, 0x06C45D188009454F])

    def test_workload_golden_trace_spans_phase_switch(self):
        hot, trace = validator.workload_trace(1)
        self.assertEqual(hot[0][:8], [7082, 9131, 529, 11828, 27475, 12396, 13282, 23079])
        self.assertFalse(set(hot[0]) & set(hot[1]))
        self.assertEqual([trace[index][1] for index in (0, 29, 30, 59)],
                         ["8feb7136a18a8a89", "c9d64f5720f89840", "24c3456023a85358", "9a44d8c956bbf8d8"])
        self.assertTrue(all(sum(counts) == validator.OPERATIONS for counts, _ in trace))

    def test_bounded_sampling_rejects_modulo_bias_tail(self):
        rng = validator.Splitmix64(0)
        values = iter([5, 16])  # threshold=6 for bound 10
        rng.next = lambda: next(values)
        self.assertEqual(rng.bounded(10), 6)

    def test_stream_rejects_wrong_scope_and_truncated_record(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-m2-") as directory:
            root = Path(directory)
            path = root / "policy.jsonl"
            event = {"schema_version": 1, "run_id": "fixture", "scenario": "policy",
                     "evidence_basis": "guest_kernel_measured", "kind": "environment"}
            path.write_text(json.dumps({**event, "run_id": "other"}) + "\n")
            with self.assertRaisesRegex(ValueError, "run_id"):
                list(validator.events(root, "policy.jsonl", "fixture"))
            path.write_text(json.dumps(event))
            with self.assertRaisesRegex(ValueError, "Truncated"):
                list(validator.events(root, "policy.jsonl", "fixture"))


if __name__ == "__main__":
    unittest.main()
