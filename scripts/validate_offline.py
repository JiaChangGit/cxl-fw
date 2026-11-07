#!/usr/bin/env python3
"""Recompute G4 from Guest drain queries and ordered CXL lifecycle evidence."""
import hashlib
import json
from pathlib import Path
import re

from validate_migration import (check_iomem, document as topology_document, integer,
                                load, number, records, require, validate_topology)

PAGE_SIZE = 4096
PAGES = 32768
BYTES = PAGES * PAGE_SIZE
MARGIN = 256 * 1024 * 1024
SENTINEL = -(1 << 31)


def header(value, run_id):
    require(isinstance(value, dict) and type(value.get("schema_version")) is int
            and value["schema_version"] == 1 and value.get("run_id") == run_id
            and value.get("scenario") == "offline"
            and value.get("evidence_basis") == "guest_kernel_measured",
            "Wrong M3 schema, run_id, scenario or evidence basis")


def document(shared, name, run_id):
    value = json.loads(load(shared, name))
    header(value, run_id)
    return value


def events(shared, name, run_id):
    text = load(shared, name)
    require(text and text.endswith("\n"), "Empty or truncated M3 raw evidence")
    result = [json.loads(line) for line in text.splitlines()]
    for event in result:
        header(event, run_id)
    return result


def unsigned(value, reason):
    require(type(value) is int and 0 <= value < (1 << 64), reason)
    return value


def kibibytes(text, key, prefix=""):
    require(isinstance(text, str), "Missing raw capacity meminfo")
    matches = re.findall(r"^" + re.escape(prefix + key) + r":\s*([0-9]+)\s+kB\s*$", text, re.M)
    require(len(matches) == 1, "Missing or ambiguous capacity meminfo field")
    value = int(matches[0]) * 1024
    require(value < (1 << 64), "Overflowing capacity meminfo")
    return value


def capacity(event, fast):
    free = kibibytes(event.get("node_meminfo"), "MemFree", f"Node {fast} ")
    available = kibibytes(event.get("meminfo"), "MemAvailable")
    expected = {"node_free_bytes": free, "mem_available_bytes": available,
                "slow_bytes": BYTES, "safety_margin_bytes": MARGIN,
                "required_bytes": BYTES + MARGIN, "passed": True}
    require(all(type(event.get(key)) is type(value) and event[key] == value
                for key, value in expected.items()), "Capacity summary differs from raw meminfo")
    require(min(free, available) >= BYTES + MARGIN, "Insufficient actual drain capacity")
    return expected


def snapshot(event, stage, node):
    require(event.get("kind") == "snapshot" and event.get("stage") == stage
            and type(event.get("query_rc")) is int and event["query_rc"] == 0
            and type(event.get("query_errno")) is int and event["query_errno"] == 0,
            "Missing or failed drain snapshot")
    nodes = event.get("nodes")
    require(isinstance(nodes, list) and len(nodes) == PAGES
            and all(type(value) is int and value == node for value in nodes),
            "Incomplete or wrong drain residency")
    return nodes.copy()


def pattern(event, stage):
    require(event.get("kind") == "pattern" and event.get("stage") == stage
            and event.get("generation") == 7 and type(event.get("generation")) is int
            and event.get("checked_bytes") == BYTES
            and type(event.get("checked_bytes")) is int
            and event.get("integrity_failures") == 0
            and type(event.get("integrity_failures")) is int,
            "Missing complete drain payload scan or integrity failure")


def drain_batch(event, index, nodes, attempts, fast, slow):
    require(event.get("kind") == "batch" and event.get("batch_index") == index
            and type(event.get("batch_index")) is int and event.get("target_node") == fast
            and event.get("source_node") == slow, "Wrong drain batch order or target")
    rc = integer(event.get("syscall_rc"), "Missing drain syscall rc")
    error = integer(event.get("syscall_errno"), "Missing drain syscall errno")
    require(rc >= -1 and (error > 0 if rc == -1 else error == 0), "Invalid drain syscall rc/errno")
    require(all(type(event.get(prefix + "_query_rc")) is int
                and event[prefix + "_query_rc"] == 0
                and type(event.get(prefix + "_query_errno")) is int
                and event[prefix + "_query_errno"] == 0 for prefix in ("before", "after")),
            "Failed drain batch query")
    pages = event.get("pages")
    require(isinstance(pages, list) and 0 < len(pages) <= 256, "Invalid drain batch size")
    seen, copied = set(), 0
    for page in pages:
        pid = integer(page.get("page_id"), "Missing drain page id")
        require(0 <= pid < PAGES and pid not in seen, "Duplicate or out-of-range drain page")
        seen.add(pid)
        before = integer(page.get("query_before_node"), "Missing drain before placement")
        after = integer(page.get("query_after_node"), "Missing drain after placement")
        require(before == nodes[pid] == slow and after in (slow, fast),
                "Drain query disagrees with committed placement or admission guard")
        status = integer(page.get("move_status"), "Missing drain move status")
        require(type(page.get("move_status_valid")) is bool
                and page["move_status_valid"] == (rc >= 0 and status != SENTINEL),
                "Drain sentinel or syscall failure treated as valid status")
        attempts[pid] += 1
        require(attempts[pid] <= 3, "Drain exceeded bounded per-page attempts")
        copied += before != after
        nodes[pid] = after
    return len(pages), copied


def validate_drain(shared, run_id, fast, slow):
    report = document(shared, "drain.json", run_id)
    require(report.get("status") == "PASS" and report.get("lifecycle_state") == "DRAINING"
            and report.get("managed_bytes") == BYTES and report.get("page_size") == PAGE_SIZE
            and report.get("page_count") == PAGES and report.get("fast_node") == fast
            and report.get("slow_node") == slow and report.get("deadline_ms") == 60000
            and report.get("max_attempts_per_page") == 3, "Wrong drain status or fixed profile")
    raw = events(shared, report.get("raw_file"), run_id)
    require(len(raw) >= 7, "Incomplete raw drain evidence")
    pattern(raw[0], "initial")
    current = snapshot(raw[1], "initial", slow)
    require(raw[2].get("kind") == "capacity", "Capacity guard was not before migration")
    actual_capacity = capacity(raw[2], fast)
    require(report.get("capacity") == actual_capacity, "Drain report capacity contradicts raw evidence")
    attempts = [0] * PAGES
    total, succeeded = 0, 0
    batches = raw[3:-3]
    require(batches, "Drain has no migration evidence")
    for index, event in enumerate(batches):
        attempted, copied = drain_batch(event, index, current, attempts, fast, slow)
        total += attempted
        succeeded += copied
    final = snapshot(raw[-3], "final", fast)
    require(current == final and all(attempts), "Final snapshot disagrees with raw drain batches")
    pattern(raw[-2], "final")
    release = raw[-1]
    elapsed = unsigned(release.get("elapsed_ms"), "Invalid drain elapsed time")
    require(release.get("kind") == "release" and release.get("workload_stopped") is True
            and release.get("mapping_released") is True and release.get("managed_slow_pages") == 0
            and type(release.get("managed_slow_pages")) is int
            and release.get("lifecycle_state") == "DRAINING" and elapsed <= 60000,
            "Missing safe mapping release or exceeded drain deadline")
    expected = {"initial_slow_pages": PAGES, "final_slow_pages": 0, "attempted_pages": total,
                "succeeded_pages": succeeded, "integrity_failures": 0, "query_missing_count": 0,
                "workload_stopped": True, "mapping_released": True, "elapsed_ms": elapsed}
    require(succeeded == PAGES and all(type(report.get(key)) is type(value) and report[key] == value
                                      for key, value in expected.items()),
            "Drain summary differs from complete kernel/release evidence")
    return {**expected, "capacity": actual_capacity, "max_observed_attempts": max(attempts)}


def planned_blocks(plan, topology):
    expected = sorted(topology["blocks"], key=lambda item: item["start"])
    names = [item["name"] for item in expected]
    require(plan.get("kind") == "plan" and plan.get("blocks") == names,
            "Offline plan differs from owned block whitelist")
    require(plan.get("drain") == {"mapping_released": True, "workload_stopped": True,
                                  "final_slow_pages": 0, "capacity_passed": True},
            "Offline plan lacks completed drain proof")
    snapshot = plan.get("snapshot", {})
    region, dax = snapshot.get("region", {}), snapshot.get("dax", {})
    start, size = topology["resource_start"], topology["resource_size"]
    require(region.get("name") == topology["region"] and region.get("mode") == "ram"
            and region.get("interleave_ways") == 1 and number(region.get("resource")) == start
            and number(region.get("size")) == size and dax.get("name") == topology["daxdev"]
            and number(dax.get("resource")) == start and number(dax.get("size")) == size
            and dax.get("target_node") == topology["slow_node"]
            and str(dax.get("driver")).split("/")[-1] == "kmem", "Offline ownership/range changed")
    check_iomem(plan.get("iomem", ""), topology["daxdev"], start, size)
    observed = snapshot.get("blocks")
    require(isinstance(observed, list) and len(observed) == len(expected), "Incomplete offline plan snapshot")
    for block, previous in zip(observed, expected):
        require(all(block.get(key) == previous.get(key) for key in
                    ("name", "start", "size", "node", "state", "zone"))
                and block.get("node_links") == [f"node{topology['slow_node']}"]
                and str(block.get("valid_zones", "")).strip() == "Movable",
                "Offline plan has foreign, non-Movable or changed memory block")
    return names


def all_offline(event, stage, names, absent=False):
    require(event.get("kind") == "all_offline" and event.get("stage") == stage,
            "Missing ordered all-offline teardown guard")
    states = event.get("blocks")
    allowed = ("offline", "absent") if absent else ("offline",)
    require(isinstance(states, list) and [item.get("name") for item in states] == names
            and all(item.get("state") in allowed for item in states),
            "Teardown guard contains online, missing or foreign block")


def command(event, argv, purpose):
    require(event.get("kind") == "command" and event.get("argv") == argv
            and event.get("purpose") == purpose and type(event.get("exit_code")) is int
            and event["exit_code"] == 0 and isinstance(event.get("stdout"), str)
            and isinstance(event.get("stderr"), str), "Missing, failed or unsafe teardown command")
    return event["stdout"]


def listing(event, argv):
    text = command(event, argv, "verify")
    data = json.loads(text or "[]")
    require(isinstance(data, (list, dict)), "Invalid raw teardown listing")
    return data


def validate_lifecycle(shared, run_id, topology):
    report = document(shared, "offline.json", run_id)
    names = [item["name"] for item in sorted(topology["blocks"], key=lambda item: item["start"])]
    require(report.get("status") == "PASS" and report.get("lifecycle_state") == "OFFLINE"
            and all(report.get(key) == topology.get(key) for key in
                    ("fast_node", "slow_node", "region", "daxdev", "resource_start", "resource_size"))
            and report.get("blocks") == names and report.get("offlined_blocks") == names
            and report.get("rollback_attempted") is False and report.get("rollback_complete") is None
            and report.get("region_removed") is True and report.get("dax_removed") is True,
            "Wrong offline status, ownership or teardown summary")
    raw = events(shared, report.get("raw_file"), run_id)
    require(raw, "Missing offline lifecycle raw events")
    plans = [event for event in raw if event.get("kind") == "plan"]
    require(len(plans) == 1, "Missing or duplicate offline plan")
    require(planned_blocks(plans[0], topology) == names, "Unexpected offline whitelist")
    # Probes/manual proofs can precede writes. All mutation evidence below is strictly ordered.
    probes, manuals, ordered = {}, set(), []
    for event in raw:
        if event.get("kind") == "command" and event.get("purpose") == "probe":
            require(not any(item.get("kind") == "write" for item in ordered), "Late tool capability probe")
            argv = event.get("argv")
            require(isinstance(argv, list) and len(argv) >= 2 and argv[0] in ("cxl", "daxctl")
                    and type(event.get("exit_code")) is int and isinstance(event.get("stdout"), str)
                    and isinstance(event.get("stderr"), str), "Malformed CLI capability probe")
            if argv[1:] == ["--version"]:
                require(event["exit_code"] == 0 and event["stdout"].strip() == "81", "Unpinned CXL tool")
                probes[argv[0]] = True
            else:
                require(tuple(argv) in (("daxctl", "reconfigure-device", "--help"),
                                       ("cxl", "disable-region", "--help"),
                                       ("cxl", "destroy-region", "--help")), "Unexpected CLI capability probe")
        elif event.get("kind") == "manual":
            require(not any(item.get("kind") == "write" for item in ordered), "Late CLI manual proof")
            pair = (event.get("tool"), event.get("command"))
            require(pair in (("daxctl", "reconfigure-device"), ("cxl", "disable-region"),
                             ("cxl", "destroy-region")) and pair not in manuals,
                    "Missing or duplicate CLI manual proof")
            text = load(shared, event.get("file"))
            require(hashlib.sha256(text.encode()).hexdigest() == event.get("sha256")
                    and isinstance(event.get("required"), list)
                    and all(isinstance(token, str) and token in text for token in event["required"]),
                    "CLI manual proof differs from copied raw manual")
            if pair == ("daxctl", "reconfigure-device"):
                require("--mode" in text and "devdax" in text, "Manual does not support devdax teardown")
            manuals.add(pair)
        else:
            ordered.append(event)
    require(set(probes) == {"cxl", "daxctl"} and len(manuals) == 3, "Incomplete pinned CLI capability evidence")
    index = 0

    def take():
        nonlocal index
        require(index < len(ordered), "Truncated lifecycle evidence")
        value = ordered[index]
        index += 1
        return value

    require(take() is plans[0], "Offline writes precede verified plan")
    for name in names:
        write = take()
        require(write.get("kind") == "write" and write.get("block") == name
                and write.get("before_state") == "online" and write.get("requested") == "offline"
                and write.get("after_state") == "offline" and type(write.get("rc")) is int
                and write["rc"] == 0 and type(write.get("errno")) is int and write["errno"] == 0
                and write.get("error") is None, "Block offline write/readback failed or reordered")
    all_offline(take(), "before_devdax", names)
    command(take(), ["daxctl", "reconfigure-device", "--mode=devdax", topology["daxdev"]], "teardown")
    dax = listing(take(), ["daxctl", "list", "-D"])
    devdax = take()
    devices = [item for item in records(dax) if item.get("chardev") == topology["daxdev"]]
    require(devdax.get("kind") == "devdax" and devdax.get("data") == dax and len(devices) == 1
            and devices[0].get("mode") == "devdax", "DAX did not leave system-ram mode")
    all_offline(take(), "before_disable", names, absent=True)
    command(take(), ["cxl", "disable-region", topology["region"]], "teardown")
    disabled = take()
    require(disabled.get("kind") == "disabled" and disabled.get("region_exists") is True
            and disabled.get("driver_bound") is False, "Region disable readback is missing")
    all_offline(take(), "before_destroy", names, absent=True)
    command(take(), ["cxl", "destroy-region", topology["region"]], "teardown")
    regions = listing(take(), ["cxl", "list", "-R"])
    dax_devices = listing(take(), ["daxctl", "list", "-D"])
    final = take()
    require(final.get("kind") == "final" and final.get("regions") == regions
            and final.get("dax_devices") == dax_devices and final.get("region_exists") is False
            and final.get("dax_exists") is False
            and not any(item.get("region") == topology["region"] for item in records(regions))
            and not any(item.get("chardev") == topology["daxdev"] for item in records(dax_devices)),
            "Region/DAX teardown is incomplete or contradicts raw listings")
    final_blocks = final.get("blocks")
    require(isinstance(final_blocks, list) and [item.get("name") for item in final_blocks] == names
            and all(item.get("state") in ("offline", "absent") for item in final_blocks),
            "Final whitelist contains online or missing block evidence")
    require(index == len(ordered), "Unexpected extra lifecycle mutation or failure evidence")
    return {"lifecycle_state": "OFFLINE", "offlined_blocks": names,
            "region_removed": True, "dax_removed": True}


def validate_offline(shared: Path, run_id: str) -> dict:
    """Validate M3 observations only; caller proves Guest identity and console health."""
    try:
        shared = Path(shared)
        topology = topology_document(shared, "topology.json", run_id)
        fast, slow = validate_topology(shared, topology, run_id)
        drain = validate_drain(shared, run_id, fast, slow)
        lifecycle = validate_lifecycle(shared, run_id, topology)
        return {"status": "PASS", "gates": {"G4": "PASS"}, "fast_node": fast, "slow_node": slow,
                "managed_bytes": BYTES, "page_count": PAGES, "drain": drain, **lifecycle,
                "evidence_basis": "guest_kernel_measured"}
    except (KeyError, TypeError, AttributeError, IndexError, OverflowError, OSError,
            UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Malformed M3 evidence: {exc}") from exc
