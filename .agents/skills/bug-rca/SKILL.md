---
name: bug-rca
description: Use when a user reports a concrete defect, crash, regression, failing test, or unexpected runtime behavior and requests diagnosis or repair.
license: MIT
---


# bug-rca


## Procedure
1. Capture environment, exact error, inputs and known-good revision; avoid dumping secrets.
2. Reproduce with a deterministic test. Label hypotheses separately from verified facts.
3. Narrow suspected component and explain triggering data flow, stack and state transitions.
4. Produce smallest corrective patch and a regression case that fails before the patch.
5. Execute quick/full project gate; report root cause, evidence, failed checks and residual risk.

## Boundary
Do not use for normal feature implementation or conceptual how-to questions.
