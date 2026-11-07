#!/usr/bin/env python3
"""Recompute M1 gates from correlated Guest kernel observations, not summary claims."""
import json
import math
from pathlib import Path
import re

BYTES = 64 * 1024 * 1024
PAGE_SIZE = 4096
PAGES = BYTES // PAGE_SIZE
SENTINEL = -(1 << 31)


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def integer(value, reason):
    require(type(value) is int, reason)
    return value


def number(value):
    if type(value) is int:
        return value
    require(isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+|[0-9]+", value),
            "Invalid raw sysfs number")
    return int(value, 16 if value.startswith("0x") else 10)


def check_header(data, run_id):
    require(isinstance(data, dict) and type(data.get("schema_version")) is int
            and data["schema_version"] == 1 and data.get("run_id") == run_id
            and data.get("scenario") == "migrate"
            and data.get("evidence_basis", "guest_kernel_measured") == "guest_kernel_measured",
            "Wrong schema, run_id, scenario or evidence basis")


def load(shared, name):
    require(isinstance(name, str) and name, "Missing evidence filename")
    path = (shared / name).resolve()
    require(path.is_relative_to(shared.resolve()) and path.is_file(), "Missing or unsafe evidence path")
    require(path.stat().st_size <= 64 * 1024 * 1024, "Evidence exceeds 64 MiB")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Unreadable evidence: {name}") from exc


def document(shared, name, run_id):
    try:
        data = json.loads(load(shared, name))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {name}") from exc
    check_header(data, run_id)
    return data


def records(data):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from records(value)
    elif isinstance(data, list):
        for value in data:
            yield from records(value)


def find(data, key, value):
    found = [r for r in records(data) if r.get(key) == value]
    require(len(found) == 1, f"Ambiguous or missing raw {key}: {value}")
    return found[0]


def check_iomem(text, name, start, size):
    resources = []
    for line in text.splitlines():
        match = re.fullmatch(r"\s*([0-9a-fA-F]+)-([0-9a-fA-F]+) : (.+)", line)
        if match:
            resources.append((int(match[1], 16), int(match[2], 16) + 1, match[3]))
    owners = [r for r in resources if r[2] == name]
    require(len(owners) == 1 and owners[0][:2] == (start, start + size),
            "Iomem DAX owner/range mismatch")
    cursor = start
    for low, high, _ in sorted(r for r in resources if r[2] == "System RAM (kmem)"
                               and r[0] < start + size and r[1] > start):
        require(low == cursor and low < high <= start + size, "Iomem kmem coverage mismatch")
        cursor = high
    require(cursor == start + size, "Incomplete iomem kmem coverage")


def validate_topology(shared, topology, run_id):
    require(topology.get("status") == "PASS", "Topology did not pass")
    fast = integer(topology.get("fast_node"), "Invalid fast nid")
    slow = integer(topology.get("slow_node"), "Invalid slow nid")
    require(fast >= 0 and slow >= 0 and fast != slow, "Need distinct valid NUMA nodes")
    require(topology.get("region_type") == "ram" and topology.get("interleave_ways") == 1,
            "Need a 1-way RAM region")
    start = integer(topology.get("resource_start"), "Invalid DAX resource start")
    size = integer(topology.get("resource_size"), "Invalid DAX resource size")
    require(0 <= start < (1 << 64) and size > 0 and start + size <= (1 << 64),
            "Overflowing DAX resource range")
    raw_files = topology.get("raw_files", {})
    raw = {}
    for key in ("memdevs", "decoders", "regions", "dax_devices", "sysfs_before", "sysfs_after"):
        raw[key] = document(shared, raw_files.get(key), run_id).get("data")
        require(raw[key] is not None, f"Missing raw {key} data")
    check_iomem(load(shared, raw_files.get("iomem")), topology.get("daxdev"), start, size)
    memdev = find(raw["memdevs"], "memdev", topology.get("memdev"))
    volatile = number(memdev.get("ram_size"))
    require(volatile == 1024**3 and number(memdev.get("pmem_size", 0)) == 0
            and topology.get("memdev_volatile_bytes") == volatile, "Wrong volatile memdev capacity")
    region = find(raw["regions"], "region", topology.get("region"))
    require(region.get("type", region.get("mode")) == "ram"
            and region.get("interleave_ways") == 1 and region.get("decode_state") == "commit"
            and number(region.get("resource")) == start and number(region.get("size")) == size,
            "Raw region contradicts topology")
    decoder = find(raw["decoders"], "decoder", topology.get("root_decoder"))
    decoder_start, decoder_size = number(decoder.get("resource")), number(decoder.get("size"))
    require(decoder.get("volatile_capable") is True and decoder.get("interleave_ways") == 1
            and 0 <= decoder_start <= start and decoder_size > 0
            and start + size <= decoder_start + decoder_size <= (1 << 64),
            "Root decoder does not own the volatile RAM range")
    dax = find(raw["dax_devices"], "chardev", topology.get("daxdev"))
    require(dax.get("mode") == "system-ram" and dax.get("target_node") == slow,
            "DAX raw mode/node contradicts topology")
    sysfs = raw["sysfs_after"]
    require(isinstance(sysfs, dict), "Missing sysfs snapshot")
    attrs = sysfs.get("dax", {})
    require(number(attrs.get("resource")) == start and number(attrs.get("size")) == size
            and attrs.get("name") == topology.get("daxdev")
            and attrs.get("target_node") == slow
            and str(attrs.get("driver", "")).split("/")[-1] == "kmem", "DAX ownership/range mismatch")
    region_attrs = sysfs.get("region", {})
    require(region_attrs.get("name") == topology.get("region") and region_attrs.get("mode") == "ram"
            and number(region_attrs.get("resource")) == start and number(region_attrs.get("size")) == size
            and region_attrs.get("interleave_ways") == 1, "DAX range is not owned by the RAM region")
    blocks = topology.get("blocks")
    require(isinstance(blocks, list) and blocks, "Missing memory blocks")
    actual = sysfs.get("blocks", [])
    require(len(blocks) == len(actual), "Missing sysfs block evidence")
    cursor, names = start, set()
    for block in sorted(blocks, key=lambda b: b["start"]):
        name = block.get("name")
        require(isinstance(name, str) and re.fullmatch(r"memory[0-9]+", name)
                and name not in names, "Invalid or duplicate block")
        names.add(name)
        base, length = integer(block.get("start"), "Invalid block start"), integer(block.get("size"), "Invalid block size")
        require(base == cursor and length > 0 and base + length <= start + size,
                "Block hole, overlap or range escape")
        require(block.get("node") == slow and block.get("state") == "online"
                and block.get("zone") == "Movable", "Block is not online Movable CXL memory")
        observed = find(actual, "name", name)
        require(all(observed.get(k) == block.get(k) for k in ("start", "size", "node", "state", "zone")),
                "Raw sysfs block differs from summary")
        require(observed.get("node_links") == [f"node{slow}"]
                and str(observed.get("valid_zones", "")).strip() == "Movable", "Ambiguous block node/zone")
        cursor += length
    require(cursor == start + size, "Incomplete DAX block coverage")
    fast_raw = find(sysfs.get("nodes", []), "node", fast)
    memtotal = re.search(r"MemTotal:\s*([0-9]+)", str(fast_raw.get("meminfo", "")))
    require(str(fast_raw.get("cpulist", "")).strip() and memtotal and int(memtotal[1]) > 0,
            "Fast node has no CPUs or online memory")
    return fast, slow


def _validate_migration(shared: Path, run_id: str) -> dict:
    """Raise ValueError on incomplete/contradictory evidence; caller proves Guest identity."""
    shared = Path(shared)
    topology = document(shared, "topology.json", run_id)
    report = document(shared, "migration.json", run_id)
    fast, slow = validate_topology(shared, topology, run_id)
    require(report.get("status") == "PASS" and report.get("managed_bytes") == BYTES
            and report.get("page_size") == PAGE_SIZE and report.get("fast_node") == fast
            and report.get("slow_node") == slow, "Wrong migration profile or nodes")
    require(report.get("integrity_failures") == 0 and report.get("query_missing_count") == 0,
            "Integrity or query failures")
    try:
        events = [json.loads(line) for line in load(shared, report.get("raw_file", "migration.jsonl")).splitlines()]
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid raw migration JSONL") from exc
    require(events, "Empty raw migration evidence")
    for event in events:
        check_header(event, run_id)
        require(event.get("kind") in {"snapshot", "pattern", "batch", "negative"}, "Unknown raw event kind")
    require(sum(e["kind"] == "snapshot" for e in events) == 3
            and sum(e["kind"] == "pattern" for e in events) == 4
            and sum(e["kind"] == "negative" for e in events) == 3, "Unexpected raw event count")
    require(all(type(e.get("leg")) is int and e["leg"] in (0, 1)
                for e in events if e["kind"] == "batch"), "Invalid raw leg")
    for stage, node in (("initial", fast), ("slow", slow), ("final", fast)):
        snapshots = [e for e in events if e.get("kind") == "snapshot" and e.get("stage") == stage]
        require(len(snapshots) == 1, f"Missing/duplicate {stage} snapshot")
        snap = snapshots[0]
        require(snap.get("query_rc") == 0 and snap.get("query_errno") == 0
                and snap.get("integrity_failures") == 0, "Failed snapshot query/integrity")
        nodes = snap.get("nodes", [])
        require(len(nodes) == PAGES and all(type(n) is int and n == node for n in nodes),
                f"Incomplete or wrong {stage} residency")
        require(report.get(stage + "_node_counts") == {str(node): PAGES}, "Snapshot count mismatch")
    patterns = [("initial", 0), ("slow_preserved", 0), ("slow_rewrite", 1), ("final", 1)]
    for stage, generation in patterns:
        pattern = [e for e in events if e.get("kind") == "pattern" and e.get("stage") == stage]
        require(len(pattern) == 1 and pattern[0].get("generation") == generation
                and pattern[0].get("checked_bytes") == BYTES
                and pattern[0].get("integrity_failures") == 0, "Missing/full-scan pattern evidence")
    summaries = report.get("legs", [])
    require(len(summaries) == 2, "Need both migration legs")
    verified_legs = []
    for leg, (source, target) in enumerate(((fast, slow), (slow, fast))):
        attempted, succeeded = set(), set()
        batches = [e for e in events if e.get("kind") == "batch" and e.get("leg") == leg]
        require(batches, "Missing raw migration batches")
        for index, batch in enumerate(batches):
            require(batch.get("batch_index") == index and batch.get("source_node") == source
                    and batch.get("target_node") == target, "Wrong batch order or nodes")
            rc = integer(batch.get("syscall_rc"), "Missing syscall rc")
            require(type(batch.get("syscall_errno")) is int and (rc >= 0 or batch["syscall_errno"] > 0), "Missing syscall errno")
            require(all(batch.get(p + "_query_rc") == 0 and batch.get(p + "_query_errno") == 0
                        for p in ("before", "after")), "Batch query failed")
            pages = batch.get("pages", [])
            require(0 < len(pages) <= 256, "Invalid migration batch size")
            for page in pages:
                pid = integer(page.get("page_id"), "Invalid page id")
                require(0 <= pid < PAGES and pid not in attempted, "Repeated/out-of-range attempted page")
                attempted.add(pid)
                status = integer(page.get("move_status"), "Missing raw move status")
                require(type(page.get("move_status_valid")) is bool
                        and page["move_status_valid"] == (rc >= 0 and status != SENTINEL), "Invalid status validity")
                require(page.get("query_before_node") == source
                        and page.get("query_after_node") in (source, target), "Unknown/wrong batch residency")
                if page["query_after_node"] == target:
                    succeeded.add(pid)
        summary = summaries[leg]
        require(len(attempted) == PAGES and summary.get("eligible_pages") == PAGES
                and summary.get("attempted_pages") == len(attempted)
                and summary.get("succeeded_pages") == len(succeeded)
                and summary.get("source_node") == source and summary.get("target_node") == target,
                "Migration counts or fixed eligibility disagree with raw evidence")
        ratio = len(succeeded) / PAGES
        stated = summary.get("success_ratio")
        require(type(stated) in (int, float) and math.isclose(stated, ratio, abs_tol=1e-12)
                and ratio >= .95, "Migration success ratio mismatch/below 95%")
        verified_legs.append({"eligible_pages": PAGES, "attempted_pages": len(attempted),
                              "succeeded_pages": len(succeeded), "success_ratio": ratio})
    controls = report.get("negative_controls", [])
    require({c.get("name") for c in controls} == {"invalid_target", "invalid_va", "unmapped_va"}
            and len(controls) == 3, "Missing negative controls")
    for control in controls:
        raw = [e for e in events if e.get("kind") == "negative" and e.get("name") == control["name"]]
        require(len(raw) == 1 and control.get("passed") is True, "Negative control did not pass")
        payload = {k: v for k, v in raw[0].items()
                   if k not in {"kind", "schema_version", "run_id", "scenario", "evidence_basis"}}
        require(payload == control, "Negative summary differs from raw evidence")
        result = control.get("raw", control)
        rc, status = result.get("syscall_rc"), result.get("move_status")
        require(type(rc) is int and type(status) is int
                and type(result.get("query_before_node")) is int and type(result.get("query_after_node")) is int,
                "Negative control lacks raw syscall/query evidence")
        require(type(result.get("move_status_valid")) is bool
                and result["move_status_valid"] == (rc >= 0 and status != SENTINEL)
                and ((rc < 0 and result.get("syscall_errno", 0) > 0)
                     or (result.get("move_status_valid") is True and status < 0)), "Negative control has no actual error")
        if control["name"] == "invalid_target":
            require(type(result.get("requested_node")) is int and result["requested_node"] not in (fast, slow)
                    and result.get("query_before_rc") == 0 and result.get("query_after_rc") == 0
                    and result["query_before_node"] == fast and result["query_after_node"] == fast,
                    "Invalid-target control changed managed placement")
        else:
            require(type(result.get("query_before_rc")) is int and type(result.get("query_after_rc")) is int
                    and result["query_before_node"] < 0 and result["query_after_node"] < 0,
                    "Invalid-VA control unexpectedly refers to resident memory")
    position = {(e["kind"], e.get("stage")): i for i, e in enumerate(events)
                if e["kind"] in {"snapshot", "pattern"}}
    legs = [[i for i, e in enumerate(events) if e["kind"] == "batch" and e["leg"] == leg]
            for leg in (0, 1)]
    require(position["snapshot", "initial"] < min(legs[0])
            and max(legs[0]) < position["snapshot", "slow"]
            < position["pattern", "slow_rewrite"] < min(legs[1])
            and max(legs[1]) < position["snapshot", "final"]
            and position["pattern", "initial"] < min(legs[0])
            and max(legs[0]) < position["pattern", "slow_preserved"]
            < position["pattern", "slow_rewrite"]
            and position["pattern", "final"] > max(legs[1]),
            "Raw operation order does not prove CXL writes and round-trip migration")
    return {"status": "PASS", "gates": {"G1": "PASS", "G2": "PASS"},
            "fast_node": fast, "slow_node": slow, "managed_bytes": BYTES,
            "page_count": PAGES, "legs": verified_legs, "evidence_basis": "guest_kernel_measured"}


def validate_migration(shared: Path, run_id: str) -> dict:
    """Validate M1 artifact shapes and observations; never perform Guest operations."""
    try:
        return _validate_migration(Path(shared), run_id)
    except (KeyError, TypeError, AttributeError, IndexError, OverflowError) as exc:
        raise ValueError(f"Malformed M1 evidence: {exc}") from exc
