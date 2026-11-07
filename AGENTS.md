# CXL/MM project guidance

## Current scope
- This checkout is at the **host foundation** stage: AI guidance, Host dependencies, KVM access and environment verification.
- Do not start tierlab, QEMU/kernel builds, Guest images or memory experiments until the user requests the implementation stage. An explicit later implementation request is sufficient authorization; do not ask again for routine steps already covered.
- Technical source of truth: cxl_memory_tiering_codex_implementation_v2.html. Read only relevant sections. README.md describes commands and current readiness.

## Engineering
- Keep one Guest binary, one Host runner and the five milestones in the HTML. Avoid speculative abstractions, extra daemons, duplicated documents and unrelated refactoring.
- Use C++17 for the eventual policy/workload and Python standard library for Host tooling. Run Linux tools in WSL Ubuntu-26.04, not Windows tools against the UNC checkout.
- Keep policy separate from syscalls. Kernel queries are placement truth; preserve rc/errno, partial failures and invalid statuses. Evaluator must not use the policy residency cache.
- Guest memory online/offline, CXL operations and tracing target the designated Guest only. Never use WSL host memory sysfs as a substitute.
- Honor existing user authorization. Do not add blanket confirmation steps from imported templates. Remote publishing, destructive cleanup and actions outside the requested scope still require actual authorization.
- Secrets stay out of commands, code, logs and reports. Use existing OS authentication; never persist chat-provided credentials.

## Verification and communication
- Use make check-quick for scaffold/config changes and make check before reporting Host readiness. These are foundation checks, not Guest G0-G5.
- Never label an unrun build/test/Guest boot PASS. Distinguish HOST_READY, KVM API/empty-VM evidence and Guest boot NOT_RUN.
- Add focused tests for changed behavior when meaningful; do not add mirrored tests, fake project tests or a demo app just to satisfy CTest.
- Explain results in natural Taiwan Traditional Chinese; CLI/log output uses English.
- Use the most specific relevant skill from .agents/skills when needed, not every skill on every task. Read-only review remains read-only.
- Parallel agents use disjoint file ownership. Do not create worktrees just for read-only work.
- No automatic commit, push, PR, merge, hook installation or global Codex configuration changes.

## PAEF adoption
- .paef/upstream.json records selected PAEF 2.0.0 files and hashes. This is not a PAEF manager installation.
- quality.json is project-owned. Add real future checks when implementation exists; do not copy generic kernel placeholders or disable required checks to obtain PASS.
