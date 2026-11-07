#include "guest_runtime.hpp"
#include <numaif.h>
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <limits>
#include <numeric>
#include <poll.h>
#include <sstream>
#include <unistd.h>

using namespace tierlab;

namespace {
constexpr std::size_t trace_pages = 64;
constexpr std::size_t trace_bytes = trace_pages * page_bytes;
constexpr unsigned generation = 11;

std::string hex_address(void* address) {
    std::ostringstream stream;
    stream << "0x" << std::hex << reinterpret_cast<std::uintptr_t>(address);
    return stream.str();
}

void wait_for_collector() {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    std::string line;
    while (line.size() < 64) {
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        require(remaining > 0, "Trace collector readiness deadline exceeded");
        pollfd input{STDIN_FILENO, POLLIN, 0};
        const int ready = ::poll(&input, 1, static_cast<int>(remaining));
        if (ready < 0 && errno == EINTR) continue;
        require(ready > 0 && !(input.revents & (POLLERR | POLLNVAL)),
                "Trace collector readiness unavailable");
        char value = 0;
        const auto bytes = ::read(STDIN_FILENO, &value, 1);
        if (bytes < 0 && errno == EINTR) continue;
        require(bytes == 1, "Trace collector readiness input closed");
        if (value == '\n') {
            require(line == "TRACE_READY", "Invalid trace collector readiness input");
            return;
        }
        line += value;
    }
    throw std::runtime_error("Trace collector readiness input too long");
}

class Marker {
public:
    Marker() {
        const char* path = std::getenv("TIERLAB_TRACE_MARKER");
        require(path && *path, "Missing isolated trace marker path");
        fd_ = ::open(path, O_WRONLY | O_CLOEXEC);
        require(fd_ >= 0, "Unable to open isolated trace marker");
    }
    Marker(const Marker&) = delete;
    Marker& operator=(const Marker&) = delete;
    ~Marker() { if (fd_ >= 0) ::close(fd_); }
    void write(const std::string& value) const {
        ssize_t result;
        do { result = ::write(fd_, value.data(), value.size()); } while (result < 0 && errno == EINTR);
        require(result == static_cast<ssize_t>(value.size()), "Unable to write full trace marker");
    }
private:
    int fd_ = -1;
};
}  // namespace

int run_trace(int fast, int slow, const fs::path& output, const std::string& run_id) {
    json report{{"schema_version", 1}, {"run_id", run_id}, {"scenario", "trace"},
                {"evidence_basis", "guest_kernel_measured"}, {"status", "FAIL"},
                {"page_count", trace_pages}, {"page_size", page_bytes},
                {"managed_bytes", trace_bytes}, {"generation", generation},
                {"fast_node", fast}, {"slow_node", slow}, {"passed_cases", 0},
                {"integrity_failures", 0}, {"query_missing_count", 0},
                {"raw_file", "trace_batches.jsonl"}, {"tap_file", "trace-selftest.tap"}};
    try {
        check_node(fast);
        check_node(slow);
        fs::create_directories(output);
        std::ofstream raw(output / "trace_batches.jsonl"), tap(output / "trace-selftest.tap");
        require(raw && tap, "Unable to open trace selftest evidence");
        tap << "TAP version 13\n1..5\n";
        auto emit = [&](json event) {
            event["schema_version"] = 1;
            event["run_id"] = run_id;
            event["scenario"] = "trace";
            event["evidence_basis"] = "guest_kernel_measured";
            raw << event.dump() << '\n';
            raw.flush();
            require(static_cast<bool>(raw), "Unable to write raw trace evidence");
        };
        wait_for_collector();
        Marker marker;
        Mapping mapping(trace_bytes, fast);
        LinuxMigrator migrator(reinterpret_cast<std::uintptr_t>(mapping.data()), trace_bytes, page_bytes);
        std::vector<std::uint64_t> all_ids(trace_pages);
        std::iota(all_ids.begin(), all_ids.end(), 0);
        write_pattern(mapping.data(), generation, trace_bytes);
        require_no_huge_pages(mapping.data(), trace_bytes);
        const auto initial_failures = verify_pattern(mapping.data(), generation, trace_bytes);
        emit({{"kind", "environment"}, {"pid", ::getpid()}, {"mapping_base", hex_address(mapping.data())},
              {"page_count", trace_pages}, {"page_size", page_bytes}, {"generation", generation},
              {"fast_node", fast}, {"slow_node", slow}, {"checked_bytes", trace_bytes},
              {"integrity_failures", initial_failures}, {"mapping_live", true}});
        require(initial_failures == 0, "Initial trace selftest payload mismatch");

        std::vector<int> expected(trace_pages, fast);
        auto batch = [&](unsigned id, const std::string& name, std::size_t count, int target,
                         bool invalid_va, bool invalid_target) {
            const auto before = migrator.query(all_ids);
            std::vector<void*> addresses;
            json page_ids = json::array(), vas = json::array();
            for (std::size_t i = 0; i < count; ++i) {
                addresses.push_back(migrator.address(i));
                page_ids.push_back(i);
            }
            if (invalid_va) {
                addresses.push_back(reinterpret_cast<void*>(static_cast<std::uintptr_t>(page_bytes)));
                page_ids.push_back(nullptr);
            }
            for (auto* address : addresses) vas.push_back(hex_address(address));
            std::vector<int> targets(addresses.size(), target), status(addresses.size(), unknown_status);
            const auto prefix = "tierlab " + run_id + " " + std::to_string(id);
            marker.write(prefix + " begin\n");
            const auto call = linux_move_pages(addresses.size(), addresses.data(), targets.data(),
                                               status.data(), MPOL_MF_MOVE);
            std::string marker_error;
            try {
                marker.write(prefix + " end\n");
            } catch (const std::exception& error) {
                // The syscall already ran. Preserve its result and reconcile
                // placement even when the trace boundary could not be saved.
                marker_error = error.what();
            }
            const auto after = migrator.query(all_ids);
            const auto failures = verify_pattern(mapping.data(), generation, trace_bytes);
            std::vector<bool> valid(status.size());
            for (std::size_t i = 0; i < status.size(); ++i)
                valid[i] = call.rc >= 0 && status[i] != unknown_status;
            bool passed = marker_error.empty() && before.rc == 0 && before.error == 0 && before.status == expected &&
                          after.rc == 0 && after.error == 0 && failures == 0;
            if (invalid_target) {
                passed = passed && call.rc == -1 && call.error > 0 && after.status == expected;
            } else {
                // Lookup errors are reported per entry by the pinned kernel;
                // a successful flush can still return zero for mixed input.
                passed = passed && call.rc == 0 && call.error == 0;
                for (std::size_t i = 0; i < count; ++i) {
                    passed = passed && valid[i] && status[i] == target;
                    expected[i] = target;
                }
                if (invalid_va) passed = passed && valid.back() && status.back() < 0;
                passed = passed && after.status == expected;
            }
            emit({{"kind", "batch"}, {"id", id}, {"name", name}, {"requested_node", target},
                  {"request_page_ids", page_ids}, {"request_vas", vas},
                  {"query_before_rc", before.rc}, {"query_before_errno", before.error},
                  {"query_before_nodes", before.status}, {"syscall_rc", call.rc},
                  {"syscall_errno", call.error}, {"move_status", status}, {"move_status_valid", valid},
                  {"query_after_rc", after.rc}, {"query_after_errno", after.error},
                  {"query_after_nodes", after.status}, {"integrity_failures", failures},
                  {"checked_bytes", trace_bytes}, {"marker_error", marker_error}, {"passed", passed}});
            tap << (passed ? "ok " : "not ok ") << id + 1 << " - " << name << '\n';
            tap.flush();
            require(static_cast<bool>(tap), "Unable to write trace selftest TAP");
            require(passed, "Trace selftest failed: " + name);
            report["passed_cases"] = id + 1;
        };
        batch(0, "roundtrip_out", trace_pages, slow, false, false);
        batch(1, "roundtrip_back", trace_pages, fast, false, false);
        batch(2, "mixed_va", trace_pages / 2, slow, true, false);
        batch(3, "mixed_return", trace_pages / 2, fast, false, false);
        batch(4, "invalid_target", trace_pages / 2, std::numeric_limits<int>::max(), false, true);
        require_no_huge_pages(mapping.data(), trace_bytes);
        const auto failures = verify_pattern(mapping.data(), generation, trace_bytes);
        emit({{"kind", "complete"}, {"integrity_failures", failures}, {"mapping_live", mapping.data() != nullptr},
              {"checked_bytes", trace_bytes}});
        require(failures == 0, "Final trace selftest payload mismatch");
        report["status"] = "PASS";
    } catch (const std::exception& error) {
        report["reason"] = error.what();
    }
    try {
        fs::create_directories(output);
        const auto temporary = output / "trace_batches.json.tmp";
        std::ofstream stream(temporary);
        stream << report.dump(2) << '\n';
        stream.close();
        require(static_cast<bool>(stream), "Unable to write trace selftest report");
        const int fd = ::open(temporary.c_str(), O_RDONLY | O_CLOEXEC);
        require(fd >= 0, "Unable to reopen trace selftest report");
        const int sync_result = ::fsync(fd);
        ::close(fd);
        require(sync_result == 0, "Unable to sync trace selftest report");
        fs::rename(temporary, output / "trace_batches.json");
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
    std::cout << "Trace selftest scenario: " << report["status"].get<std::string>() << '\n';
    return report["status"] == "PASS" ? 0 : 1;
}
