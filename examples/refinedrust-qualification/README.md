# RefinedRust qualification

This fixture tests the proposed Rust-to-Rocq path before VKit integrates it.
It is not an accepted VKit formal package or a completed qualification result.

`reserve` checks a scalar capacity update. Its specification requires the exact
result for zero, insufficient-capacity and successful requests. The input
precondition is `used ≤ capacity`; the Rust integer types supply their bounds.
This fixture does not implement the reservation ledger in `PLAN.md`.

`upstream.json` fixes the upstream revision and dependency-lock digest.
`sources.nix` reads the required GitLab library revisions, hashes and store paths
from that pinned dependency graph. The runner captures their trees through Git
and requires the original NAR hashes and store paths. This avoids the archive
endpoint's HTTP 429 failure without changing the accepted library content.
`RefinedRust.toml` rejects assumed functions, proof admission and skipped
unsupported features. Those switches do not replace a transitive assumption
audit or independent proof checking.

The runner records separate body, contract and replay stages. The body stage
invokes Dune explicitly and requires the compiled body proof. Upstream's
`run_check` callback only logs Dune failure, so this probe disables it.

`audit/Contract.v` states VKit's required scalar result and the permitted
panic-shim and layout premises. It compiles before a fresh Rocq process loads
the producer proof. `audit/Expected.v` applies that proof to the compiled
contract using qualified references. Producer names cannot replace the contract
during that application. The replay stage independently checks the compiled
module and its dependencies with `rocq check`, without dependency-skip flags.
`Print Assumptions` writes a dedicated report with qualified names. The runner
rejects missing, malformed or changed baseline assumptions. The contract also
typechecks the two identified standard-library constants against explicit type
witnesses. This qualification boundary does not accept a product assumption
policy. Displayed type hashes identify report text, not serialized kernel terms.

`frontend.patch` adds one compiler-owned census to the pinned frontend.
The Nix override applies it to the original source; the report retains the patch
digest with the other fixture inputs. It records active-configuration callable
HIR items before verification selection, their translation outcomes and compiler
module paths. The fixture includes two unannotated functions and a public alias
to check that the census exposes code omitted from body proofs. This is a
coverage diagnostic, not acceptance of those unproved functions.

The same patch corrects fixed-array refinement types to match `array_t`'s stored
`list (place_rfn rt)` representation. The collection probe keeps an abstract
`list Z` model and maps it explicitly into that representation. Without the
frontend correction, generated struct pretypes fail before field invariants
can be checked. The corrected collection proof compiled in CI `40151475498`.
The runner also audits that theorem's assumptions and recursively checks its
dependency closure. This support probe audits the fixed qualification fixture;
it does not bind the collection to a separately owned expected contract or prove
a bound on execution.

After a successful baseline, the same runner executes fixed negative probes in
fresh source copies. They change the body, weaken the producer contract, shadow
the verification judgment, admit a proof or request an assumed-function mode.
Each invocation has a ten-minute limit including Nix setup. Timeouts and
unexpected failures cannot count as rejection. Matchers for the wrong body and
assumed modes are based on observed CI diagnostics. An admitted proof must expose
the unapproved `reserve_proof` axiom. The shadowing probe also requires a
deliberately vulnerable driver to compile the same mutant that the current
driver must reject. These experiments do not accept a product assumption policy
or complete Q0.

Run the Linux probe with Nix installed:

```sh
python3 scripts/qualify_refinedrust.py --upstream /path/to/pinned-checkout --output /path/to/new-run
```

For pure ledger proof iteration, add `--model-only`. This reuses the pinned
Rocq/stdpp environment, source hashes, assumption audit and independent checker,
while skipping Rust translation and capability probes. The first invocation
builds dependencies; subsequent invocations reuse the local Nix store. Each run
requires a fresh output directory. The report remains `INCONCLUSIVE` for product
qualification even when its model stage completes. CI offers the same mode
through the workflow's `model_only` input.

The `RefinedRust qualification` GitHub Actions workflow runs the same command
and retains its report, logs and generated sources as an artifact. The probe
reports `INCONCLUSIVE` and exits with code 3 until all Q0 gates are implemented
and checked, even if every recorded stage completes. Per-stage elapsed time includes
the entire recorded command, including any environment setup.

The workflow reuses `nix-community/cache-nix-action` to retain build dependencies
after failed probes. Cache keys include the upstream pin and run ID. The cache
contains pinned source dependencies and built libraries. The fresh scalar fixture
copy and its generated per-run proofs are outside it and are checked on every run.

Q0 also requires trusted statement binding, invalid fixtures, implementation
coverage and a compatible independent checker. Q1 requires the full ledger
proofs, including termination. Neither gate is satisfied by this source file
or a successful translation.

The runner captures `formal/packages/resource_ledger/Model.v` with its inputs,
compiles the provisional model and independently checks its initialization and
fold-accounting lemmas. Its audit remains `PROVISIONAL_MODEL_ONLY`. Operation
preservation, concrete body refinement and resource-aware bounded execution are
separate obligations.
