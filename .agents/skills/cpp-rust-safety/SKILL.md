---
name: cpp-rust-safety
description: Use when analyzing C, C++, or Rust undefined behavior, ownership, concurrency, memory lifetime, or ABI compatibility in specific code.
license: MIT
---


# cpp-rust-safety


## Procedure
1. Identify ownership, lifetimes, aliasing, allocations and FFI boundaries.
2. Audit C/C++ UB, integer overflow, use-after-free, data races and error paths.
3. Audit Rust unsafe blocks, Send/Sync claims, panic safety and FFI assumptions.
4. Prefer minimal deterministic unit tests; use ASan/UBSan/TSan or Miri where applicable.
5. Do not label a passing sanitizer as proof of memory safety. Provide residual risks.

## Boundary
Do not auto-run large safety audits for routine documentation changes.
