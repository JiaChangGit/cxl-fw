---
name: qemu-gdb-tracing
description: Use when debugging a QEMU guest, kernel boot, GDB remote session, or missing hardware emulation signal with a concrete trace or test case.
license: MIT
---


# qemu-gdb-tracing


## Procedure
1. Record QEMU machine, CPU, boot args, kernel symbols and guest/host boundary.
2. Establish a stable baseline and exact reproducer before instrumentation.
3. Select GDB for breakpoint/state, ftrace for call flow, perf for sampling, eBPF for filtered telemetry.
4. Capture monotonic timestamps where possible; avoid conflating guest and host clocks.
5. Do not enable expensive tracing on live systems without a load budget and cleanup plan.
6. Export evidence, explain measurement bias and provide a reversible test command.

## Boundary
Do not launch arbitrary guests or alter virtual machine settings without approval.
