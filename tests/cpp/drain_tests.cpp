#include "drain.hpp"
#include <algorithm>
#include <cerrno>
#include <iostream>
#include <limits>
#include <stdexcept>

using namespace tierlab;
namespace {
void check(bool value, const char* reason) {
    if (!value) throw std::runtime_error(reason);
}
template<class F> void rejects(F action) {
    bool rejected = false;
    try { action(); } catch (const std::exception&) { rejected = true; }
    check(rejected, "Invalid drain action was accepted");
}
struct FakeMigrator {
    std::vector<int> nodes;
    BatchResult move(const std::vector<std::uint64_t>& ids, std::size_t successes,
                     long rc = 0, int error = 0, int failure = -EBUSY) {
        BatchResult result{rc, error, {0, 0, {}}, {0, 0, {}}, {}};
        for (std::size_t i = 0; i < ids.size(); ++i) {
            const auto id = ids[i];
            const int before = nodes.at(id);
            if (i < successes) nodes[id] = 0;
            const int status = i < successes ? 0 : failure;
            result.before.status.push_back(before);
            result.after.status.push_back(nodes[id]);
            result.pages.push_back({id, before, status, rc >= 0 && status != unknown_status, nodes[id]});
        }
        return result;
    }
};
void capacity_and_admission() {
    constexpr std::uint64_t need = drain_margin_bytes + 4096;
    check(drain_capacity(1, need, need).enough, "Exact capacity boundary rejected");
    check(!drain_capacity(1, need - 1, need).enough && !drain_capacity(1, need, need - 1).enough,
          "Insufficient node or global capacity accepted");
    rejects([] { drain_capacity(std::numeric_limits<std::uint64_t>::max(), 0, 0); });
    DrainController rejected(1, 0, 1);
    rejected.require_target(1);
    const auto result = rejected.begin({1}, need - 1, need);
    check(!result.enough && rejected.state() == DrainState::Failed, "Capacity rejection did not fail admission");
    rejects([&] { rejected.select({1}); });
    rejects([&] { rejected.require_target(0); });
    rejects([&] { rejected.require_target(1); });
    DrainController running(1, 0, 1);
    FakeMigrator before_begin{{1}};
    rejects([&] { running.select(before_begin.nodes); });
    rejects([&] { running.reconcile(before_begin.move({0}, 0)); });
    rejects([&] { running.finish({0}, true, true); });
    check(running.state() == DrainState::Active && !running.ready_for_offline(),
          "Pre-begin rejection changed drain state");
    check(running.begin({1}, need, need).enough, "Valid begin failed");
    running.require_target(0);
    rejects([&] { running.require_target(1); });
    rejects([&] { running.begin({1}, need, need); });
    rejects([&] { running.select({unknown_status}); });
}
void full_drain_ignores_policy_cap_and_budget() {
    constexpr std::size_t pages = 32768;
    FakeMigrator fake{std::vector<int>(pages, 1)};
    DrainController core(pages, 0, 1);
    check(core.begin(fake.nodes, 2ULL << 30, 2ULL << 30).enough, "Full profile capacity failed");
    std::size_t attempted = 0;
    while (std::count(fake.nodes.begin(), fake.nodes.end(), 1)) {
        const auto batch = core.select(fake.nodes);
        check(!batch.empty() && batch.size() <= 256, "Invalid drain batch");
        core.reconcile(fake.move(batch, batch.size()));
        attempted += batch.size();
    }
    check(attempted == pages && std::count(fake.nodes.begin(), fake.nodes.end(), 0) == pages,
          "Drain retained normal policy capacity or epoch budget");
    core.finish(fake.nodes, true, true);
    check(core.ready_for_offline() && core.state() == DrainState::Draining,
          "Drain claimed OFFLINE before Guest sysfs actions");
    rejects([&] { core.require_target(0); });
    rejects([&] { core.require_target(1); });
}
void failed_and_partial_queries() {
    DrainController core(2, 0, 1);
    FakeMigrator fake{{1, 1}};
    core.begin(fake.nodes, 1ULL << 30, 1ULL << 30);
    // rc=-1 invalidates syscall statuses; observed placement still controls the next selection.
    core.reconcile(fake.move({0, 1}, 1, -1, EPERM, unknown_status));
    check(core.select(fake.nodes) == std::vector<std::uint64_t>{1}, "Failed syscall overrode query truth");
    // A positive rc and untouched sentinel cannot claim a page moved.
    core.reconcile(fake.move({1}, 0, 1, 0, unknown_status));
    check(core.select(fake.nodes) == std::vector<std::uint64_t>{1}, "Untouched sentinel counted as migration");
    core.reconcile(fake.move({1}, 0));
    check(core.select(fake.nodes).empty(), "Drain retried a page more than three times");
    rejects([&] { core.finish(fake.nodes, true, true); });
    check(core.state() == DrainState::Failed && !core.ready_for_offline(), "Partial drain permitted offline");

    DrainController bad(1, 0, 1);
    FakeMigrator untouched{{1}};
    bad.begin(untouched.nodes, 1ULL << 30, 1ULL << 30);
    auto invalid = untouched.move({0}, 0);
    invalid.after.rc = -1;
    invalid.after.error = EINVAL;
    rejects([&] { bad.reconcile(invalid); });
    check(bad.select(untouched.nodes) == std::vector<std::uint64_t>{0}, "Invalid query corrupted drain state");
}
void completion_guards() {
    for (const bool pattern : {false, true}) {
        DrainController core(1, 0, 1);
        core.begin({0}, 1ULL << 30, 1ULL << 30);
        rejects([&] { core.finish({0}, pattern, false); });
        check(!core.ready_for_offline(), "Unreleased mapping permitted offline");
    }
    DrainController corrupted(1, 0, 1);
    corrupted.begin({0}, 1ULL << 30, 1ULL << 30);
    rejects([&] { corrupted.finish({0}, false, true); });
    rejects([] { DrainController bad(0, 0, 1); });
    rejects([] { DrainController bad(1, 0, 0); });
}
}
int main() {
    try {
        capacity_and_admission();
        full_drain_ignores_policy_cap_and_budget();
        failed_and_partial_queries();
        completion_guards();
        std::cout << "PASS: drain admission, real-capacity guard, bounded attempts, query truth and release guards\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
