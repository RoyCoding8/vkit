# R5 gap list, produced 2026-10-01 against merged master

Produced by a read-only evidence sweep, then verified by hand for the four
findings marked **verified**. Everything here is a gap between what
`tmp/research/KIT_ACCEPTANCE.md` advertises and what has a receipt behind it.
Nothing in this file is a claim that the product is wrong; it is a claim that
the *record* is wrong or absent, which is what R5 exists to reconcile.

## Blocks a release claim

### B1 — `scripts/acceptance02.py` cannot open a task (VERIFIED, my regression)

Seven rows call `tasks.open_task` with a contract R1's `TaskContract.from_json`
correctly refuses (no `repository`, no string `policy_digest`/`scope`, no
`required_checks`). Rows 5, 8, 9, 10, 11, 13, 14 — lines 773, 1040, 1208, 1287,
1384, 1492, 1621.

```
$ python scripts/acceptance02.py --only 10
0/1 acceptance rows pass (0 skipped, 1 failed, 0 never ran)
[ FAIL ] row 10: Changed contract or policy
         raised AdmissionRefused: this task recorded a malformed policy_digest
```

This is the regression I introduced by merging R1, and it is the worst kind:
`plans/STATUS.md:12` advertises "14/14 acceptance rows", so the milestone
record claims green while the harness is red. Row 10 is the row that
demonstrates *changed policy invalidates old evidence* — the guarantee R1 was
written to strengthen — and it is the row that cannot run.

`tests/test_acceptance02.py` does not catch it: the harness's own tests check
that rows are *present*, never that they *execute*. The docstring claims "every
row carries the plan's own words, so a table row cannot be quietly dropped" —
true, and beside the point.

Fix dispatched. Re-verify the full harness count before R5 starts.

### B2 — `formal/run_python_mutants.py` points at tests that moved (VERIFIED)

```
run_python_mutants.py:68  test="test_formal_correspondence.py::test_a_superseded_attempt_cannot_publish_ready"
run_python_mutants.py:80  test="test_formal_correspondence.py::test_one_missing_required_check_is_never_ready"
```

Both targets now live in `tests/test_stale_attempt_traces.py` (lines 71, 107);
neither exists in `test_formal_correspondence.py`. The mutation *texts* also
drifted: `MUTANT_STALE_GENERATION` removes a literal string that `tasks.py:860`
no longer contains (R1 rewrote it to `decided_at = ...` then compares).

The harness fails loudly on a text mismatch rather than falsely passing, which is
good — but `formal/RESULTS.md:122-133` still presents its old results as
current. Either re-point and re-run, or record BLOCKED with the refactor as the
reason. Leaving the receipt standing is the one option that is not available.

### B3 — the TLA+ and Lean mutation evidence has no artifact at all

`formal/results/` holds only the Lean and TLC acceptance receipts. The four TLA+
mutants (`RESULTS.md:73-80`) and the two Python mutants have **no receipt**. Both
harnesses need a JRE and `tla2tools.jar` under gitignored `tmp/formal-tools/`, so
on any host without them the evidence is BLOCKED — and no receipt says so either
way. The matrix row "Formal tool is not installed → BLOCKED, not a silently
skipped required proof" is the row that requires this record.

### B4 — hosted protected CI has zero evidence (VERIFIED)

No `.github/` directory exists; the only CI file is `examples/ci/integration.yml`.
`docs/INTEGRATION-CI.md` is honest ("Neither has been executed on a forge") but
prose is not a receipt. The two matrix rows — "protected integration remains
effective" and "protected CI executes checks again and does not trust the edited
local report" — have neither a pass nor a recorded BLOCKED.

### B5 — GAP-3 documents contradict each other, and the gate cannot tell

`docs/HOST-SESSION.md:18-19` records a real executed session (Claude Code
2.1.285, 16 tests, 57s). `docs/RELEASE-CHECKLIST.md:93` still reads "No live
Claude Code host session has exercised the plugin", as does `docs/PILOT.md:20`.

`tests/test_release_docs.py:89` pins `("GAP-3", ("validate", "host session"))`.
Those two tokens appear in *both* the stale row and a closed receipt, so the gate
passes either way — it verifies a gap's *wording*, not its *status*. That is the
same defect shape as R1's `unverified_identities`: the record green, the claim
false.

Worse, the whole GAP-3 evidence set sits behind four `pytest.skip` paths
(`claude` not on PATH, vkit not installed, no hook payload, no model
credentials). One missing API key turns 16 tests into 16 skips while the suite
still reads green.

## Weakens, does not block

### W1 — 16 platform skips, 13 guarding advertised guarantees

`tests/test_procidentity_posix.py` (10 skipped) guards "diagnose the owner when
a process exits holding a lock" and "many contenders claim one resource".
`tests/test_procs_posix_real.py` (5 skipped) guards the **only** test of
three-level descendant termination. `tests/test_procidentity.py:440` guards
"missing prerequisite → BLOCKED".

`docs/RELEASE-CHECKLIST.md:148` claims `444 passed, 2 skipped` — stale by ≥15.

`tests/test_procs_posix.py` is worse than skipped: its two tests are gated on
`sys.platform != "win32"` but *pass here* via a `taskkill` stub, and
`tmp/posix-triage.md` records them as broken on real POSIX. The gate is inverted
— a latent false pass, not an honest skip.

### W2 — the correspondence test covers only the un-identity-checked half

`tests/test_formal_correspondence.py` is genuine (real `Store`, real SQLite,
real `claims`), but `Core.decide` calls `compute_readiness` with **no
`AcceptanceContext`**, so `expected` is None and `_identity_gaps` never runs. The
one test that could cover identity invalidation doesn't — and B1's row 10, which
does, is red. `record_readiness` is imported and never called.

The two gaps compound into one: **identity invalidation currently has no working
evidence at all.**

### W3 — implemented, unevidenced, and silently so

- `_reject_network_path` (`storage.py:209-217`) refuses UNC and network drives.
  Zero tests, zero probes. Matrix row "network filesystem → refuse" is
  implemented and unmeasured.
- Matrix rows with no artifact of any kind: "agent teams enabled", "secret in
  command output is redacted", "disk full / interrupted report write" — the last
  conceded in `scripts/acceptance02.py:1539`.
- GAP-5's own shape (`plans/STATUS.md:15`): the console layout is unpinned, so a
  regression there is silent.

### W4 — counts and revisions have drifted from the code

`docs/RELEASE-CHECKLIST.md` pins revision `5087624`; master is well past it, and
`tests/test_release_docs.py` only checks the pin is an *ancestor*, so it cannot
detect that verdicts were measured elsewhere. `formal/RESULTS.md:17` names Lean
4.32.2 while the receipt records 4.34.1 — the receipt is the newer real run and
the prose is stale.

Also: `formal/run_tlc.py:80` hashes `path.read_bytes()`, so a receipt produced on
this CRLF checkout records a hash an LF reviewer cannot reproduce. The receipts
verify only after CRLF→LF normalization.

### W5 — uncommitted artifacts that R5 needs

`tmp/posix-triage.md` is the only record of the POSIX triage and `tmp/*` is
gitignored. It holds five genuine defects, including an uncaught
`UnsupportedPlatform` in the console cancel path. Commit it or lose it.

## What is in good shape, and should be said plainly

The formal receipts are correctly scoped and cryptographically intact. Neither
advertises a TLA+/Lean proof as Python correctness, and that scoping is in the
machine-readable receipts, not just the prose — verified by hash under CRLF
normalization against `Acceptance.lean`, `OwnershipAcceptance.tla` and
`OwnershipAcceptance.cfg`.

The two 100-client research probes are honestly labelled synthetic in their own
receipts ("No AI agents, hooks, or kit implementation tested"). The pilot itself
has not started, and `docs/PILOT.md:14` already says so: "A condition with no
receipt is not met."

The problem is concentrated in two places: documents whose counts and verdicts
drifted from a codebase that moved under them, and artifacts that exist only as
uncommitted scratch under a gitignored `tmp/`.
## The pattern under all of it

A second sweep over the merged R1–R4 wave, looking for slop rather than for
missing evidence, found the same failure in five places: **something asserts a
guarantee that no longer has a check behind it.**

| Claim | Location | Reality |
|---|---|---|
| the hook's gate is the core's readiness | `vkit_hook.py:366` | calls `compute_readiness` with no context, so no identity is ever compared — audit finding F15 restored on the adapter R1 never migrated |
| the model is *known to diverge* from the core, and a test records it | `formal/reference.py:26-32` | R1 deleted the divergence; the named test `real_acceptance_allows_any_attempt` appears exactly once in the repo — in the sentence claiming it exists |
| the console's readiness is what `vkit doctor` reports | `operations.py` `readiness_view` | doctor probes the state store and folds it into `ok`; the console hardcodes `"state_writable": True`, so it reports ready when the store cannot be opened |
| an AST test pins this module's guarantee | `api.py:17-20` | no test parses `api.py`; the AST tests parse operations.py, plan.py, server.py |
| four operations refuse with 501 | `api.py:76` | there are zero; every `WRITABLE` entry is implemented, and `raise NotImplementedInBuild` is unreachable |

The first is a live defect. The rest are records that assert something untrue —
and the repair wave wrote the *narrative* of correctness more reliably than it
verified it. Note the shape of the hook one specifically: it resolves a full
`Project` through `_open_store`, binds it to `_paths`, and discards it, then
calls the one entry point that skips the comparison. The context was one line
away the whole time.

What was checked and came back clean, so it is not re-litigated:

- `_decide` is genuinely the single decision rule; `admit`/`_floor` the single
  floor derivation. No adapter reimplements either. `cli._manifest` and the
  hook's `REQUIRED_CHECKS_KEY` are gone with no alias and no fallback.
- `claims.acquire_in` is a justified split (a two-line delegation plus one site
  for the arbitration SQL), not a shim. R3's `_verifier_identity` /
  `_environment_facts` / `_fixture_identity` were deleted in favour of
  `oracle.py` rather than duplicated. `_settle` replaced `_persist` rather than
  wrapping it.
- `Server._manifest` vs `_registered_policy` is a deliberate split — inspection
  reports, admission decides — not a mirrored authority.
- Every `compute_readiness` caller other than the hook is a bare `Store()` over
  a tempfile with no repository, which is the case that entry point exists for.

## A warning about probes built from documents

Twice this wave, a probe was written against what a design document *described*
instead of what the code holds, and both times it produced a confident false
claim:

1. `review/probe_migrate_rebuild.py` invented a `claim_members` table with a
   foreign key to `runs`, because the R2 design named such referents. It
   "proved" that `_migrate` needed a foreign-keys-off flag. The real schema has
   **zero** `REFERENCES` clauses — `PRAGMA foreign_key_list(runs)` is `[]`, and
   the rebuild succeeds with foreign keys on and the pragma untouched.
2. `review/probe_lifecycle.py` reported a schema shape taken from the same
   document rather than from `storage.py`.

A probe inherits the mistakes of its source. Before probing behaviour that
depends on the shape of the code, read the shape out of the code: print the DDL,
query `sqlite_master`, count the clauses. In both cases the failing half of the
probe — the assertion that actually bit — was the part copied from the document,
and the half that would have caught the mistake was the part I skipped.

Retracted in `a267166`; the corrected probe prints what the schema declares
before it touches anything.

## The migration index loss, found while retracting it

Re-verifying the retraction surfaced a real defect nobody had named: `DROP TABLE
runs` takes `runs_by_task` with it. The rebuild "succeeds", the version is
recorded, every row survives — and the query plan changes from
`SEARCH runs USING INDEX runs_by_task (task_id=?)` to `SCAN runs`. Nothing
fails. `_live_holder` reads `runs` by `task_id` on every claim release, which is
the F13 path, so the one query that most needs to be current is the one that
silently becomes a full scan.

Migration 5 must re-issue the index after the rename, and the gate must assert
the query plan rather than merely that the migration applied — a test asserting
only "migration 5 ran" passes with the index gone.
