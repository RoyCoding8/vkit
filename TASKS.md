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
| P3. Persist immutable job subjects | [x] Implemented and checked | Raw Git/working-tree capture, closed persisted jobs, shared registered/formal dispatch, same-job retry and tagged evidence indexes. [CI 40325493199](https://github.com/RoyCoding8/vkit/actions/runs/40325493199) contains three actual passing attempts with one Git candidate/job ID, despite a changed live checkout; tampered replay rejects. |
| P4. Execute, seal and replay | [x] Complete | Product receipts, detached workers, retry and replay pass in [CI 40325493199](https://github.com/RoyCoding8/vkit/actions/runs/40325493199). [CI 40326428454](https://github.com/RoyCoding8/vkit/actions/runs/40326428454) kills a real Docker worker, records its interrupted attempt and confirms removal of its exact owned container. |
| P5. Connect CLI, MCP and the protected gate | [ ] Gate qualification in progress | CLI/MCP and local projections pass in [CI 40325632130](https://github.com/RoyCoding8/vkit/actions/runs/40325632130). The committed gate now loads target policy from raw Git, binds authority into jobs/receipts, runs fresh exact-candidate checks, and seals empirical commands in the existing container lifecycle. Real branch-interaction and policy-deletion controls are awaiting CI. A protected host-workflow example remains. |
| P6. Demonstrate reuse and finish | [ ] Pending P5 | Two API/data integration fixtures share the same package without copied proofs; installed-wheel and real Linux CI pass; direct-Verus effort/overhead measured; duplicate paths deleted. |

## Claim boundaries

- Initial product contract: `ledger.source.v1`, with `required_api` coverage of all six declared bindings under the Verus/Rust/vstd/SMT trust boundary.
- Real CI product receipts now exist for the standalone accepted source contract. The report records N=3/N=5 positives, a weakened-contract rejection, three durable Git-candidate attempts and a tamper rejection. Earlier green runs without a `product` result establish package qualification only.
- No whole-application proof, independently checked kernel certificate or operational resource bound is established. `ledger.total.v1` remains deferred; its resource-abort semantics are preserved in the plan.
- Existing successful CI runs establish their reported checks. Protected workflow authority and merge enforcement remain implementation/deployment work.

Earlier long task lists and planning decisions are retained in local audit history. They are not additional active backlogs.
