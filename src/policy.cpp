#include "policy.hpp"
#include <algorithm>
#include <cerrno>
#include <limits>
#include <stdexcept>

namespace tierlab {
namespace {
void need(bool condition, const char* reason) {
    if (!condition) throw std::invalid_argument(reason);
}
}
PolicyCore::PolicyCore(std::size_t pages, int fast_node, int slow_node,
                       std::size_t fast_cap, std::size_t attempt_budget)
    : pages_(pages), fast_(fast_node), slow_(slow_node), cap_(fast_cap), budget_(attempt_budget) {
    need(pages > 0 && fast_node >= 0 && slow_node >= 0 && fast_node != slow_node,
         "Invalid policy geometry");
    need(fast_cap <= pages, "Fast capacity exceeds managed pages");
}
void PolicyCore::validate_nodes(const std::vector<int>& nodes) const {
    need(nodes.size() == pages_.size(), "Incomplete observed placement");
    need(std::all_of(nodes.begin(), nodes.end(), [this](int node) {
        return node == fast_ || node == slow_;
    }), "Unknown or foreign observed node");
    need(static_cast<std::size_t>(std::count(nodes.begin(), nodes.end(), fast_)) <= cap_,
         "Observed fast residency exceeds capacity");
}
void PolicyCore::validate_epoch(std::uint64_t epoch) const {
    need(epoch_.has_value() && epoch == *epoch_, "Observe the epoch before planning or commit");
}
void PolicyCore::observe(std::uint64_t epoch, const std::vector<std::uint32_t>& counts,
                         const std::vector<int>& nodes) {
    validate_nodes(nodes);
    need(counts.size() == pages_.size(), "Incomplete page counts");
    need(!epoch_ ? epoch == 0 : (*epoch_ != std::numeric_limits<std::uint64_t>::max() &&
                               epoch == *epoch_ + 1), "Epochs must be observed exactly once in order");
    epoch_ = epoch;
    attempted_ = 0;
    for (std::size_t id = 0; id < pages_.size(); ++id) {
        auto& page = pages_[id];
        page.score = 0.5 * std::min(1.0, static_cast<double>(counts[id]) / 2.0) +
                     0.5 * page.score;
        page.hot_streak = page.score >= 0.60 ? std::min(2U, page.hot_streak + 1) : 0;
        page.cold_streak = page.score <= 0.30 ? std::min(3U, page.cold_streak + 1) : 0;
        if (page.hot_streak == 2) page.temperature = Temperature::Hot;
        if (page.cold_streak == 3) page.temperature = Temperature::Cold;
    }
}
std::vector<std::uint64_t> PolicyCore::select_demotions(std::uint64_t epoch,
        const std::vector<int>& nodes, std::size_t remaining) const {
    return select(epoch, nodes, remaining, false);
}
std::vector<std::uint64_t> PolicyCore::select_promotions(std::uint64_t epoch,
        const std::vector<int>& nodes, std::size_t remaining) const {
    return select(epoch, nodes, remaining, true);
}
std::vector<std::uint64_t> PolicyCore::select(std::uint64_t epoch,
        const std::vector<int>& nodes, std::size_t remaining, bool promote) const {
    validate_epoch(epoch);
    validate_nodes(nodes);
    need(remaining <= budget_, "Remaining attempts exceeds the epoch budget");
    std::size_t limit = std::min(remaining, budget_ - attempted_);
    if (promote) {
        const auto fast_count = static_cast<std::size_t>(std::count(nodes.begin(), nodes.end(), fast_));
        limit = std::min(limit, cap_ - fast_count);
    }
    std::vector<std::uint64_t> result;
    for (std::size_t id = 0; id < pages_.size(); ++id) {
        const auto& page = pages_[id];
        if (nodes[id] != (promote ? slow_ : fast_) ||
            page.temperature != (promote ? Temperature::Hot : Temperature::Cold) ||
            (page.last_move_epoch && epoch - *page.last_move_epoch < 5) ||
            (page.attempted_epoch && *page.attempted_epoch == epoch) || epoch < page.retry_epoch)
            continue;
        result.push_back(id);
    }
    std::sort(result.begin(), result.end(), [&](std::uint64_t left, std::uint64_t right) {
        const double a = pages_[left].score, b = pages_[right].score;
        return a == b ? left < right : (promote ? a > b : a < b);
    });
    if (result.size() > limit) result.resize(limit);
    return result;
}
void PolicyCore::reconcile(std::uint64_t epoch, int target, const BatchResult& batch) {
    validate_epoch(epoch);
    need(target == fast_ || target == slow_, "Unknown migration target");
    need(!batch.pages.empty() && batch.pages.size() <= batch_limit &&
         batch.pages.size() <= budget_ - attempted_, "Invalid batch or exhausted epoch budget");
    // Validate the entire input before changing any state.
    std::vector<std::uint64_t> ids;
    for (const auto& result : batch.pages) {
        need(result.page_id < pages_.size(), "Migration page outside managed range");
        const auto& page = pages_[result.page_id];
        need(!page.attempted_epoch || *page.attempted_epoch != epoch, "Repeated attempt in one epoch");
        need(result.query_before_node < 0 || result.query_before_node == fast_ ||
             result.query_before_node == slow_, "Foreign before-query node");
        need(result.query_after_node < 0 || result.query_after_node == fast_ ||
             result.query_after_node == slow_, "Foreign after-query node");
        ids.push_back(result.page_id);
    }
    std::sort(ids.begin(), ids.end());
    need(std::adjacent_find(ids.begin(), ids.end()) == ids.end(), "Duplicate migration page");
    attempted_ += batch.pages.size();
    for (const auto& result : batch.pages) {
        auto& page = pages_[result.page_id];
        page.attempted_epoch = epoch;
        if (result.query_before_node >= 0 && result.query_before_node != target &&
            result.query_after_node == target) {
            page.last_move_epoch = epoch;
            page.busy_failures = 0;
            page.retry_epoch = 0;
        } else if ((batch.syscall_rc == -1 && batch.syscall_errno == EBUSY) ||
                   (batch.syscall_rc >= 0 && result.move_status_valid &&
                    result.move_status != unknown_status && result.move_status == -EBUSY)) {
            page.busy_failures = std::min(5U, page.busy_failures + 1);
            const std::uint64_t delay = 1ULL << page.busy_failures;
            page.retry_epoch = epoch > std::numeric_limits<std::uint64_t>::max() - delay
                ? std::numeric_limits<std::uint64_t>::max() : epoch + delay;
        }
    }
}
}  // namespace tierlab
