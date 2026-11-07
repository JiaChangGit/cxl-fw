#include "policy.hpp"
#include "workload.hpp"
#include <algorithm>
#include <cerrno>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace tierlab;
namespace {
void check(bool condition, const char* reason) {
    if (!condition) throw std::runtime_error(reason);
}
template<class F> void rejects(F action) {
    bool rejected = false;
    try { action(); } catch (const std::invalid_argument&) { rejected = true; }
    check(rejected, "Invalid policy input accepted");
}
struct FakeMigrator {
    std::vector<int> nodes;
    BatchResult move(const std::vector<std::uint64_t>& ids, int target,
                     std::size_t successes, long rc = 0, int error = 0,
                     int failed_status = -EBUSY) {
        BatchResult result{rc, error, {0, 0, {}}, {0, 0, {}}, {}};
        for (std::size_t i = 0; i < ids.size(); ++i) {
            const auto id = ids[i];
            const int before = nodes.at(id);
            if (i < successes) nodes[id] = target;
            const int status = i < successes ? target : failed_status;
            const bool valid = rc >= 0 && status != unknown_status;
            result.before.status.push_back(before);
            result.after.status.push_back(nodes[id]);
            result.pages.push_back({id, before, status, valid, nodes[id]});
        }
        return result;
    }
};
void hysteresis_and_ranking() {
    PolicyCore core(4, 0, 1, 2, 4);
    const std::vector<int> nodes{0, 0, 1, 1};
    core.observe(0, {0, 0, 2, 2}, nodes);
    check(core.state(0).temperature == Temperature::Unknown &&
          core.state(2).score == 0.5, "Initial EWMA or class wrong");
    core.observe(1, {0, 0, 2, 2}, nodes);
    check(core.state(2).temperature == Temperature::Unknown, "HOT classified too early");
    core.observe(2, {0, 0, 2, 2}, nodes);
    check(core.state(0).temperature == Temperature::Cold &&
          core.state(2).temperature == Temperature::Hot, "Hysteresis did not mature");
    check(core.select_demotions(2, nodes, 4) == std::vector<std::uint64_t>({0, 1}),
          "Equal-score demotions not page-id ordered");
    check(core.select_promotions(2, nodes, 4).empty(), "Full capacity allowed promotion");
    core.observe(3, {0, 0, 1, 2}, nodes);
    check(core.state(2).temperature == Temperature::Hot && core.state(2).score == 0.6875,
          "Middle score discarded established class");
    core.observe(4, {0, 0, 0, 0}, nodes);
    core.observe(5, {0, 0, 0, 0}, nodes);
    check(core.state(2).temperature == Temperature::Hot, "COLD classified without three low epochs");
    core.observe(6, {0, 0, 0, 0}, nodes);
    check(core.state(2).temperature == Temperature::Hot, "COLD counter threshold wrong");
    core.observe(7, {0, 0, 0, 0}, nodes);
    check(core.state(2).temperature == Temperature::Cold, "COLD class never reached");

    // A below-threshold epoch resets the HOT streak instead of accumulating hits.
    PolicyCore reset(1, 0, 1, 1, 1);
    reset.observe(0, {2}, {1});
    reset.observe(1, {2}, {1});
    reset.observe(2, {0}, {1});
    reset.observe(3, {2}, {1});
    check(reset.state(0).temperature == Temperature::Unknown, "HOT streak survived a gap");
    reset.observe(4, {2}, {1});
    check(reset.state(0).temperature == Temperature::Hot, "HOT streak did not restart");

    PolicyCore promote_rank(2, 0, 1, 2, 2);
    PolicyCore demote_rank(2, 0, 1, 2, 2);
    for (unsigned e = 0; e < 3; ++e) {
        promote_rank.observe(e, {2, 2}, {1, 1});
        demote_rank.observe(e, {0, 0}, {0, 0});
    }
    promote_rank.observe(3, {1, 2}, {1, 1});
    demote_rank.observe(3, {1, 0}, {0, 0});
    check(promote_rank.select_promotions(3, {1, 1}, 2) == std::vector<std::uint64_t>({1, 0}),
          "Promotions did not rank descending score");
    check(demote_rank.select_demotions(3, {0, 0}, 2) == std::vector<std::uint64_t>({1, 0}),
          "Demotions did not rank ascending score");
}
void failures_capacity_and_cooldown() {
    PolicyCore core(4, 0, 1, 2, 4);
    FakeMigrator fake{{0, 0, 1, 1}};
    for (unsigned e = 0; e < 3; ++e) core.observe(e, {0, 0, 2, 2}, fake.nodes);
    auto demote = core.select_demotions(2, fake.nodes, 4);
    core.reconcile(2, 1, fake.move(demote, 1, 0));
    check(core.select_promotions(2, fake.nodes, 2).empty(),
          "Failed demotion speculatively freed a fast slot");
    check(!core.state(0).last_move_epoch && core.state(0).retry_epoch == 4,
          "EBUSY fabricated a committed move or wrong retry");
    rejects([&] { core.reconcile(2, 1, fake.move({0}, 1, 0)); });
    core.observe(3, {0, 0, 2, 2}, fake.nodes);
    check(core.select_demotions(3, fake.nodes, 4).empty(), "Backoff did not suppress retries");
    core.observe(4, {0, 0, 2, 2}, fake.nodes);
    core.reconcile(4, 1, fake.move(core.select_demotions(4, fake.nodes, 4), 1, 1));
    const auto promote = core.select_promotions(4, fake.nodes, 2);
    check(promote == std::vector<std::uint64_t>({2}),
          "Promotion did not use actual partial-demotion capacity and deterministic rank");
    core.reconcile(4, 0, fake.move(promote, 0, 1));
    check(core.state(0).last_move_epoch == 4 && core.state(0).busy_failures == 0 &&
          core.state(2).last_move_epoch == 4, "Query-confirmed success did not commit");
    for (unsigned e = 5; e <= 8; ++e) {
        core.observe(e, {2, 0, 0, 0}, fake.nodes);
        const auto selected = core.select_demotions(e, fake.nodes, 4);
        check(std::find(selected.begin(), selected.end(), 2) == selected.end(),
              "Successful page migrated again before five epochs");
    }
    core.observe(9, {2, 0, 0, 0}, fake.nodes);
    const auto selected = core.select_demotions(9, fake.nodes, 4);
    check(std::find(selected.begin(), selected.end(), 2) != selected.end(),
          "Cooldown failed to expire at five epochs");
}
void query_truth_and_busy() {
    PolicyCore truth(2, 0, 1, 2, 2);
    FakeMigrator fake{{1, 1}};
    for (unsigned e = 0; e < 3; ++e) truth.observe(e, {2, 2}, fake.nodes);
    // A batch error invalidates statuses, but the follow-up query remains truth.
    truth.reconcile(2, 0, fake.move({0, 1}, 0, 1, -1, EPERM, unknown_status));
    check(truth.state(0).last_move_epoch == 2 && !truth.state(1).last_move_epoch,
          "Batch error/sentinel overrode actual query placement");
    check(truth.select_promotions(2, fake.nodes, 2).empty(), "Shared attempt budget exceeded");

    PolicyCore busy(1, 0, 1, 1, 1);
    FakeMigrator stubborn{{1}};
    const std::vector<unsigned> attempts{2, 4, 8, 16, 32, 64, 96};
    std::size_t attempt = 0;
    for (unsigned e = 0; e <= 96; ++e) {
        busy.observe(e, {2}, stubborn.nodes);
        const auto selected = busy.select_promotions(e, stubborn.nodes, 1);
        const bool due = attempt < attempts.size() && e == attempts[attempt];
        check(!selected.empty() == due, "EBUSY backoff is not 2/4/8/16/32 epochs");
        if (!due) continue;
        const bool final = attempt == attempts.size() - 1;
        // Alternate per-page and whole-batch EBUSY controls.
        const long rc = attempt % 2 ? -1 : 0;
        busy.reconcile(e, 0, stubborn.move(selected, 0, final ? 1 : 0, rc,
                       rc == -1 ? EBUSY : 0));
        ++attempt;
    }
    check(busy.state(0).last_move_epoch == 96 && busy.state(0).busy_failures == 0 &&
          busy.state(0).retry_epoch == 0, "Success did not clear saturated backoff");

    PolicyCore partial(1, 0, 1, 1, 1);
    FakeMigrator untouched{{1}};
    for (unsigned e = 0; e < 3; ++e) partial.observe(e, {2}, untouched.nodes);
    partial.reconcile(2, 0, untouched.move({0}, 0, 0, 1, 0, unknown_status));
    check(!partial.state(0).last_move_epoch && partial.state(0).busy_failures == 0,
          "Positive rc with untouched sentinel counted as success or busy");
}
void invalid_and_zero_budget() {
    rejects([] { PolicyCore bad(0, 0, 1); });
    rejects([] { PolicyCore bad(1, 0, 0, 1); });
    PolicyCore core(2, 0, 1, 1, 1);
    rejects([&] { core.select_promotions(0, {1, 1}, 1); });
    rejects([&] { core.observe(1, {0, 0}, {1, 1}); });
    rejects([&] { core.observe(0, {0}, {1, 1}); });
    rejects([&] { core.observe(0, {0, 0}, {1, unknown_status}); });
    rejects([&] { core.observe(0, {0, 0}, {0, 0}); });
    core.observe(0, {0, 0}, {1, 1});
    rejects([&] { core.observe(0, {0, 0}, {1, 1}); });
    rejects([&] { core.select_demotions(0, {1, 1}, 2); });
    rejects([&] { core.select_promotions(0, {1, 7}, 1); });
    PolicyCore stopped(1, 0, 1, 1, 0);
    for (unsigned e = 0; e < 60; ++e) {
        stopped.observe(e, {2}, {1});
        check(stopped.select_promotions(e, {1}, 0).empty(), "Zero budget still moved pages");
    }
    check(stopped.state(0).temperature == Temperature::Hot && !stopped.state(0).last_move_epoch,
          "Zero-budget negative control did not remain unplaced");
}
void standard_profile_fixture(std::uint64_t seed, std::size_t budget) {
    Workload workload(seed);
    PolicyCore core(policy_pages, 0, 1, fast_cap, budget);
    FakeMigrator fake{std::vector<int>(policy_pages, 1)};
    std::vector<double> steady;
    int recovery_epoch = -1;
    for (unsigned epoch = 0; epoch < epoch_count; ++epoch) {
        const auto trace = workload.next_epoch(epoch);
        if (seed == 1) {
            if (epoch == 0) check(trace.hash == 0x8feb7136a18a8a89ULL, "Epoch 0 trace changed");
            if (epoch == 29) check(trace.hash == 0xc9d64f5720f89840ULL, "Epoch 29 trace changed");
            if (epoch == 30) check(trace.hash == 0x24c3456023a85358ULL, "Epoch 30 trace changed");
            if (epoch == 59) check(trace.hash == 0x9a44d8c956bbf8d8ULL, "Epoch 59 trace changed");
        }
        core.observe(epoch, trace.counts, fake.nodes);
        std::size_t attempted = 0;
        auto execute = [&](const std::vector<std::uint64_t>& ids, int target) {
            for (std::size_t begin = 0; begin < ids.size(); begin += batch_limit) {
                const auto end = std::min(begin + batch_limit, ids.size());
                const std::vector<std::uint64_t> batch(ids.begin() + begin, ids.begin() + end);
                core.reconcile(epoch, target, fake.move(batch, target, batch.size()));
                attempted += batch.size();
                check(attempted <= budget, "Standard profile exceeded shared attempts budget");
                check(static_cast<std::size_t>(std::count(fake.nodes.begin(), fake.nodes.end(), 0)) <= fast_cap,
                      "Standard profile exceeded actual fast residency cap");
            }
        };
        execute(core.select_demotions(epoch, fake.nodes, budget), 1);
        execute(core.select_promotions(epoch, fake.nodes, budget - attempted), 0);
        const auto& labels = workload.hot(epoch);  // Evaluator-only, never passed to PolicyCore.
        const auto hot_fast = std::count_if(labels.begin(), labels.end(), [&](std::uint64_t id) {
            return fake.nodes[id] == 0;
        });
        const double hfr = static_cast<double>(hot_fast) / static_cast<double>(hot_pages);
        if (epoch >= 50) steady.push_back(hfr);
        if (epoch >= 30 && epoch < 50 && hfr >= 0.80 && recovery_epoch < 0)
            recovery_epoch = static_cast<int>(epoch);
    }
    std::sort(steady.begin(), steady.end());
    const double median = (steady[4] + steady[5]) / 2;
    if (budget == 0) {
        check(median == 0 && recovery_epoch == -1, "Zero budget did not defeat G3 placement targets");
    } else {
        check(median >= 0.85, "Standard profile steady ready HFR below fixed G3 target");
        check(recovery_epoch >= 30 && recovery_epoch < 50, "Standard profile missed fixed recovery target");
    }
    std::cout << "Fake profile seed=" << seed << " budget=" << budget
              << " steady_ready_hfr_median=" << median << " recovery_epoch=" << recovery_epoch << '\n';
}
}
int main() {
    try {
        hysteresis_and_ranking();
        failures_capacity_and_cooldown();
        query_truth_and_busy();
        invalid_and_zero_budget();
        for (const auto seed : {1ULL, 7ULL, 42ULL}) standard_profile_fixture(seed, epoch_budget);
        standard_profile_fixture(1, 0);
        std::cout << "PASS: policy hysteresis, deterministic ranking, capacity, cooldown, query truth and busy controls\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
