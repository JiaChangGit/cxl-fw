---
name: kernel-rca
description: Use when debugging a Linux Kernel module, driver probe, kernel warning, oops, lockup, or kernel regression with observed evidence.
license: MIT
---


# kernel-rca


## Prerequisites
- Confirm exact target kernel, headers, configuration, architecture and VM environment.
- Prefer a snapshot-capable QEMU/VM over modifying the host kernel.
## Procedure
1. Capture sanitized dmesg, oops, call trace, taint state and reproduction steps.
2. Trace fops/probe/IRQ/workqueue paths; identify locking, lifetime, refcount and memory ownership.
3. Reproduce with QEMU if feasible; do not run unsafe experiments on production hosts.
4. Use KASAN/KCSAN/lockdep where available; document limitations, overhead and expected configs.
5. Add focused regression and full build evidence; never infer hardware behavior from QEMU alone.

## Boundary
Do not claim driver faults can be reproduced without the required kernel environment.
