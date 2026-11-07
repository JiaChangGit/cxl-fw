#!/usr/bin/env python3
"""Recompute G5 from syscall, independent kernel queries, and loss-checked ftrace."""
import hashlib
import copy
import json
from pathlib import Path
import re

from validate_migration import (document as topology_document, integer, load,
                                require, validate_topology)

PAGES, PAGE_SIZE, GENERATION = 64, 4096, 11
BYTES = PAGES * PAGE_SIZE
SENTINEL = -(1 << 31)
EVENTS = ["migrate:mm_migrate_pages_start", "migrate:mm_migrate_pages"]
CASES = ["roundtrip_out", "roundtrip_back", "mixed_va", "mixed_return", "invalid_target"]
SOURCE_SHA = "4633bbda2ab95eaf2a7af4041236451d6759c72e6946265531af4b3aacefbf89"
TRACE_LINE = re.compile(r"^\s*.+-(\d+)\s+\[(\d+)\]\s+(?:\S+\s+)?(\d+\.\d+):\s+(\w+):\s*(.*)$")


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def header(value, run_id):
    require(isinstance(value, dict) and type(value.get("schema_version")) is int
            and value["schema_version"] == 1 and value.get("run_id") == run_id
            and value.get("scenario") == "trace"
            and value.get("evidence_basis") == "guest_kernel_measured",
            "Wrong G5 schema, run_id, scenario or evidence basis")


def document(shared, name, run_id):
    value = json.loads(load(shared, name))
    header(value, run_id)
    return value


def loss_stats(raw):
    require(isinstance(raw, dict) and raw, "Missing per-CPU trace stats")
    result = {}
    for cpu, text in raw.items():
        require(re.fullmatch(r"cpu[0-9]+", cpu) and isinstance(text, str), "Invalid trace CPU stats")
        fields = {}
        for name in ("overrun", "commit overrun", "dropped events"):
            matches = re.findall(r"^" + name + r":\s*([0-9]+)\s*$", text, re.M)
            require(len(matches) == 1, "Missing or ambiguous trace loss counter")
            fields[name] = int(matches[0])
        require(not any(fields.values()), "Trace buffer overrun or dropped events")
        result[cpu] = fields
    return result


def tap(text, expected):
    require(text.endswith("\n") and re.search(r"^TAP version 13\s*$", text, re.M), "Missing TAP header or truncated TAP")
    plans = re.findall(r"^1\.\.([0-9]+)\s*$", text, re.M)
    require(plans == [str(len(expected))], "Wrong TAP plan")
    outcomes = re.findall(r"^(not ok|ok)\s+([0-9]+)\s+(.*)$", text, re.M)
    require(len(outcomes) == len(expected), "Missing TAP testcase result")
    for index, (status, number, name) in enumerate(outcomes):
        require(status == "ok" and int(number) == index + 1
                and name.strip().removeprefix("- ") == expected[index]
                and not re.search(r"#\s*(SKIP|TODO)\b", name, re.I), "Failed, skipped or wrong TAP testcase")
    require(not re.search(r"^Bail out!", text, re.M), "TAP bailed out")


def validate_baseline(shared, run_id):
    report = document(shared, "upstream-selftest.json", run_id)
    binary = shared / "upstream-migration"
    require(binary.is_file() and binary.stat().st_size <= 64 * 1024**2, "Missing upstream selftest binary")
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    require(report.get("status") == "PASS" and report.get("testcase") == "migration.private_anon"
            and report.get("exit_code") == 0 and report.get("binary_sha256") == binary_hash,
            "Upstream migration selftest did not pass")
    argv = report.get("argv")
    require(isinstance(argv, list) and len(argv) == 3
            and Path(argv[0]).name == "upstream-migration"
            and argv[1:] == ["-r", "migration.private_anon"], "Wrong upstream selftest selection")
    text = load(shared, "upstream-selftest.tap")
    tap(text, ["migration.private_anon"])
    require(report.get("tap_sha256") == digest(text)
            and report.get("results") == {"planned": 1, "passed": 1, "failed": 0, "skipped": 0},
            "Upstream TAP summary disagrees with raw evidence")
    build = json.loads(load(shared, "upstream-build.json"))
    require(build.get("status") == "BUILT" and build.get("kernel_version") == "6.18.56"
            and build.get("source_sha256") == SOURCE_SHA and build.get("binary_sha256") == binary_hash,
            "Wrong upstream selftest build provenance")
    inputs = build.get("inputs_sha256")
    require(isinstance(inputs, dict) and inputs
            and any(Path(name).name == "migration.c" for name in inputs)
            and all(isinstance(name, str) and isinstance(value, str)
                    and re.fullmatch(r"[0-9a-f]{64}", value) for name, value in inputs.items()),
            "Missing upstream selftest source hashes")
    return {"testcase": "migration.private_anon", "binary_sha256": binary_hash, "status": "PASS"}


def validate_batches(shared, run_id, fast, slow):
    report = document(shared, "trace_batches.json", run_id)
    require(report.get("status") == "PASS" and report.get("raw_file") == "trace_batches.jsonl"
            and report.get("passed_cases") == 5 and report.get("managed_bytes") == BYTES
            and report.get("page_count") == PAGES and report.get("page_size") == PAGE_SIZE
            and report.get("fast_node") == fast and report.get("slow_node") == slow
            and report.get("integrity_failures") == 0 and report.get("query_missing_count") == 0,
            "Wrong trace selftest profile or result")
    raw = load(shared, "trace_batches.jsonl")
    require(raw.endswith("\n"), "Truncated raw batch evidence")
    events = [json.loads(line) for line in raw.splitlines()]
    require(len(events) == 7, "Missing or extra raw trace selftest events")
    for event in events:
        header(event, run_id)
    env, complete = events[0], events[-1]
    require(env.get("kind") == "environment" and env.get("page_count") == PAGES
            and env.get("page_size") == PAGE_SIZE and env.get("generation") == GENERATION
            and env.get("fast_node") == fast and env.get("slow_node") == slow,
            "Wrong raw trace environment")
    pid = integer(env.get("pid"), "Missing migrator PID")
    base = env.get("mapping_base")
    require(pid > 0 and isinstance(base, str) and re.fullmatch(r"0x[0-9a-fA-F]+", base), "Invalid mapping/PID")
    base = int(base, 16)
    require(base >= PAGE_SIZE and base % PAGE_SIZE == 0 and base + BYTES < (1 << 64), "Invalid managed VA range")
    require(complete.get("kind") == "complete" and complete.get("mapping_live") is True,
            "Mapping lifetime is not established")
    for endpoint in (env, complete):
        require(endpoint.get("checked_bytes") == BYTES and endpoint.get("integrity_failures") == 0,
                "Missing full payload scan")
    expected_nodes = [fast] * PAGES
    batches = []
    for index, event in enumerate(events[1:-1]):
        require(event.get("kind") == "batch" and event.get("id") == index
                and event.get("name") == CASES[index], "Wrong batch identity or order")
        require(event.get("passed") is True and event.get("marker_error", "") == "",
                "Batch failure or trace marker error contradicts PASS")
        ids = list(range(PAGES if index < 2 else 32)) + ([None] if index == 2 else [])
        target = slow if index in (0, 2) else fast if index in (1, 3) else (1 << 31) - 1
        vas = [hex(base + page * PAGE_SIZE) if page is not None else hex(PAGE_SIZE) for page in ids]
        require(event.get("request_page_ids") == ids and event.get("requested_node") == target
                and event.get("request_vas") == vas, "Wrong batch requests or VA mapping")
        for prefix in ("before", "after"):
            require(event.get(f"query_{prefix}_rc") == 0 and event.get(f"query_{prefix}_errno") == 0,
                    "Missing or failed kernel query")
            nodes = event.get(f"query_{prefix}_nodes")
            require(isinstance(nodes, list) and len(nodes) == PAGES
                    and all(type(node) is int and node in (fast, slow) for node in nodes),
                    "Missing or invalid query residency")
        require(event["query_before_nodes"] == expected_nodes, "Broken query residency chain")
        if index < 4:
            expected_nodes = [target if page < len(ids) - (index == 2) else expected_nodes[page]
                              for page in range(PAGES)]
        require(event["query_after_nodes"] == expected_nodes, "Wrong actual placement after syscall")
        rc = integer(event.get("syscall_rc"), "Missing syscall rc")
        error = integer(event.get("syscall_errno"), "Missing syscall errno")
        statuses, valid = event.get("move_status"), event.get("move_status_valid")
        require(isinstance(statuses, list) and len(statuses) == len(ids)
                and all(type(status) is int for status in statuses)
                and isinstance(valid, list) and len(valid) == len(ids)
                and all(type(flag) is bool for flag in valid)
                and valid == [rc >= 0 and status != SENTINEL for status in statuses], "Invalid raw status validity")
        if index < 4:
            require(rc == 0 and error == 0 and all(valid)
                    and statuses[:len(ids) - (index == 2)] == [target] * (len(ids) - (index == 2))
                    and (index != 2 or statuses[-1] < 0), "Migration syscall or mixed-VA rejection failed")
        else:
            require(rc == -1 and error > 0 and not any(valid), "Invalid target was not rejected before migration")
        require(event.get("checked_bytes") == BYTES and event.get("integrity_failures") == 0,
                "Payload corruption or incomplete batch scan")
        batches.append({"id": index, "name": CASES[index], "migrated_pages": (64, 64, 32, 32, 0)[index],
                        "syscall_rc": rc, "syscall_errno": error})
    tap(load(shared, "trace-selftest.tap"), CASES)
    return pid, batches


def validate_trace_stream(text, run_id, pid, batches):
    require(text and text.endswith("\n"), "Missing or truncated trace")
    markers, active, starts, finishes = [], None, 0, []
    last_time = 0
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parsed = TRACE_LINE.fullmatch(line)
        require(parsed is not None, "Unparseable or truncated trace line")
        actual_pid, _, timestamp, event, body = parsed.groups()
        require(int(actual_pid) == pid, "Wrong PID in trace")
        time_value = float(timestamp)
        require(time_value >= last_time, "Trace clock order regressed")
        last_time = time_value
        if event == "tracing_mark_write":
            marker = re.fullmatch(r"tierlab " + re.escape(run_id) + r" ([0-4]) (begin|end)", body)
            require(marker is not None, "Wrong run or batch trace marker")
            index, phase = int(marker[1]), marker[2]
            require((index, phase) == (len(markers) // 2, "begin" if len(markers) % 2 == 0 else "end"),
                    "Wrong batch marker order")
            markers.append((index, phase))
            if phase == "begin":
                require(active is None, "Nested trace batches")
                active, starts, finishes = index, 0, []
            else:
                require(active == index and starts == len(finishes), "Unpaired migration trace events")
                expected = batches[index]["migrated_pages"]
                require((starts > 0) == (expected > 0)
                        and sum(finishes) == expected, "Missing or contradictory migration trace")
                batches[index]["trace_start_events"] = starts
                batches[index]["trace_succeeded_pages"] = sum(finishes)
                active = None
        else:
            require(active is not None and event in ("mm_migrate_pages_start", "mm_migrate_pages"),
                    "Migration trace outside batch or unknown trace event")
            fields = dict(re.findall(r"(\w+)=(\S+)", body))
            require(fields.get("mode") == "MIGRATE_SYNC" and fields.get("reason") == "syscall_or_cpuset",
                    "Wrong kernel migration mode or reason")
            if event == "mm_migrate_pages_start":
                require(starts == len(finishes), "Nested migration trace calls")
                starts += 1
            else:
                require(starts == len(finishes) + 1, "Missing migration start trace")
                for key in ("nr_succeeded", "nr_failed", "nr_thp_succeeded", "nr_thp_failed", "nr_thp_split", "nr_split"):
                    require(key in fields and re.fullmatch(r"[0-9]+", fields[key]), "Missing kernel migration trace count")
                require(all(int(fields[key]) == 0 for key in fields if key.startswith("nr_") and key != "nr_succeeded"),
                        "Unexpected migration failure, THP or folio split")
                finishes.append(int(fields["nr_succeeded"]))
    require(len(markers) == 10 and active is None, "Missing or truncated batch trace markers")


def validate_trace(shared: Path, run_id: str) -> dict:
    """Caller proves Guest identity, input hashes, console and kernel health."""
    try:
        shared = Path(shared)
        topology = topology_document(shared, "topology.json", run_id)
        fast, slow = validate_topology(shared, topology, run_id)
        pid, batches = validate_batches(shared, run_id, fast, slow)
        collector = document(shared, "collector.json", run_id)
        require(collector.get("status") == "PASS" and collector.get("pid") == pid
                and collector.get("clock") == "mono" and collector.get("enabled_events") == EVENTS
                and collector.get("target_exit_code") == 0 and collector.get("cleanup_success") is True
                and collector.get("eof_drained") is True, "Incomplete trace collection or cleanup")
        before, after = loss_stats(collector.get("stats_before")), loss_stats(collector.get("stats_after"))
        require(set(before) == set(after), "Missing final per-CPU trace stats")
        formats = collector.get("format_sha256")
        require(isinstance(formats, dict) and set(formats) == {event.split(":")[1] for event in EVENTS}, "Missing trace event formats")
        for event, value in formats.items():
            text = load(shared, f"trace_formats/{event}.format")
            require(digest(text) == value and re.search(r"^name:\s*" + event + r"\s*$", text, re.M)
                    and "common_pid" in text and "reason" in text and "mode" in text,
                    "Trace event format hash or identity mismatch")
        config = load(shared, "kernel.config")
        require(digest(config) == collector.get("kernel_config_sha256")
                and all(re.search(r"^" + symbol + r"=y$", config, re.M) for symbol in
                        ("CONFIG_MIGRATION", "CONFIG_TRACEPOINTS", "CONFIG_EVENT_TRACING", "CONFIG_FTRACE")),
                "Missing matching trace kernel configuration")
        trace = load(shared, "trace.log")
        require(collector.get("trace_sha256") == digest(trace)
                and collector.get("trace_bytes") == len(trace.encode()), "Trace hash or length mismatch")
        validate_trace_stream(trace, run_id, pid, batches)
        baseline = validate_baseline(shared, run_id)
        return {"status": "PASS", "gates": {"G5": "PASS"}, "fast_node": fast, "slow_node": slow,
                "managed_bytes": BYTES, "page_count": PAGES, "pid": pid, "batches": batches,
                "upstream_selftest": baseline, "trace_sha256": digest(trace), "loss_counters": after,
                "trace_scope": "aggregate migration calls, per-VA placement from independent queries",
                "evidence_basis": "guest_kernel_measured"}
    except (KeyError, TypeError, AttributeError, IndexError, OverflowError, OSError,
            UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Malformed G5 evidence: {exc}") from exc


def trace_negative_controls(shared: Path, run_id: str, verified: dict) -> list:
    """Corrupt copies in memory after a real run; preserve the original evidence."""
    text = load(Path(shared), "trace.log")
    lines = text.splitlines(keepends=True)
    completion = next((index for index, line in enumerate(lines)
                       if ": mm_migrate_pages:" in line), None)
    require(completion is not None and text.endswith("\n"), "No complete trace for negative controls")
    wrong_marker = text.replace(f"tierlab {run_id} 0 begin", f"tierlab {run_id} 1 begin", 1)
    mutations = [("missing_migration_event", "".join(lines[:completion] + lines[completion + 1:])),
                 ("wrong_batch_marker", wrong_marker), ("truncated_trace", text[:-8])]
    results = []
    for name, altered in mutations:
        require(altered != text, "Negative control did not alter trace")
        try:
            validate_trace_stream(altered, run_id, verified["pid"], copy.deepcopy(verified["batches"]))
        except ValueError as exc:
            results.append({"name": name, "status": "REJECTED", "reason": str(exc),
                            "evidence_basis": "modeled"})
        else:
            raise ValueError("Trace corruption was not rejected: " + name)
    return results
