# Formal measurements

These committed receipts describe specific model and regression measurements.
They do not establish correctness of arbitrary projects or provide isolated
verification of unreviewed Lean solutions. See the
[verification reference](../docs/verification.md) for current product behavior.

| Artifact | State | Receipt | Tool and scope |
| --- | --- | --- | --- |
| A. TLC finite model check | VERIFIED | `formal/results/OwnershipAcceptance-receipt.json` | TLC 2.19, the recorded finite ownership model |
| B. Lean theorem check | VERIFIED | `formal/results/Acceptance-lean-receipt.json` | Lean 4.34.1, the declarations in Acceptance.lean |
| C. Python core mutation run | VERIFIED | `formal/results/python-core-mutants-receipt.json` | Two guards in tasks.py and their targeted regression tests |
| D. TLA+ mutation run | BLOCKED | none | run_mutants.py writes no receipt |

## Scope of the receipts

The TLC receipt records two owners, resources, checks, revisions, and generations.
It reports 207,360 distinct states and checks exclusive ownership, generation
validity, required checks, readiness invalidation, and crash behavior. Its result
applies to that model at those bounds. It makes no fairness or liveness claim
and does not verify the Python implementation.

The Lean receipt records compilation of `Acceptance.lean` and its allowed
axioms, `Classical.choice`, `Quot.sound`, and `propext`. The theorem result applies
to that Lean model. It does not prove the Python implementation.

The Python mutation receipt records detection of the stale-generation and
missing-required-check mutants. CI regenerates this receipt for the source
revision it tests. It covers those two mutations, not every possible defect.

`formal/results/OwnershipAcceptance-blocked-receipt.json` records a separate
attempt that could not run TLC. That attempt supplies no model-check result.
It does not replace the successful receipt from a host with the required tools.

`formal/run_mutants.py` is BLOCKED as a source of recorded evidence because it
writes no receipt. Its console output is not a verified mutation result.

## Reproduce the measurements

- `formal/run_tlc.py` runs the finite model when the pinned JRE and TLC jar are available.
- `formal/run_lean.py` checks the Lean model with the required toolchain.
- `formal/run_python_mutants.py` checks the two Python mutations.
- `tests/test_formal_receipts.py` checks receipt paths and recorded source digests.

Use GitHub CI for toolchain execution. The
[formal workflow](https://github.com/RoyCoding8/vkit/actions/workflows/formal-verifiers.yml)
also exercises reviewed project verifier adapters.

Receipts declare their digest normalization. Where specified, source digests
use SHA-256 after folding CRLF to LF. Binary tool digests use exact bytes.
The digest fields bind a receipt to its inputs. Digest agreement alone does not
rerun a checker or establish a new result.
