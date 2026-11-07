#include "guest_runtime.hpp"
#include <nlohmann/json.hpp>
#include <numaif.h>
#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <numeric>
#include <regex>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <unistd.h>

using namespace tierlab;

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--help") {
        std::cout << "tierlab {migrate|policy|offline|trace} --fast-node N --slow-node N --output DIR --run-id RUN\n";
        return 0;
    }
    int fast = -1, slow = -1;
    fs::path output;
    std::string run_id;
    try {
        require(argc == 10 && (std::string(argv[1]) == "migrate" || std::string(argv[1]) == "policy" ||
                std::string(argv[1]) == "offline" || std::string(argv[1]) == "trace"), "Invalid scenario arguments");
        for (int i = 2; i < argc; i += 2) {
            const std::string key(argv[i]), value(argv[i + 1]);
            if (key == "--fast-node" || key == "--slow-node") {
                require(std::regex_match(value, std::regex("[0-9]{1,4}")), "Invalid node value");
                (key == "--fast-node" ? fast : slow) = std::stoi(value);
            } else if (key == "--output") output = value;
            else if (key == "--run-id") run_id = value;
            else throw std::runtime_error("Unknown argument: " + key);
        }
        require(fast >= 0 && slow >= 0 && fast != slow && !output.empty() &&
                std::regex_match(run_id, std::regex("[A-Za-z0-9][A-Za-z0-9._-]{0,79}")),
                "Missing or invalid required arguments");
        guest_guard();
    } catch (const std::exception& error) {
        std::cerr << "BLOCKED: " << error.what() << '\n';
        return 78;
    }

    if (std::string(argv[1]) == "policy") return run_policy(fast, slow, output, run_id);
    if (std::string(argv[1]) == "offline") return run_drain(fast, slow, output, run_id);
    if (std::string(argv[1]) == "trace") return run_trace(fast, slow, output, run_id);

    json report{{"schema_version", 1}, {"run_id", run_id}, {"scenario", "migrate"},
                {"evidence_basis", "guest_kernel_measured"}, {"status", "FAIL"},
                {"managed_bytes", managed_bytes}, {"page_size", page_bytes},
                {"fast_node", fast}, {"slow_node", slow}, {"integrity_failures", 0},
                {"query_missing_count", 0}, {"initial_node_counts", json::object()},
                {"slow_node_counts", json::object()}, {"final_node_counts", json::object()},
                {"legs", json::array()}, {"negative_controls", json::array()},
                {"raw_file", "migration.jsonl"}, {"evidence_paths", {"migration.jsonl"}}};
    try {
        check_node(fast);
        check_node(slow);
        fs::create_directories(output);
        std::ofstream raw(output / "migration.jsonl");
        require(static_cast<bool>(raw), "Unable to open raw migration evidence");
        auto emit = [&](json event) {
            event["schema_version"] = 1;
            event["run_id"] = run_id;
            event["scenario"] = "migrate";
            event["evidence_basis"] = "guest_kernel_measured";
            raw << event.dump() << '\n';
            raw.flush();
            require(static_cast<bool>(raw), "Unable to write raw migration evidence");
        };
        Mapping mapping(managed_bytes, fast);
        LinuxMigrator migrator(reinterpret_cast<std::uintptr_t>(mapping.data()), managed_bytes, page_bytes);
        std::vector<std::uint64_t> ids(managed_bytes / page_bytes);
        std::iota(ids.begin(), ids.end(), 0);
        write_pattern(mapping.data(), 0);
        require_no_huge_pages(mapping.data());

        auto scan = [&](const std::string& stage, unsigned generation) {
            const auto failures = verify_pattern(mapping.data(), generation);
            report["integrity_failures"] = report["integrity_failures"].get<std::uint64_t>() + failures;
            emit({{"kind", "pattern"}, {"stage", stage}, {"generation", generation},
                  {"checked_bytes", managed_bytes}, {"integrity_failures", failures}});
            require(failures == 0, "Data pattern mismatch at " + stage);
        };
        auto snapshot = [&](const std::string& stage, int target) {
            const auto query = migrator.query(ids);
            std::vector<int> nodes;
            nodes.reserve(ids.size());
            for (std::size_t i = 0; i < ids.size(); ++i) nodes.push_back(query.node(i));
            const auto missing = std::count_if(nodes.begin(), nodes.end(), [](int n) { return n < 0; });
            report["query_missing_count"] = report["query_missing_count"].get<std::uint64_t>() +
                                             static_cast<std::uint64_t>(missing);
            report[stage + "_node_counts"] = counts(nodes);
            emit({{"kind", "snapshot"}, {"stage", stage}, {"nodes", nodes},
                  {"query_rc", query.rc}, {"query_errno", query.error},
                  {"integrity_failures", report["integrity_failures"]}});
            require(missing == 0 && std::all_of(nodes.begin(), nodes.end(),
                    [target](int node) { return node == target; }), "Unexpected placement at " + stage);
        };
        auto leg = [&](unsigned index, int source, int target) {
            std::size_t attempted = 0, succeeded = 0, batch_index = 0;
            for (std::size_t begin = 0; begin < ids.size(); begin += batch_limit) {
                const auto finish = std::min(begin + batch_limit, ids.size());
                std::vector<std::uint64_t> batch(ids.begin() + begin, ids.begin() + finish);
                const auto result = migrator.move(batch, target);
                json pages = json::array();
                std::size_t missing = 0;
                for (const auto& page : result.pages) {
                    succeeded += page.query_before_node == source && page.query_after_node == target;
                    missing += page.query_before_node < 0;
                    missing += page.query_after_node < 0;
                    pages.push_back({{"page_id", page.page_id},
                        {"query_before_node", page.query_before_node}, {"move_status", page.move_status},
                        {"move_status_valid", page.move_status_valid},
                        {"query_after_node", page.query_after_node}});
                }
                attempted += batch.size();
                emit({{"kind", "batch"}, {"leg", index}, {"batch_index", batch_index++},
                      {"source_node", source}, {"target_node", target},
                      {"syscall_rc", result.syscall_rc}, {"syscall_errno", result.syscall_errno},
                      {"before_query_rc", result.before.rc}, {"before_query_errno", result.before.error},
                      {"after_query_rc", result.after.rc}, {"after_query_errno", result.after.error},
                      {"pages", pages}});
                report["query_missing_count"] = report["query_missing_count"].get<std::uint64_t>() + missing;
                require(missing == 0, "Migration query could not establish actual placement");
            }
            const auto ratio = static_cast<double>(succeeded) / static_cast<double>(ids.size());
            report["legs"].push_back({{"source_node", source}, {"target_node", target},
                {"eligible_pages", ids.size()}, {"attempted_pages", attempted},
                {"succeeded_pages", succeeded}, {"success_ratio", ratio}});
            require(ratio >= 0.95, "Migration success below the fixed eligible denominator threshold");
        };

        scan("initial", 0);
        snapshot("initial", fast);
        leg(0, fast, slow);
        scan("slow_preserved", 0);
        snapshot("slow", slow);
        write_pattern(mapping.data(), 1);
        scan("slow_rewrite", 1);
        leg(1, slow, fast);
        scan("final", 1);
        snapshot("final", fast);
        require_no_huge_pages(mapping.data());

        auto negative = [&](const std::string& name, void* address, int target, bool mapped) {
            std::array<void*, 1> addresses{{address}};
            std::array<int, 1> before{{unknown_status}}, status{{unknown_status}}, after{{unknown_status}};
            const std::array<int, 1> targets{{target}};
            const auto before_call = linux_move_pages(1, addresses.data(), nullptr, before.data(), 0);
            const auto call = linux_move_pages(1, addresses.data(), targets.data(), status.data(), MPOL_MF_MOVE);
            const auto after_call = linux_move_pages(1, addresses.data(), nullptr, after.data(), 0);
            const bool valid = call.rc >= 0 && status[0] != unknown_status;
            const bool error = (call.rc == -1 && call.error > 0) || (valid && status[0] < 0);
            const bool unchanged = mapped ? (before_call.rc >= 0 && after_call.rc >= 0 &&
                    before[0] == fast && after[0] == fast) :
                    (after_call.rc == -1 || after[0] < 0);
            json control{{"name", name}, {"passed", error && unchanged}, {"raw", {
                {"syscall_rc", call.rc}, {"syscall_errno", call.error},
                {"move_status", status[0]}, {"move_status_valid", valid},
                {"query_before_node", before_call.rc >= 0 ? before[0] : unknown_status},
                {"query_after_node", after_call.rc >= 0 ? after[0] : unknown_status},
                {"query_before_rc", before_call.rc}, {"query_before_errno", before_call.error},
                {"query_after_rc", after_call.rc}, {"query_after_errno", after_call.error},
                {"requested_node", target}}}};
            report["negative_controls"].push_back(control);
            control["kind"] = "negative";
            emit(control);
            require(error && unchanged, "Negative control not rejected safely: " + name);
        };
        negative("invalid_target", mapping.data(), std::numeric_limits<int>::max(), true);
        negative("invalid_va", reinterpret_cast<void*>(static_cast<std::uintptr_t>(page_bytes)), fast, false);
        void* disposable = ::mmap(nullptr, page_bytes, PROT_READ | PROT_WRITE,
                                  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        require(disposable != MAP_FAILED, "Disposable mmap failed");
        *static_cast<unsigned char*>(disposable) = 0x5a;
        const auto historical_address = reinterpret_cast<std::uintptr_t>(disposable);
        require(::munmap(disposable, page_bytes) == 0, "Disposable munmap failed");
        negative("unmapped_va", reinterpret_cast<void*>(historical_address), fast, false);
        require(verify_pattern(mapping.data(), 1) == 0, "Negative controls changed managed data");
        report["status"] = "PASS";
        raw.close();
    } catch (const std::exception& error) {
        report["reason"] = error.what();
    }
    try {
        fs::create_directories(output);
        const auto temporary = output / "migration.json.tmp";
        std::ofstream stream(temporary);
        stream << report.dump(2) << '\n';
        stream.close();
        require(static_cast<bool>(stream), "Unable to write migration report");
        fs::rename(temporary, output / "migration.json");
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
    std::cout << "Migration scenario: " << report["status"].get<std::string>() << '\n';
    return report["status"] == "PASS" ? 0 : 1;
}
