#pragma once
#include <cstddef>
#include <cstdint>
#include <vector>

namespace tierlab {
constexpr std::size_t policy_pages = 32768;
constexpr std::size_t hot_pages = 6144;
constexpr std::size_t fast_cap = 8192;
constexpr std::size_t epoch_budget = 2048;
constexpr unsigned epoch_count = 60;
constexpr unsigned epoch_ops = 25000;
struct Access { std::uint32_t page; std::uint32_t word; };
struct EpochTrace {
    std::vector<Access> accesses;
    std::vector<std::uint32_t> counts;
    std::uint64_t hash;
};
class SplitMix64 {
public:
    explicit SplitMix64(std::uint64_t seed) : state_(seed) {}
    std::uint64_t next();
    std::uint64_t bounded(std::uint64_t bound);
private:
    std::uint64_t state_;
};
class Workload {
public:
    explicit Workload(std::uint64_t seed);
    EpochTrace next_epoch(unsigned epoch);
    const std::vector<std::uint64_t>& hot(unsigned epoch) const;
private:
    SplitMix64 random_;
    unsigned next_epoch_ = 0;
    std::vector<std::uint64_t> hot_[2], cold_[2];
};
}  // namespace tierlab
