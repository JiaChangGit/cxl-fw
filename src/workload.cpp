#include "workload.hpp"
#include <algorithm>
#include <numeric>
#include <stdexcept>

namespace tierlab {
std::uint64_t SplitMix64::next() {
    auto z = (state_ += 0x9e3779b97f4a7c15ULL);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}
std::uint64_t SplitMix64::bounded(std::uint64_t bound) {
    if (!bound) throw std::invalid_argument("Empty RNG bound");
    const auto threshold = (std::uint64_t{0} - bound) % bound;
    std::uint64_t value;
    do { value = next(); } while (value < threshold);
    return value % bound;
}
Workload::Workload(std::uint64_t seed) : random_(seed ^ 0x94d049bb133111ebULL) {
    std::vector<std::uint64_t> pages(policy_pages);
    std::iota(pages.begin(), pages.end(), 0);
    SplitMix64 shuffle(seed ^ 0xd1b54a32d192ed03ULL);
    for (std::size_t i = pages.size() - 1; i; --i)
        std::swap(pages[i], pages[shuffle.bounded(i + 1)]);
    for (unsigned phase = 0; phase < 2; ++phase) {
        hot_[phase].assign(pages.begin() + phase * hot_pages, pages.begin() + (phase + 1) * hot_pages);
        std::vector<bool> is_hot(policy_pages, false);
        for (const auto id : hot_[phase]) is_hot[id] = true;
        for (std::size_t id = 0; id < policy_pages; ++id)
            if (!is_hot[id]) cold_[phase].push_back(id);
    }
}
const std::vector<std::uint64_t>& Workload::hot(unsigned epoch) const {
    if (epoch >= epoch_count) throw std::out_of_range("Workload epoch");
    return hot_[epoch >= 30];
}
EpochTrace Workload::next_epoch(unsigned epoch) {
    if (epoch != next_epoch_ || epoch >= epoch_count)
        throw std::invalid_argument("Workload epochs must be sequential");
    ++next_epoch_;
    EpochTrace trace{{}, std::vector<std::uint32_t>(policy_pages, 0), 14695981039346656037ULL};
    trace.accesses.reserve(epoch_ops);
    for (unsigned op = 0; op < epoch_ops; ++op) {
        const auto& group = random_.bounded(10) < 9 ? hot_[epoch >= 30] : cold_[epoch >= 30];
        const auto id = static_cast<std::uint32_t>(group[random_.bounded(group.size())]);
        const auto word = static_cast<std::uint32_t>(random_.bounded(512));
        trace.accesses.push_back({id, word});
        ++trace.counts[id];
        trace.hash = (trace.hash ^ id) * 1099511628211ULL;
        trace.hash = (trace.hash ^ word) * 1099511628211ULL;
    }
    return trace;
}
}  // namespace tierlab
