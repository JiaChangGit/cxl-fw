#pragma once
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <vector>

namespace tierlab {
constexpr int unknown_status = std::numeric_limits<int>::min();
constexpr std::size_t batch_limit = 256;
struct SyscallReturn { long rc; int error; };
using Invoke = std::function<SyscallReturn(unsigned long, void* const*, const int*, int*, int)>;
SyscallReturn linux_move_pages(unsigned long count, void* const* pages,
                               const int* nodes, int* status, int flags);
struct QueryResult {
    long rc;
    int error;
    std::vector<int> status;
    int node(std::size_t index) const;
};
struct PageMoveResult {
    std::uint64_t page_id;
    int query_before_node;
    int move_status;
    bool move_status_valid;
    int query_after_node;
};
struct BatchResult {
    long syscall_rc;
    int syscall_errno;
    QueryResult before;
    QueryResult after;
    std::vector<PageMoveResult> pages;
};
class LinuxMigrator {
public:
    LinuxMigrator(std::uintptr_t base, std::size_t bytes, std::size_t page_size,
                  Invoke invoke = linux_move_pages);
    void* address(std::uint64_t id) const;
    QueryResult query(const std::vector<std::uint64_t>& ids) const;
    BatchResult move(const std::vector<std::uint64_t>& ids, int target) const;
private:
    std::uintptr_t base_;
    std::size_t bytes_;
    std::size_t page_size_;
    Invoke invoke_;
};
}  // namespace tierlab
