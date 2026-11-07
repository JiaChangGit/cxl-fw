#include "migrator.hpp"
#include <cerrno>
#include <linux/mempolicy.h>
#include <stdexcept>
#include <utility>
#include <sys/syscall.h>
#include <unistd.h>

namespace tierlab {
SyscallReturn linux_move_pages(unsigned long count, void* const* pages,
                               const int* nodes, int* status, int flags) {
    errno = 0;
    const long rc = ::syscall(SYS_move_pages, 0, count, pages, nodes, status, flags);
    return {rc, rc == -1 ? errno : 0};
}
int QueryResult::node(std::size_t index) const {
    return rc >= 0 && index < status.size() ? status[index] : unknown_status;
}
LinuxMigrator::LinuxMigrator(std::uintptr_t base, std::size_t bytes,
                             std::size_t page_size, Invoke invoke)
    : base_(base), bytes_(bytes), page_size_(page_size), invoke_(std::move(invoke)) {
    if (page_size != 4096 || base % page_size != 0 || bytes == 0 || bytes % page_size != 0 ||
        bytes > std::numeric_limits<std::uintptr_t>::max() - base || !invoke_)
        throw std::invalid_argument("Invalid managed mapping range");
}
void* LinuxMigrator::address(std::uint64_t id) const {
    if (id >= bytes_ / page_size_) throw std::out_of_range("page_id outside managed mapping");
    return reinterpret_cast<void*>(base_ + static_cast<std::uintptr_t>(id) * page_size_);
}
QueryResult LinuxMigrator::query(const std::vector<std::uint64_t>& ids) const {
    if (ids.empty()) throw std::invalid_argument("Empty query");
    std::vector<void*> pages;
    pages.reserve(ids.size());
    for (const auto id : ids) pages.push_back(address(id));
    QueryResult result{0, 0, std::vector<int>(ids.size(), unknown_status)};
    const auto call = invoke_(pages.size(), pages.data(), nullptr, result.status.data(), 0);
    result.rc = call.rc;
    result.error = call.error;
    return result;
}
BatchResult LinuxMigrator::move(const std::vector<std::uint64_t>& ids, int target) const {
    if (ids.empty() || ids.size() > batch_limit || target < 0)
        throw std::invalid_argument("Invalid migration batch or target");
    std::vector<void*> pages;
    pages.reserve(ids.size());
    for (const auto id : ids) pages.push_back(address(id));
    const auto before = query(ids);
    std::vector<int> targets(ids.size(), target), status(ids.size(), unknown_status);
    const auto call = invoke_(pages.size(), pages.data(), targets.data(), status.data(), MPOL_MF_MOVE);
    const auto after = query(ids);
    BatchResult result{call.rc, call.error, before, after, {}};
    for (std::size_t i = 0; i < ids.size(); ++i)
        result.pages.push_back({ids[i], before.node(i), status[i],
                                call.rc >= 0 && status[i] != unknown_status, after.node(i)});
    return result;
}
}  // namespace tierlab
