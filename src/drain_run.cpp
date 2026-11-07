#include "drain.hpp"
#include "guest_runtime.hpp"
#include <algorithm>
#include <chrono>
#include <fstream>
#include <iostream>
#include <numeric>
#include <regex>

using namespace tierlab;
namespace {
constexpr std::size_t drain_bytes = 128ULL * 1024 * 1024;
constexpr std::size_t drain_pages = drain_bytes / page_bytes;
constexpr unsigned generation = 7;
constexpr std::uint64_t deadline_ms = 60000;
std::uint64_t memory_bytes(const std::string& text, const std::string& key) {
    std::smatch match;
    require(std::regex_search(text, match, std::regex(key + R"(:\s+([0-9]+)\s+kB)")),
            "Missing capacity field " + key);
    const auto kb = std::stoull(match[1]);
    require(kb <= std::numeric_limits<std::uint64_t>::max() / 1024, "Capacity field overflow");
    return kb * 1024;
}
}

int run_drain(int fast, int slow, const fs::path& output, const std::string& run_id) {
    json report{{"schema_version", 1}, {"run_id", run_id}, {"scenario", "offline"},
        {"evidence_basis", "guest_kernel_measured"}, {"status", "FAIL"},
        {"lifecycle_state", "ACTIVE"}, {"managed_bytes", drain_bytes}, {"page_size", page_bytes},
        {"page_count", drain_pages}, {"fast_node", fast}, {"slow_node", slow},
        {"initial_slow_pages", 0}, {"final_slow_pages", drain_pages},
        {"attempted_pages", 0}, {"succeeded_pages", 0}, {"integrity_failures", 0},
        {"query_missing_count", 0}, {"workload_stopped", false}, {"mapping_released", false},
        {"raw_file", "drain.jsonl"}, {"deadline_ms", deadline_ms},
        {"max_attempts_per_page", drain_attempt_limit}, {"elapsed_ms", 0}};
    DrainController controller(drain_pages, fast, slow);
    const auto started = std::chrono::steady_clock::now();
    auto elapsed = [&]() {
        return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - started).count());
    };
    try {
        check_node(fast);
        check_node(slow);
        fs::create_directories(output);
        std::ofstream raw(output / "drain.jsonl");
        require(static_cast<bool>(raw), "Unable to open raw drain evidence");
        auto emit = [&](json event) {
            event["schema_version"] = 1;
            event["run_id"] = run_id;
            event["scenario"] = "offline";
            event["evidence_basis"] = "guest_kernel_measured";
            raw << event.dump() << '\n';
            raw.flush();
            require(static_cast<bool>(raw), "Unable to write raw drain evidence");
        };
        auto check_deadline = [&]() {
            if (elapsed() >= deadline_ms) {
                emit({{"kind", "failure"}, {"reason", "Drain deadline expired"}, {"elapsed_ms", elapsed()}});
                throw std::runtime_error("Drain deadline expired");
            }
        };
        Mapping mapping(drain_bytes, slow);
        LinuxMigrator migrator(reinterpret_cast<std::uintptr_t>(mapping.data()), drain_bytes, page_bytes);
        std::vector<std::uint64_t> ids(drain_pages);
        std::iota(ids.begin(), ids.end(), 0);
        write_pattern(mapping.data(), generation, drain_bytes);
        require_no_huge_pages(mapping.data(), drain_bytes);
        auto scan = [&](const std::string& stage) {
            const auto failures = verify_pattern(mapping.data(), generation, drain_bytes);
            report["integrity_failures"] = report["integrity_failures"].get<std::uint64_t>() + failures;
            emit({{"kind", "pattern"}, {"stage", stage}, {"generation", generation},
                {"checked_bytes", drain_bytes}, {"integrity_failures", failures}});
            require(failures == 0, "Drain pattern mismatch at " + stage);
        };
        auto snapshot = [&](const std::string& stage) {
            const auto query = migrator.query(ids);
            emit({{"kind", "snapshot"}, {"stage", stage}, {"query_rc", query.rc},
                {"query_errno", query.error}, {"nodes", query.status}});
            std::size_t missing = 0;
            for (std::size_t i = 0; i < ids.size(); ++i) missing += query.node(i) < 0;
            report["query_missing_count"] = report["query_missing_count"].get<std::uint64_t>() + missing;
            require(query.rc == 0 && query.error == 0 && missing == 0 &&
                std::all_of(query.status.begin(), query.status.end(),
                    [=](int node) { return node == fast || node == slow; }), "Incomplete drain kernel query");
            return query.status;
        };
        scan("initial");
        auto nodes = snapshot("initial");
        const auto initial_slow = static_cast<std::size_t>(std::count(nodes.begin(), nodes.end(), slow));
        report["initial_slow_pages"] = initial_slow;
        require(initial_slow == drain_pages, "Drain mapping must start entirely on CXL");
        const auto node_meminfo = text_file("/sys/devices/system/node/node" + std::to_string(fast) + "/meminfo");
        const auto meminfo = text_file("/proc/meminfo");
        const auto capacity = controller.begin(nodes, memory_bytes(meminfo, "MemAvailable"),
            memory_bytes(node_meminfo, "MemFree"));
        report["lifecycle_state"] = "DRAINING";
        json capacity_json{{"node_free_bytes", capacity.fast_free_bytes},
            {"mem_available_bytes", capacity.mem_available_bytes}, {"slow_bytes", initial_slow * page_bytes},
            {"safety_margin_bytes", drain_margin_bytes}, {"required_bytes", capacity.required_bytes},
            {"passed", capacity.enough}};
        report["capacity"] = capacity_json;
        capacity_json["kind"] = "capacity";
        capacity_json["node_meminfo"] = node_meminfo;
        capacity_json["meminfo"] = meminfo;
        emit(capacity_json);
        require(capacity.enough, "Insufficient fast memory capacity; no migration or offline permitted");
        check_deadline();
        std::size_t batch_index = 0, attempted = 0, succeeded = 0;
        while (std::count(nodes.begin(), nodes.end(), slow)) {
            check_deadline();
            const auto selected = controller.select(nodes);
            require(!selected.empty(), "Drain page retry limit exhausted");
            controller.require_target(fast);
            const auto result = migrator.move(selected, fast);
            json pages = json::array();
            for (const auto& page : result.pages) pages.push_back({
                {"page_id", page.page_id}, {"query_before_node", page.query_before_node},
                {"move_status", page.move_status}, {"move_status_valid", page.move_status_valid},
                {"query_after_node", page.query_after_node}});
            emit({{"kind", "batch"}, {"batch_index", batch_index++}, {"source_node", slow}, {"target_node", fast},
                {"syscall_rc", result.syscall_rc}, {"syscall_errno", result.syscall_errno},
                {"before_query_rc", result.before.rc}, {"before_query_errno", result.before.error},
                {"after_query_rc", result.after.rc}, {"after_query_errno", result.after.error}, {"pages", pages}});
            attempted += selected.size();
            report["attempted_pages"] = attempted;
            for (const auto& page : result.pages) {
                report["query_missing_count"] = report["query_missing_count"].get<std::uint64_t>() +
                    static_cast<unsigned>(page.query_before_node < 0) + static_cast<unsigned>(page.query_after_node < 0);
                require(page.query_before_node == nodes.at(page.page_id), "Drain placement changed before batch");
            }
            controller.reconcile(result);
            for (const auto& page : result.pages) {
                succeeded += page.query_before_node == slow && page.query_after_node == fast;
                nodes[page.page_id] = page.query_after_node;
            }
            report["succeeded_pages"] = succeeded;
            check_deadline();
        }
        const auto final_nodes = snapshot("final");
        require(final_nodes == nodes, "Drain final query differs from batch observations");
        report["final_slow_pages"] = std::count(final_nodes.begin(), final_nodes.end(), slow);
        require(report["final_slow_pages"] == 0, "Managed pages remain on CXL");
        scan("final");
        require_no_huge_pages(mapping.data(), drain_bytes);
        check_deadline();
        // This scenario has no concurrent worker or policy; no workload access follows this point.
        report["workload_stopped"] = true;
        mapping.release();
        report["mapping_released"] = true;
        check_deadline();
        controller.finish(final_nodes, true, true);
        report["elapsed_ms"] = elapsed();
        emit({{"kind", "release"}, {"workload_stopped", true}, {"mapping_released", true},
            {"managed_slow_pages", 0}, {"lifecycle_state", "DRAINING"}, {"elapsed_ms", report["elapsed_ms"]}});
        report["status"] = "PASS";
    } catch (const std::exception& error) {
        controller.fail();
        report["lifecycle_state"] = "FAILED";
        report["reason"] = error.what();
        report["elapsed_ms"] = elapsed();
        std::cerr << "Drain FAIL: " << error.what() << '\n';
    }
    try {
        fs::create_directories(output);
        std::ofstream stream(output / "drain.json.tmp");
        stream << report.dump(2) << '\n';
        stream.close();
        require(static_cast<bool>(stream), "Unable to write drain report");
        fs::rename(output / "drain.json.tmp", output / "drain.json");
    } catch (const std::exception& error) {
        std::cerr << "Drain report FAIL: " << error.what() << '\n';
        return 1;
    }
    std::cout << "Drain scenario: " << report["status"].get<std::string>() << '\n';
    return report["status"] == "PASS" ? 0 : 1;
}
