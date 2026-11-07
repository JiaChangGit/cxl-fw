#pragma once
#include "migrator.hpp"
#include <nlohmann/json.hpp>
#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>
namespace fs = std::filesystem;
using json = nlohmann::json;
constexpr std::size_t managed_bytes = 64ULL * 1024 * 1024;
constexpr std::size_t page_bytes = 4096;
constexpr std::uint64_t pattern_seed = 0xa7c35e1934b62d81ULL;
std::string text_file(const fs::path& path);
void require(bool condition, const std::string& message);
void guest_guard();
void check_node(int node);
class Mapping {
public:
    Mapping(std::size_t bytes, int node);
    Mapping(const Mapping&) = delete;
    Mapping& operator=(const Mapping&) = delete;
    ~Mapping();
    void release();
    void* data() const { return data_; }
private:
    void* data_ = nullptr;
    std::size_t bytes_;
};
std::uint64_t pattern(std::size_t offset, unsigned generation);
void write_pattern(void* data, unsigned generation, std::size_t bytes = managed_bytes);
std::uint64_t verify_pattern(void* data, unsigned generation, std::size_t bytes = managed_bytes);
void require_no_huge_pages(void* mapping, std::size_t bytes = managed_bytes);
json counts(const std::vector<int>& nodes);
int run_policy(int fast, int slow, const fs::path& output, const std::string& run_id);
int run_drain(int fast, int slow, const fs::path& output, const std::string& run_id);
int run_trace(int fast, int slow, const fs::path& output, const std::string& run_id);
