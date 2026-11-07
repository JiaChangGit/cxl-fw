#!/usr/bin/env python3
"""Recompute G3 from streamed Guest kernel observations, never a policy cache."""
import json
from pathlib import Path
import statistics

from validate_migration import document, integer, require, validate_topology

PAGE_SIZE = 4096
PAGES = 32768
HOT_PAGES = 6144
FAST_CAP = 8192
ATTEMPT_BUDGET = 2048
OPERATIONS = 25000
EPOCHS = 60
SEEDS = (1, 7, 42)
BASELINES = ("B0", "B1", "B2")
SENTINEL = -(1 << 31)
MAX_LINE = 8 * 1024 * 1024
MAX_RAW = 512 * 1024 * 1024
MASK = (1 << 64) - 1
WINDOW_COUNTERS = ("pgmigrate_success", "pgmigrate_fail", "pswpin", "pswpout",
                   "pgscan_kswapd", "pgscan_direct")


def header(event, run_id):
    require(isinstance(event, dict) and type(event.get("schema_version")) is int
            and event["schema_version"] == 1 and event.get("run_id") == run_id
            and event.get("scenario") == "policy"
            and event.get("evidence_basis") == "guest_kernel_measured",
            "Wrong policy schema, run_id, scenario or evidence basis")


def events(shared, filename, run_id):
    require(isinstance(filename, str) and filename, "Missing policy raw filename")
    path = (shared / filename).resolve()
    require(path.is_relative_to(shared.resolve()) and path.is_file(),
            "Missing or unsafe policy evidence path")
    require(0 < path.stat().st_size <= MAX_RAW, "Empty or oversized policy raw evidence")
    with path.open(encoding="utf-8") as stream:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            require(len(line) <= MAX_LINE and line.endswith("\n"),
                    "Truncated or oversized policy raw record")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid policy JSONL") from exc
            header(event, run_id)
            yield event


def nodes(values, fast, slow):
    require(isinstance(values, list) and len(values) == PAGES
            and all(type(node) is int and node in (fast, slow) for node in values),
            "Incomplete or invalid kernel residency query")
    require(values.count(fast) <= FAST_CAP, "Actual fast placement exceeds capacity")
    return values


def query(event, key, fast, slow):
    value = event.get(key)
    require(isinstance(value, dict) and value.get("rc") == 0
            and type(value.get("rc")) is int and value.get("errno") == 0
            and type(value.get("errno")) is int, "Failed kernel residency query")
    return nodes(value.get("nodes"), fast, slow)


def batch(event, current, fast, slow):
    """Apply only independently queried placement; raw syscall status is retained."""
    target = integer(event.get("target_node"), "Missing batch target")
    require(target in (fast, slow), "Invalid batch target")
    rc = integer(event.get("syscall_rc"), "Missing syscall return")
    error = integer(event.get("syscall_errno"), "Missing syscall errno")
    require(rc >= -1 and (error > 0 if rc == -1 else error == 0), "Invalid syscall return/errno")
    require(all(type(event.get(prefix + "_query_rc")) is int
                and event[prefix + "_query_rc"] == 0
                and type(event.get(prefix + "_query_errno")) is int
                and event[prefix + "_query_errno"] == 0 for prefix in ("before", "after")),
            "Migration batch lacks successful before/after queries")
    pages = event.get("pages")
    require(isinstance(pages, list) and 0 < len(pages) <= 256, "Invalid policy batch size")
    seen, successful = set(), 0
    for page in pages:
        pid = integer(page.get("page_id"), "Invalid attempted page")
        require(0 <= pid < PAGES and pid not in seen, "Repeated or out-of-range batch page")
        seen.add(pid)
        before = integer(page.get("query_before_node"), "Missing before-query placement")
        after = integer(page.get("query_after_node"), "Missing after-query placement")
        require(before == current[pid] and before in (fast, slow) and after in (fast, slow),
                "Batch query disagrees with actual placement")
        require(before != target and after in (before, target), "Invalid migration direction")
        status = integer(page.get("move_status"), "Missing migration status")
        require(type(page.get("move_status_valid")) is bool
                and page["move_status_valid"] == (rc >= 0 and status != SENTINEL),
                "Sentinel or syscall failure treated as a valid status")
        successful += before != after
        current[pid] = after
    require(current.count(fast) <= FAST_CAP, "Migration exceeded actual fast capacity")
    return seen, successful


class Splitmix64:
    def __init__(self, seed):
        self.state = seed & MASK

    def next(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        value = self.state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
        return value ^ (value >> 31)

    def bounded(self, bound):
        threshold = ((-bound) & MASK) % bound
        while True:
            value = self.next()
            if value >= threshold:
                return value % bound


def workload_trace(seed):
    """Replay the specified workload, independent of all policy state and residency."""
    shuffle = Splitmix64(seed ^ 0xD1B54A32D192ED03)
    pages = list(range(PAGES))
    for index in range(PAGES - 1, 0, -1):
        selected = shuffle.bounded(index + 1)
        pages[index], pages[selected] = pages[selected], pages[index]
    hot = (pages[:HOT_PAGES], pages[HOT_PAGES:2 * HOT_PAGES])
    cold = []
    for labels in hot:
        label_set = set(labels)
        cold.append([page for page in range(PAGES) if page not in label_set])
    rng = Splitmix64(seed ^ 0x94D049BB133111EB)
    trace = []
    for epoch in range(EPOCHS):
        phase = int(epoch >= 30)
        counts = [0] * PAGES
        digest = 14695981039346656037
        for _ in range(OPERATIONS):
            candidates = hot[phase] if rng.bounded(10) < 9 else cold[phase]
            page = candidates[rng.bounded(len(candidates))]
            word = rng.bounded(PAGE_SIZE // 8)
            counts[page] += 1
            digest = ((digest ^ page) * 1099511628211) & MASK
            digest = ((digest ^ word) * 1099511628211) & MASK
        trace.append((counts, f"{digest:016x}"))
    return hot, trace


def zero(value):
    return type(value) is int and value == 0


def equal_metric(actual, expected, reason):
    require(type(actual) in (int, float) and abs(actual - expected) <= 1e-12, reason)


def full_scan(event, expected):
    require(type(event.get("scan_bytes")) is int and event["scan_bytes"] == expected
            and zero(event.get("scan_integrity_failures")),
            "Missing full payload scan or integrity failure")


def apply_batches(items, current, fast, slow, baseline, epoch, hot0):
    require(isinstance(items, list), "Missing raw migration batches")
    require(baseline == "B2" or epoch == -1 or not items,
            "Static baseline migrated during workload")
    attempted, succeeded = set(), 0
    promoting = False
    for item in items:
        direction = item.get("direction")
        require(direction in ("demote", "promote")
                and item.get("target_node") == (slow if direction == "demote" else fast),
                "Wrong batch direction or target")
        if direction == "promote":
            promoting = True
        require(not promoting or direction == "promote", "Demotion follows promotion")
        if epoch == -1:
            require(baseline == "B1" and direction == "promote", "Invalid initial baseline moves")
        ids, copied = batch(item, current, fast, slow)
        require(not (attempted & ids), "Repeated attempted page in one epoch")
        attempted.update(ids)
        succeeded += copied
    if epoch == -1:
        require(attempted == (set(hot0) if baseline == "B1" else set()),
                "Initial baseline moves differ from initial hot labels")
    else:
        require(len(attempted) <= ATTEMPT_BUDGET, "Per-epoch attempts exceed shared budget")
    return len(attempted), succeeded


def check_environment(event):
    require(event.get("kind") == "environment" and event.get("numa_balancing") in ("0", "unavailable")
            and event.get("ksm_run") in ("0", "unavailable")
            and event.get("demotion_enabled") in ("0", "false", "unavailable")
            and zero(event.get("swap_total_kb")),
            "Automatic migration, KSM or swap invalidates residency windows")


def validate_epoch(event, current, fast, slow, baseline, epoch, hot, expected_trace):
    require(event.get("kind") == "epoch" and type(event.get("epoch")) is int
            and event["epoch"] == epoch, "Missing or out-of-order epoch")
    before = query(event, "before", fast, slow)
    end = query(event, "window_end", fast, slow)
    require(before == current and before == end, "INVALID_RESIDENCY_WINDOW: kernel endpoints changed")
    counter_before, counter_end = event.get("window_counters_before"), event.get("window_counters_end")
    require(isinstance(counter_before, dict) and isinstance(counter_end, dict)
            and set(counter_before) == set(WINDOW_COUNTERS) and set(counter_end) == set(WINDOW_COUNTERS)
            and all(type(value) is int and value >= 0
                    for counters in (counter_before, counter_end) for value in counters.values())
            and counter_before == counter_end,
            "INVALID_RESIDENCY_WINDOW: migration, reclaim or swap counters changed")
    counts = event.get("counts")
    require(isinstance(counts, list) and len(counts) == PAGES
            and all(type(count) is int and count >= 0 for count in counts)
            and sum(counts) == OPERATIONS, "Missing counts or wrong operations denominator")
    require(counts == expected_trace[0] and event.get("trace_hash") == expected_trace[1],
            "Workload counts/hash differ from deterministic trace")
    require(zero(event.get("access_integrity_failures")), "Workload access integrity failure")
    full_scan(event, PAGES * PAGE_SIZE if epoch in (29, 59) else 0)
    awfr = sum(count for count, node in zip(counts, before) if node == fast) / OPERATIONS
    attempted, succeeded = apply_batches(event.get("batches"), current, fast, slow,
                                         baseline, epoch, hot[0])
    after = query(event, "after", fast, slow)
    require(after == current, "After-query placement contradicts raw migration batches")
    if baseline == "B0":
        require(after == [slow] * PAGES, "B0 is not all slow")
    elif baseline == "B1":
        require({page for page, node in enumerate(after) if node == fast} == set(hot[0]),
                "B1 changed its initial static placement")
    labels = hot[int(epoch >= 30)]
    hfr = sum(after[page] == fast for page in labels) / HOT_PAGES
    equal_metric(event.get("awfr_during_epoch"), awfr, "AWFR uses wrong residency or denominator")
    equal_metric(event.get("ready_hfr"), hfr, "Ready HFR differs from kernel placement")
    require(type(event.get("attempted_pages")) is int and event["attempted_pages"] == attempted
            and type(event.get("succeeded_pages")) is int and event["succeeded_pages"] == succeeded,
            "Migration totals disagree with queried state changes")
    return hfr, awfr, attempted, succeeded


def _validate_policy(shared, run_id, fast_node=None, slow_node=None):
    topology = document(shared, "topology.json", run_id)
    fast, slow = validate_topology(shared, topology, run_id)
    require((fast_node is None or fast_node == fast) and (slow_node is None or slow_node == slow),
            "Policy nodes disagree with topology")
    report_path = shared / "policy.json"
    require(report_path.is_file() and report_path.stat().st_size <= 1024 * 1024,
            "Missing or oversized policy summary")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid policy summary JSON") from exc
    header(report, run_id)
    require(report.get("status") == "PASS" and report.get("managed_bytes") == PAGES * PAGE_SIZE
            and report.get("page_size") == PAGE_SIZE and report.get("fast_node") == fast
            and report.get("slow_node") == slow and report.get("rng") == "splitmix64-rejection-v1",
            "Wrong policy profile, nodes or RNG")
    summaries = report.get("experiments")
    require(isinstance(summaries, list) and len(summaries) == len(SEEDS) * len(BASELINES),
            "Missing required baseline/seed summaries")
    stream = iter(events(shared, report.get("raw_file"), run_id))

    def next_event():
        value = next(stream, None)
        require(value is not None, "Incomplete raw policy evidence")
        return value

    check_environment(next_event())
    verified = []
    for seed in SEEDS:
        hot, trace = workload_trace(seed)
        for baseline in BASELINES:
            start = next_event()
            require(start.get("kind") == "start" and start.get("seed") == seed
                    and type(start.get("seed")) is int and start.get("baseline") == baseline,
                    "Wrong baseline/seed order or missing start")
            full_scan(start, PAGES * PAGE_SIZE)
            current = query(start, "initial", fast, slow).copy()
            require(current == [slow] * PAGES, "Baseline did not start entirely on slow node")
            attempted, succeeded = apply_batches(start.get("batches"), current, fast, slow,
                                                 baseline, -1, hot[0])
            require(query(start, "ready", fast, slow) == current,
                    "Initial ready-query contradicts baseline moves")
            initial_fast = {page for page, node in enumerate(current) if node == fast}
            require(initial_fast == (set(hot[0]) if baseline == "B1" else set()),
                    "Wrong initial baseline placement")
            hfrs, awfrs = [], []
            for epoch in range(EPOCHS):
                event = next_event()
                require(event.get("seed") == seed and type(event.get("seed")) is int
                        and event.get("baseline") == baseline, "Wrong epoch baseline/seed")
                hfr, awfr, moves, copied = validate_epoch(event, current, fast, slow, baseline,
                                                         epoch, hot, trace[epoch])
                attempted += moves
                succeeded += copied
                hfrs.append(hfr)
                awfrs.append(awfr)
            median = statistics.median(hfrs[-10:])
            recovery = next((epoch for epoch in range(30, 50) if hfrs[epoch] >= 0.80), None)
            if baseline == "B2":
                require(median >= 0.85, f"G3 seed {seed}: steady-state HFR below 0.85")
                require(recovery is not None, f"G3 seed {seed}: no recovery within 20 epochs")
            summary = summaries[len(verified)]
            require(summary.get("seed") == seed and summary.get("baseline") == baseline
                    and summary.get("epochs") == EPOCHS and summary.get("recovery_epoch") == recovery
                    and summary.get("attempted_pages") == attempted
                    and summary.get("succeeded_pages") == succeeded,
                    "Baseline summary differs from raw kernel evidence")
            equal_metric(summary.get("last10_ready_hfr_median"), median,
                         "Summary steady-state HFR differs from kernel evidence")
            verified.append({"seed": seed, "baseline": baseline, "epochs": EPOCHS,
                             "last10_ready_hfr_median": median, "recovery_epoch": recovery,
                             "attempted_pages": attempted, "succeeded_pages": succeeded,
                             "awfr_all_epochs": sum(awfrs) / EPOCHS,
                             "awfr_last10": sum(awfrs[-10:]) / 10,
                             "ready_hfr": hfrs, "awfr_during_epoch": awfrs})
    require(next(stream, None) is None, "Unexpected extra raw policy evidence")
    return {"status": "PASS", "gates": {"G3": "PASS"}, "fast_node": fast, "slow_node": slow,
            "managed_bytes": PAGES * PAGE_SIZE, "page_count": PAGES,
            "experiments": verified, "evidence_basis": "guest_kernel_measured",
            "metric_basis": "kernel_placement", "rng": "splitmix64-rejection-v1"}


def validate_policy(shared: Path, run_id: str, fast_node=None, slow_node=None) -> dict:
    """Validate M2 artifacts; caller proves Guest identity and kernel health."""
    try:
        return _validate_policy(Path(shared), run_id, fast_node, slow_node)
    except (KeyError, TypeError, AttributeError, IndexError, OverflowError, OSError, UnicodeError) as exc:
        raise ValueError(f"Malformed M2 evidence: {exc}") from exc
