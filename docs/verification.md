# Verification semantics

VKit separates accepted requirements, candidate source, verification execution and retained evidence. A passing result establishes only the property and scope recorded by its method.

## Components

| Component | Responsibility |
| --- | --- |
| Mathematical package | Own the model, expected contracts, proof sources and verifier pins. |
| Component policy | Bind candidate API names, source entrypoint and parameters to that package. |
| Job | Freeze source and bind accepted policy, package and checker identities. |
| Worker | Check actual tools, execute the required procedure and publish evidence. |
| Receipt | Record obligations, scope, actual tool inventory and retained artifact hashes. |
| Gate | Require intact passing evidence for the intended source and policy authority. |

CLI and MCP share the same job service, store and worker lifecycle. Formal requests select a component, the `implements` goal and a source selector. They cannot supply replacement formulas, checker commands or a smaller obligation set.

## Resource-ledger contract

`vkit.resource-ledger` provides one generic positive-sized `Ledger<N>` model. Its invariant requires positive active amounts, unique reservation IDs, bounded record count, cached usage equal to the map's reservation total, and usage at most capacity. IDs, owners, capacities and amounts are `u32`; batch capacity is three; command arrays have compile-time length `M`.

| Binding | Required behavior |
| --- | --- |
| `new` | Clear supplied preallocated slots and construct a valid empty ledger. |
| `reserve` | Reject zero amount, duplicate ID, full storage or exceeded capacity in that order. Success inserts the exact record; rejection preserves state. |
| `release` | Check existence before ownership. Success removes the ID and returns its amount; rejection preserves state. |
| `reserve_batch` | Check count first, process requests in order and return the first failing index. Commit all requests together or preserve the original state. |
| `apply_command` | Match the model's operation and tagged response. |
| `run_commands` | Relate every state and response in an `M+1` trace to the command model. Empty traces preserve the initial state. |

The verifier freshly checks and exports the owned library, then the candidate, then an unchanged expected client that imports both. The client calls all six bound symbols under the accepted preconditions and requires the owned postconditions. Stronger candidate preconditions or weaker postconditions do not discharge that check. Fixed strict arguments reject unchecked bodies and partial verification.

The package also checks `transfer` against an atomic two-ledger model: success conserves combined usage; either failure preserves both ledgers and identifies the failing operation. `SharedLedger` uses the pinned vstd write lock to preserve valid state and establish each completed reserve/release transition. These are package contracts, separate from candidate API coverage. They do not establish global history linearizability, lock liveness or abort recovery.

The same procedure compiles the checked candidate and library into a retained executable and runs fixed observations through the bound APIs. The `compilation` query section records source, driver and artifact hashes, compiler arguments and empirical results. Compilation trusts Verus erasure, Rust and the linker; it supplies no independent translation-validation theorem.

The result is `ledger.source.v1`, method `verus_source`, assurance `trusted_engine`, coverage `required_api`. Extra exports and surrounding application code are unclaimed. The trust boundary includes Verus, Rust, vstd and SMT solving. This is source verification, not independently checked kernel-certificate evidence or a source-to-binary theorem. Resource aborts remain possible; source loop measures do not establish an operational step bound.

## Component configuration

A version-3 manifest has `checks` and an optional `formal.components` list. Each component follows this shape:

```json
{
  "id": "allocation",
  "package": {
    "id": "vkit.resource-ledger",
    "version": "0.1.0",
    "sha256": "<installed-package-sha256>"
  },
  "contract": "ledger.source.v1",
  "source": {"entry": "src/allocation.rs"},
  "profile": "verus-linux-x86_64-v1",
  "parameters": {"max_records": 3},
  "bindings": {
    "new": "api::new",
    "reserve": "api::reserve",
    "release": "api::release",
    "reserve_batch": "api::reserve_batch",
    "apply_command": "api::apply_command",
    "run_commands": "api::run_commands"
  },
  "required": true
}
```

Bindings are relative Rust identifier paths in the candidate library. The default state type is the owned `Ledger<N>`. An optional `"representation": "columns::Columns"` selects a candidate type with the same ordered `LedgerView`. The expected client checks the owned invariant and all six contracts against that view. The [split-column example](../examples/ledger-refinement/columns.rs) uses verified conversions with O(N) cost. This profile does not translate arbitrary Rust code. See the [schema](../schemas/manifest.v3.json) and [API reuse fixtures](../examples/ledger-reuse/README.md).

Read the installed package digest with:

```sh
uv run python -c "from vkit.formal.ledger import load_package; print(load_package().identity.sha256)"
```

Acceptance binds policy, package and checker digests. Changing any of them requires renewed acceptance. Submission retains the accepted checker digest; the worker hashes actual tool bytes before verification and records their inventory in the receipt. Host paths do not affect a formal job's semantic identity.

The initial profile requires Linux x86_64 and Docker. Host configuration supplies `VKIT_VERUS_IMAGE`, `VKIT_VERUS_ARCHIVE`, `VKIT_VERUS_ROOT`, `VKIT_RUSTUP_HOME` and `VKIT_CARGO_HOME`. The image must be an installed full `sha256:` ID. The [package pins](../src/vkit/formal/packages/resource_ledger/upstream.json) specify official Verus `0.2026.10.18.7d82994`, Rust `1.99.1` and Z3 `4.16.0`. Missing or changed assets block checking. Dependency acquisition and candidate build scripts do not run inside verification.

## Source and execution

| Source | Identity and capture |
| --- | --- |
| Formal working tree | Raw tracked and untracked nonignored bytes. Provisional evidence. |
| Formal Git commit | Immutable raw commit, tree and blob identities, file modes and bytes. Dirty checkout edits cannot change the subject. |
| Registered local check | Selected declared inputs, with CRLF normalized to LF for content identity. |

Formal capture rejects secret-looking paths, links, submodules and unsupported files. Registered local selectors exclude secret-looking paths. Neither policy detects secret content. Declare source, specification, configuration and trusted dependencies; VKit cannot infer arbitrary dependency closures.

Formal attempts run in restricted containers with read-only source/tools, no network, non-root execution and private output. Cancellation and recovery address only the attempt's owned container. Retained evidence rejects links, special files and unlisted artifacts, and has a post-execution limit of 10,000 entries and 512 MiB. This is not an aggregate execution-time disk quota. Local registered adapters retain service-user filesystem permissions.

## Results and evidence

| Dimension | Meaning |
| --- | --- |
| Execution state | `queued`, `running`, `done` or `interrupted`. |
| Outcome | `PASS`, `FAIL` or `BLOCKED`, according to the method. |
| Claim outcome | `established`, `refuted` or `unknown`, within recorded assumptions and scope. |
| Assurance | `empirical`, `finite_model`, `kernel_proof` or `trusted_engine`. |
| Freshness | Whether retained evidence still matches the intended source, policy and checker. |

A rejected Lean proof leaves its theorem unknown. A TLC safety counterexample refutes an invariant only after replay against the frozen model and configuration reproduces both the named violation and the ordered state sequence. Unsupported or unvalidated witnesses leave the claim unknown. A failed Verus obligation is `BLOCKED` with an unknown claim. Tests retain empirical assurance.

Each submission creates a unique run for a content-derived job by default. Opt-in `verify --reuse` (MCP `reuse: true`) requires intact passing local evidence with identical full captured source, accepted policy, package, checker and target authority. It rechecks current tool identity and preserves the original receipt; it does not infer a smaller dependency closure. Retry creates a new run for the same frozen job. Worker loss is interrupted execution, never a pass. A bounded wait can time out while its worker continues.

The [lifecycle model](../verification/lifecycle/Attempts.tla) checks a finite abstraction with at most three attempts, shared job identity and a publication pointer. The [qualification script](../scripts/check_lifecycle.py) separately exercises concurrent attempts, cancellation/completion contention, worker loss, recovery and retry through the production job API. Model checking and runtime observations provide separate evidence; they do not establish trace conformance, every runtime interleaving or implementation refinement.

CLI and MCP run responses default to a summary with identities, status, obligation counts and the next action. CLI `--section` flags and MCP `sections` select `diagnostics`, `obligations`, `provenance`, `coverage`, `compilation`, `artifacts` or `log`. Coverage maps claims to bindings and behaviors, lists separate package claims, and identifies unclaimed application code. Request one, several, or `all`. Selection reads retained results without changing verification scope or starting another run. Logs and artifact lists are bounded and support paging.

Counts distinguish observed results from unobserved obligations. If a partial result lacks the required denominator, `total` and `unknown` are `null`.

Provenance includes `logical_trust` when available. It distinguishes engine-reported axioms, accepted policy assumptions, unchecked dependencies, and unreported information. Lean reports axioms per theorem; TLC records finite-model scope; Verus records its trusted engine dependencies without claiming a complete axiom audit. An external checker is marked checked only after its bound receipt is accepted. Execution limits and performance budgets remain separate from logical trust.

Diagnostics include supported locations in captured source. Lean columns are normalized to one-based positions. TLC retains native JSON witnesses as artifacts and records replay and bounded minimization results. A shorter candidate is retained only after another successful validation; exhausting a budget does not establish minimality. Native TLC replay uses the exact build and digest pinned by the model configuration and CI download.

```sh
vkit run show --run <run-id> --section diagnostics --section provenance --json
```

Receipts bind the job, source, outcome, obligations, checker and artifact digests. Export validates the bundle. Replay independently captures the candidate, requires matching accepted identities and runs checking again. Imported success flags cannot satisfy replay. Hashes provide integrity relative to trusted state; they do not authenticate an external writer or prove verifier soundness.

Formal `evidence export --include-assets` preserves the exact Docker image, official Verus archive and expanded tool trees in a digest-bound capsule. `evidence replay --restore-assets` restores those assets on Linux x86-64 before a fresh check under the currently accepted policy. MCP exposes the corresponding `include_assets` and `restore_assets` booleans. Capsules have separate 16 GiB archive and total limits and a 100,000-entry limit per tool tree; they require the installed VKit runtime and the independently available candidate source. A matching image tag is insufficient.

Package qualification classifies deliberate mutations as detected rejections, survivors or inconclusive runs. Package comparisons report changed assets, obligations and trust identities. They leave package-wide semantic equivalence unknown. The ledger qualification script accepts `--compare-package` with an earlier report or package snapshot.

Ledger adequacy qualification separately checks a concrete reserve precondition inhabitant, the equivalence of remaining-capacity and mathematical total-usage admission predicates, and a strict-bound counterexample at exact capacity. A false equivalence must be rejected. A reconstructed zero-bound control records the original Rocq model fix and source digest; it is not a replay of that proof. These checks cover the recorded predicates and witnesses, not specification completeness.

## Capability feedback

CLI `feedback add` and MCP `feedback_add` record a task, a missing capability, and its expected verification benefit. `kind` defaults to `formal`; `reliable` covers other rigorous checks. An optional run ID links the request to retained evidence.

Each submission creates a Markdown file under the repository's shared Git directory at `vkit/feedback/`. Duplicate submissions preserve the existing file. The tools cannot edit, delete, or consolidate feedback; maintainers do that manually. CLI `feedback list` and MCP `feedback_list` provide bounded previews, with formal verification requests first. Feedback never changes verification results or gates.

## Gates and CI

A local gate is `READY` only when all registered checks and required formal components have intact current passing evidence. A failed empirical check can produce `REJECTED`; missing, stale or inconclusive evidence produces `BLOCKED`. Display filters cannot reduce requirements.

The [protected CI gate](../examples/protected-ci/README.md) loads requirements from the exact target commit and freshly checks the combined candidate. It uses a private store outside candidate execution and imports neither local approvals nor branch receipts. Host workflow, installation, policy governance and merge enforcement must be protected separately.

| Exit code | Meaning |
| --- | --- |
| `0` | PASS or READY. |
| `1` | FAIL or REJECTED. |
| `2` | Invalid request. |
| `3` | BLOCKED. |
| `4` | Internal error. |
| `5` | Unavailable interface. |

## Foundations

The design draws on [Anthropic's formal-verification research](https://www.anthropic.com/research/formal-verification) and [Verus's checking and trust model](https://verus-lang.github.io/verus/guide/tcb.html). VKit reuses the pinned [vstd map](https://github.com/verus-lang/verus/blob/7d829946231d6413a398f6e4160cdfe951261dad/source/vstd/map_lib.rs) and [sequence laws](https://github.com/verus-lang/verus/blob/7d829946231d6413a398f6e4160cdfe951261dad/source/vstd/seq_lib.rs) for ledger accounting. Those sources do not expand the scope of VKit's recorded claim.
