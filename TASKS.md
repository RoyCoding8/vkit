# VKit implementation tasks

Implementation of the [plan](PLAN.md) is in progress. The product integration is unfinished.
Follow P1 through P6 in order; update this file with each unit's actual evidence.

## Established foundation

- [x] Remove the utility catalog and retire the unqualified RefinedRust route. Archive the Rocq compatibility worktree.
- [x] Verify the generic positive-sized `Ledger<N>`, exact reserve/release behavior, atomic batches and command traces with the pinned official Verus release.
- [x] Qualify fresh export/import against an unchanged reserve contract. A checked candidate with a weakened postcondition fails the separate expected client.
- [x] Confirm clean Linux compilation/execution and deliberate failure controls in real CI.
- [x] Complete the researched implementation plan, required contract, data shapes, trust boundaries, deletion sequence and acceptance checks. Review mathematical binding and integration separately with Luna.

| Checkpoint | Evidence | Scope |
| --- | --- | --- |
| Generic package, `aba3eb9` | [CI 40237404026](https://github.com/RoyCoding8/vkit/actions/runs/40237404026), 50 verified functions, eleven rejected controls, compiled execution. | Generic source proof; no complete product integration. |
| Reserve binding, `0cb26cf` | [CI 40258535126](https://github.com/RoyCoding8/vkit/actions/runs/40258535126), all 22 checks matched, twelve rejected controls, unchanged expected client, compiled execution. Captured sources matched Git and report hashes. | One external binding over the fixed owned library. |
| Retained Python suite | 168 passed, four skipped on Windows after backend cleanup. | Earlier infrastructure regression result; not a test of the planned formal product. |

## Execute next

| Task | State | Required completion evidence |
| --- | --- | --- |
| P1. Complete the mathematical package and qualification | [x] Complete | [Sealed Linux CI 40265368525](https://github.com/RoyCoding8/vkit/actions/runs/40265368525): 59 verified functions, all six bindings, 56 checks and 29 rejected controls, compiled execution, read-only/network/cancellation/crash probes. Actual container cancellation took 2.155 seconds. Active Rocq model/audit removed. |
| P2. Define and package accepted policy | [x] Implemented | Closed v3 component/request types, distinct approval records and locked concurrent acceptance. One installed copy of model/template/pins checked through an isolated wheel. Full formal-policy proposals and CLI acceptance implemented; preflight failures write nothing. |
| P3. Persist immutable job subjects | [ ] Integration checks | Raw Git/working-tree capture, closed persisted jobs, shared registered/formal dispatch, same-job retry and tagged evidence indexes implemented. Checkout-movement and tamper tests pass. Existing report/status projections are under regression review. |
| P4. Execute, seal and replay | [ ] Integration checks | Product executor and typed receipts implemented; formal replay reruns the checker and independently recaptures candidate bytes. Real product-container CI and restart reconciliation remain. Packaged expected harness passes [CI 40266914096](https://github.com/RoyCoding8/vkit/actions/runs/40266914096). |
| P5. Connect CLI, MCP and the protected gate | [ ] In progress | CLI/MCP share the closed request and produce identical job IDs in interface tests. Formal status/gate consumers are being integrated. Protected target authority and exact combined-candidate checking remain. |
| P6. Demonstrate reuse and finish | [ ] Pending P5 | Two API/data integration fixtures share the same package without copied proofs; installed-wheel and real Linux CI pass; direct-Verus effort/overhead measured; duplicate paths deleted. |

## Claim boundaries

- Initial product contract: `ledger.source.v1`, with `required_api` coverage of all six declared bindings under the Verus/Rust/vstd/SMT trust boundary.
- No accepted product receipt exists yet. Map accounting, all-six binding and the isolated execution recipe are qualified; durable product execution and gates remain unfinished.
- No whole-application proof, independently checked kernel certificate or operational resource bound is established. `ledger.total.v1` remains deferred; its resource-abort semantics are preserved in the plan.
- Existing successful CI runs establish their reported checks. Protected workflow authority and merge enforcement remain implementation/deployment work.

Earlier long task lists and planning decisions are retained in local audit history. They are not additional active backlogs.
