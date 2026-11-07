#include "guest_runtime.hpp"
#include "policy.hpp"
#include "workload.hpp"
#include <algorithm>
#include <array>
#include <cstring>
#include <cctype>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <sstream>

using namespace tierlab;
namespace {
constexpr std::size_t bytes = policy_pages * page_bytes;
std::string setting(const fs::path& path) {
    if (!fs::exists(path)) return "unavailable";
    auto value = text_file(path);
    while (!value.empty() && std::isspace(static_cast<unsigned char>(value.back()))) value.pop_back();
    return value;
}
json window_counters() {
    json counters = json::object();
    std::istringstream input(text_file("/proc/vmstat"));
    std::string key;
    std::uint64_t value;
    const std::array<std::string, 6> names{{"pgmigrate_success", "pgmigrate_fail", "pswpin",
        "pswpout", "pgscan_kswapd", "pgscan_direct"}};
    while (input >> key >> value)
        if (std::find(names.begin(), names.end(), key) != names.end()) counters[key] = value;
    require(counters.size() == names.size(), "Missing kernel interference counters");
    return counters;
}
json query_json(const QueryResult& query) {
    return {{"rc", query.rc}, {"errno", query.error}, {"nodes", query.status}};
}
void valid_query(const QueryResult& query, int fast, int slow) {
    require(query.rc == 0 && query.error == 0 && query.status.size() == policy_pages &&
        std::all_of(query.status.begin(), query.status.end(), [=](int n) { return n == fast || n == slow; }),
        "Incomplete or foreign-node kernel query");
}
double ready_hfr(const std::vector<int>& nodes, const std::vector<std::uint64_t>& hot, int fast) {
    return static_cast<double>(std::count_if(hot.begin(), hot.end(),
        [&](auto id) { return nodes[id] == fast; })) / hot.size();
}
double median(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    return (values[4] + values[5]) / 2.0;
}
}  // namespace

int run_policy(int fast, int slow, const fs::path& output, const std::string& run_id) {
    json report{{"schema_version", 1}, {"run_id", run_id}, {"scenario", "policy"},
        {"evidence_basis", "guest_kernel_measured"}, {"status", "FAIL"},
        {"managed_bytes", bytes}, {"page_size", page_bytes}, {"fast_node", fast}, {"slow_node", slow},
        {"rng", "splitmix64-rejection-v1"}, {"raw_file", "policy.jsonl"},
        {"experiments", json::array()}};
    try {
        check_node(fast);
        check_node(slow);
        fs::create_directories(output);
        std::ofstream raw(output / "policy.jsonl");
        require(static_cast<bool>(raw), "Unable to open raw policy evidence");
        auto emit = [&](json event) {
            event["schema_version"] = 1;
            event["run_id"] = run_id;
            event["scenario"] = "policy";
            event["evidence_basis"] = "guest_kernel_measured";
            raw << event.dump() << '\n';
            raw.flush();
            require(static_cast<bool>(raw), "Unable to write raw policy evidence");
        };
        const auto balancing = setting("/proc/sys/kernel/numa_balancing");
        const auto ksm = setting("/sys/kernel/mm/ksm/run");
        const auto demotion = setting("/sys/kernel/mm/numa/demotion_enabled");
        std::istringstream meminfo(text_file("/proc/meminfo"));
        std::string line;
        std::uint64_t swap_kb = std::numeric_limits<std::uint64_t>::max();
        while (std::getline(meminfo, line))
            if (line.rfind("SwapTotal:", 0) == 0) swap_kb = std::stoull(line.substr(10));
        emit({{"kind", "environment"}, {"numa_balancing", balancing}, {"ksm_run", ksm},
            {"demotion_enabled", demotion}, {"swap_total_kb", swap_kb}});
        require((balancing == "0" || balancing == "unavailable") &&
            (ksm == "0" || ksm == "unavailable") &&
            (demotion == "false" || demotion == "unavailable") && swap_kb == 0,
            "Requires disabled automatic migration/KSM and no swap");
        std::vector<std::uint64_t> ids(policy_pages);
        std::iota(ids.begin(), ids.end(), 0);
        for (const unsigned seed : {1, 7, 42}) {
            for (const std::string baseline : {"B0", "B1", "B2"}) {
                Workload workload(seed);
                PolicyCore core(policy_pages, fast, slow, fast_cap, epoch_budget);
                Mapping mapping(bytes, slow);
                LinuxMigrator migrator(reinterpret_cast<std::uintptr_t>(mapping.data()), bytes, page_bytes);
                auto reject = [&](const std::string& reason, json evidence) {
                    evidence["kind"] = "failure";
                    evidence["seed"] = seed;
                    evidence["baseline"] = baseline;
                    evidence["reason"] = reason;
                    emit(evidence);
                    throw std::runtime_error(reason);
                };
                auto snapshot = [&](const std::string& stage, int epoch) {
                    const auto result = migrator.query(ids);
                    try { valid_query(result, fast, slow); }
                    catch (const std::exception& error) {
                        reject(error.what(), {{"stage", stage}, {"epoch", epoch}, {"query", query_json(result)}});
                    }
                    return result;
                };
                write_pattern(mapping.data(), seed, bytes);
                require_no_huge_pages(mapping.data(), bytes);
                auto initial = snapshot("initial", -1);
                require(std::all_of(initial.status.begin(), initial.status.end(),
                    [=](int n) { return n == slow; }), "Every baseline must start entirely slow");
                std::vector<int> nodes = initial.status;
                std::uint64_t total_attempted = 0, total_succeeded = 0;
                auto execute = [&](const std::vector<std::uint64_t>& selected, int target,
                                   int epoch, json& batches) {
                    std::size_t succeeded = 0;
                    try {
                    for (std::size_t begin = 0; begin < selected.size(); begin += batch_limit) {
                        const auto end = std::min(selected.size(), begin + batch_limit);
                        std::vector<std::uint64_t> batch(selected.begin() + begin, selected.begin() + end);
                        const auto result = migrator.move(batch, target);
                        json pages = json::array();
                        for (const auto& page : result.pages) pages.push_back({
                            {"page_id", page.page_id}, {"query_before_node", page.query_before_node},
                            {"move_status", page.move_status}, {"move_status_valid", page.move_status_valid},
                            {"query_after_node", page.query_after_node}});
                        batches.push_back({{"target_node", target}, {"direction", target == fast ? "promote" : "demote"},
                            {"syscall_rc", result.syscall_rc}, {"syscall_errno", result.syscall_errno},
                            {"before_query_rc", result.before.rc}, {"before_query_errno", result.before.error},
                            {"after_query_rc", result.after.rc}, {"after_query_errno", result.after.error},
                            {"pages", pages}});
                        require(result.before.rc == 0 && result.before.error == 0 &&
                            result.after.rc == 0 && result.after.error == 0, "Batch query failed");
                        for (const auto& page : result.pages) {
                            require(page.query_before_node == nodes.at(page.page_id) &&
                                (page.query_after_node == fast || page.query_after_node == slow),
                                "Batch kernel query differs from committed observation");
                            succeeded += page.query_before_node != target && page.query_after_node == target;
                            nodes[page.page_id] = page.query_after_node;
                        }
                        if (baseline == "B2") core.reconcile(static_cast<unsigned>(epoch), target, result);
                        require(static_cast<std::size_t>(std::count(nodes.begin(), nodes.end(), fast)) <= fast_cap,
                            "Fast cap exceeded after a batch");
                    }
                    } catch (const std::exception& error) {
                        reject(error.what(), {{"stage", "migration"}, {"epoch", epoch}, {"batches", batches}});
                    }
                    total_attempted += selected.size();
                    total_succeeded += succeeded;
                    return succeeded;
                };
                json initial_batches = json::array();
                if (baseline == "B1") execute(workload.hot(0), fast, -1, initial_batches);
                auto ready = snapshot("ready", -1);
                if (ready.status != nodes) reject("Initial placement query differs from batch truth",
                    {{"epoch", -1}, {"query", query_json(ready)}, {"batches", initial_batches}});
                const auto initial_errors = verify_pattern(mapping.data(), seed, bytes);
                emit({{"kind", "start"}, {"seed", seed}, {"baseline", baseline},
                    {"initial", query_json(initial)}, {"ready", query_json(ready)},
                    {"batches", initial_batches}, {"scan_bytes", bytes},
                    {"scan_integrity_failures", initial_errors}});
                require(initial_errors == 0, "Initial payload corruption");
                std::vector<double> steady;
                json recovery_epoch = nullptr;
                for (unsigned epoch = 0; epoch < epoch_count; ++epoch) {
                    const auto before = snapshot("before", static_cast<int>(epoch));
                    if (before.status != nodes) reject("Placement changed between epochs",
                        {{"epoch", epoch}, {"before", query_json(before)}, {"previous_nodes", nodes}});
                    const auto trace = workload.next_epoch(epoch);
                    const auto counters_before = window_counters();
                    std::uint64_t access_errors = 0, fast_accesses = 0;
                    for (const auto& access : trace.accesses) {
                        const std::size_t offset = static_cast<std::size_t>(access.page) * page_bytes +
                            access.word * sizeof(std::uint64_t);
                        std::uint64_t value;
                        std::memcpy(&value, static_cast<const unsigned char*>(mapping.data()) + offset, sizeof(value));
                        access_errors += value != pattern(offset, seed);
                        fast_accesses += before.status[access.page] == fast;
                    }
                    const auto window_end = snapshot("window_end", static_cast<int>(epoch));
                    const auto counters_end = window_counters();
                    if (window_end.status != before.status || counters_before != counters_end || access_errors)
                        reject("INVALID_RESIDENCY_WINDOW or workload payload corruption",
                            {{"epoch", epoch}, {"counts", trace.counts}, {"before", query_json(before)},
                             {"window_end", query_json(window_end)}, {"window_counters_before", counters_before},
                             {"window_counters_end", counters_end}, {"access_integrity_failures", access_errors}});
                    json batches = json::array();
                    std::size_t attempted = 0, succeeded = 0;
                    if (baseline == "B2") {
                        core.observe(epoch, trace.counts, window_end.status);
                        const auto demote = core.select_demotions(epoch, nodes, epoch_budget);
                        attempted += demote.size();
                        succeeded += execute(demote, slow, static_cast<int>(epoch), batches);
                        // nodes contains only query-after truth: failed demotions never free slots.
                        const auto promote = core.select_promotions(epoch, nodes, epoch_budget - attempted);
                        attempted += promote.size();
                        succeeded += execute(promote, fast, static_cast<int>(epoch), batches);
                    }
                    const auto after = snapshot("after", static_cast<int>(epoch));
                    if (after.status != nodes) reject("Epoch final query differs from batch truth",
                        {{"epoch", epoch}, {"after", query_json(after)}, {"batches", batches}});
                    const auto scan_bytes = epoch == 29 || epoch == 59 ? bytes : 0;
                    const auto scan_errors = scan_bytes ? verify_pattern(mapping.data(), seed, bytes) : 0;
                    const double awfr = static_cast<double>(fast_accesses) / epoch_ops;
                    const double hfr = ready_hfr(after.status, workload.hot(epoch), fast);
                    if (epoch >= 50) steady.push_back(hfr);
                    if (epoch >= 30 && epoch < 50 && hfr >= 0.80 && recovery_epoch.is_null()) recovery_epoch = epoch;
                    std::ostringstream hash;
                    hash << std::hex << std::setfill('0') << std::setw(16) << trace.hash;
                    emit({{"kind", "epoch"}, {"seed", seed}, {"baseline", baseline}, {"epoch", epoch},
                        {"counts", trace.counts}, {"before", query_json(before)},
                        {"window_end", query_json(window_end)}, {"after", query_json(after)},
                        {"batches", batches}, {"trace_hash", hash.str()}, {"access_integrity_failures", access_errors},
                        {"scan_bytes", scan_bytes}, {"scan_integrity_failures", scan_errors},
                        {"window_counters_before", counters_before}, {"window_counters_end", counters_end},
                        {"awfr_during_epoch", awfr}, {"ready_hfr", hfr},
                        {"attempted_pages", attempted}, {"succeeded_pages", succeeded}});
                    require(scan_errors == 0 && attempted <= epoch_budget, "Integrity or attempt budget failure");
                }
                require_no_huge_pages(mapping.data(), bytes);
                const double steady_hfr = median(steady);
                report["experiments"].push_back({{"seed", seed}, {"baseline", baseline}, {"epochs", epoch_count},
                    {"last10_ready_hfr_median", steady_hfr}, {"recovery_epoch", recovery_epoch},
                    {"attempted_pages", total_attempted}, {"succeeded_pages", total_succeeded}});
                std::cout << "Policy seed=" << seed << " baseline=" << baseline << " last10_ready_hfr="
                    << steady_hfr << " recovery_epoch=" << recovery_epoch << '\n';
                if (baseline == "B2") require(steady_hfr >= 0.85 && !recovery_epoch.is_null(),
                    "B2 failed the unchanged G3 HFR thresholds");
            }
        }
        report["status"] = "PASS";
    } catch (const std::exception& error) {
        report["reason"] = error.what();
        std::cerr << "Policy FAIL: " << error.what() << '\n';
    }
    try {
        fs::create_directories(output);
        std::ofstream stream(output / "policy.json.tmp");
        stream << report.dump(2) << '\n';
        stream.close();
        require(static_cast<bool>(stream), "Unable to write policy report");
        fs::rename(output / "policy.json.tmp", output / "policy.json");
    } catch (const std::exception& error) {
        std::cerr << "Policy report FAIL: " << error.what() << '\n';
        return 1;
    }
    return report["status"] == "PASS" ? 0 : 1;
}
