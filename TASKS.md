# VKit implementation tasks

P1 through P6 of the [formal core plan](PLAN.md) are implemented and verified for `ledger.source.v1`.
Verified implementation head: `007c5cd5e676cdbcf85a9f64b914810b059a6f29`, branch `codex/formal-qualification`.

## Completed work

| Task | State | Actual evidence |
| --- | --- | --- |
| P1. Mathematical package | [x] | One generic resource-ledger package, map accounting and all six API contracts. The final qualification's 56 checks matched, including deliberate contract/body/binding defects. Captured source compiles and executes. Superseded Rocq model/audit removed, 611 lines. |
| P2. Accepted policy | [x] | Closed version-3 manifest and request types, distinct check/formal approvals, concurrent acceptance and installed package assets pass the retained suite. No duplicated package or template. |
| P3. Immutable job subjects | [x] | Raw Git/working-tree capture, shared tagged jobs and evidence indexes. Three passing submission/retry/replay attempts retain one committed candidate/job despite live checkout edits. Jobs store the accepted checker digest; workers hash actual tools before execution. |
| P4. Execution and evidence | [x] | Fresh model/candidate/expected-client checks, receipts, replay and tamper rejection pass. A killed real worker becomes interrupted and its exact container is removed. Cancellation takes 2.115 seconds; no orphan containers remain. |
| P5. CLI, MCP and protected gate | [x] | Installed CLI and MCP handler produce the same job with distinct passing attempts. Both branch candidates are READY; their combined candidate is BLOCKED on a formal obligation. Candidate policy deletion retains target requirements. Authority, zero-exit and retained-file controls pass. |
| P6. Reuse and qualification | [x] | Quota-admission and worker-slot APIs share one package and pass fresh proofs plus five- and six-command runtime traces. Five interleaved direct/VKit comparisons measure median extra orchestration time of 0.066 seconds, below the unchanged two-second budget. |

## Verification record

| Check | Result |
| --- | --- |
| [Real Linux Verus qualification, CI 40336150324](https://github.com/RoyCoding8/vkit/actions/runs/40336150324) | Passed all package, product, durable-job, recovery, protected-gate, reuse, installed CLI/MCP and cost checks. |
| [Retained suite, CI 40336181171](https://github.com/RoyCoding8/vkit/actions/runs/40336181171) | Linux and macOS: 238 passed, one skipped each. Windows: 235 passed, four skipped. Ruff, wheel builds, installed CLI and real Lean/TLC checks pass. |
| Local verification | Targeted formal execution/interface/jobs/query/gate checks and Ruff pass. After download, all 18 finished retained run records independently pass receipt/artifact validation and job-ID recomputation. |
| Performance history | 7.22 seconds initially; 2.74 after removing repeated archive inflation; 0.066 after removing duplicate submission-time inventory. Actual tool-byte checks remain mandatory. No cache or relaxed gate. |
| Integration effort | Two API namespace/binding sets and eleven command inputs. No new model, copied proof, adapter handler or transport operation for the second interpretation. |
| Repair evidence | The deliberate weakened reserve contract blocks at `verified_expected`; restoring the baseline contract passes. No autonomous-repair performance claim. |

The final qualification used the installed wheel at `site-packages/vkit/formal/verus.py`. Both reuse fixtures record package SHA-256 `fcd3c9f02cab666d9e7750141171d1f6088ff60f06651b8ab13c40bde82317ef`.

Retained artifact `verus-ledger-007c5cd5e676cdbcf85a9f64b914810b059a6f29` contains:

| File | SHA-256 |
| --- | --- |
| `container-qualification.json` | `247809b69d09bbf0ad9e43de3ac29e7d4eddf66e53fe672b66757ab26eadc3b3` |
| `product/reuse/reuse-qualification.json` | `523a42ed3b2697fe1e99d882a6502efb3aa52bb13819f59146ef29c5fc2547f7` |

Reproduce with the `Ledger qualification` and `CI` workflows at the verified head. The checked-in scripts are `scripts/check_ledger_container.py` and `scripts/qualify_ledger_reuse.py`; provisioning is in `.github/workflows/ledger-qualification.yml`.

## Scope and remaining deployment work

- The supported claim is `ledger.source.v1`, method `verus_source`, assurance `trusted_engine`, coverage `required_api` for six declared bindings under the Verus/Rust/vstd/SMT trust boundary.
- This establishes neither whole-application correctness, independently checked kernel certificates, physical resource exclusivity nor operational resource bounds. `ledger.total.v1` remains deferred with the agreed resource-abort semantics.
- Protected target-policy checking is implemented. Enforced GitHub branch protection or organization rulesets are not configured. Use the [protected host-workflow example](examples/protected-ci/README.md) when deploying it.
- Windows child-process launches use the shared hidden-window options; the source-policy check guards test/runtime launch sites.

Earlier investigation and failed-run history remain in the local audit trail and Git history. They are not additional active tasks.
