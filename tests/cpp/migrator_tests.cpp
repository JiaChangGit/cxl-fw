#include "migrator.hpp"
#include <cerrno>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace tierlab;
void check(bool condition, const std::string& reason) {
    if (!condition) throw std::runtime_error(reason);
}
template<class F> void rejects(F action) {
    bool rejected = false;
    try { action(); } catch (const std::exception&) { rejected = true; }
    check(rejected, "Invalid input was accepted");
}

int main() {
    try {
        int calls = 0;
        Invoke unused = [&](unsigned long, void* const*, const int*, int*, int) {
            ++calls;
            return SyscallReturn{0, 0};
        };
        LinuxMigrator geometry(4096, 8192, 4096, unused);
        check(reinterpret_cast<std::uintptr_t>(geometry.address(1)) == 8192, "Wrong VA offset");
        rejects([&] { geometry.address(2); });
        rejects([&] { geometry.address(UINT64_MAX); });
        rejects([&] { geometry.query({2}); });
        rejects([&] { geometry.move({0}, -1); });
        rejects([&] { geometry.query({}); });
        rejects([&] { LinuxMigrator invalid(4097, 8192, 4096, unused); });
        rejects([&] { LinuxMigrator invalid(4096, 0, 4096, unused); });
        rejects([&] { LinuxMigrator invalid(UINTPTR_MAX - 4095, 8192, 4096, unused); });
        LinuxMigrator large(4096, 257 * 4096, 4096, unused);
        rejects([&] { large.move(std::vector<std::uint64_t>(257, 0), 1); });
        check(calls == 0, "Range checks reached the injected syscall");

        int step = 0;
        LinuxMigrator partial(4096, 8192, 4096,
            [&](unsigned long n, void* const* addresses, const int* nodes, int* status, int flags) {
                check(n == 2 && reinterpret_cast<std::uintptr_t>(addresses[1]) == 8192,
                      "Wrong syscall address array");
                const int current = step++;
                if (!nodes) {
                    check(flags == 0, "Query has migration flags");
                    status[0] = current == 0 ? 0 : 1;
                    status[1] = 0;
                    return SyscallReturn{0, 0};
                }
                check(nodes[0] == 1 && nodes[1] == 1, "Wrong target array");
                check(status[0] == unknown_status && status[1] == unknown_status,
                      "Move statuses not initialized to sentinel");
                status[0] = 1;  // 第二頁模擬尚未嘗試，不能被當成 node 0。
                return SyscallReturn{1, 0};
            });
        const auto result = partial.move({0, 1}, 1);
        check(result.syscall_rc == 1 && result.pages.size() == 2, "Partial result discarded");
        check(result.pages[0].move_status_valid && result.pages[0].query_after_node == 1,
              "Verified success missing");
        check(!result.pages[1].move_status_valid && result.pages[1].move_status == unknown_status &&
              result.pages[1].query_after_node == 0, "Untouched status treated as success");

        step = 0;
        LinuxMigrator batch_error(4096, 4096, 4096,
            [&](unsigned long, void* const*, const int* nodes, int* status, int) {
                if (nodes) {
                    status[0] = 1;  // rc=-1 時，即使內容看似成功也不可相信。
                    return SyscallReturn{-1, EFAULT};
                }
                status[0] = step++ == 0 ? 0 : 1;
                return SyscallReturn{0, 0};
            });
        const auto failed = batch_error.move({0}, 1);
        check(failed.syscall_errno == EFAULT && !failed.pages[0].move_status_valid,
              "Whole-batch errno or validity lost");
        check(failed.pages[0].query_after_node == 1,
              "Reconciliation ignored actual placement after a syscall error");

        LinuxMigrator query_error(4096, 4096, 4096,
            [](unsigned long, void* const*, const int*, int* status, int) {
                status[0] = 0;
                return SyscallReturn{-1, EPERM};
            });
        const auto denied = query_error.query({0});
        check(denied.error == EPERM && denied.node(0) == unknown_status && denied.status[0] == 0,
              "Failed query fabricated a valid node or lost raw data");
        std::cout << "PASS: migrator range, partial status, batch error and reconciliation controls\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
