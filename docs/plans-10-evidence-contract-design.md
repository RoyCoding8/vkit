# Plan 10 checkpoint 1: the frozen evidence contract

Status: design note. Nothing in this document is implemented. Every claim about
current behavior carries a `file:line`. Every claim about new behavior is marked
PROPOSAL. A reader must be able to tell the two apart without cross-checking.

Baseline `4dabb87`. Branch `wt/10-1-design`. Design only; no production code
was written.

## What exists today, and the gap

The core already has a sum type for verdicts, a versioned boundary-schema
loader, an immutable parsed manifest, a single acceptance decision, and a
durable run record. What it does not have is anywhere to put an evidence
category.

Four findings, each checked:

1. `claimkind.py` defines four categories (`SCENARIO` at
   `src/vkit/claimkind.py:62`, `PROPERTY` at :63, `FINITE_MODEL` at :64,
   `THEOREM` at :65`) and nothing in `src/` imports them. Verified by grep
   across the whole worktree for `claimkind`, `from vkit.claimkind import`, and
   `import claimkind`: the only hit outside `src/vkit/claimkind.py` itself is
   `tests/test_claimkind.py:28`. The production runner never sees a category.

2. No category is stored, derived, or compared. Grep for `category` across
   `src/vkit/storage.py`, `src/vkit/tasks.py`, and `src/vkit/execution.py`
   returns nothing. The `runs` table (`src/vkit/storage.py:189`) has `result`
   and `reason` and no category column. `_decide` at `src/vkit/tasks.py:843`
   branches on `result` alone.

3. The evidence a check reports is a list of scenario strings.
   `execution._scenarios_from_artifact` at `src/vkit/execution.py:72` validates
   `check-artifact.v1.json`, reads `document["scenarios"]` at :91, and maps
   `item["id"]` to `ScenarioResult` at :98. A check that reports a Lean theorem
   as a scenario id is accepted today. Nothing distinguishes the two.

4. `integration/policy.py` already carries the shape the plan needs, and it is
   stricter than the manifest in a way that is easy to miss.
   `CHECK_KEYS = frozenset({"id", "required_scenarios"})` at
   `src/vkit/integration/policy.py:59` refuses any other key on a required check
   at :152-157. So the approved policy can require a check id and a list of
   scenario strings and nothing else. Adding a category to the policy means
   changing `CHECK_KEYS`, which means every policy document in every example and
   fixture changes too.

Point 4 is the single most consequential constraint on this design, and the plan
text does not mention it. It is why section 3 has to argue against itself.

---

## 1. The discriminated union

### 1.1 The rule

Every native check carries a `kind` and exactly the fields that kind has. A
`lean` variant has no `required_scenarios` field to be empty, because there is
no such field on it. A `scenario` variant has no `theorems` field, so it cannot
name a theorem. This is the same move `outcome.py:1-15` already justifies in
prose for the verdict, applied to the declaration instead.

PROPOSAL: `src/vkit/verifiers/spec.py` owns `CheckKind` and the variant
dataclasses. It is a new module because `manifest.py` already owns "what is safe
to execute" and this adds no new safety rule; it changes what a check *is*.

```python
# PROPOSAL: src/vkit/verifiers/spec.py
class CheckKind(enum.StrEnum):
    SCENARIO  = "scenario"     # an approved driver writes v1 check-artifact bytes
    PYTEST    = "pytest"       # vkit launches the pinned pytest runner
    NODE_TEST = "node_test"    # vkit launches the pinned node test runner
    PROPERTY  = "property"     # pytest, with a hypothesis settings block pinned
    LEAN      = "lean"         # vkit launches the pinned lean toolchain
    TLC       = "tlc"          # vkit launches the pinned JRE and jar
```

Each variant is a frozen dataclass with a `kind` class attribute and no optional
fields whose absence means anything:

| Variant | Distinguishing fields | Valid means |
| --- | --- | --- |
| `scenario` | `required_scenarios: tuple[str, ...]` (non-empty), `artifact_name` | An approved driver process wrote `check-artifact.v1.json` bytes and vkit's parser accepted them. |
| `pytest` | `required_tests: tuple[str, ...]` (node ids, non-empty), `runner: PinnedRunner`, `report_format`, `expect_report_version: int` | vkit built the argv, ran the pinned runner, and parsed a structured report it alone produced. |
| `node_test` | `required_tests: tuple[str, ...]`, `runner: PinnedRunner`, `report_format`, `expect_report_version: int` | Same, for the Node built-in runner. |
| `property` | `required_tests: tuple[str, ...]`, `runner: PinnedRunner`, `report_format`, `generator: HypothesisSettings`, `replay: ReplaySettings` | A pytest run whose required tests are Hypothesis tests, with the generator settings vkit pinned. |
| `lean` | `challenge: ChallengeRef`, `theorems: tuple[str, ...]` (non-empty), `profile: LeanProfile`, `permitted_axioms: tuple[str, ...]`, `toolchain: ToolchainRef` | A kernel or comparator run over a pinned challenge, with named theorems and an audited axiom set. |
| `tlc` | `model: TlaModuleRef`, `config: TlaConfigRef`, `properties: tuple[str, ...]` (non-empty), `bounds: DomainBounds`, `fingerprint: FingerprintSpec`, `toolchain: ToolchainRef` | Exhaustive exploration of one finite model at one recorded configuration. |

What every variant shares, and therefore does not re-declare: `id`,
`description`, `cwd`, `timeout_seconds`, `prerequisites`, `inputs`,
`expectations`. Those are `src/vkit/manifest.py:80-89` today and they do not vary
by kind. Keeping them out of the variants is the laziness call: six copies of
the timeout field is six places to forget one.

PROPOSAL: `src/vkit/verifiers/spec.py` splits the one `CheckSpec` dataclass
(`src/vkit/manifest.py:72`) into a `NativeCheckSpec` carrying the shared eight
fields plus `subject: SubjectRef` and `claim_id`, and the six variants above.
`CheckSpec` becomes a union member too, so `Manifest.checks` is a dict of
`NativeCheckSpec` and the existing type keeps working for every current caller.

### 1.2 What each variant must not be allowed to say

This is the part that makes the union worth having, so it is stated per variant
and enforced by construction.

`scenario` must not name a theorem. It has no field for one, and the only thing
it can carry is a scenario id the driver's own artifact reports. It must not
carry a category other than `scenario`. That is what makes "a legacy scenario
driver that claims `theorem_checking`" structurally impossible rather than a
checked condition.

`pytest` and `node_test` must not carry a `generator` block. Without one, a
`pytest` check cannot be read as property evidence no matter what the runner
printed, because the block that records the generator settings is the thing
`claimkind.py:17-22` says a PROPERTY result licenses.

`property` must not be constructible without `generator`. A property result
whose generator settings were not pinned by policy says nothing about what was
sampled, which is the whole difference between PROPERTY and SCENARIO.

`lean` must not carry `required_scenarios`, and must not be constructible
without `theorems` and `profile`. It must not name an axiom outside
`permitted_axioms`, which is the set `formal/run_lean.py:53` already uses
(`ALLOWED_AXIOMS`).

`tlc` must not be constructible without `bounds` and `fingerprint`. A finite
model result at an unrecorded configuration is the specific overstatement
`claimkind.py:26-31` refuses and `formal/run_tlc.py:130-157` was written to
catch.

### 1.3 One JSON example of each

These are manifest entries under `schema_version: 2`. The `kind` key is the
discriminator.

```json
{
  "schema_version": 2,
  "description": "A small real CLI that totals integer item amounts, verified by running it.",
  "checks": [
    {
      "id": "totals-behavior",
      "kind": "scenario",
      "description": "Runs the real totals CLI and compares actual output with literal expected totals.",
      "command": ["python", "verify_totals.py", "--app", "src/totals.py", "--out", "{{run_dir}}/result.json"],
      "cwd": ".",
      "timeout_seconds": 120,
      "artifact": "result.json",
      "prerequisites": [{"name": "python", "executable": "python", "args": ["--version"]}],
      "inputs": ["src/totals.py", "verify_totals.py"],
      "expectations": [],
      "subject": {"paths": ["src/totals.py", "verify_totals.py"], "digest": null},
      "claim_id": "totals-sums-item-amounts",
      "required_scenarios": ["empty-cart", "single-positive", "mixed-sign"]
    }
  ]
}
```

`subject.digest` is null in the manifest because the owner has not declared an
expected digest yet; vkit measures it and the receipt carries the measurement.
Recording null is the honest direction and matches `fixture_digest` at
`schemas/run-report.v1.json:58`.

```json
{
  "id": "storage-regression",
  "kind": "pytest",
  "cwd": ".",
  "timeout_seconds": 600,
  "artifact": "pytest-report.json",
  "prerequisites": [{"name": "python", "executable": "python", "args": ["--version"]}],
  "inputs": ["pyproject.toml"],
  "expectations": [],
  "subject": {"paths": ["src/totals.py"], "digest": null},
  "claim_id": "store-survives-restart",
  "required_tests": [
    "tests/test_storage.py::test_publish_is_atomic",
    "tests/test_storage.py::test_republish_is_refused"
  ],
  "runner": {"executable": "{{python}}", "base_argv": ["-m", "pytest", "-p", "no:cacheprovider", "--json-report"]},
  "report_format": "pytest_json_report",
  "expect_report_version": 1
}
```

The `{{python}}` placeholder is the one already substituted at
`src/vkit/manifest.py:104`. The runner is named as an argument array, never a
shell string, per `plans/CONTRACT.md:56`.

```json
{
  "id": "node-split-bill-cases",
  "kind": "node_test",
  "cwd": ".",
  "timeout_seconds": 300,
  "artifact": "node-report.json",
  "prerequisites": [{"name": "node", "executable": "node", "args": ["--version"]}],
  "inputs": ["src/split-bill.js"],
  "expectations": [],
  "subject": {"paths": ["src/split-bill.js"], "digest": null},
  "claim_id": "split-bill-partitions-the-total",
  "required_tests": ["test/split-bill.test.js:leftover cent goes to the first person"],
  "runner": {"executable": "node", "base_argv": ["--test", "--test-reporter=tap"]},
  "report_format": "node_tap",
  "expect_report_version": 1
}
```

```json
{
  "id": "totals-correspondence",
  "kind": "property",
  "cwd": ".",
  "timeout_seconds": 1800,
  "artifact": "pytest-report.json",
  "prerequisites": [{"name": "python", "executable": "python", "args": ["--version"]}],
  "inputs": ["pyproject.toml", "formal/reference.py"],
  "expectations": [],
  "subject": {"paths": ["src/totals.py", "formal/reference.py"], "digest": null},
  "claim_id": "core-and-reference-model-answer-the-same-questions",
  "required_tests": ["tests/test_formal_correspondence.py::test_core_matches_reference"],
  "runner": {"executable": "{{python}}", "base_argv": ["-m", "pytest", "-p", "no:cacheprovider", "--json-report"]},
  "report_format": "pytest_json_report",
  "expect_report_version": 1,
  "generator": {"max_examples": 200, "stateful_step_count": 25, "deadline": null, "suppress_health_check": ["too_slow"]},
  "replay": {"database": ".hypothesis", "seed": null}
}
```

The `generator` block mirrors the real `settings(...)` call at
`tests/test_formal_correspondence.py:92-97`. It is in the manifest rather than
inferred from the package being installed, which is the hard question in section
7.

```json
{
  "id": "ownership-acceptance-theorems",
  "kind": "lean",
  "cwd": ".",
  "timeout_seconds": 3600,
  "artifact": "lean-receipt.json",
  "prerequisites": [{"name": "lean", "executable": "lean", "args": ["--version"]}],
  "inputs": ["formal/lean/Acceptance.lean", "lakefile.lean", "lean-toolchain"],
  "expectations": ["formal/lean/Acceptance.lean"],
  "subject": {"paths": ["formal/lean/Acceptance.lean"], "digest": null},
  "claim_id": "acceptance-decides-readiness",
  "challenge": {"module": "Acceptance", "path": "formal/lean/Acceptance.lean"},
  "theorems": ["acceptance_ready_iff_all_required_pass", "acceptance_rejects_stale_attempt"],
  "profile": "unreviewed_agent",
  "permitted_axioms": ["propext", "Classical.choice", "Quot.sound"],
  "toolchain": {"tool": "lean", "version": "4.x.y", "comparator": "github.com/leanprover/comparator@<pinned>"}
}
```

```json
{
  "id": "ownership-model-invariants",
  "kind": "tlc",
  "cwd": "formal/tla",
  "timeout_seconds": 7200,
  "artifact": "tlc-receipt.json",
  "prerequisites": [{"name": "java", "executable": "java", "args": ["-version"]}],
  "inputs": ["formal/tla/OwnershipAcceptance.tla", "formal/tla/OwnershipAcceptance.cfg", "tmp/formal-tools/tla2tools.jar"],
  "expectations": ["formal/tla/OwnershipAcceptance.cfg"],
  "subject": {"paths": ["formal/tla/OwnershipAcceptance.tla"], "digest": null},
  "claim_id": "no-owner-holds-a-resource-after-release",
  "model": {"module": "OwnershipAcceptance", "path": "formal/tla/OwnershipAcceptance.tla"},
  "config": {"path": "formal/tla/OwnershipAcceptance.cfg"},
  "properties": ["NoDoubleOwnership", "ReleaseClearsOwnership"],
  "bounds": {"owners": 2, "resources": 2, "checks": 2, "revisions": 2, "generations": 2},
  "fingerprint": {"constants_from_config": true, "checksum_states": false, "workers": 1},
  "toolchain": {"tool": "tlc", "jar_sha256": null, "version": null}
}
```

`fingerprint.workers: 1` is not arbitrary. `formal/run_tlc.py:207-214` records
why: with `-workers auto` each worker prints its own summary and they interleave,
so the first regex match is whichever worker finished first, not the run total.
One worker costs wall-clock time and buys an unambiguous receipt.

---

## 2. The obligation representation

The plan says "Do not force Lean theorem names into scenario fields. Introduce
an obligation representation that can hold test IDs, theorem IDs, or model
properties." A single `string` field would do exactly what the plan forbids. A
sum type does it properly.

PROPOSAL: `src/vkit/verifiers/obligation.py`.

```python
# PROPOSAL
@dataclass(frozen=True)
class CaseObligation:      # one named case: a scenario id or a test node id
    test_id: str

@dataclass(frozen=True)
class TheoremObligation:    # one named Lean theorem
    theorem: str
    module: str            # never inferred from a path the adapter guesses

@dataclass(frozen=True)
class PropertyObligation:   # one named TLA+ property
    property_name: str
    bounds: tuple[tuple[str, int], ...]   # sorted, so the pair is comparable

Obligation = CaseObligation | TheoremObligation | PropertyObligation
```

What is genuinely common is the obligation key: `(check_id, obligation)`, which
is what a receipt must match against and what a policy pins. Everything else is
variant-specific. A test id names a row in a runner's report. A theorem name
names a declaration in a module. A model property names an entry in a `.cfg`
together with the bounds it was checked at, and the bounds are part of the
obligation rather than a receipt detail, because `formal/run_tlc.py:130-157`
already establishes that a run at different bounds is a different claim.

There is no `Obligation(id="x")` base class with an `id` attribute. That would
put a string back where the union is, and `TheoremObligation("x").test_id` would
either raise or lie.

The receipt carries the *result* of an obligation, which is also a sum type:

```python
# PROPOSAL
@dataclass(frozen=True)
class CaseSatisfied:      case: CaseObligation;      observation: str
@dataclass(frozen=True)
class TheoremSatisfied:   obligation: TheoremObligation; axioms: tuple[str, ...]
@dataclass(frozen=True)
class PropertySatisfied:  obligation: PropertyObligation; states: StateCounts
@dataclass(frozen=True)
class Counterexample:     obligation: Obligation; trace: str
ObligationResult = CaseSatisfied | TheoremSatisfied | PropertySatisfied | Counterexample
```

A `Counterexample` is variant-agnostic on purpose. Its `trace` is a string for a
TLC state sequence, a pytest assertion diff, and a Lean error message, and the
plan asks for counterexamples without asking for them to be structured. The
thing that must be typed is the obligation it belongs to.

Deletion opportunity, taken. `check.required_scenarios` (`src/vkit/manifest.py:85`)
is deleted rather than generalized. `CaseObligation("empty-cart")` is what it
becomes, and every existing v1 scenario check reads unchanged. The alternative,
keeping `required_scenarios` and adding `required_tests` and `theorems` beside
it, is three fields where one union belongs, and it is the exact shape the plan
warns about.

---

## 3. Category enforcement

### 3.1 Where it goes

PROPOSAL: enforcement lives in the schema and in the parser, not in the runner.
Three enforcement points, in order of how early they fire.

**First, at manifest parse.** The `kind` key selects a `oneOf` branch in the new
`schemas/manifest.v2.json`. A check whose kind is `scenario` cannot carry
`theorems` because that branch has `additionalProperties: false`. The parser at
`src/vkit/manifest.py:285` dispatches on `entry["kind"]` to the matching variant
constructor. This is the boundary discipline already in place: the rejection
happens before a process is launched, which is the whole reason
`src/vkit/manifest.py:1-11` puts them in one module.

**Second, at approval.** `src/vkit/integration/policy.py:59` currently pins
`CHECK_KEYS` to `{"id", "required_scenarios"}`. That becomes the obligation set:

```python
# PROPOSAL: src/vkit/integration/policy.py
CHECK_KEYS = frozenset({"id", "obligations", "evidence_kind"})
```

A required check names its obligations as the union, not as scenario strings.
`compare` at `src/vkit/integration/policy.py:348` currently computes
`lost = [s for s in required.required_scenarios if s not in spec.required_scenarios]`;
that becomes a set difference over `Obligation`, which is a total order only
because each variant is frozen and hashable. A candidate that drops a theorem
from a required check produces the same `required_obligation_removed` finding it
produces today for a dropped scenario.

**Third, at acceptance.** `_decide` at `src/vkit/tasks.py:843` gains one
comparison before the `result == "PASS"` branch is taken.

### 3.2 Where a legacy scenario driver claiming `theorem_checking` gets refused

Head-on, with the exact test.

The lie can be told in three places, and each is refused at a different point.
Two of them are refused by a schema branch that has no field to refuse it with.

**If the lie is in the manifest.** The driver check's `kind` is `scenario`. The
new `manifest.v2.json` `scenario` branch has `additionalProperties: false` and no
`evidence_kind` property, so a manifest carrying
`"kind": "scenario", "evidence_kind": "theorem_checking"` is refused by
`validate` at `src/vkit/manifest.py:331` before any variant is constructed. The
raised message is `ManifestError`, raised at `src/vkit/manifest.py:333`.

**If the lie is in the policy.** The policy names the check as required with
`evidence_kind: "theorem_checking"` while the approved manifest defines it as
`scenario`. This is refused in the check-entry parser at
`src/vkit/integration/policy.py:188`, which cross-checks the declared category
against `evidence_kind(spec)` for the approved manifest and raises `PolicyError`
on a mismatch.

**If the lie is in the artifact.** The scenario driver writes v1
`check-artifact.v1.json` bytes naming a scenario id like
`acceptance_ready_iff_all_required_pass`. This is the case that today succeeds
and must not. It is refused at the receipt construction step in section 4: the
receipt's `evidence_kind` is derived from the check's variant, never from the
artifact, so a `scenario` check cannot produce a receipt whose category is
`theorem_checking`. There is nothing to check at runtime because there is nothing
to get wrong.

The exact test a reviewer would run, stated as behavior so it survives
renaming:

```
A project whose manifest declares check "ownership-theorems" with kind "lean"
and a policy that requires it as evidence_kind "theorem_checking", re-declared
by the candidate with kind "scenario", produces a REJECT finding named
evidence_kind_downgraded. The finding text quotes the required theorem name
that the scenario variant can no longer carry.
```

That is `PolicyFinding("evidence_kind_downgraded", check_id, "REJECT", ...)`,
a new member of the same tuple `compare` at `src/vkit/integration/policy.py:336`
already returns. The existing test for the sibling rule is
`tests/test_policy.py:113`, `test_a_candidate_that_drops_a_required_scenario_is_refused`,
which asserts `"required_scenario_removed" in kinds`. The new test sits beside it
and asserts `"evidence_kind_downgraded" in kinds`.

Why a new finding name rather than reusing `required_scenario_removed`: the
existing name says a scenario went missing, and here nothing went missing. The
variant changed. A reader grepping for the old name would not find the rule that
matters.

### 3.3 How a generic PASS fails to discharge a theorem obligation

The code path, named, end to end.

Today, `tasks._decide` at `src/vkit/tasks.py:838` iterates required check ids,
looks up the newest eligible run for each at :839, and branches on
`run["result"]` at :843. A PASS from any check under the right id and the right
identities discharges it. There is no category in the loop.

PROPOSAL: the required set stops being `tuple[str, ...]` of check ids and becomes
a tuple of `Requirement(check_id, obligations, evidence_kind)`. The loop gains one
step between :841 and :843:

1. Load the receipt for the run, which is the typed receipt from section 4. Today
   `store.list_runs` (`src/vkit/tasks.py:818`, `_runs_in` at :886) selects nine
   columns and reconstructs a dict at :896. It has no receipt. PROPOSAL: add
   `evidence_kind` and `satisfied` to the row, or read the published report via
   `store.load` (`src/vkit/storage.py:993`).
2. Compare `receipt.evidence_kind` to `requirement.evidence_kind`. A mismatch
   appends a gap and `continue`s, so the check does not reach the PASS branch.
   The gap text names both categories and states that a PASS in one category does
   not discharge an obligation in another.
3. Compare the obligation sets. Every `Requirement.obligations` member must have
   a satisfying `ObligationResult` in `receipt.satisfied`. A `Counterexample`
   means the run failed that obligation and the whole requirement is a FAIL.
4. Only then is `result == "PASS"` read.

The specific failure asked about. A check named `ownership-theorems` is
`kind: "lean"` and required with `evidence_kind: "theorem_checking"` and two
theorem obligations. A separate check named `smoke` is `kind: "scenario"`, passes,
and produces a receipt with `evidence_kind: "scenario"`. If `smoke` is passed to
`finalize(..., additional_checks=["smoke"])`, its requirement carries
`evidence_kind: "scenario"` derived from its own manifest variant, and it passes.
It cannot discharge the theorem obligation, because the theorem obligation is
attached to `ownership-theorems`, which has no run at all. The gap is
`"no completed run for required check 'ownership-theorems'"`, produced by the
existing line at `src/vkit/tasks.py:841`.

The harder case, and the one worth writing the test for: the candidate renames.
A candidate edits its manifest so the scenario check is called
`ownership-theorems` and the lean check is called something else. Then
`Manifest.digest()` (`src/vkit/manifest.py:168`) changes, `configuration_digest`
on the run row no longer matches `policy_digest` on the contract, and
`_identity_gaps` at `src/vkit/tasks.py:931` produces
`"passed against a different policy"` and the re-run instruction. That guard
already exists today at :950-954. The new obligation check is not what stops the
rename; the identity check is. Recording that is the honest answer, and it means
the new check's job is narrower than it first looks: it stops a same-policy PASS
in the wrong category, not a policy change.

### 3.4 The premise, attacked

Two facts are in tension and one of them is wrong.

Fact one, `plans/10-native-verifiers.md:34`: "Preserve explicit legacy scenario
checks, with their actual evidence category. A legacy driver claiming
`theorem_checking` must be refused."

Fact two, `src/vkit/claimkind.py:62-65`: the four categories are `scenario`,
`property`, `finite_model_checking`, `theorem_checking`.

Those line up. But the plan at :44 says "Use separate variants for `scenario`,
`pytest`, `node_test`, `lean`, and `tlc`", which is five *verifier kinds*, while
the categories are four *evidence kinds*. `pytest` and `node_test` are both
`scenario` evidence. `property` is a sixth kind in the plan's own sentence at :44
but not in its list of five.

Resolution, and it is a deletion: `CheckKind` and `ClaimCategory` are different
types with different jobs, and conflating them is the bug this whole design
exists to prevent. `CheckKind` answers "who interprets the output". `ClaimCategory`
answers "what does a green result license". A `pytest` kind with a
`generator` block yields category `property`; without it, category `scenario`.
A `lean` kind yields category `theorem_checking` or BLOCKED. A `tlc` kind yields
`finite_model_checking` or BLOCKED.

PROPOSAL: `checkkind` becomes a function of the variant, not a field anybody
sets.

```python
# PROPOSAL: src/vkit/verifiers/spec.py
def evidence_kind(check: NativeCheckSpec) -> ClaimCategory:
    """The category a PASS from this check licenses. Derived, never declared.

    Every branch maps one variant to exactly one category, so a check cannot
    claim a category its interpreter cannot produce. A `pytest` variant with no
    generator block is a SCENARIO result no matter what the runner printed,
    which is the whole difference claimkind.py draws at lines 17-22.
    """
```

There is deliberately no `evidence_kind` field on any manifest check. A declared
category is a label the candidate controls, and the plan says at :56 that "A
candidate-controlled JSON document ... cannot promote evidence to a stronger
category." Making it a field would build the thing the plan forbids.

The consequence is a real cost and it is worth naming: `integration/policy.py`'s
`CHECK_KEYS` cannot carry a category the policy author chose, only the one the
variant produces. So the policy's role for category is to require the *check
and its obligations*, and the category follows. `evidence_kind` in the policy
becomes an assertion the parser verifies against `evidence_kind(spec)` and
refuses on mismatch, which is check 3.2's second case. The assertion is
checkable precisely because the derivation is total.

---

## 4. The receipt

### 4.1 What the plan lists and where each item lands

Plan 10 at `plans/10-native-verifiers.md:48-54` lists required contents. Each one,
with its actual type.

| Plan item | Field | Type |
| --- | --- | --- |
| Check, claim, task generation, run IDs | `check_id: str`, `claim_id: str`, `task_id: str \| None`, `generation: int \| None`, `run_id: str` | `generation` is `None` for a standalone run, which is what makes it standalone evidence |
| Category | `evidence_kind: ClaimCategory` | derived by `evidence_kind(spec)`, never written by a candidate |
| Subject digest | `subject: SubjectRef` | `{"paths": [...], "digest": str \| None}` |
| Specification digest | `specification_digest: str \| None` | null when the check declares no specification file |
| Approved policy digest | `policy_digest: str` | maps to the `configuration_digest` column today (`src/vkit/tasks.py:924`) |
| Source inventory | `source: SourceIdentity` | reuse `src/vkit/identity.py:194` verbatim, field for field as its own docstring requires |
| Fixture digest | `fixture_digest: str \| None` | `src/vkit/manifest.py:55-68` |
| Verifier implementation identity | `verifier: VerifierIdentity` | `{"module": str, "version": str, "source_digest": str}` |
| Tool versions | `tool_versions: dict[str, str]` | already exists at `src/vkit/execution.py:200` |
| Dependency identities | `dependencies: tuple[DependencyIdentity, ...]` | `{"name", "version", "digest"}` over the resolved lock |
| Supported runtime | `runtime: RuntimeIdentity` | `{"python_version", "platform", "requires_os": "any" \| "posix"}` |
| Checked cases, theorems, properties, bounds | `satisfied: tuple[ObligationResult, ...]` | the union from section 2 |
| Status | `status: Literal["PASS", "FAIL", "BLOCKED"]` | unchanged |
| Assumptions | `assumptions: tuple[str, ...]` | named, human-readable, quoted in `describe()` |
| Execution limits | `limits: ExecutionLimits` | `{"timeout_seconds", "workers", "seed"}` |
| Counterexamples | `counterexamples: tuple[Counterexample, ...]` | |
| Bounded artifact references | `artifacts: dict[str, str]` | run-relative, as `src/vkit/execution.py:486` already requires |

### 4.2 Where the hash limit is recorded, so it cannot be lost

Plan 10 at :56-57: "Hashes bind evidence to bytes. They do not establish that an
oracle is correct or that a candidate cannot compromise a process. Document the
approved test/checker code and isolation assumptions."

The limit lives in three places that must all be written, because one is a
schema, one is a type, and one is a viewer, and a reader meets each at a
different moment.

**In the schema.** `schemas/receipt.v2.json` carries a required
`trust_boundary` object with `establishes` and `does_not_establish` as required
non-empty strings, and `oracle_review` as a required string naming the reviewed
test or checker source. A receipt without them does not validate. That is the
same device `claimkind.py:147` `to_json` already uses, moved into the boundary
where it cannot be forgotten.

**In the type.** PROPOSAL: `Receipt.__post_init__` copies
`ClaimCategory.establishes` and `does_not_establish` from
`src/vkit/claimkind.py:68` and `:88` into the receipt rather than letting each
adapter write its own prose. There is one source of those sentences in the
repository today and this keeps it one.

**In the viewer.** `project_inspect`, `run_get`, `task_finalize`, and the
console views at `src/vkit/console/operations.py:212` all render category plus
scope. A PASS is never rendered as a bare tick.
`src/vkit/console/static/app.js:175` already prints `required scenarios` for a
check; the change adds the category beside it. The CLI run payload at
`src/vkit/cli.py:322-328` gains the same field.

What the hashes do not cover, stated so it is on the record: a receipt proves
that the bytes of the oracle were these bytes, not that the oracle is right. The
`oracle_review` field names who reviewed them and is a claim about a human
process, not something vkit can check. Under before/after hashing, a transient
edit that is restored between the two measurements is invisible; this is already
stated in `plans/CONTRACT.md:85` and is repeated here because a receipt is where
a reader will look for it.

Cross-platform digests already have a defined rule.
`formal/digest.py:22-24` defines `canonical_sha256` over text with CRLF folded
to LF, and :27-30 states the bound in prose as `NORMALIZATION`. The receipt
quotes `NORMALIZATION` into every receipt the way
`formal/run_tlc.py:362` already does. A jar is hashed over exact bytes instead,
as `formal/run_tlc.py:91-98` explains.

### 4.3 What the core writes

PROPOSAL: `src/vkit/verifiers/receipt.py`. The core writes the receipt from
`AdapterResult`, which is the structured output of an adapter's parser. The
candidate never supplies one. `execution.run_check` at
`src/vkit/execution.py:249` gains one branch after `_derive` at :342: the
dispatcher picks the adapter by `check.kind`, the adapter returns an
`AdapterResult` or a `Blocked`, and the receipt is built from whichever came
back. `_derive` at `src/vkit/execution.py:132` keeps its ordering for the
scenario path unchanged, so no legacy behavior moves.

---

## 5. Migration

### 5.1 The two existing schemas

`schemas/check-artifact.v1.json` stays. It is the artifact format for `scenario`
checks and nothing else, and `src/vkit/schemas.py:19` keeps exporting it. It is
not deprecated, because the scenario variant is a first-class variant and
delegating to a driver is the right shape for browser and API checks
(`plans/10-native-verifiers.md:68`).

`schemas/run-report.v1.json` stays readable. It keeps describing the run: the
process, the argv, the lifecycle, the environment, the provenance. What changes
is that a native run's report gains a `receipt` reference rather than gaining
receipt fields. `src/vkit/execution.py:441` `_terminal_report` builds the report
and `validate`s it at :488; PROPOSAL adds one optional property `receipt` and
points it at the receipt file inside the run directory.

PROPOSAL: new files `schemas/manifest.v2.json`, `schemas/receipt.v2.json`. The
`src/vkit/schemas.py:22` `SCHEMA_VERSION = 1` becomes a per-schema map rather
than one constant, because two schemas are at version 2 and one stays at 1.
`parse_manifest_bytes` at `src/vkit/manifest.py:324-328` already refuses a mismatched
version with a good message; it dispatches on the declared version instead of
comparing to one constant.

### 5.2 Every caller that changes, by file and line

| File:line | Change |
| --- | --- |
| `src/vkit/manifest.py:72` | `CheckSpec` gains `kind`, `subject`, `claim_id`; becomes a union with the six variants |
| `src/vkit/manifest.py:80-89` | `required_scenarios` moves off the shared spec onto `ScenarioVariant` |
| `src/vkit/manifest.py:124` | `canonical_form` gains `kind`, `subject`, `claim_id`, obligations. This changes every `Manifest.digest()`, which is intended and is the same event as a policy change |
| `src/vkit/manifest.py:168` | `digest()` unchanged; its inputs are |
| `src/vkit/manifest.py:324-328` | version dispatch instead of one-constant compare |
| `src/vkit/manifest.py:341` | argv parsed per variant. `scenario` keeps `command`; every other variant derives argv from its `runner` and refuses a `command` key |
| `src/vkit/manifest.py:352` | `required = tuple(entry["required_scenarios"])` becomes obligation parsing, with the non-empty rule per variant |
| `src/vkit/manifest.py:371` | `CheckSpec(...)` construction dispatches on `kind` |
| `src/vkit/execution.py:33` | `CHECK_ARTIFACT` retained; the new receipt schema added for validation |
| `src/vkit/execution.py:72` | `_scenarios_from_artifact` keeps its signature. It becomes the `scenario` variant's parser, called only for `kind == "scenario"` |
| `src/vkit/execution.py:157` | call site moves behind the dispatcher |
| `src/vkit/execution.py:200` | `tool_versions` extended with the adapter's own tool identity |
| `src/vkit/execution.py:249` | `run_check` gains the adapter dispatch after `_derive` |
| `src/vkit/execution.py:303` | `check.resolved_argv_for` dispatches: scenario substitutes placeholders into `command`; other variants build argv from `runner` |
| `src/vkit/execution.py:441` | `_terminal_report` gains the `receipt` reference |
| `src/vkit/execution.py:486` | `report["artifacts"]` gains the receipt name |
| `src/vkit/supervisor.py:110` | `outcome_from_report` unchanged. It reads the verdict, and the verdict did not move |
| `src/vkit/storage.py:189` | `runs_new` gains `evidence_kind` and `satisfied_json` columns. Migration 6, following the rename dance at :164-236 |
| `src/vkit/storage.py:981` | the `UPDATE runs SET lifecycle = ...` in `publish` also writes the two new columns |
| `src/vkit/storage.py:993` | `load` unchanged |
| `src/vkit/tasks.py:88` | `TaskContract.required_checks` becomes `requirements`, or the pinned contract gains the obligation set alongside |
| `src/vkit/tasks.py:716` | `required = tuple(sorted(set(contract.required_checks) \| set(additional_checks)))` becomes a union over `Requirement` |
| `src/vkit/tasks.py:757` | `compute_readiness` signature keeps `required_check_ids` and gains the category check in `_decide` |
| `src/vkit/tasks.py:838-862` | the loop gains the category and obligation comparison described in 3.3 |
| `src/vkit/tasks.py:886` | `_runs_in` selects the two new columns |
| `src/vkit/tasks.py:915` | `COMPARED_IDENTITIES` unchanged. Its docstring at :908-914 is right and the map is still the only place column names are spelled |
| `src/vkit/tasks.py:555` | `_floor` unchanged. It unions manifest checks with the caller's selection, and it does not care what a check is |
| `src/vkit/integration/policy.py:59` | `CHECK_KEYS` becomes `{"id", "obligations", "evidence_kind"}` |
| `src/vkit/integration/policy.py:73` | `PolicyCheck.required_scenarios` becomes `obligations` plus `evidence_kind` |
| `src/vkit/integration/policy.py:126` | `Policy.digest()` input changes, so every policy digest changes |
| `src/vkit/integration/policy.py:188` | check-entry parsing gains the variant-cross check |
| `src/vkit/integration/policy.py:348` | obligation set difference |
| `src/vkit/integration/policy.py:375` | the `scenario_added` REVIEW rule becomes an obligation-added REVIEW rule |
| `src/vkit/integration/policy.py:394` | `load_approved` unchanged. It calls `parse_manifest_bytes`, which gains version dispatch |
| `src/vkit/integration/sandbox.py:185` | `validate_artifact_bytes` dispatches per variant. The scenario branch is the existing `_scenarios_from_artifact` call |
| `src/vkit/integration/verify.py:539` | the `definition` block gains `kind` |
| `src/vkit/mcp/_tools.py:324-335` | `_check_view` gains `kind` and `evidence_kind` so an agent can see what a check licenses before running it |
| `src/vkit/mcp/_tools.py:551` | the contract shape check admits the new keys |
| `src/vkit/console/operations.py:212` | `checks_view` gains `kind` and `evidence_kind` |
| `src/vkit/console/static/app.js:175` | renders the category beside the scenarios |
| `src/vkit/cli.py:317` | unchanged. `run_check` is still the one call |
| `src/vkit/cli.py:155` | the contract file's allowed keys unchanged, since `required_checks` is still a list of ids from the caller's side |
| `src/vkit/enroll.py:261` | `required_scenarios` line becomes a kind-aware line |
| `src/vkit/enroll.py:353` | `_entry_for` emits `kind: "scenario"` and the empty `required_scenarios`, which the schema still refuses. The proposal stays structurally un-acceptable |
| `src/vkit/claimkind.py:59` | no code change. Its four values become the receipt's category vocabulary |
| `examples/python-cli/verification/manifest.json` | `schema_version` 1 to 2 and `kind` added |
| `examples/node-cli/verification/manifest.json` | same |
| `examples/node-http/verification/manifest.json` | same |
| `tests/fixtures.py:286` | `manifest_document` emits `kind: "scenario"` |
| `tests/fixtures.py:305` | `policy_document` emits obligations instead of `required_scenarios` |
| `tests/test_policy.py:113` | unchanged. The dropped-scenario rule still fires |
| `tests/test_claimkind.py:45` | unchanged. Every category still constructs and describes |
| `README.md:148-165` | the documented manifest gains `kind` |

Two files are deliberately untouched. `src/vkit/claimkind.py` keeps its four
values; its prose is already the enforcement vocabulary and rewriting it would
change `tests/test_claimkind.py:80` for nothing. `src/vkit/supervisor.py:110`
keeps reading `outcome` only, because a retry returns the recorded verdict and
must not re-derive it.

### 5.3 Records already written under the old shape

`plans/CONTRACT.md:48`: "Later migration must preserve old reports as standalone
evidence; it must not invent task acceptance for them."

Three rules, and each maps onto a mechanism that already exists.

**A v1 run stays readable.** `store.load` at `src/vkit/storage.py:993` reads
`report.json` and does not validate it. Nothing breaks. The run row gains
`evidence_kind` and `satisfied_json` as nullable, and migration 6 backfills both
as NULL.

**A v1 run cannot discharge a native obligation.** The rule is not a special
case. `_decide` requires `run["evidence_kind"] == requirement.evidence_kind`, and
a NULL never equals a category, so the run produces a gap. This is the same
device `_UNRECORDED_ON_RUN` at `src/vkit/tasks.py:929` already uses: an identity
a run never recorded cannot be shown to describe what is in force now, and its
absence is a gap. Its comment at :928 says exactly that, about fixtures. The
rule generalizes rather than being invented.

**A v1 run cannot invent acceptance.** `attempt` is NULL on a run that predates
task attempts, and `_decide` at `src/vkit/tasks.py:824` already routes a run
whose attempt is not the current generation into `history` and never into
`eligible`. That code exists today for this exact reason, per the `finalize`
docstring at `src/vkit/tasks.py:698-700`. No new code is needed.

The one thing migration must not do is silently upgrade a v1 row to
`evidence_kind = "scenario"`. That would be inventing a category the run never
recorded, on the reasoning that a v1 check must have been a scenario driver.
It might have been, and "might have been" is exactly what `plans/CONTRACT.md:48`
forbids. NULL it stays, and the reader sees a gap with the reason.

---

## 6. Orders of decision

### Frozen before any adapter is written

These six are the contract. An adapter written against a different one of them is
thrown away.

1. `src/vkit/verifiers/spec.py`, the six variants and their fields.
2. `src/vkit/verifiers/obligation.py`, the obligation union and its hashable
   ordering.
3. `evidence_kind(spec)` as a total function in `spec.py`, with no declared
   category field anywhere.
4. `schemas/manifest.v2.json`, the `oneOf` and its `additionalProperties: false`
   branches.
5. `schemas/receipt.v2.json`, including the required `trust_boundary` object.
6. `integration/policy.CHECK_KEYS` and the `evidence_kind_downgraded` finding.

### Then, and only then, the adapters

Adapters can be written in parallel once 1 through 6 are frozen, because none of
them touches another's file:

| Adapter | Owns | Blocked on |
| --- | --- | --- |
| pytest | `src/vkit/verifiers/pytest_adapter.py` | items 1-6 |
| node_test | `src/vkit/verifiers/node_adapter.py` | items 1-6 |
| property | `src/vkit/verifiers/property_adapter.py` | items 1-6, and the pytest adapter's `report_format` parser |
| lean | `src/vkit/verifiers/lean_adapter.py` | items 1-6, and real `lean --help` output per `plans/10-native-verifiers.md:83` |
| tlc | `src/vkit/verifiers/tlc_adapter.py` | items 1-6, and `tlc2.TLC` output codes per `plans/10-native-verifiers.md:93` |

### Sequenced after the adapters

`src/vkit/execution.py` dispatch, then `src/vkit/storage.py` migration 6, then
`src/vkit/tasks.py` readiness, then the views. The storage migration follows the
dispatch because the column must be written by something that exists.

### Parallel from day one

`examples/*/verification/manifest.json` can be rewritten to `schema_version: 2`
the day item 4 lands, since they are all `scenario` variants and need no
adapter. `README.md:148-165` follows.

---

## 7. The hard question: where `property` classification happens

The plan at `plans/10-native-verifiers.md:44`: "Classify approved Hypothesis
tests as `property` when their registered obligation specifies that category. Do
not infer it from a package being installed."

Trace it. Three candidate sites, and two of them are wrong.

**Wrong site one: the runner detecting imports.** A pytest adapter that greps the
report for a hypothesis plugin, or that checks whether `hypothesis` is
importable in the child, infers from installation. Rejected. `pyproject.toml:41`
puts hypothesis in the `test` extra, so its presence is a property of the
environment, not of the claim. `tests/test_formal_correspondence.py:70` skips the
whole module without it, which is the exact condition the plan is guarding
against: a host with the package installed would read a skipped file as
property evidence.

**Wrong site two: the artifact.** Letting the check's own report declare its
category is a candidate-controlled JSON document, refused by
`plans/10-native-verifiers.md:56`.

**Right site: the manifest variant, verified against the obligations.** PROPOSAL.
The classification is made once, at `src/vkit/manifest.py:341`, when
`parse_manifest_bytes` constructs the variant. `kind: "property"` is the only way
to get `evidence_kind(spec) == ClaimCategory.PROPERTY`, and the `property` branch
requires the `generator` block, which records the settings vkit passed. There is
no `evidence_kind` field for a candidate to write.

Now the second half of the requirement: "when their registered obligation
specifies that category." The obligation is in the policy, and the policy is
where a candidate is least able to move it. PROPOSAL: `PolicyCheck` carries
`evidence_kind`, and `src/vkit/integration/policy.py:188` cross-checks it against
`evidence_kind(spec)` for the approved manifest and refuses a mismatch. So the
category is asserted twice, in two places the candidate controls separately (its
own manifest, and its own policy file), and the two must agree with the
derivation. Disagreement in either direction is a REJECT: a candidate cannot
downgrade, because a `property` manifest whose policy says `scenario` is refused,
and it cannot promote, because a `scenario` manifest whose policy says `property`
is refused by the same check.

Why the candidate cannot set it, stated as the three independent obstacles:

1. There is no field. `evidence_kind` is a function. A manifest that writes
   `"evidence_kind": "property_checking"` has an unknown key, and
   `additionalProperties: false` on the `scenario` branch refuses it at
   `src/vkit/manifest.py:331`.
2. The variant requires the thing the category claims. `kind: "property"`
   without `generator` fails the schema, so a category cannot be claimed without
   the settings that make it meaningful. And `generator` is policy, in the
   manifest, reviewed by a person, at `pyproject.toml` level of scrutiny.
3. The receipt records the derivation, and acceptance compares it. The receipt's
   `evidence_kind` comes from `evidence_kind(spec)`, and
   `src/vkit/tasks.py:838` compares it to the requirement's. A run that produced
   a `scenario` receipt cannot discharge a `property` requirement even if the
   check id, source, policy, and fixtures all match.

The exact test:

```
A manifest whose only check is kind "pytest" over a Hypothesis test file passes,
its policy requires it as evidence_kind "scenario", and the run is recorded. The
receipt's evidence_kind is "scenario" and the run discharges the requirement.
Change the manifest kind to "property" with no generator block and the manifest
is refused by the schema with the generator field named. Change it to "property"
with a generator block and change the policy's evidence_kind to match, and
Manifest.digest() changes, so every run recorded before the change no longer
matches policy_digest and every one of them produces the existing
"passed against a different policy" gap at src/vkit/tasks.py:951.
```

---

## 8. What to build, what to refuse

### Build

- The variant union, the obligation union, `evidence_kind` as a derivation, the
  receipt, the two new schemas, the migration, the readiness category check.
- One vertical pytest check through `vkit check run`, detached MCP, and
  `finalize`, which is checkpoint 1's own gate.

### Refuse, and say so

**The reviewed Lean profile on Windows.** `plans/10-native-verifiers.md:85` says
the stronger profile can require Linux and that "Windows process ownership is
not proof-build isolation." `src/vkit/execution.py:206-219` `RunEnvironment`
documents what the trusted path already does and says plainly that containment
is the reason. A `lean` variant whose `profile` is `reviewed_proof_sources` on
`sys.platform == "win32"` returns BLOCKED with `BlockedReason.TOOL_MISSING` and a
detail naming the missing capability. It does not fall back to
`unreviewed_agent`, and it does not run a fake sandbox, per
`plans/10-native-verifiers.md:83`.

**A weaker profile discharging a stronger obligation.** An admitted obligation
pinned to `reviewed_proof_sources` requires a receipt whose `profile` is
`reviewed_proof_sources`. A receipt from `unreviewed_agent` is a mismatch, and
mismatches are gaps at `src/vkit/tasks.py:838`. `plans/10-native-verifiers.md:79`
states the agent cannot choose the weaker profile; this is the code that says so.

**Hashes as proof of oracle correctness.** Recorded in three places in section
4.2 and never elided from a viewer. A receipt whose `trust_boundary` is missing
does not validate.

**Candidate-supplied verdicts.** The receipt is built by
`src/vkit/verifiers/receipt.py` from an `AdapterResult` the adapter parsed from
bytes the runner wrote. The candidate's own artifact is validated against
`schemas/receipt.v2.json` and its disagreement with the trusted launcher's copy
is already a BLOCKED at `src/vkit/integration/sandbox.py:202`. The candidate
cannot supply a receipt; it can only supply bytes that fail to become one.

**A generic PASS for a theorem.** Section 3.3, code path named.

**Remote artifact import as an acceptance shortcut.**
`plans/10-native-verifiers.md:103` forbids it and nothing here adds it.

### Blocked at design time, honestly

**Whether Hypothesis reports its settings in a structured report.** The `pytest`
adapter is specified to parse `pytest_json_report` at a pinned version
(`expect_report_version`). Whether that report carries the hypothesis profile
that matches the `generator` block is a fact about two third-party packages that
would need running to answer. The design does not depend on it: the `generator`
block is policy, pinned in the manifest, and vkit passes it to the run. The
receipt records what vkit pinned and what the runner reports, and if the runner
reports none, that is recorded as none rather than as agreement. The
implementation must confirm the concrete mechanism; if the runner ignores the
pinned settings, the property adapter reports BLOCKED rather than asserting an
unverified match.

**TLC's exit codes and output text.** `plans/10-native-verifiers.md:93` says to
use the TLC implementation as the authority and to generalize
`formal/run_tlc.py` only after checking its assumptions. The assumptions that
script makes, all visible at `formal/run_tlc.py:232-311`, are that completion is
the `Model checking completed. No error has been found.` line together with
`states left on queue` reading zero (:246-249), that violation is one of four
markers (:256-261), that exhaustion is one of four more (:262-265), and that
totals come from the last summary rather than the first match (:319-342). Whether
those hold for an arbitrary project model with different constants is a fact
that needs a real TLC run against a real model. The adapter inherits the rules;
confirming them is checkpoint 3 work in Ubuntu CI.

**The Lean comparator contract.** `plans/10-native-verifiers.md:83` says "Check
actual tool help and a real example before freezing argv." The `lean` variant in
this design pins `challenge.module`, `challenge.path`, `theorems`,
`permitted_axioms`, and `toolchain.comparator`. Which of those map to real
comparator CLI flags, and what the flag spelling is, is not decidable from this
repository. The design commits to the shape and refuses to invent the argv.

---

## Principles applied

**principle-laziness-protocol** changed two decisions. `required_scenarios` is
deleted rather than generalized into a union, because a `scenario` variant that
carries `CaseObligation("empty-cart")` is the same field with a new type and
keeping both is two spellings of one fact. The eight shared fields
(`id`, `description`, `cwd`, `timeout_seconds`, `prerequisites`, `inputs`,
`expectations`, `artifact`) stay off the variants, so six copies of the timeout
rule do not exist. It also rejected the alternative design for section 3, which
was a base class with an `id` property on every obligation variant.

**principle-model-the-domain** produced `evidence_kind` as a total function of
the variant rather than a field, and the three obligations as three types rather
than one string. Before those, category was a value nobody stored
(`src/vkit/tasks.py:843` branches on `result` alone) and evidence was a scenario
id (`src/vkit/execution.py:98`). Both were the same mistake: a domain concept
carried by whatever string happened to be available.

**principle-type-system-discipline** produced the variant union itself, and the
decision that `evidence_kind` has no setter. The test the principle states,
"can I write a comment explaining when this combination of fields is valid",
answers yes for `{"kind": "lean", "required_scenarios": [...]}` and no for
anything the variants allow. It also produced `ObligationResult` as a sum type,
so a receipt cannot carry a satisfied theorem with no axiom audit.

**principle-attack-the-premise** changed the category question. Two apparently
separate facts, "a legacy driver claiming `theorem_checking` must be refused"
(`plans/10-native-verifiers.md:34`) and "`claimkind.py` describes evidence
categories but does not enforce them"
(`plans/10-native-verifiers.md:19`), share the premise that category is a
*declared property of a check*. It is not; it is a *consequence of which
interpreter ran and what it pinned*. Refusing the specific bad declaration was
the fix that assumed the premise. Questioning the premise deletes the field the
fix would have added a validator for, and the refusal in section 3.2 becomes a
consequence rather than a rule.

## Open items for the coordinator

1. `integration/policy.py:59`'s `CHECK_KEYS` change breaks every policy document
   in the tree: the three `examples/*/verification/manifest.json`, `tests/fixtures.py:305`,
   and anything under `scripts/`. That is a wide edit against a file this design
   does not own. The coordinator owns the decision about whether the obligation
   set belongs in the policy at all, or whether the policy keeps naming only
   check ids and the obligations are read from the approved manifest. The second
   is smaller and is defensible, because `policy.compare` at
   `src/vkit/integration/policy.py:348` already reads
   `spec.required_scenarios` from the candidate's manifest and compares it to the
   policy's own list. This design recommends the first, because a policy that
   pins the obligations is the only place a candidate cannot quietly rename a
   theorem, but it is a real trade and the coordinator should make it.
2. `SCHEMA_VERSION` at `src/vkit/schemas.py:22` is one constant for three
   schemas. Two schemas go to version 2 and one stays at 1. Whether that becomes a
   map or whether `manifest` gets its own version constant is a small decision
   with a wide blast radius across `src/vkit/manifest.py:324-328` and
   `src/vkit/integration/policy.py:158`.
3. Whether `lean` and `tlc` are `kind` values at all in checkpoint 1's schema, or
   declared and BLOCKED until checkpoint 3. Declaring them now means the manifest
   schema is frozen with a shape nobody has run, which is the risk
   `plans/10-native-verifiers.md:112` names when it says "Check actual tool help
   and a real example before freezing argv."
