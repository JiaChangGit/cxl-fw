---
name: code-review
description: Use when reviewing a proposed code change, diff, commit, or pull request for defects, security risks, and missing tests without editing code.
license: MIT
---


# code-review


## Procedure
1. Read the exact diff and changed interface contracts; do not modify files in review mode.
2. Prioritize exploitable vulnerabilities, data loss, race conditions and regression risk.
3. For each finding cite file, line, trigger, impact and a concrete verification step.
4. Explicitly separate proven findings from suspicions; list unknowns and test gaps.
5. Do not claim review proves absence of defects; report coverage limits.

## Boundary
Do not silently run a full review for an unrelated simple question; do not modify files.
