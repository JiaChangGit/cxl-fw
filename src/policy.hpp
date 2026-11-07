#pragma once
#include "migrator.hpp"
#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

namespace tierlab {
enum class Temperature { Unknown, Hot, Cold };
struct PolicyPage {
    double score = 0;
    Temperature temperature = Temperature::Unknown;
    unsigned hot_streak = 0;
    unsigned cold_streak = 0;
    std::optional<std::uint64_t> last_move_epoch;
    std::optional<std::uint64_t> attempted_epoch;
    unsigned busy_failures = 0;
    std::uint64_t retry_epoch = 0;
};

// Placement is supplied by kernel queries; this core never predicts residency.
class PolicyCore {
public:
    PolicyCore(std::size_t pages, int fast_node, int slow_node,
               std::size_t fast_cap = 8192, std::size_t attempt_budget = 2048);
    void observe(std::uint64_t epoch, const std::vector<std::uint32_t>& counts,
                 const std::vector<int>& nodes);
    std::vector<std::uint64_t> select_demotions(std::uint64_t epoch,
        const std::vector<int>& nodes, std::size_t remaining) const;
    std::vector<std::uint64_t> select_promotions(std::uint64_t epoch,
        const std::vector<int>& nodes, std::size_t remaining) const;
    void reconcile(std::uint64_t epoch, int target, const BatchResult& batch);
    const PolicyPage& state(std::size_t page) const { return pages_.at(page); }
private:
    void validate_nodes(const std::vector<int>& nodes) const;
    void validate_epoch(std::uint64_t epoch) const;
    std::vector<std::uint64_t> select(std::uint64_t epoch,
        const std::vector<int>& nodes, std::size_t remaining, bool promote) const;
    std::vector<PolicyPage> pages_;
    int fast_;
    int slow_;
    std::size_t cap_;
    std::size_t budget_;
    std::size_t attempted_ = 0;
    std::optional<std::uint64_t> epoch_;
};
}  // namespace tierlab
