#include "guest_runtime.hpp"
#include <nlohmann/json.hpp>
#include <numaif.h>
#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <numeric>
#include <regex>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <unistd.h>

namespace fs = std::filesystem;
using json = nlohmann::json;
using namespace tierlab;

std::string text_file(const fs::path& path) {
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("Unable to read " + path.string());
    return {std::istreambuf_iterator<char>(stream), {}};
}
void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}
void guest_guard() {
    utsname info{};
    require(::uname(&info) == 0, "uname failed");
    const std::string release(info.release);
    require(release == "6.18.56", "Requires the pinned Guest kernel 6.18.56");
    const auto vendor = text_file("/sys/class/dmi/id/sys_vendor");
    const auto product = text_file("/sys/class/dmi/id/product_name");
    require(vendor.find("QEMU") != std::string::npos ||
            product.find("QEMU") != std::string::npos, "Requires the designated QEMU Guest");
    require(::sysconf(_SC_PAGESIZE) == static_cast<long>(page_bytes), "Requires 4 KiB base pages");
    const fs::path balancing("/proc/sys/kernel/numa_balancing");
    if (fs::exists(balancing))
        require(std::stoi(text_file(balancing)) == 0, "Automatic NUMA balancing must be disabled");
    else
        std::cout << "Automatic NUMA balancing: unavailable (not configured)\n";
    const fs::path ksm("/sys/kernel/mm/ksm/run");
    if (fs::exists(ksm)) require(std::stoi(text_file(ksm)) == 0, "KSM must be disabled");
}
void check_node(int node) {
    require(node >= 0 && node < 4096, "Node outside the supported range");
    const auto contents = text_file("/sys/devices/system/node/node" + std::to_string(node) + "/meminfo");
    std::smatch match;
    require(std::regex_search(contents, match, std::regex(R"(MemTotal:\s+([0-9]+))")) &&
            std::stoull(match[1]) > managed_bytes / 1024, "Node has insufficient online memory");
}

Mapping::Mapping(std::size_t bytes, int node) : bytes_(bytes) {
        data_ = ::mmap(nullptr, bytes_, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (data_ == MAP_FAILED) throw std::runtime_error("mmap: " + std::string(std::strerror(errno)));
        try {
            if (::madvise(data_, bytes_, MADV_NOHUGEPAGE) != 0)
                throw std::runtime_error("madvise: " + std::string(std::strerror(errno)));
            constexpr std::size_t bits = sizeof(unsigned long) * 8;
            std::vector<unsigned long> mask(static_cast<std::size_t>(node) / bits + 1, 0);
            mask[static_cast<std::size_t>(node) / bits] |= 1UL << (static_cast<std::size_t>(node) % bits);
            // Linux 6.18.56 get_nodes() subtracts one, as expected by libnuma's size+1 ABI.
            // Include exactly the allocated words, so the kernel cannot read beyond mask.
            const auto maxnode = static_cast<unsigned long>(mask.size() * bits + 1);
            if (::mbind(data_, bytes_, MPOL_BIND, mask.data(), maxnode, 0) != 0)
                throw std::runtime_error("mbind: " + std::string(std::strerror(errno)));
        } catch (...) {
            ::munmap(data_, bytes_);
            throw;
        }
    }

Mapping::~Mapping() { if (data_) ::munmap(data_, bytes_); }
void Mapping::release() {
    if (!data_) return;
    if (::munmap(data_, bytes_) != 0)
        throw std::runtime_error("munmap: " + std::string(std::strerror(errno)));
    data_ = nullptr;
}

std::uint64_t pattern(std::size_t offset, unsigned generation) {
    const auto page = offset / page_bytes;
    const auto word = (offset % page_bytes) / sizeof(std::uint64_t);
    std::uint64_t value = pattern_seed ^ (static_cast<std::uint64_t>(generation) * 0xd6e8feb86659fd93ULL) ^
                          (static_cast<std::uint64_t>(page) * 0x9e3779b97f4a7c15ULL) ^ word;
    value += 0x9e3779b97f4a7c15ULL;
    value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31);
}
void write_pattern(void* data, unsigned generation, std::size_t bytes) {
    auto* payload = static_cast<unsigned char*>(data);
    for (std::size_t off = 0; off < bytes; off += sizeof(std::uint64_t)) {
        const auto value = pattern(off, generation);
        std::memcpy(payload + off, &value, sizeof(value));
    }
}
std::uint64_t verify_pattern(void* data, unsigned generation, std::size_t bytes) {
    const auto* payload = static_cast<const unsigned char*>(data);
    std::uint64_t failures = 0;
    for (std::size_t off = 0; off < bytes; off += sizeof(std::uint64_t)) {
        std::uint64_t value = 0;
        std::memcpy(&value, payload + off, sizeof(value));
        failures += value != pattern(off, generation);
    }
    return failures;
}
void require_no_huge_pages(void* mapping, std::size_t bytes) {
    const auto begin = reinterpret_cast<std::uintptr_t>(mapping);
    const auto end = begin + bytes;
    std::ifstream stream("/proc/self/smaps");
    std::string line;
    bool intersects = false, found = false;
    while (std::getline(stream, line)) {
        unsigned long long start = 0, finish = 0;
        if (std::sscanf(line.c_str(), "%llx-%llx", &start, &finish) == 2) {
            intersects = start < end && finish > begin;
            found = found || intersects;
        } else if (intersects && line.rfind("AnonHugePages:", 0) == 0) {
            require(std::stoull(line.substr(line.find(':') + 1)) == 0, "Managed mapping contains THP");
        }
    }
    require(found, "Managed mapping not found in smaps");
}
json counts(const std::vector<int>& nodes) {
    json result = json::object();
    for (const auto node : nodes) {
        const auto key = std::to_string(node);
        result[key] = result.value(key, std::uint64_t{0}) + 1;
    }
    return result;
}

