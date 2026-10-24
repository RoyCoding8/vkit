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

`audit/Expected.v` states VKit's required scalar result and the permitted
panic-shim and layout premises. Applying the generated proof to that explicit
statement must typecheck. The replay stage independently checks the compiled
module and its dependencies with `rocq check`, without dependency-skip flags.
`Print Assumptions` and the checker's context summary are retained for review.
Printing assumptions does not enforce an allowed-assumption policy.

`frontend-inventory.patch` adds one compiler-owned census to the pinned frontend.
The Nix override applies it to the original source; the report retains the patch
digest with the other fixture inputs. It records active-configuration callable
HIR items before verification selection, their translation outcomes and compiler
module paths. The fixture includes two unannotated functions and a public alias
to check that the census exposes code omitted from body proofs. This is a
coverage diagnostic, not acceptance of those unproved functions.

Run the Linux probe with Nix installed:

```sh
python3 scripts/qualify_refinedrust.py --upstream /path/to/pinned-checkout --output /path/to/new-run
```

The `RefinedRust qualification` GitHub Actions workflow runs the same command
and retains its report, logs and generated sources as an artifact. The probe
reports `INCONCLUSIVE` and exits with code 3 until all Q0 gates are implemented
and checked, even if all three stages complete. Per-stage elapsed time includes
the entire recorded command, including any environment setup.

The workflow reuses `nix-community/cache-nix-action` to retain build dependencies
after failed probes. Cache keys include the upstream pin and run ID. The cache
contains pinned source dependencies and built libraries. The fresh scalar fixture
copy and its generated per-run proofs are outside it and are checked on every run.

Q0 also requires trusted statement binding, invalid fixtures, implementation
coverage and a compatible independent checker. Q1 requires the full ledger
proofs, including termination. Neither gate is satisfied by this source file
or a successful translation.
