#include "drain.hpp"
#include <algorithm>
#include <limits>
#include <stdexcept>

namespace tierlab {
namespace {
void need(bool condition, const char* message) {
    if (!condition) throw std::invalid_argument(message);
}
}
DrainCapacity drain_capacity(std::uint64_t pages, std::uint64_t available, std::uint64_t free) {
    need(pages <= (std::numeric_limits<std::uint64_t>::max() - drain_margin_bytes) / 4096,
         "Drain capacity arithmetic overflow");
    const auto required = pages * 4096 + drain_margin_bytes;
    return {pages, required, available, free, available >= required && free >= required};
}
DrainController::DrainController(std::size_t pages, int fast_node, int slow_node)
    : attempts_(pages, 0), fast_(fast_node), slow_(slow_node) {
    need(pages > 0 && fast_node >= 0 && slow_node >= 0 && fast_node != slow_node,
         "Invalid drain geometry");
}
void DrainController::validate_nodes(const std::vector<int>& nodes) const {
    need(nodes.size() == attempts_.size() && std::all_of(nodes.begin(), nodes.end(),
        [this](int node) { return node == fast_ || node == slow_; }), "Incomplete drain placement");
}
DrainCapacity DrainController::begin(const std::vector<int>& nodes,
                                     std::uint64_t available, std::uint64_t free) {
    need(state_ == DrainState::Active, "Drain already started");
    validate_nodes(nodes);
    state_ = DrainState::Draining;  // Admission closes before capacity can reject the drain.
    const auto capacity = drain_capacity(static_cast<std::uint64_t>(std::count(nodes.begin(), nodes.end(), slow_)),
                                          available, free);
    if (!capacity.enough) fail();
    return capacity;
}
void DrainController::require_target(int node) const {
    need((state_ == DrainState::Active && (node == fast_ || node == slow_)) ||
         (state_ == DrainState::Draining && !ready_ && node == fast_),
         "Drain admission rejects migration target");
}
std::vector<std::uint64_t> DrainController::select(const std::vector<int>& nodes) const {
    need(state_ == DrainState::Draining, "Begin drain before selecting pages");
    require_target(fast_);
    validate_nodes(nodes);
    std::vector<std::uint64_t> selected;
    for (std::size_t id = 0; id < nodes.size() && selected.size() < batch_limit; ++id)
        if (nodes[id] == slow_ && attempts_[id] < drain_attempt_limit) selected.push_back(id);
    return selected;
}
void DrainController::reconcile(const BatchResult& result) {
    need(state_ == DrainState::Draining, "Begin drain before reconciling migration");
    require_target(fast_);
    need(!result.pages.empty() && result.pages.size() <= batch_limit, "Invalid drain batch size");
    need(result.before.rc == 0 && result.before.error == 0 && result.after.rc == 0 &&
         result.after.error == 0 && result.before.status.size() == result.pages.size() &&
         result.after.status.size() == result.pages.size(), "Missing drain batch kernel query");
    std::vector<std::uint64_t> ids;
    for (std::size_t i = 0; i < result.pages.size(); ++i) {
        const auto& page = result.pages[i];
        need(page.page_id < attempts_.size() && attempts_[page.page_id] < drain_attempt_limit,
             "Drain page attempt limit exceeded");
        need(page.query_before_node == slow_ &&
             (page.query_after_node == fast_ || page.query_after_node == slow_) &&
             page.query_before_node == result.before.node(i) && page.query_after_node == result.after.node(i),
             "Drain result lacks reconciled placement truth");
        ids.push_back(page.page_id);
    }
    std::sort(ids.begin(), ids.end());
    need(std::adjacent_find(ids.begin(), ids.end()) == ids.end(), "Duplicate drain page");
    for (const auto id : ids) ++attempts_[id];
}
void DrainController::finish(const std::vector<int>& nodes, bool pattern_ok, bool released) {
    need(state_ == DrainState::Draining, "Begin drain before authorizing offline");
    require_target(fast_);
    validate_nodes(nodes);
    if (!pattern_ok || !released || !std::all_of(nodes.begin(), nodes.end(),
            [this](int node) { return node == fast_; })) {
        fail();
        throw std::runtime_error("Drain cannot authorize memory offline");
    }
    // OFFLINE is reserved for the Guest helper after actual sysfs offlining.
    ready_ = true;
}
}  // namespace tierlab
