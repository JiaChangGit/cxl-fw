"""Collector contracts with pipes/fixtures; never operate Host tracefs."""
import argparse
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

GUEST = Path(__file__).resolve().parents[1] / "guest"
sys.path.insert(0, str(GUEST))
spec = importlib.util.spec_from_file_location("guest_trace", GUEST / "trace.py")
trace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace)

STATS = "entries: 0\noverrun: 0\ncommit overrun: 0\nbytes: 0\ndropped events: 0\nread events: 17\n"
TAP = "TAP version 13\n1..1\n# Starting 1 tests from 1 test cases.\nok 1 migration.private_anon\n"


class CollectorTests(unittest.TestCase):
    def test_loss_counters_cover_every_cpu_and_required_field(self):
        trace.verify_stats({"cpu0": STATS, "cpu1": STATS}, {"cpu0": STATS, "cpu1": STATS})
        for field in trace.LOSS_FIELDS:
            with self.subTest(field=field):
                bad = STATS.replace(field + ": 0", field + ": 1")
                with self.assertRaisesRegex(ValueError, "lost"):
                    trace.verify_stats({"cpu0": STATS}, {"cpu0": bad})
                with self.assertRaisesRegex(ValueError, "Missing"):
                    trace.parse_stats("\n".join(line for line in STATS.splitlines() if not line.startswith(field + ":")))
        with self.assertRaisesRegex(ValueError, "coverage"):
            trace.verify_stats({"cpu0": STATS, "cpu1": STATS}, {"cpu0": STATS})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            trace.parse_stats(STATS + "overrun: 0\n")
        with self.assertRaisesRegex(ValueError, "lost"):
            trace.verify_stats({"cpu0": STATS.replace("overrun: 0", "overrun: 1")}, {"cpu0": STATS})

    def test_nonblocking_drain_retains_all_records_and_partial_write(self):
        reader, writer = os.pipe2(os.O_NONBLOCK)
        try:
            records = b"first event\nsecond event\n"
            os.write(writer, records[:8])
            output = io.BytesIO()
            self.assertEqual(trace.consume_available(reader, output), 8)
            os.write(writer, records[8:])
            self.assertEqual(trace.consume_available(reader, output), len(records) - 8)
            self.assertEqual(trace.consume_available(reader, output), 0)
            self.assertEqual(output.getvalue(), records)
        finally:
            os.close(reader)
            os.close(writer)

    def test_pipe_read_failure_is_not_treated_as_complete(self):
        with patch.object(trace.os, "read", side_effect=OSError(errno.EIO, "Reader failure")):
            with self.assertRaises(OSError):
                trace.consume_available(99, io.BytesIO())

    def test_baseline_requires_one_real_selected_test(self):
        self.assertEqual(trace.tap_results(TAP), {"planned": 1, "passed": 1, "failed": 0, "skipped": 0})
        for bad in (TAP.replace("1..1", "1..0"), TAP.replace("private_anon", "shared_anon"),
                    TAP + "ok 2 migration.private_anon\n", TAP.replace("ok 1", "ok 7")):
            with self.subTest(text=bad):
                with self.assertRaises(ValueError):
                    trace.tap_results(bad)
        skipped = trace.tap_results(TAP.replace("private_anon\n", "private_anon # SKIP no NUMA\n"))
        self.assertEqual(skipped["passed"], 0)
        self.assertEqual(skipped["skipped"], 1)

    def test_real_baseline_skip_is_blocked_and_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            binary = output / "upstream-migration"
            binary.write_text("#!/bin/sh\nprintf 'TAP version 13\\n1..1\\nok 1 migration.private_anon # SKIP no NUMA\\n'\nexit 4\n")
            binary.chmod(0o755)
            with self.assertRaises(trace.Blocked):
                trace.baseline_test(binary, output, {"run_id": "test"})
            report = json.loads((output / "upstream-selftest.json").read_text())
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["exit_code"], 4)
            self.assertEqual(report["argv"][1:], ["-r", "migration.private_anon"])
            self.assertEqual(report["tap_sha256"], trace.sha256(output / "upstream-selftest.tap"))

    def test_cleanup_only_owned_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = trace.TraceInstance(Path(directory), "test")
            instance.path.mkdir(parents=True)
            with patch.object(instance, "write") as write:
                self.assertTrue(instance.cleanup())
                write.assert_not_called()
                self.assertTrue(instance.path.exists())
                instance.owned = True
                self.assertTrue(instance.cleanup())
                self.assertFalse(instance.path.exists())
                self.assertEqual(write.call_args_list[0].args, ("tracing_on", 0))
                self.assertEqual(write.call_args_list[1].args, ("events/enable", 0))

    def test_identity_guard_precedes_mount_and_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(output=Path(directory), run_id="test", fast_node=0, slow_node=1,
                                      binary=Path("unused"), baseline=Path("unused"))
            with patch.object(trace, "guest_identity", side_effect=ValueError("Wrong Guest")), \
                    patch.object(trace.subprocess, "run") as run, patch.object(trace.subprocess, "Popen") as popen:
                report = trace.collect(args, Path(directory) / "tracefs")
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["reason"], "Wrong Guest")
            self.assertTrue(report["cleanup_success"])
            run.assert_not_called()
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
