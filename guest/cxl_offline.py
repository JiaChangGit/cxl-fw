#!/usr/bin/env python3
"""Offline only the current run's drained CXL range in the designated Guest."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import cxl_setup as cxl


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def load(path):
    require(path.is_file() and path.stat().st_size <= 1024 * 1024, "Missing or oversized input")
    return json.loads(path.read_text())


def validate_plan(topology, snapshot, iomem, drain, config, run_id):
    for data, scenario in ((topology, "migrate"), (drain, "offline"), (config, "offline")):
        require(type(data.get("schema_version")) is int and data["schema_version"] == 1 and data.get("run_id") == run_id
                and data.get("scenario") == scenario, "Wrong run or scenario evidence")
    require(topology.get("evidence_basis") == "guest_kernel_measured"
            and drain.get("evidence_basis") == "guest_kernel_measured", "Wrong evidence domain")
    require(topology.get("status") == "PASS" and drain.get("status") == "PASS"
            and drain.get("lifecycle_state") == "DRAINING", "Drain has not completed")
    require(drain.get("mapping_released") is True and drain.get("workload_stopped") is True
            and all(type(drain.get(key)) is int and drain[key] == 0 for key in
                    ("final_slow_pages", "integrity_failures", "query_missing_count"))
            and drain.get("capacity", {}).get("passed") is True,
            "Drain/capacity proof does not permit offline")
    require(drain.get("managed_bytes") == 128 * 1024 * 1024 and drain.get("page_size") == 4096
            and drain.get("page_count") == 32768 and drain.get("initial_slow_pages") == 32768,
            "Unexpected drain profile")
    capacity = drain["capacity"]
    for key in ("node_free_bytes", "mem_available_bytes", "slow_bytes", "safety_margin_bytes", "required_bytes"):
        require(type(capacity.get(key)) is int and 0 <= capacity[key] < cxl.U64, "Invalid capacity evidence")
    require(capacity["slow_bytes"] == drain["initial_slow_pages"] * drain["page_size"]
            and capacity["safety_margin_bytes"] == 256 * 1024 * 1024
            and capacity["required_bytes"] == capacity["slow_bytes"] + capacity["safety_margin_bytes"]
            and capacity["node_free_bytes"] >= capacity["required_bytes"]
            and capacity["mem_available_bytes"] >= capacity["required_bytes"], "Insufficient drain capacity")
    fast, slow = topology.get("fast_node"), topology.get("slow_node")
    require(type(fast) is int and type(slow) is int and fast >= 0 and slow >= 0 and fast != slow
            and drain.get("fast_node") == fast and drain.get("slow_node") == slow,
            "Drain topology differs from live range")
    start, size = topology["resource_start"], topology["resource_size"]
    cxl.interval(start, size)
    region, dax = snapshot["region"], snapshot["dax"]
    require(region.get("name") == topology["region"] and region.get("mode") == "ram"
            and region.get("interleave_ways") == 1
            and region.get("resource") == start and region.get("size") == size,
            "Region does not own the requested RAM range")
    require(dax.get("name") == topology["daxdev"] and dax.get("resource") == start
            and dax.get("size") == size and dax.get("target_node") == slow
            and dax.get("driver") == "kmem", "DAX ownership mismatch")
    cxl.verify_ownership(iomem, dax["name"], start, size)
    blocks = cxl.contained_blocks(start, size, snapshot["blocks"])
    expected = sorted(topology["blocks"], key=lambda b: b["start"])
    require(len(blocks) == len(expected), "Topology memory blocks changed")
    for actual, old in zip(blocks, expected):
        require(re.fullmatch(r"memory[0-9]+", actual["name"]) is not None
                and all(actual.get(k) == old.get(k) for k in ("name", "start", "size", "node")),
                "Memory block identity changed")
        cxl.movable_state(actual)
        require(actual["state"] == "online" and actual["node"] == slow
                and actual["node_links"] == [f"node{slow}"], "Block is not owned online Movable memory")
    require(snapshot.get("auto_online_blocks") == "offline", "Automatic memory online is enabled")
    return [block["name"] for block in blocks]


class SystemIO:
    def identity(self):
        cxl.guest_identity()

    def snapshot(self, topology):
        region, dax = topology["region"], topology["daxdev"]
        rp = cxl.SYS / "bus/cxl/devices" / region
        dp = cxl.SYS / "bus/dax/devices" / dax
        require(rp.resolve() in dp.resolve().parents, "DAX is not a child of this CXL region")
        setup = cxl.Setup(None, "unused")
        blocks = cxl.contained_blocks(topology["resource_start"], topology["resource_size"], setup.memory_blocks())
        return setup.snapshot(region, dax, blocks), cxl.read(Path("/proc/iomem"))

    def state(self, name):
        path = cxl.SYS / "devices/system/memory" / name / "state"
        return cxl.read(path) if path.exists() else "absent"

    def write(self, name, value):
        (cxl.SYS / "devices/system/memory" / name / "state").write_text(value + "\n")

    def command(self, argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    def manual(self, tool, command):
        path = Path("/usr/share/man/man1") / f"{tool}-{command}.1.gz"
        return gzip.decompress(path.read_bytes()).decode().replace("\\-", "-")

    def exists(self, kind, name):
        return (cxl.SYS / "bus" / kind / "devices" / name).exists()

    def bound(self, region):
        return (cxl.SYS / "bus/cxl/devices" / region / "driver").exists()


class Offline:
    def __init__(self, output, run_id, io=None):
        self.output, self.run_id, self.io = Path(output), run_id, io or SystemIO()
        self.successes = []
        self.report = {"schema_version": 1, "run_id": run_id, "scenario": "offline",
                       "evidence_basis": "guest_kernel_measured", "status": "FAIL",
                       "lifecycle_state": "FAILED", "offlined_blocks": self.successes,
                       "rollback_attempted": False, "rollback_complete": None,
                       "region_removed": False, "dax_removed": False, "raw_file": "offline.jsonl"}

    def emit(self, event):
        event.update(schema_version=1, run_id=self.run_id, scenario="offline",
                     evidence_basis="guest_kernel_measured")
        with (self.output / "offline.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def command(self, argv, purpose, allow_error=False):
        try:
            result = self.io.command(argv)
        except subprocess.TimeoutExpired as exc:
            self.emit({"kind": "command", "purpose": purpose, "argv": argv,
                       "exit_code": None, "stdout": str(exc.stdout or ""), "stderr": str(exc.stderr or ""),
                       "unknown_inflight": True})
            raise
        self.emit({"kind": "command", "purpose": purpose, "argv": argv,
                   "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        require(allow_error or result.returncode == 0, f"Command failed: {' '.join(argv)}")
        return result.stdout

    def probes(self):
        for tool in ("cxl", "daxctl"):
            require(self.command([tool, "--version"], "probe").strip() == "81", "Unsupported Guest tool version")
        for tool, command, required in (("daxctl", "reconfigure-device", ["--mode", "devdax"]),
                                         ("cxl", "disable-region", ["disable-region", "<region>"]),
                                         ("cxl", "destroy-region", ["destroy-region", "<region>"])):
            self.command([tool, command, "--help"], "probe", allow_error=True)
            text = self.io.manual(tool, command)
            require(all(token in text for token in required), "Installed manual lacks required interface")
            filename = f"offline-{tool}-{command}.manual.txt"
            data = text.encode()
            (self.output / filename).write_bytes(data)
            self.emit({"kind": "manual", "tool": tool, "command": command, "required": required,
                       "file": filename, "sha256": hashlib.sha256(data).hexdigest()})

    def states(self, names, preserve_errors=False):
        blocks = []
        for name in names:
            try:
                blocks.append({"name": name, "state": self.io.state(name)})
            except OSError as exc:
                if not preserve_errors:
                    raise
                blocks.append({"name": name, "state": "unreadable", "errno": exc.errno, "error": str(exc)})
        return blocks

    def write(self, name, requested):
        before = self.io.state(name)
        try:
            self.io.write(name, requested)
            after = self.io.state(name)
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            self.emit({"kind": "write", "block": name, "before_state": before,
                       "requested": requested, "after_state": None, "rc": -1,
                       "errno": getattr(exc, "errno", None), "error": str(exc), "unknown_inflight": True})
            raise
        except OSError as exc:
            try:
                after = self.io.state(name)
            except OSError:
                after = None
            self.emit({"kind": "write", "block": name, "before_state": before,
                       "requested": requested, "after_state": after, "rc": -1,
                       "errno": exc.errno, "error": str(exc)})
            raise
        self.emit({"kind": "write", "block": name, "before_state": before,
                   "requested": requested, "after_state": after, "rc": 0, "errno": 0, "error": None})
        require(after == ("offline" if requested == "offline" else "online"), "Memory state readback mismatch")

    def rollback(self, names, unknown=False):
        # A timed-out sysfs operation may still run in the kernel. Never race it.
        if unknown:
            self.emit({"kind": "failure_snapshot", "blocks": None, "unknown_inflight": True})
            return
        self.emit({"kind": "failure_snapshot", "blocks": self.states(names, preserve_errors=True), "unknown_inflight": False})
        self.report["rollback_attempted"] = bool(self.successes)
        for name in reversed(self.successes):
            try:
                require(self.io.state(name) == "offline", "Rollback block state changed")
                self.write(name, "online_movable")
            except (TimeoutError, subprocess.TimeoutExpired):
                self.report["rollback_complete"] = False
                self.emit({"kind": "rollback_final", "blocks": None, "complete": False, "unknown_inflight": True})
                return
            except (OSError, ValueError):
                # Preserve the remaining known successes; never write an unreadable block.
                continue
        blocks = self.states(names, preserve_errors=True)
        # Preflight required every original block online. An unsuccessful write may
        # nevertheless change state, so restoring only the success list is insufficient.
        complete = all(block["state"] == "online" for block in blocks)
        self.report["rollback_complete"] = complete
        self.emit({"kind": "rollback_final", "blocks": blocks, "complete": complete})

    def all_offline(self, names, stage):
        blocks = self.states(names)
        self.emit({"kind": "all_offline", "stage": stage, "blocks": blocks})
        allowed = {"offline"} if stage == "before_devdax" else {"offline", "absent"}
        require(all(block["state"] in allowed for block in blocks), "Teardown refused while a block is online")

    def execute(self):
        self.io.identity()
        topology = load(self.output / "topology.json")
        for key, expression in (("region", r"region[0-9]+"), ("daxdev", r"dax[0-9]+\.[0-9]+")):
            require(isinstance(topology.get(key), str) and re.fullmatch(expression, topology[key]), "Invalid device identifier")
        drain, config = load(self.output / "drain.json"), load(self.output / "config.json")
        snapshot, iomem = self.io.snapshot(topology)
        names = validate_plan(topology, snapshot, iomem, drain, config, self.run_id)
        self.report.update({key: topology[key] for key in
                            ("fast_node", "slow_node", "region", "daxdev", "resource_start", "resource_size")})
        self.report["blocks"] = names
        self.probes()
        print("CXL offline plan: " + ", ".join(names), flush=True)
        self.emit({"kind": "plan", "snapshot": snapshot, "iomem": iomem, "blocks": names,
                   "drain": {"mapping_released": True, "workload_stopped": True,
                             "final_slow_pages": 0, "capacity_passed": True}})
        try:
            for name in names:
                require(self.io.state(name) == "online", "Block state changed since preflight")
                self.write(name, "offline")
                self.successes.append(name)
        except (TimeoutError, subprocess.TimeoutExpired):
            self.rollback(names, unknown=True)
            raise
        except (OSError, ValueError):
            self.rollback(names)
            raise
        self.all_offline(names, "before_devdax")
        dax, region = topology["daxdev"], topology["region"]
        self.command(["daxctl", "reconfigure-device", "--mode=devdax", dax], "teardown")
        devices = json.loads(self.command(["daxctl", "list", "-D"], "verify"))
        self.emit({"kind": "devdax", "data": devices})
        device = cxl.one((item for item in cxl.objects(devices) if item.get("chardev") == dax), "reconfigured DAX device")
        require(device.get("mode") == "devdax", "DAX did not leave system-ram mode")
        self.all_offline(names, "before_disable")
        self.command(["cxl", "disable-region", region], "teardown")
        bound = self.io.bound(region)
        self.emit({"kind": "disabled", "region_exists": self.io.exists("cxl", region), "driver_bound": bound})
        require(not bound, "Region remains bound after disable")
        self.all_offline(names, "before_destroy")
        self.command(["cxl", "destroy-region", region], "teardown")
        regions = json.loads(self.command(["cxl", "list", "-R"], "verify") or "[]")
        devices = json.loads(self.command(["daxctl", "list", "-D"], "verify") or "[]")
        region_exists, dax_exists = self.io.exists("cxl", region), self.io.exists("dax", dax)
        blocks = self.states(names)
        self.emit({"kind": "final", "blocks": blocks, "region_exists": region_exists,
                   "dax_exists": dax_exists, "regions": regions, "dax_devices": devices})
        require(not region_exists and not dax_exists and all(b["state"] in ("offline", "absent") for b in blocks)
                and not any(item.get("region") == region for item in cxl.objects(regions))
                and not any(item.get("chardev") == dax for item in cxl.objects(devices)), "Teardown readback failed")
        self.report.update(status="PASS", lifecycle_state="OFFLINE", region_removed=True, dax_removed=True)
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.run_id):
        parser.error("Invalid run ID")
    args.output.mkdir(parents=True, exist_ok=True)
    operation = Offline(args.output, args.run_id)
    try:
        operation.execute()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        operation.report["reason"] = str(exc)
        print(f"Offline FAIL: {exc}", file=sys.stderr)
    target = args.output / "offline.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(operation.report, indent=2) + "\n")
    temporary.replace(target)
    print("CXL offline: " + operation.report["status"])
    return 0 if operation.report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
