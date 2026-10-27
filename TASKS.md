# Verification depth

## Interface decision

Run responses default to a compact summary. A retained run can be queried for one section, several sections, or all structured details. Selection changes presentation only; it never skips obligations or reruns verification. Raw logs remain separately accessible and bounded.

## Tasks

- [x] Ground the existing result, contract, qualification, and lifecycle paths.
- [x] Select the smallest design from two independent reviews.
- [x] Implement structured failure feedback and selective CLI/MCP run queries.
- [x] Verify summary, single-section, multi-section, and full-detail behavior.
- [x] Run a bounded ledger-refinement experiment with two representations and a broken control.
- [ ] Keep refinement only if the accepted behavior is checked without an unchecked conversion.
- [x] Consolidate reusable package qualification and change-review controls.
- [x] Add a bounded TLA+ lifecycle model and implementation-trace conformance check.
- [ ] Run local checks and real verifier qualification in CI.
- [ ] Review and remove redundant code, tests, and documentation.
- [ ] Create a corrected, consolidated remaining-features file from unfinished work only.

## Refinement stopping rule

Use the pinned Verus toolchain and existing ledger model. First establish one operation with two distinct representations, then extend the same checked relation to the six accepted operations. Allow at most two structural approaches. Each approach must produce a runnable positive case and a deliberately broken negative case. Stop an approach when it needs an unchecked conversion, weaker accepted contract, or repeated special-case workarounds. Record the exact unsupported obligation and move unfinished work into the remaining-features file rather than adding fallback infrastructure.
