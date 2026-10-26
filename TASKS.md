# VKit implementation tasks

The [implementation plan](PLAN.md) is complete. The product integration is unfinished.
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
| P1. Complete the mathematical package and qualification | [ ] Next | Explicit map lookup/fold accounting; all six required bindings; positive and adversarial controls; sealed Linux execution and actual-container cancellation. Then delete active Rocq model/audit. |
| P2. Define and package accepted policy | [ ] Pending P1 | Closed manifest/proposal types; distinct check/formal approval records; concurrent acceptance preserves both updates; one installed copy of sources/templates/pins/schemas. |
| P3. Persist immutable job subjects | [ ] Pending P2 | Registered/formal tagged jobs; raw-byte working-tree/Git capture; detach, locking and retry; committed jobs unaffected by checkout movement. Delete worker manifest reconstruction and v2 production parsing. |
| P4. Execute, seal and replay | [ ] Pending P3 | One container lifecycle and Verus procedure; complete typed receipts; tampering/staleness blocks; replay reruns checking; cancellation/restart leaves no owned orphan. |
| P5. Connect CLI, MCP and the protected gate | [ ] Pending P4 | Identical request identities; existing settings/status/console consumers work; target authority cannot be replaced by candidate policy; exact combined-candidate checking. |
| P6. Demonstrate reuse and finish | [ ] Pending P5 | Two API/data integration fixtures share the same package without copied proofs; installed-wheel and real Linux CI pass; direct-Verus effort/overhead measured; duplicate paths deleted. |

## Claim boundaries

- Initial product contract: `ledger.source.v1`, with `required_api` coverage of all six declared bindings under the Verus/Rust/vstd/SMT trust boundary.
- No accepted product receipt exists yet. Map-fold equivalence, all-six external binding and sealed container execution remain P1 work.
- No whole-application proof, independently checked kernel certificate or operational resource bound is established. `ledger.total.v1` remains deferred; its resource-abort semantics are preserved in the plan.
- Existing successful CI runs establish their reported checks. Protected workflow authority and merge enforcement remain implementation/deployment work.

Earlier long task lists and planning decisions are retained in local audit history. They are not additional active backlogs.
