# VKit formal core implementation plan

P1 through P6 are implemented and verified for the source contract. Current results and exact artifacts are recorded in [TASKS.md](TASKS.md).
Ship one reusable mathematical package through the existing CLI, MCP, workers and evidence store.
The first contract is `ledger.source.v1`, checked with Verus. It does not satisfy `ledger.total.v1`.
Execute P1 through P6 in order. This document supersedes earlier backend and integration plans.

## What an agent gets

`verify allocation` resolves an accepted component into every required mathematical obligation, captures the exact source, verifies its implementation and expected contracts, then publishes scoped evidence. The agent cannot choose weaker obligations or trust its own success report.

| Public operation | Change | Authority |
| --- | --- | --- |
| `status` | Show accepted components, required contracts, declared coverage and current evidence gaps. | Read policy and receipts. Never run proofs implicitly. |
| `propose` and CLI `accept` | Review complete formal-policy changes alongside existing check proposals. | Proposal creation cannot accept policy. No MCP acceptance tool. |
| `gate` | Require complete evidence for the actual candidate under protected target policy. | Candidate declarations and local approval files cannot reduce CI requirements. |
| `verify` | Add one component-level operation that expands the owned obligation set and uses the existing durable job lifecycle. | Requests select a component and source, not formulas, commands or checker flags. |

Existing run inspection, wait, cancel, retry, export and replay remain the lifecycle interface. There is no second tool catalog.

```mermaid
flowchart LR
    A[CLI or MCP request] --> B[Accepted policy and owned package]
    B --> C[Resolve obligations and freeze source]
    C --> D[Existing job worker]
    D --> E[Fresh Verus library and candidate checks]
    E --> F[Unchanged expected contract harness]
    F --> G[Existing evidence store]
    G --> H[Status and protected candidate gate]
```

## Fix the first supported scope

| Decision | Initial implementation |
| --- | --- |
| Mathematical package | `vkit.resource-ledger`, initially version `0.1.0`, with content-addressed sources and expected templates. |
| Contract | `ledger.source.v1`. Functional source verification under the pinned Verus/Rust/vstd/SMT trusted computing base. |
| Candidate shape | A standalone Verus-compatible Rust component using the fixed owned `Ledger<N>` type. Candidate functions may compose its operations. |
| Required surface | All six bindings: `new`, `reserve`, `release`, `reserve_batch`, `apply_command`, `run_commands`. |
| Parameters | Positive compile-time `N` fitting the target `usize`. IDs, owners, capacities and amounts are `u32`. Batch capacity is exactly three. Command arrays have compile-time length `M`. |
| Coverage | `required_api`: every package-required callable and expected contract. Additional exports and surrounding application behavior are outside this claim. |
| Build | Official Verus `0.2026.10.18.7d82994`, revision `7d829946231d6413a398f6e4160cdfe951261dad`, Rust `1.99.1`, Z3 `4.16.0`, Linux x86-64. |
| Dependencies | Fixed owned package, Rust and pinned vstd. No candidate Cargo execution, build scripts, procedural-macro dependencies, arbitrary compiler flags or dependency acquisition during checking. |
| Execution | One digest-pinned Linux container image through Docker. Read-only source and trusted inputs, private writable output, no network or host credentials. Missing Docker is BLOCKED, never an unisolated fallback. |
| Extensibility | Add a package handler, its assets and a typed assurance method when justified. Preserve the same resolved-job, worker and receipt contracts. No plugin loader or verifier fork. |

This first profile proves callable contracts over ledger values. It does not prove authentication, physical resource allocation, persistence, shared-state synchronization, correct use by unverified callers, compiled-binary correctness or whole-application correctness. Copying a ledger value does not establish exclusive ownership of real resources.

A fresh Verus rerun is an independent execution of source verification. It is not independently checked kernel-certificate evidence. Keep assurance dimensions explicit; do not describe vstd as axiom-free.

Defer custom storage representations, automatic public-API discovery, relational `change` goals, arbitrary Cargo applications, other verifier packages and operational totality. The v1 request schema accepts only `implements`. Reprove the actual merged candidate against the same contract instead of adding a two-version engine.

## Establish the mathematical contract

The invariant requires positive active amounts, unique reservation IDs, bounded record count, cached usage equal to the reservation total, and usage at most capacity.

| Callable | Required proposition |
| --- | --- |
| `new` | Clear supplied preallocated slots; produce a valid empty ledger with zero usage and the requested capacity. No application precondition stronger than the declared storage/type bounds. |
| `reserve` | Error order is zero amount, duplicate ID, full storage, capacity exceeded. Success inserts exactly the requested record and increases usage by its amount. Rejection preserves the complete view. |
| `release` | Check existence before owner equality. Success removes exactly that ID, returns its amount and decreases usage accordingly. Rejection preserves the complete view. Released IDs may be reused. |
| `reserve_batch` | Check count against three first. Apply requests in order and return the first failing index. Commit all successful requests together or preserve the original view. |
| `apply_command` | Select the exact operation and tagged response specified by `apply_model`. |
| `run_commands` | Produce an `M+1` state trace relating every next state and response to `apply_model`. Empty sequences preserve the initial view. |

Require checked initialization, invariant preservation, exact errors, map effects, arithmetic/access safety, batch atomicity and command composition. Source loop measures remain checked; they are not an operational step bound.

### Close map accounting before shipping

Use the pinned vstd sequence and finite-set laws rather than another map library:

1. Define `active_ids(slots)` in slot order. Prove no duplicates and equality with the projected map's key set under the invariant.
2. Prove exact map lookups for every occupied slot, including both owner and amount. Domain membership alone is insufficient.
3. Define `map_used(m)` by folding natural-number addition over `m.dom().to_seq()`, reading each key's amount. Do not fold a set of values, which would collapse equal amounts in different records.
4. Relate that key sequence to `active_ids` as a permutation. Reuse `lemma_fold_left_permutation`, the set-to-sequence laws and addition commutativity to show enumeration independence.
5. Prove slot sum equals this map fold, then expose `ledger.view().used == map_used(ledger.reservations())` for valid states. Check initialization, insertion, removal and preservation through batches/traces.

The initial-profile properties and binding checks passed, and the active `Model.v` and `ModelAudit.v` have been deleted. Their broader Rocq statements and kernel evidence remain recoverable in Git and retained artifact 28. No cross-language equivalence theorem is claimed.

`ledger.total.v1` remains future work. Its stronger requirement permits a correct normal return or a constructor-justified resource abort within a proved operational bound, plus a resource-sufficient normal-execution witness. The owner's resource-abort decision remains recorded. Source-profile evidence cannot discharge that contract, and this research does not block delivery of the separately named source profile.

## Bind code to unchanged expected statements

Keep three inputs structurally separate:

| Input | Owner | Rule |
| --- | --- | --- |
| Ledger model, type and verified implementation | Installed digest-pinned package | Restore from accepted package assets, never candidate copies. |
| Candidate component and annotations | Captured candidate source | Fully verify and compile with fixed arguments; its assertions and postconditions are untrusted inputs. |
| Expected harness | Accepted package templates plus validated policy bindings | Candidate source cannot edit it, substitute its model or choose its formulas. |

The existing import experiment supplies the mechanism:

1. Fully check and compile the owned library with Verus `--export`.
2. Compile the candidate as a library under the fixed crate alias `candidate_api`, importing the fresh owned VIR/library through `--import` and `--extern`.
3. Generate the expected module from trusted templates. Each function calls the compiler-resolved candidate symbol with exactly the accepted precondition and requires the owned postcondition.
4. Fully check that expected module against the candidate export. Missing symbols, incompatible types, stronger candidate preconditions such as `False`, or weaker postconditions cannot discharge it.
5. Instantiate the accepted `N` through Rust types and literal const arguments. Do not discover or rewrite source constants with a parser. Preserve generic `M` in the command contract and include empty/nonempty downstream witnesses.

Bindings are validated Rust identifier paths relative to `candidate_api`; templates provide the fixed crate prefix, types and parameters. Candidate input is never interpolated as arbitrary Rust text. The compiler, not a string comparison of statements, performs the binding.

The package owns immutable obligation IDs and their expected-function mapping. A complete strict invocation of the unchanged expected module establishes its entire generated obligation set. A success counter or candidate-reported function list is never a coverage oracle. Partial successes cannot satisfy the component gate.

Use `--no-cheating` and the pinned release's `--no-external-by-default`. Own the full argument vector. Deny verification filters, skipped verification/lifetime checking, solver-version bypasses and unapproved trusted-body annotations. Record call graphs and axiom-usage diagnostics; they do not constitute a kernel assumption audit. No stale or candidate-supplied VIR substitutes for a fresh source check.

## Define requests and accepted policy once

CLI and MCP map to the same installed closed request:

```json
{
  "component": "allocation",
  "goal": {"kind": "implements"},
  "candidate": {"kind": "working_tree"},
  "wait_seconds": 0
}
```

The other source selector is `{"kind":"git_commit","oid": full_commit_oid}`. Resolve CLI refs before constructing it; persist a full immutable commit ID, never a branch name. Git mode may include `target_commit` as context. Working-tree mode cannot supply an integration target. Wait is bounded to 0 through 60 seconds and excluded from semantic identity.

Planned CLI forms are `vkit verify allocation --working-tree --wait 0` and `vkit verify allocation --candidate HEAD --wait 0`. No request accepts a checker command, obligation subset or policy-authority override.

Extend `verification/manifest.json` to v3. Keep existing checks and add `formal.components` with the following closed component shape:

| Field | Contract |
| --- | --- |
| `id` | Stable component identifier. |
| `package` | Installed package ID, version and actual SHA-256 content digest. |
| `contract` | Exactly `ledger.source.v1` for the initial package. |
| `source.entry` | Normalized repository-relative Rust entry file. Candidate crate alias is fixed by the executor. |
| `profile` | Exactly `verus-linux-x86_64-v1`, resolving target, tool pins and arguments from package metadata. Bind the actual immutable image and mounted-tool bytes in the separately accepted checker identity. No request-supplied build options. |
| `parameters.max_records` | Positive integer used by trusted const/type instantiation. Batch limit is a package constant, not another configurable field. |
| `bindings` | All six operation keys mapped to validated relative Rust paths. Missing or additional operation keys are rejected. |
| `required` | Whether protected repository policy requires this component at the integration gate. |

Candidate annotations live in captured source. There is no arbitrary proof-command field, logical-axiom override, empty-TCB declaration or second model registry. Package metadata owns expected templates, obligation IDs, trusted dependencies, environment premises and supported parameters.

Reuse `proposals.validate/submit/apply`. A `formal` proposal contains a complete replacement formal section; validate the complete resulting manifest before acceptance. Review old/new contract, scope, bindings, package and tool identities. CLI acceptance writes a local review record; no proposal accepts itself.

Migrate `approved.json` atomically to `{"schema_version":2,"records":{...}}`. Records are tagged `check_definition` or `formal_policy`; formal records bind policy, package and checker digests. Legacy approvals become check records only, preserving their old identities. Changed checker identities require renewed approval, never automatic promotion. Add one store-wide approval lock using the existing OS-lock primitive; hold it across read/merge/atomic replacement. Per-run locks do not protect this shared file. Concurrent acceptance from two worktrees must retain both records.

Protected CI authority is constructed by the trusted host workflow from its target policy and pinned VKit installation. It is not deserialized from a request, candidate manifest, bundle or local approval file. Accepting a repository policy later is distinct from selecting this implementation design now.

## Resolve and execute one job type

```text
Source = WorkingTreeSnapshot(raw_file_inventory, capture_digest)
       | GitCommit(commit, tree, parents, raw_file_inventory, capture_digest)

ResolvedJob = RegisteredCheck(definition, source, baseline, accepted_identity)
            | FormalGoal(component_policy, implements, source, target_context,
                         package_identity, accepted_checker_sha256, obligations)

Approval = CheckDefinition(...) | FormalPolicy(...)
ReceiptMethod = RegisteredCheckMethod(check_kind) | VerusSource
```

The earliest incompatible assumption is `check_id` as every job's subject. Replace that seam at `runner.prepare/execute` and `jobs.submit/main`; keep `_launch`, Store, process locks and the existing queued/running/interrupted/done lifecycle.

Resolve policy, obligations and source once before launching. Persist the complete closed job payload in `job.json`. The job binds the accepted checker digest; the worker hashes the actual tool inventory and requires that digest before verification, then records the inventory in the receipt. Submission does not repeat the inventory scan. The worker executes the captured payload, not a reconstructed manifest or mutable checkout. Retry creates a new `run_id` for the same immutable `job_id`; submission timestamps and host paths do not affect semantic identity.

Hash canonical validated semantic JSON with ordered file inventories. Bind source mode/commit/tree/raw bytes, accepted definition or policy, target context, package/templates, required obligations, verifier method, execution image, tool/library bytes, fixed arguments and relevant environment. Preserve request/command list order. Exclude wait/log windows, attempt IDs and timestamps. Formal and registered subjects are distinct tagged values, never synthetic check IDs.

For Git capture use `git ls-tree -rz` and batched `git cat-file`, materializing immutable blobs and recording modes. Recompute Git blob identities and raw SHA-256s from captured bytes. Dirty/untracked files cannot enter a committed source. Formal scope is the repository tree: all commit blobs in Git mode; tracked and untracked-but-not-ignored files in working-tree mode. Registered local checks retain their declared-input selection semantics. Freeze raw bytes and record the source kind. Apply the existing `inputs.is_secret` path policy and reject a formal capture containing such paths or unsupported symlinks/submodules; do not silently omit files. This is a path policy, not secret-content detection.

Every source-valued path resolves beneath the captured root. Relative paths in parsed check definitions remain relative until resolution; do not retain original absolute source paths while merely changing `cwd`. Committed checks never invalidate themselves because another checkout moves. Working-tree freshness remains a separate current-source query. Preserve original snapshots; ordinary tests may use a private execution copy for generated files.

Each attempt owns separate source, trusted-package, candidate-export, expected-check and output directories. No shared generated proofs. Use a digest-pinned Docker image containing the official toolchain and required execution runtimes. Mount the separately hashed trusted VKit executor and package assets read-only; keeping them outside the image avoids a package/image digest cycle. Build/pull dependencies before sealed checking. Mount only captured inputs and trusted assets read-only; mount private output, use a non-root UID, drop capabilities, disable networking and privilege escalation, and bound temporary/output resources. Do not mount the repository common directory, Docker socket, home directory or credentials.

Run the container through existing process/cancellation code. Persist its Docker CID outside candidate-writable output, label it with `run_id`, and stop/kill that exact container on cancellation/timeout. Reconcile only matching attempt-labeled containers after worker interruption. Killing the Docker client alone is insufficient. No new scheduler or cleanup daemon.

## Make evidence method-specific

| Boundary | Change |
| --- | --- |
| `schemas/manifest.v3.json` | Closed checks plus formal component definitions. Migrate active manifests; delete the v2 production parser. |
| `schemas/verification-request.v1.json` | One `implements` request with tagged source selector. |
| `schemas/job.v2.json` | Closed resolved registered/formal subject and semantic identity. |
| `schemas/evidence-record.v2.json` | Typed subject, attempt outcome, resolved-job digest, obligations and integrity receipt. |

Register shared schema references from installed resources in `schemas.py`; resolve them offline. Validate at submission/load/publication/import/replay boundaries and recompute identities. Keep old records behind a historical decoder for inspection/export only; they cannot satisfy current formal gates.

Add claim category `source_verification`. A `verus_source` receipt must use assurance `trusted_engine`; it cannot be labeled `kernel_proof`. Existing registered Lean/TLC/external semantics remain distinct. Do not force Verus through `ExternalCheck`, whose separate producer/checker protocol means something different.

Seal policy and authority identity, package/templates, scope `required_api`, required/bound symbol mapping, source and tool identities, complete obligation results, trusted specification inventory, full argv, logs, VIR/library digests and all retained artifacts. Hashes detect changes; they do not authenticate an external writer or establish verifier soundness.

Index current evidence at `evidence/<subject-kind>/<safe-id>/<job-id>.json`. Latest pointers are advisory. Formal replay validates the bundle and current matching accepted policy/package/method, restores captured source, and reruns the complete library/candidate/expected procedure. Do not accept only saved counters or unverified exports. A changed policy/checker is stale. Historical replay cannot certify a new candidate.

Map established obligations to PASS. Missing proofs, unsupported input, timeouts, tool absence, invalid evidence and failed proof obligations are BLOCKED with structured stage/obligation diagnostics. Add `BlockedReason.FORMAL_OBLIGATION`. Do not call an SMT proof failure a validated source counterexample; source-level refutation is not implemented in v1.

## Gate the actual combined candidate

The trusted gate loads required formal components and empirical checks from protected target authority. It requires exact candidate and target commits, matching package/profile/method, all mandatory obligations, complete required-API binding, current identities and untampered evidence. A working-tree receipt cannot pass it.

Required empirical checks also execute the captured candidate through the same resolved-job lifecycle. Report their evidence separately. A path/status display filter cannot shrink gate requirements, and deleting candidate policy cannot remove a target requirement.

For GitHub use `pull_request` and `merge_group`; obtain candidate/base IDs from the host event. Run fresh verification on the combined merge candidate. Two branch passes cannot substitute for this job. Target movement requires the host's new candidate check. There is no custom merge watcher or proof-cache composition engine.

The workflow definition, policy source and pinned VKit installation must be protected by host controls. Keep policy resolution, receipt sealing and gate publication outside candidate execution. In protected CI, execute required empirical checks inside the same sealed container boundary, using their existing registered-check worker over a private execution copy. The host-selected image must contain their declared runtimes; unsupported runtimes block. Local registered-check execution retains its existing route. Reuse the container lifecycle helper rather than introduce another runner. Candidate containers receive neither authority files nor publishing credentials.

The first sealed empirical profile runs target-policy driver code declared as `{{python}} -I -c CODE ...` in a scenario check. Its executable and standard library come from the immutable host-selected image. Candidate-owned checker files and other registered profiles block. This keeps checker authority explicit without guessing which repository files are trusted. Every passing protected check must exit zero. Retained evidence rejects links, special files and unlisted artifacts; it has a post-execution inventory limit of 10,000 files and 512 MiB. The per-file execution limit does not establish an aggregate execution-time disk quota.

If host controls cannot protect that authority, report a standalone verdict, not an enforceable merge guarantee. This temporary repository's successful Actions runs demonstrate checking, not configured branch protection. Enabling organization/ruleset controls is a deployment concern, not a mathematical claim.

## Execute six verifiable units

One unit ends with its stated evidence before the next begins. Use sequential commits on the existing implementation branch. These are implementation tasks, not requests for new chats, scheduled automation or PR ceremony. Drive CLI/MCP surfaces with structured requests and saved logs rather than screenshots.

| Unit | Build and file boundary | Completion evidence | Remove in the same unit |
| --- | --- | --- | --- |
| P1. Complete the package | Deepen `src/vkit/formal/packages/resource_ledger/ledger.rs`, the import fixtures, existing `scripts/check_verus_ledger.py` and qualification workflow. Add the map-fold bridge, expected contracts for all six operations, and qualified sealed Linux execution recipe. | Fresh full verification; all six bindings pass externally; strengthened precondition, weak contract, wrong symbol/body, missing binding and trust-escape controls reject; captured source compiles/runs. Probe read-only inputs, disabled network and cancellation of the actual container. | Remove active Rocq model/audit after the selected laws pass. Remove reserve-only qualification assumptions. Do not add a second driver. |
| P2. Define and package policy | Add the small `src/vkit/formal` domain/resolver module and `formal/ledger.py` for obligations/templates. Extend `manifest.py`, `schemas.py`, proposals, approval Store methods and CLI acceptance. Move owned math/templates/pins to `src/vkit/formal/packages/resource_ledger/`; update `pyproject.toml`. | Manifest/proposal boundary tests; local check/formal approvals cannot substitute for each other; changed package needs approval; concurrent worktree acceptance retains both records; isolated installed-wheel test locates every source/template/schema. | Remove old root copies, duplicate fixture templates and superseded approval-map access. Keep one canonical package source. Migrate persisted manifests now; retire the worker's last v2 reconstruction with P3. |
| P3. Persist immutable subjects | Refactor `jobs.py`, `runner.py`, `inputs.py`, `store.py` and `outcome.py` around the tagged resolved payload and raw snapshots. Migrate registered callers, retry and worker startup together. | Existing check lifecycle passes; detached jobs survive client exit; duplicate workers cannot execute one attempt; retry keeps job ID/new run ID; moving another checkout cannot change a committed job; raw-byte and source-mode changes alter identity. | Remove manifest reconstruction in the worker and the v2 production parser, semantic use of host paths and unconditional live-checkout validation for committed jobs. Only historical record decoding remains. |
| P4. Execute and replay formal jobs | Add one `formal/verus.py` procedure using the existing process/store machinery. Reuse one container lifecycle helper for formal attempts and protected-CI registered-check workers. Extend evidence, claim kinds and versioned receipts. Reuse the procedure from the comparison script rather than copy execution logic. | Real container-backed worker jobs, tamper checks, cancellation and restart reconciliation; complete receipts/replay; stale policy/tool/package and modified exports block; source receipts cannot satisfy kernel/totality profiles. | Remove duplicate comparison-only setup/verification logic once product calls replace it. No producer/checker shim or second queue. |
| P5. Expose and enforce | Extend `cli.py`, MCP registration, `query.py`, `state.py`, status/gate, evidence replay and existing console record consumers. Add one protected-policy CI example. | CLI/MCP resolve identical requests to identical job IDs; disabled-tool settings remain enforced; pending diagnostics are actionable; candidate policy deletion, missing evidence and combined-merge bugs cannot pass trusted-target gate tests. | Remove check-only assumptions in shared status/retry/replay/index paths and stale console projections. No console redesign or new tool category. |
| P6. Demonstrate reuse and ship | Add two small integration fixtures interpreting ledger units as quota admission and worker-slot reservations. They are candidate API modules plus command data, not new services or scheduler features. Use the same owned package/templates. Finish owner-facing explanation in this plan, retained suite, installed-wheel and real Linux CI checks. | Both fixtures pass; deliberate accounting/ownership/batch/merge defects cannot establish the claim; no copied domain proofs or new handler for the second instance. Record direct-Verus comparison, integration effort, repair effort and measured overhead. | Delete superseded fixtures/temporary bridges and any helper with no remaining caller. Keep historical evidence out of the active task list. |

Package construction and verification are internal work, not extra CLI tools. A new formal package later implements the same obligation-resolution/expected-statement boundary and typed receipt method; CLI, MCP, Store and job lifecycle stay unchanged. Collapse proposed module splits if they become one-caller forwarding layers.

### Require these behavior controls

| Control | Required result |
| --- | --- |
| Correct init/reserve/release/batch/dispatch/trace, including `N != 3` and `M = 0` | Every required API obligation checks and the captured source executes as expected. |
| Duplicate equal amounts under different IDs | Map total counts both records; domain/value-set confusion fails. |
| Wrong error order, owner, usage, returned amount, rollback index, partial commit or trace response | No established component receipt. |
| Candidate changes model/type, required symbol or precondition to `False`; weakens postcondition to `true` | Fixed library/type binding or unchanged expected harness rejects. |
| Extra public API | Receipt scope still names only the six required bindings. State that other exports are unclaimed; no automatic export inventory is implied. |
| Attempted filter/unchecked body/assumed proof/alternate solver | Fixed executor arguments and strict checker reject; no accepted partial run. |
| CRLF-only change, changed package/template/dependency/tool/image/flag | Different source or execution identity; no reuse of a passing receipt. |
| Dirty checkout during a commit job | Immutable committed bytes remain its subject; untracked files cannot contaminate it. |
| Modified snapshot/job/receipt/VIR or replay under changed authority | Validation or fresh checking blocks. |
| Two individually passing branches whose combined source violates the contract | Fresh combined-candidate verification blocks the merge gate. |
| Candidate removes policy or tries to use local approval as CI authority | Protected target requirements still apply. |
| Worker/client exit, cancel, timeout and retry | Durable state stays accurate; only the attempt's own container stops; no orphan container or passing partial receipt. |

### Bound cost without weakening claims

Keep the current 180-second per-Verus-process watchdog and 15-minute CI-job limit as execution budgets, not formal step bounds. Require cancellation acknowledgement and container termination within five seconds in the qualification fixture. A budget expiry is BLOCKED.

Use the existing report timings. For a fixture of at most 100 captured files, compare five interleaved warm-image runs of the same complete direct-Verus procedure and VKit execution. Target median orchestration overhead at most two seconds, excluding identical proof work and one-time image acquisition. Measure the old registered-check path separately; do not invent a formal baseline for software that lacks this feature. A missed budget triggers diagnosis before adding caching, parallel proof workers or another abstraction.

## Finish when the evidence exists

- [x] P1 through P6 have their verified head, reproduction commands, positive/negative outcomes and artifact hashes recorded in TASKS.md.
- [x] The same installed package works through CLI and MCP, survives detach/retry/replay, and gates the exact committed combined candidate under protected policy.
- [x] All six required contracts, map accounting and scope boundaries are checked. Full application, kernel and operational-totality claims remain explicitly absent.
- [x] Retired backends and duplicated models/templates/execution code have no active callers or packaged remnants.
- [x] The retained tests, isolated wheel and real Linux CI pass. Local and remote results are recorded separately.

These boxes describe the delivered source profile. Host merge enforcement remains a deployment requirement; the stronger totality profile remains deferred. Keep the current evidence in [TASKS.md](TASKS.md).

## Evidence and harvested mechanisms

| Existing evidence | Established result |
| --- | --- |
| [CI 40237404026](https://github.com/RoyCoding8/vkit/actions/runs/40237404026), `aba3eb9` | Generic ledger: 50 verified functions, eleven rejected controls, compiled execution; 57 seconds. |
| [CI 40258535126](https://github.com/RoyCoding8/vkit/actions/runs/40258535126), `0cb26cf` | Full reserve import contract: 22 checks matched, twelve rejected controls, unchanged expected client and compiled execution; 49 seconds. Source and fixture bytes matched Git and report hashes. |
| Planning baseline, retained Python suite after backend cleanup | 168 passed, four skipped on Windows before product integration. Superseded by the final three-OS suite in TASKS.md. |

Those early proof counts did not establish the later map-fold equivalence, all-six binding, container execution or protected product gate. Final qualification records those separately; earlier evidence is not relabeled.

| Source | Harvest or boundary |
| --- | --- |
| [Anthropic formal verification research](https://www.anthropic.com/research/formal-verification) | Target useful software properties with checked implementation correspondence; VKit's source profile does not inherit the article's assembly-level scope. |
| [Pinned Verus](https://github.com/verus-lang/verus/tree/7d829946231d6413a398f6e4160cdfe951261dad), [trust boundary](https://verus-lang.github.io/verus/guide/tcb.html), [executable termination](https://verus-lang.github.io/verus/guide/exec_termination.html) | Reuse frontend, solver integration, strict checking and import/export; disclose trust and termination limits. |
| [Pinned vstd set laws](https://github.com/verus-lang/verus/blob/7d829946231d6413a398f6e4160cdfe951261dad/source/vstd/set_lib.rs), [sequence folds/permutations](https://github.com/verus-lang/verus/blob/7d829946231d6413a398f6e4160cdfe951261dad/source/vstd/seq_lib.rs), [map laws](https://github.com/verus-lang/verus/blob/7d829946231d6413a398f6e4160cdfe951261dad/source/vstd/map_lib.rs) | Reuse finite-domain enumeration and fold-permutation machinery for the owned accounting theorem. |
| [Docker execution controls](https://docs.docker.com/engine/containers/run/), [GitHub merge queues](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue) | Standard sealed execution and native combined-candidate triggering; neither replaces policy authority or mathematical proof. |
| [Anvil](https://www.usenix.org/conference/osdi24/presentation/sun-xudong), [Perennial](https://pdos.lcs.mit.edu/projects/perennial.html), [Cedar](https://arxiv.org/abs/2407.01688), [MutDafny](https://arxiv.org/abs/2511.15403) | Retain research precedents for domain models, systems proofs, policy verification and specification criticism. They are not additional dependencies in this build. |
| [SymDiff](https://www.microsoft.com/en-us/research/project/symdiff-differential-program-verifier/), [VST](https://vst.cs.princeton.edu/) | Later relational verification and C proof precedents. No simultaneous implementation. |
| [RefinedRust paper](https://plv.mpi-sws.org/refinedrust/paper-refinedrust.pdf), [Iris adequacy](https://plv.mpi-sws.org/coqdoc/iris/iris.program_logic.adequacy.html), [stdpp map laws](https://plv.mpi-sws.org/coqdoc/stdpp/stdpp.fin_maps.html), [Rocq checking](https://rocq-prover.org/doc/v9.1/refman/practical-tools/coq-commands.html) | Historical research and distinct proof semantics. RefinedRust and the Rocq port are retired routes. |
| [Aeneas issue 1116](https://github.com/AeneasVerif/aeneas/issues/1116) | Preserve the reason the earlier Rocq-backend recommendation was withdrawn; no active integration. |

Use the append-only planning decision trail for history. Earlier long-form plan/task snapshots are retained in local audit evidence; they are not competing execution specifications.
