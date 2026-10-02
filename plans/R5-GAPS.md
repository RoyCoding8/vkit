# R5 gap list

Produced by a read-only evidence sweep over the merged R1 to R4 wave, then
verified by hand for the findings marked verified. The sweep ran on 2026-10-01
against a tree that has since moved. **What each finding said is preserved
below with the state I could verify today, so a reviewer can see which claims
were repaired and which were not.** Nothing in this file claims the product is
wrong. Each entry is a gap between what the acceptance matrix advertises and
what has a receipt behind it, which is what R5 exists to reconcile.

State was re-checked against the tree on 2026-10-01. "Fixed" means the specific
claim no longer describes the repository, and names where the fix lives.

## Closed since the sweep

| # | The finding | Now |
| --- | --- | --- |
| B1 | `scripts/acceptance02.py` could not open a task, so `plans/STATUS.md` advertised "14/14" while the harness was red | Fixed. Row 10 runs and exits 0. The harness's own tests still check only that rows are *present*, never that they *execute*, which is why this survived |
| B2 | `formal/run_python_mutants.py` pointed at tests that had moved | Fixed. Both targets name `tests/test_stale_attempt_traces.py` |
| B4 | No `.github/` directory, so the two protected-CI matrix rows had no evidence either way | Fixed. `.github/workflows/ci.yml` runs a three-OS matrix |
| B5 | GAP-3 documents contradicted each other and the gate could not tell | Fixed. `docs/RELEASE-CHECKLIST.md` and `docs/PILOT.md` both updated. The gap the finding names is still open: `tests/test_release_docs.py` verifies a gap's *wording*, not its *status*, and the whole GAP-3 evidence set still sits behind four `pytest.skip` paths, so one missing API key turns 16 tests into 16 skips while the suite reads green |
| W1 | The checklist claimed `444 passed, 2 skipped`, stale by at least 15 | Fixed. That string is gone from the checklist |
| W4 | The checklist pinned `5087624` behind master, and `formal/RESULTS.md` named Lean 4.32.2 while the receipt records 4.34.1 | Fixed. The pin is `5cc14e0` and `formal/RESULTS.md` explains why its Lean row is the newer toolchain |

## Still open

### The TLA+ mutation evidence has no artifact

`formal/results/` holds the Lean receipt, the TLC acceptance receipt, the
blocked receipt, and the Python core mutants receipt. The four TLA+ mutants named
at `formal/RESULTS.md:171-174` have no receipt of any kind.
`formal/run_mutants.py` needs a JRE and `tla2tools.jar` under gitignored
`tmp/formal-tools/`, so on any host without them the evidence is BLOCKED and no
receipt says so. The matrix row "Formal tool is not installed to BLOCKED, not a
silently skipped required proof" is the row that requires this record.

`formal/run_python_mutants.py` does write a receipt now, so the receipt half of
this finding is closed for the Python mutants. The TLA+ half is not.

### No test asserts the migration re-issued `runs_by_task`

This is the finding the R2 design recorded as the cost of its table rebuild, and
it is still uncosted. `DROP TABLE runs` takes the index with it. The rebuild
succeeds, the version is recorded, every row survives, and the query plan
silently changes from `SEARCH runs USING INDEX runs_by_task (task_id=?)` to
`SCAN runs`. `recover._live_holder` reads `runs` by `task_id` on every claim
release, so the query that most needs to be current is the one that becomes a
full scan. `storage.py` re-issues the index, so the defect is fixed. **No test
asserts the query plan.** A test asserting only that migration 5 ran would pass
with the index gone.

### The POSIX triage is still uncommitted scratch

`tmp/posix-triage.md` is still the only record of the POSIX triage and
`tmp/*` is gitignored. It holds five genuine defects, including an uncaught
`UnsupportedPlatform` in the console cancel path. Commit it or lose it. This
finding has now been open across two sweeps.

### Identity invalidation has no working evidence

`tests/test_formal_correspondence.py` is genuine, with a real `Store`, real
SQLite and real `claims`. But `Core.decide` calls `compute_readiness` with no
`AcceptanceContext`, so `expected` is None and `_identity_gaps` never runs. The
one test that could cover identity invalidation does not, and
`record_readiness` is imported nowhere in `formal/` or `tests/` and has no test
naming it, though `src/vkit/tasks.py:867` still defines it and
`formal/run_python_mutants.py` mutates it. Two mutants are therefore aimed at a
path nothing exercises.

### Network path refusal is implemented and unmeasured

`_reject_network_path` refuses UNC and network drives. The matrix row "network
filesystem refuses" is implemented and had no test when the sweep ran.
`tests/test_network_path_refusal.py` now exists, so the sweep's claim is stale,
but nothing in the release documents records a receipt for the row itself.

### Counts and revisions drift, and the pin cannot catch it

`tests/test_release_docs.py` checks that a pinned revision is an *ancestor* of
HEAD. It cannot detect that verdicts were measured elsewhere, so a pin sitting
far behind master passes. `formal/run_tlc.py` hashes `path.read_bytes()`, so a
receipt produced on this CRLF checkout records a hash an LF reviewer cannot
reproduce; the receipts verify only after CRLF-to-LF normalization. That
normalization is real work with no test behind it.

## What is in good shape, and should be said plainly

The formal receipts are correctly scoped and cryptographically intact. Neither
advertises a TLA+ or Lean proof as Python correctness, and that scoping is in
the machine-readable receipts, not only in the prose, verified by hash under CRLF
normalization against `Acceptance.lean`, `OwnershipAcceptance.tla` and
`OwnershipAcceptance.cfg`.

The two 100-client research probes are honestly labelled synthetic in their own
receipts ("No AI agents, hooks, or kit implementation tested"). The pilot has not
started, and `docs/PILOT.md` already says so, in the line "A condition with no
receipt is not met."

The problem is concentrated in two places: documents whose counts and verdicts
drifted from a codebase that moved under them, and artifacts that exist only as
uncommitted scratch under a gitignored `tmp/`. The drift half is mostly repaired.
The scratch half is not.

## The pattern under all of it

A second sweep, looking for slop rather than for missing evidence, found the same
failure in five places: something asserts a guarantee that no longer has a check
behind it.

| Claim | Location | Reality |
| --- | --- | --- |
| the hook's gate is the core's readiness | `vkit_hook.py:366` | called `compute_readiness` with no context, so no identity was ever compared. **Fixed.** It passes `context=_acceptance_context(tasks_mod, project)` at `:403` |
| the model is *known to diverge* from the core, and a test records it | `formal/reference.py:26-32` | R1 deleted the divergence; the named test `real_acceptance_allows_any_attempt` appears exactly once in the whole repository, in this sentence |
| the console's readiness is what `vkit doctor` reports | `operations.py` `readiness_view` | **Fixed.** `probe_state` at `:160` replaced a hardcoded `state_writable: True` |
| an AST test pins this module's guarantee | `api.py:17-20` | **Fixed.** `tests/test_console.py:316` parses `api.py` |
| four operations refuse with 501 | `api.py:76` | **Fixed.** every `WRITABLE` entry is implemented, and `api.py:79-80` now says so at the point a name would reach the 501 path |

Only the second remains true, and it has the shape the first had. A claim whose
only evidence is the sentence making it.

## A warning about probes built from documents

Twice this wave, a probe was written against what a design document *described*
instead of what the code holds, and both times it produced a confident false
claim.

`review/probe_migrate_rebuild.py` invented a `claim_members` table with a
foreign key to `runs`, because the R2 design named such referents. It "proved"
that `_migrate` needed a foreign-keys-off flag. The real schema has zero
`REFERENCES` clauses, `PRAGMA foreign_key_list(runs)` is `[]`, and the rebuild
succeeds with foreign keys on and the pragma untouched.
`review/probe_lifecycle.py` reported a schema shape taken from the same
document rather than from `storage.py`.

A probe inherits the mistakes of its source. Before probing behaviour that
depends on the shape of the code, read the shape out of the code: print the DDL,
query `sqlite_master`, count the clauses. In both cases the assertion that
actually bit was the part copied from the document, and the half that would have
caught the mistake was the part that got skipped.

Retracted in `a267166`; the corrected probe prints what the schema declares
before it touches anything.

## Row 10's `pinned == "policy-v1"` is load-bearing, do not simplify it away

A worker reported this conjunct as tautological, on the grounds that it just
asserts `open_task` stored the string it was handed. It is not, and the claim
was checked rather than argued:

```
negating  changed["pinned"] == "policy-v1"  ->  0/1 rows pass, row 10 FAILS
```

`pinned` is not the literal that was passed in. At `acceptance02.py:1332`,
inside the child process that rewrites the manifest,
`pinned=tasks.get_task(store, "t10").policy_digest` reads it back. The conjunct
asserts that the task's pinned policy digest survived a manifest change made by
a separate process, which is the row's actual claim. Deleting it would have
removed the only assertion about the *pinned digest* specifically, and the row
would still pass while testing less.

It looked tautological in isolation because the indirection is three lines above
the use and one screen away.
