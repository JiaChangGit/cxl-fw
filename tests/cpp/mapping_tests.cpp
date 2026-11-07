// The actual Mapping constructor is linked below; mbind is intercepted.
#include "guest_runtime.hpp"
#include <numaif.h>
#include <sys/mman.h>
#include <iostream>

namespace {
int expected_node = 0;
int bind_calls = 0;
}

// The linker wraps mbind: this test never changes Host memory policy.
extern "C" long __wrap_mbind(void* start, unsigned long len, int mode,
                             const unsigned long* mask, unsigned long maxnode, unsigned flags) {
    ++bind_calls;
    require(start != nullptr && len == page_bytes && mode == MPOL_BIND && flags == 0,
            "Unexpected mbind arguments");
    constexpr std::size_t bits = sizeof(unsigned long) * 8;
    const auto words = static_cast<std::size_t>(expected_node) / bits + 1;
    require(maxnode == words * bits + 1, "mbind maxnode must include the allocated mask bits plus one");
    // Linux 6.18.56 get_nodes() consumes maxnode-1 bits. Check the selected bit survives.
    require(maxnode - 1 > static_cast<unsigned long>(expected_node), "Selected node excluded by ABI");
    for (std::size_t word = 0; word < words; ++word) {
        const auto expected = word == static_cast<std::size_t>(expected_node) / bits
            ? 1UL << (static_cast<std::size_t>(expected_node) % bits) : 0;
        require(mask[word] == expected, "Incorrect nodemask content");
    }
    return 0;
}

int main() {
    try {
        for (const int node : {0, 1, 63, 64, 4095}) {
            expected_node = node;
            Mapping mapping(page_bytes, node);
            require(mapping.data() != MAP_FAILED, "Mapping unavailable");
        }
        require(bind_calls == 5, "Missing mbind boundary case");
        expected_node = 0;
        Mapping payload(page_bytes, expected_node);
        write_pattern(payload.data(), 42, page_bytes);
        require(verify_pattern(payload.data(), 42, page_bytes) == 0, "Initial pattern mismatch");
        auto* word = static_cast<unsigned char*>(payload.data());
        word[173] ^= 1;
        require(verify_pattern(payload.data(), 42, page_bytes) == 1, "Payload corruption was not detected");
        payload.release();
        require(payload.data() == nullptr, "Explicit mapping release did not clear ownership");
        std::cout << "PASS: mbind nodemask ABI for node 0 and word boundaries\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
