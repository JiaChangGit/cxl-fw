#pragma once
#include "migrator.hpp"
#include <cstddef>
#include <cstdint>
#include <vector>

namespace tierlab {
constexpr std::uint64_t drain_margin_bytes = 256ULL * 1024 * 1024;
constexpr unsigned drain_attempt_limit = 3;
struct DrainCapacity {
    std::uint64_t slow_pages;
    std::uint64_t required_bytes;
    std::uint64_t mem_available_bytes;
    std::uint64_t fast_free_bytes;
    bool enough;
};
DrainCapacity drain_capacity(std::uint64_t slow_pages, std::uint64_t mem_available_bytes,
                             std::uint64_t fast_free_bytes);
enum class DrainState { Active, Draining, Failed };
// No residency cache: selection and completion use fresh kernel query results.
class DrainController {
public:
    DrainController(std::size_t pages, int fast_node, int slow_node);
    DrainCapacity begin(const std::vector<int>& nodes, std::uint64_t mem_available_bytes,
                        std::uint64_t fast_free_bytes);
    void require_target(int node) const;
    std::vector<std::uint64_t> select(const std::vector<int>& nodes) const;
    void reconcile(const BatchResult& result);
    void finish(const std::vector<int>& nodes, bool pattern_ok, bool mapping_released);
    void fail() { state_ = DrainState::Failed; ready_ = false; }
    DrainState state() const { return state_; }
    bool ready_for_offline() const { return ready_; }
private:
    void validate_nodes(const std::vector<int>& nodes) const;
    std::vector<unsigned> attempts_;
    int fast_;
    int slow_;
    DrainState state_ = DrainState::Active;
    bool ready_ = false;
};
}  // namespace tierlab
