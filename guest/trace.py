#!/usr/bin/env python3
"""Collect one PID's migration trace in an owned, isolated Guest instance."""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time

from cxl_setup import guest_identity

EVENTS = ("mm_migrate_pages_start", "mm_migrate_pages")
LOSS_FIELDS = ("overrun", "commit overrun", "dropped events")


class Blocked(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def parse_stats(text):
    fields = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([^:]+):\s*([0-9]+)\s*", line)
        if match:
            key = match[1].strip()
            if key in fields:
                raise ValueError("Duplicate trace statistics field: " + key)
            fields[key] = int(match[2])
    if not all(key in fields for key in LOSS_FIELDS):
        raise ValueError("Missing trace loss statistics")
    return fields


def verify_stats(before, after):
    if not before or set(before) != set(after):
        raise ValueError("Trace CPU statistics coverage changed")
    for cpu in before:
        initial, final = parse_stats(before[cpu]), parse_stats(after[cpu])
        if any(initial[key] != 0 or final[key] != 0 for key in LOSS_FIELDS):
            raise ValueError("Trace events lost on " + cpu)


def tap_results(text):
    plans = re.findall(r"^1\.\.([0-9]+)\s*$", text, re.M)
    results = re.findall(r"^(not ok|ok)\s+([0-9]+)\s+([^\n]*)$", text, re.M)
    skipped = sum(bool(re.search(r"#\s*SKIP\b", tail, re.I)) for _, _, tail in results)
    summary = {"planned": int(plans[0]) if len(plans) == 1 else -1,
               "passed": sum(kind == "ok" and not re.search(r"#\s*SKIP\b", tail, re.I)
                             for kind, _, tail in results),
               "failed": sum(kind == "not ok" for kind, _, _ in results), "skipped": skipped}
    valid = (len(plans) == 1 and summary["planned"] == 1 and len(results) == 1 and
             results[0][1] == "1" and results[0][2].split(" #")[0].strip() == "migration.private_anon")
    if not valid:
        raise ValueError("Upstream selftest did not run exactly migration.private_anon")
    return summary


def stop_child(process):
    if process is not None and process.poll() is None:
        # Only the new session created by this Popen, including selftest workers.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def baseline_test(binary, output, header):
    argv = [str(binary), "-r", "migration.private_anon"]
    report = dict(header, status="FAIL", testcase="migration.private_anon", argv=argv,
                  exit_code=None, binary_sha256=sha256(binary))
    tap = output / "upstream-selftest.tap"
    process = None
    try:
        with tap.open("wb") as stream:
            process = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                report["exit_code"] = process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                stop_child(process)
                raise TimeoutError("Upstream migration selftest deadline exceeded") from None
        report["results"] = tap_results(tap.read_text(errors="replace"))
        if report["results"]["skipped"] or report["exit_code"] == 4:
            report["status"] = "BLOCKED"
            raise Blocked("Upstream migration selftest was skipped")
        if report["exit_code"] != 0 or report["results"] != {"planned": 1, "passed": 1, "failed": 0, "skipped": 0}:
            raise ValueError("Upstream migration selftest failed")
        report["status"] = "PASS"
    except Exception as exc:
        report["reason"] = str(exc)
        raise
    finally:
        stop_child(process)
        if process is not None:
            report["exit_code"] = process.returncode
        if tap.is_file():
            report["tap_sha256"] = sha256(tap)
        publish(output / "upstream-selftest.json", report)
    return report


class TraceInstance:
    def __init__(self, root, run_id):
        self.root = root
        self.path = root / "instances" / ("tierlab_" + run_id)
        self.owned = False

    def write(self, relative, value):
        (self.path / relative).write_text(str(value) + "\n")

    def create(self, output):
        if not (self.root / "trace_marker").is_file():
            raise Blocked("Guest tracefs is not mounted")
        self.path.mkdir()  # Existing instances are never reused or removed.
        self.owned = True
        self.write("tracing_on", 0)
        self.write("current_tracer", "nop")
        self.write("buffer_size_kb", 4096)
        self.write("trace_clock", "mono")
        if "[mono]" not in (self.path / "trace_clock").read_text():
            raise Blocked("Guest monotonic trace clock unavailable")
        self.write("options/context-info", 1)
        self.write("options/latency-format", 0)
        self.write("options/raw", 0)
        self.write("options/hex", 0)
        self.write("options/bin", 0)
        formats = output / "trace_formats"
        formats.mkdir(exist_ok=False)
        hashes = {}
        for event in EVENTS:
            directory = self.path / "events/migrate" / event
            if not (directory / "format").is_file():
                raise Blocked("Guest missing required migration tracepoint: " + event)
            destination = formats / (event + ".format")
            destination.write_bytes((directory / "format").read_bytes())
            hashes[event] = sha256(destination)
            self.write("events/migrate/" + event + "/enable", 1)
        return hashes

    def stats(self):
        return {path.parent.name: path.read_text() for path in
                sorted((self.path / "per_cpu").glob("cpu[0-9]*/stats"))}

    def cleanup(self):
        if not self.owned:
            return True
        self.write("tracing_on", 0)
        self.write("events/enable", 0)
        self.path.rmdir()
        self.owned = False
        return not self.path.exists()


def consume_available(fd, stream):
    """Drain available complete kernel records; do not wait for a tracefs EOF."""
    size = 0
    while True:
        try:
            data = os.read(fd, 65536)
        except BlockingIOError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            return size
        if not data:
            return size
        stream.write(data)
        size += len(data)


def collect(args, trace_root=Path("/sys/kernel/tracing")):
    output, run_id = args.output, args.run_id
    header = {"schema_version": 1, "run_id": run_id, "scenario": "trace",
              "evidence_basis": "guest_kernel_measured"}
    report = dict(header, status="FAIL", clock="mono", enabled_events=["migrate:" + e for e in EVENTS],
                  eof_drained=False, cleanup_success=False, target_exit_code=None)
    instance = TraceInstance(trace_root, run_id)
    process, fd = None, None
    trace = output / "trace.log"
    try:
        try:
            guest_identity()  # No mounting, process launch or tracing before Guest guard.
        except ValueError as exc:
            raise Blocked(str(exc)) from exc
        config = json.loads((output / "config.json").read_text())
        if config.get("run_id") != run_id or config.get("scenario") != "trace":
            raise ValueError("Trace request differs from correlated boot configuration")
        if not (output / "kernel.config").is_file():
            raise Blocked("Pinned kernel configuration evidence missing")
        report["kernel_config_sha256"] = sha256(output / "kernel.config")
        baseline_test(args.baseline, output, header)
        if not (trace_root / "trace_marker").is_file():
            subprocess.run(["mount", "-t", "tracefs", "tracefs", str(trace_root)],
                           check=True, capture_output=True, text=True, timeout=10)
        report["format_sha256"] = instance.create(output)
        report["stats_before"] = instance.stats()
        verify_stats(report["stats_before"], report["stats_before"])
        fd = os.open(instance.path / "trace_pipe", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        env = dict(os.environ, TIERLAB_TRACE_MARKER=str(instance.path / "trace_marker"))
        argv = [str(args.binary), "trace", "--fast-node", str(args.fast_node), "--slow-node", str(args.slow_node),
                "--output", str(output), "--run-id", run_id]
        with (output / "trace-target.log").open("wb") as log, trace.open("wb") as stream:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                       env=env, start_new_session=True)
            report["pid"], report["argv"] = process.pid, argv
            instance.write("set_event_pid", process.pid)
            if (instance.path / "set_event_pid").read_text().split() != [str(process.pid)]:
                raise ValueError("Trace PID filter readback mismatch")
            instance.write("tracing_on", 1)
            process.stdin.write(b"TRACE_READY\n")
            process.stdin.close()
            deadline = time.monotonic() + 60
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Trace target deadline exceeded")
                select.select([fd], [], [], 0.05)
                consume_available(fd, stream)
            report["target_exit_code"] = process.returncode
            instance.write("tracing_on", 0)
            consume_available(fd, stream)
            report["eof_drained"] = True
            stream.flush()
            os.fsync(stream.fileno())
        report["stats_after"] = instance.stats()
        verify_stats(report["stats_before"], report["stats_after"])
        if process.returncode != 0:
            raise ValueError("Trace target failed with exit code " + str(process.returncode))
        if trace.stat().st_size == 0 or not trace.read_bytes().endswith(b"\n"):
            raise ValueError("Trace output is empty or truncated")
        report["status"] = "PASS"
    except Blocked as exc:
        report.update(status="BLOCKED", reason=str(exc))
    except Exception as exc:
        report["reason"] = str(exc)
    finally:
        stop_child(process)
        if process is not None:
            report["target_exit_code"] = process.returncode
        if instance.owned and "stats_after" not in report:
            try:
                report["stats_after"] = instance.stats()
            except OSError as exc:
                report["stats_error"] = str(exc)
        if fd is not None:
            os.close(fd)
        try:
            report["cleanup_success"] = instance.cleanup()
        except OSError as exc:
            report.update(status="FAIL", cleanup_error=str(exc))
            report.setdefault("reason", "Trace instance cleanup failed: " + str(exc))
        if trace.is_file():
            report.update(trace_sha256=sha256(trace), trace_bytes=trace.stat().st_size)
        publish(output / "collector.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fast-node", type=int, required=True)
    parser.add_argument("--slow-node", type=int, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.run_id):
        parser.error("Invalid run ID")
    report = collect(args)
    print("Trace collector: " + report["status"])
    return {"PASS": 0, "BLOCKED": 78}.get(report["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
