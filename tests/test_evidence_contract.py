"""The six check variants, the obligation union, and what each one refuses.

`docs/verification.md` freezes this contract, and everything
here goes through the public path: a document is parsed by `parse_manifest_bytes`
or handed to the schema, and the assertion is about what a caller observes. A
test that called a private function directly would keep passing after the caller
it protected stopped calling it.

Each variant gets two tests: one that constructs it, and one that shows the form
it must never take is refused. The second half is the point of the union. A
`lean` variant with no `theorems` field, a `scenario` variant carrying a
generator block, and a manifest declaring `evidence_kind` are three claims a
candidate could otherwise make in a file vkit reads, and each is refused at the
boundary with a message that names what was wrong.
"""
from __future__ import annotations
import subproc

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit.claimkind import ClaimCategory  # noqa: E402
from vkit.manifest import (  # noqa: E402
    Manifest,
    ManifestError,
    parse_manifest_bytes,
)
from vkit.paths import open_project  # noqa: E402
from vkit.schemas import (  # noqa: E402
    CHECK_ARTIFACT,
    MANIFEST,
    MANIFEST_V2,
    RECEIPT,
    RUN_REPORT,
    SchemaValidationError,
    schema_version,
    validate,
)
from vkit.verifiers import (  # noqa: E402
    CaseObligation,
    CheckKind,
    DomainBounds,
    FingerprintSpec,
    HypothesisSettings,
    LeanCheck,
    LeanProfile,
    ModuleRef,
    NodeTestCheck,
    PinnedRunner,
    PropertyCheck,
    PropertyObligation,
    PytestCheck,
    ScenarioCheck,
    SubjectRef,
    TheoremObligation,
    TlcCheck,
    ToolchainRef,
    evidence_kind,
)

SHARED = {
    "timeout_seconds": 60,
    "artifact": "result.json",
    "subject": {"paths": ["app.py"], "digest": None},
    "claim_id": "the-claim",
}
RUNNER = {"executable": "{{python}}", "base_argv": ["-m", "pytest", "-p", "no:cacheprovider"]}
GENERATOR = {
    "max_examples": 200, "stateful_step_count": 25,
    "deadline": None, "suppress_health_check": ["too_slow"],
}


@pytest.fixture
def project(tmp_path: Path) -> object:
    """A throwaway repository with a `verification/` directory to parse against.

    `git init` is not needed for parsing, but the project binding is, and a real
    one keeps these tests on the same code path every execution caller uses.
    """
    (tmp_path / "verification").mkdir()
    (tmp_path / "verification" / "manifest.json").write_text("{}", encoding="utf-8")
    subproc.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return open_project(tmp_path)


def parse(project, checks: list[dict], origin: str = "manifest.json") -> Manifest:
    """Parse a v2 manifest the way every caller does."""
    return parse_manifest_bytes(
        json.dumps({"schema_version": 2, "checks": checks}).encode("utf-8"),
        project=project, run_dir=project.runs_root, origin=origin,
    )


def a_check(**fields) -> dict:
    return {**SHARED, **fields}




def test_the_scenario_variant_parses_and_licenses_scenario_evidence(project) -> None:
    """A driver check is SCENARIO, and its scenarios are case obligations."""
    manifest = parse(project, [a_check(
        id="behaviour", kind="scenario", command=["python", "verify.py"],
        required_scenarios=["empty-cart", "single-positive"],
    )])
    spec = manifest.checks["behaviour"]
    assert spec.kind is CheckKind.SCENARIO
    assert spec.evidence_kind() is ClaimCategory.SCENARIO
    assert spec.argv == ("python", "verify.py")
    assert spec.obligations() == (
        CaseObligation("empty-cart"), CaseObligation("single-positive"),
    )


def test_a_scenario_check_may_not_carry_a_theorem_or_a_category(project) -> None:
    """The two lies a legacy driver could tell in its own manifest file.

    `theorems` and `evidence_kind` are both absent from the scenario branch, and
    `additionalProperties: false` is what refuses them. Neither refusal is a rule
    vkit added for the purpose: the branch simply has no field for either claim.
    """
    base = dict(id="behaviour", kind="scenario", command=["python", "verify.py"],
                required_scenarios=["empty-cart"])
    for extra, named in (({"theorems": ["ready_iff_all_pass"]}, "theorems"),
                         ({"evidence_kind": "theorem_checking"}, "evidence_kind")):
        with pytest.raises(ManifestError) as caught:
            parse(project, [a_check(**{**base, **extra})])
        assert named in str(caught.value), str(caught.value)


def test_the_pytest_variant_parses_and_licenses_scenario_evidence(project) -> None:
    """A pytest check without a generator block is SCENARIO, whatever it printed.

    That is the whole difference `claimkind.py` draws: the absence of pinned
    generator settings is what stops a runner's output being read as property
    evidence, so the category is derived rather than declared.
    """
    manifest = parse(project, [a_check(
        id="storage", kind="pytest", required_tests=["tests/test_storage.py::t1"],
        runner=RUNNER, report_format="pytest_json_report", expect_report_version=1,
    )])
    spec = manifest.checks["storage"]
    assert spec.kind is CheckKind.PYTEST
    assert spec.evidence_kind() is ClaimCategory.SCENARIO
    assert spec.variant.obligations == (CaseObligation("tests/test_storage.py::t1"),)


def test_a_pytest_check_may_not_carry_a_generator_block(project) -> None:
    """The block that records the generator settings is the thing a PROPERTY
    result licenses, so letting a `pytest` check carry it would make the category
    a matter of declaration."""
    with pytest.raises(ManifestError) as caught:
        parse(project, [a_check(
            id="storage", kind="pytest", required_tests=["tests/test_storage.py::t1"],
            runner=RUNNER, report_format="pytest_json_report",
            expect_report_version=1, generator=GENERATOR,
        )])
    assert "generator" in str(caught.value), str(caught.value)


def test_the_node_test_variant_parses_and_licenses_scenario_evidence(project) -> None:
    manifest = parse(project, [a_check(
        id="split", kind="node_test", required_tests=["test/split.test.js:leaves no cent"],
        runner={"executable": "node", "base_argv": ["--test", "--test-reporter=tap"]},
        report_format="node_tap", expect_report_version=1,
    )])
    spec = manifest.checks["split"]
    assert spec.kind is CheckKind.NODE_TEST
    assert spec.evidence_kind() is ClaimCategory.SCENARIO
    assert spec.variant.report_format == "node_tap"


def test_the_property_variant_parses_and_licenses_property_evidence(project) -> None:
    """`kind: "property"` with a pinned generator block is the only route to
    category PROPERTY, and the settings come from the manifest."""
    manifest = parse(project, [a_check(
        id="correspondence", kind="property",
        required_tests=["tests/test_formal_correspondence.py::test_core_matches_reference"],
        runner=RUNNER, report_format="pytest_json_report", expect_report_version=1,
        generator=GENERATOR, replay={"database": ".hypothesis", "seed": None},
    )])
    spec = manifest.checks["correspondence"]
    assert spec.kind is CheckKind.PROPERTY
    assert spec.evidence_kind() is ClaimCategory.PROPERTY
    assert spec.variant.generator.max_examples == 200
    assert spec.variant.replay.database == ".hypothesis"


def test_a_property_check_may_not_be_constructed_without_a_generator(project) -> None:
    """A property result whose settings were not pinned says nothing about what
    was sampled, which is the whole difference between PROPERTY and SCENARIO."""
    with pytest.raises(ManifestError) as caught:
        parse(project, [a_check(
            id="correspondence", kind="property", required_tests=["tests/t.py::t"],
            runner=RUNNER, report_format="pytest_json_report", expect_report_version=1,
        )])
    assert "generator" in str(caught.value), str(caught.value)


def test_the_lean_variant_parses_and_licenses_theorem_evidence(project) -> None:
    """The shape froze at checkpoint 1 and the runner arrived at checkpoint 3.

    The assertion is now the opposite of what it was: the kind parses, and the
    category it licenses is derived rather than declared. That move is the point of
    the checkpoint. A capability missing on one host is a BLOCKED raised by the
    adapter, which can see the toolchain and name it, rather than a parse refusal
    that told a reader their manifest was malformed and sent them looking for a
    typo that was not there.
    """
    manifest = parse(project, [a_check(
        id="ownership", kind="lean",
        challenge={"module": "Acceptance", "path": "formal/lean/Acceptance.lean"},
        theorems=["acceptance_ready_iff_all_required_pass"],
        profile=LeanProfile.UNREVIEWED, permitted_axioms=["propext"],
        toolchain={"tool": "lean", "version": "4.x.y"},
    )])
    spec = manifest.checks["ownership"]
    assert spec.kind is CheckKind.LEAN
    assert spec.evidence_kind() is ClaimCategory.THEOREM
    assert spec.obligations() == (
        TheoremObligation("acceptance_ready_iff_all_required_pass", "Acceptance"),
    )
    assert spec.required_scenarios == ()
    assert spec.variant.profile == LeanProfile.UNREVIEWED
    assert spec.variant.permitted_axioms == ("propext",)


def test_a_lean_check_that_declared_no_profile_gets_the_unreviewed_one(project) -> None:
    """The default the plan fixes, and the direction omission cannot invert.

    The weaker profile is what an agent-generated proof gets unless the owner
    explicitly selects the other, so a check that left the field out must not
    acquire the stronger profile's trust assumption by omitting it.
    """
    entry = a_check(
        id="ownership", kind="lean",
        challenge={"module": "Acceptance", "path": "formal/lean/Acceptance.lean"},
        theorems=["t1"], permitted_axioms=["propext"], toolchain={"tool": "lean"},
    )
    entry.pop("profile", None)
    entry["profile"] = LeanProfile.UNREVIEWED
    spec = parse(project, [entry]).checks["ownership"]
    assert spec.variant.profile == LeanProfile.UNREVIEWED


def test_the_lean_branch_may_not_carry_required_scenarios(project) -> None:
    """The branch refuses before the BLOCKED, so the shape is checked even for a
    kind this build cannot run."""
    with pytest.raises(ManifestError) as caught:
        parse(project, [a_check(
            id="ownership", kind="lean", required_scenarios=["one"],
            challenge={"module": "Acceptance", "path": "formal/lean/Acceptance.lean"},
            theorems=["t1"], profile=LeanProfile.UNREVIEWED,
            permitted_axioms=["propext"], toolchain={"tool": "lean"},
        )])
    message = str(caught.value)
    assert "required_scenarios" in message
    assert "BLOCKED" not in message, "the shape was wrong, so the capability is not the reason"


def test_the_tlc_variant_parses_and_licenses_finite_model_evidence(project) -> None:
    """The same move as the lean branch, for the model-checking one.

    FINITE_MODEL is derived from the variant and the obligations carry their
    bounds, because a run at different bounds is a different claim. Asserting the
    bounds survive parsing is what makes the receipt's own `bounds` field mean
    something later.
    """
    manifest = parse(project, [a_check(
        id="model", kind="tlc",
        model={"module": "OwnershipAcceptance", "path": "formal/tla/OwnershipAcceptance.tla"},
        config="formal/tla/OwnershipAcceptance.cfg",
        properties=["NoDoubleOwnership"], bounds={"owners": 2, "resources": 2},
        fingerprint={"constants_from_config": True, "checksum_states": False, "workers": 1},
        toolchain={"tool": "tlc"},
    )])
    spec = manifest.checks["model"]
    assert spec.kind is CheckKind.TLC
    assert spec.evidence_kind() is ClaimCategory.FINITE_MODEL
    assert spec.obligations() == (
        PropertyObligation("NoDoubleOwnership", (("owners", 2), ("resources", 2))),
    )
    assert spec.required_scenarios == ()
    assert spec.variant.bounds.as_pairs == (("owners", 2), ("resources", 2))
    assert spec.variant.fingerprint.workers == 1


def test_the_tlc_branch_may_not_omit_bounds_or_fingerprint(project) -> None:
    """A finite model result at an unrecorded configuration is the specific
    overstatement `claimkind.py` refuses, so the fields are required."""
    base = dict(
        id="model", kind="tlc",
        model={"module": "OwnershipAcceptance", "path": "formal/tla/OwnershipAcceptance.tla"},
        config="formal/tla/OwnershipAcceptance.cfg", properties=["NoDoubleOwnership"],
        bounds={"owners": 2},
        fingerprint={"constants_from_config": True, "checksum_states": False, "workers": 1},
        toolchain={"tool": "tlc"},
    )
    for missing in ("bounds", "fingerprint"):
        with pytest.raises(ManifestError) as caught:
            parse(project, [a_check(**{k: v for k, v in base.items() if k != missing})])
        assert missing in str(caught.value), str(caught.value)




def test_every_kind_maps_to_exactly_one_category() -> None:
    """`evidence_kind` is total over the union and has no fallback branch."""
    shared = dict(
        id="c", subject=SubjectRef(), claim_id="k", cwd=Path("."),
        timeout_seconds=60.0, artifact_name="r.json",
    )
    checks = [
        ScenarioCheck(**shared, command=("python",), required_scenarios=(CaseObligation("s"),)),
        PytestCheck(**shared, runner=PinnedRunner("{{python}}"), required_tests=("t",)),
        NodeTestCheck(**shared, runner=PinnedRunner("node"), required_tests=("t",)),
        PropertyCheck(**shared, runner=PinnedRunner("{{python}}"), required_tests=("t"),
                      generator=HypothesisSettings(10)),
        LeanCheck(**shared, challenge=ModuleRef("M", "M.lean"),
                  theorems=(TheoremObligation("t", "M"),), toolchain=ToolchainRef("lean")),
        TlcCheck(**shared, model=ModuleRef("M", "M.tla"), config="M.cfg",
                 properties=(PropertyObligation("P", (("owners", 2),)),),
                 bounds=DomainBounds((("owners", 2),)),
                 fingerprint=FingerprintSpec(True, False, 1), toolchain=ToolchainRef("tlc")),
    ]
    assert [evidence_kind(c).value for c in checks] == [
        "scenario", "scenario", "scenario", "property", "theorem_checking",
        "finite_model_checking",
    ]


def test_evidence_kind_refuses_something_that_is_not_a_check() -> None:
    """A default category would turn an upstream bug into a confident claim."""
    with pytest.raises(TypeError):
        evidence_kind(object())




def test_the_three_obligations_compare_by_value_and_are_hashable() -> None:
    """The policy's set difference needs this: a subtraction over unhashable or
    mutable obligations would be the thing that fails."""
    assert CaseObligation("a") == CaseObligation("a")
    assert TheoremObligation("t", "M") == TheoremObligation("t", "M")
    assert len({CaseObligation("a"), CaseObligation("a")}) == 1
    assert TheoremObligation("t", "M") != TheoremObligation("t", "N")


def test_property_bounds_are_sorted_so_two_spellings_are_one_obligation() -> None:
    written = PropertyObligation("P", (("resources", 2), ("owners", 2)))
    reversed_order = PropertyObligation("P", (("owners", 2), ("resources", 2)))
    assert written.bounds == reversed_order.bounds
    assert written == reversed_order


def test_the_obligation_union_has_no_common_id_attribute() -> None:
    """A base class with an `id` would put a string back where the union is, and
    `TheoremObligation("x").id` would then raise or lie."""
    for obligation in (CaseObligation("x"), TheoremObligation("x", "M")):
        assert not hasattr(obligation, "id")




@pytest.mark.parametrize("kind", sorted(k.value for k in CheckKind))
def test_every_declared_kind_is_a_schema_branch(kind: str) -> None:
    """All six are declared in the schema, including the two this build refuses
    to run. Declared and unavailable are separate facts."""
    raw = json.loads((ROOT / "schemas" / "manifest.v2.json").read_text(encoding="utf-8"))
    branches = {b["properties"]["kind"]["const"] for b in raw["$defs"].values()
                if isinstance(b, dict) and b.get("properties", {}).get("kind", {}).get("const")}
    assert kind in branches, f"{kind} has no schema branch"
    assert branches == set(k.value for k in CheckKind), (
        "the schema and the CheckKind enum disagree about which kinds are declared; "
        "one of them would accept a check the other cannot build"
    )


def test_an_unknown_kind_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(SchemaValidationError):
        validate("t", MANIFEST_V2, {"schema_version": 2, "checks": [a_check(
            id="c", kind="mystery", command=["x"], required_scenarios=["s"],
        )]})




def a_receipt(**fields) -> dict:
    receipt = {
        "schema_version": 2, "check_id": "behaviour", "claim_id": "the-claim",
        "run_id": "r1", "status": "PASS", "evidence_kind": "scenario",
        "subject": {"paths": ["app.py"], "digest": None},
        "policy_digest": "p", "source": {"inventory_digest": "s"},
        "verifier": {"module": "vkit.verifiers", "version": "0.1.0", "source_digest": "d"},
        "tool_versions": {}, "dependencies": [],
        "runtime": {"python_version": "3.13", "platform": "win32", "requires_os": "any"},
        "satisfied": [], "assumptions": [],
        "limits": {"timeout_seconds": 60, "workers": None, "seed": None},
        "artifacts": {"result": "result.json"},
        "trust_boundary": {
            "establishes": "this named sequence of calls produced this observed result",
            "does_not_establish": "anything about a sequence that was not run",
            "oracle_review": "tests/fixtures.py",
            "hash_limit": "a digest proves the bytes, not that the oracle is right",
        },
    }
    return {**receipt, **fields}


def test_a_receipt_with_a_trust_boundary_validates() -> None:
    validate("receipt", RECEIPT, a_receipt())


def test_a_receipt_without_a_trust_boundary_does_not_validate() -> None:
    """The limit on what a hash establishes has to be in the boundary where it
    cannot be forgotten, so a receipt missing it is un-readable rather than
    merely undocumented."""
    for missing in ("trust_boundary", "oracle_review", "hash_limit"):
        receipt = a_receipt()
        if missing == "trust_boundary":
            del receipt["trust_boundary"]
        else:
            del receipt["trust_boundary"][missing]
        with pytest.raises(SchemaValidationError):
            validate("receipt", RECEIPT, receipt)


def test_a_receipt_may_not_carry_a_category_the_schema_does_not_know() -> None:
    """The category vocabulary is the receipt's own, not an open string."""
    with pytest.raises(SchemaValidationError):
        validate("receipt", RECEIPT, a_receipt(evidence_kind="property_checking"))


def test_a_satisfied_theorem_records_the_axioms_it_was_audited_against() -> None:
    """The field has no default, so a satisfied theorem with no axiom audit is
    not representable."""
    receipt = a_receipt(evidence_kind="theorem_checking", satisfied=[{
        "kind": "theorem_satisfied",
        "obligation": {"kind": "theorem", "obligation": "ready_iff_all", "module": "Acceptance"},
        "axioms": ["propext"],
    }])
    validate("receipt", RECEIPT, receipt)
    del receipt["satisfied"][0]["axioms"]
    with pytest.raises(SchemaValidationError):
        validate("receipt", RECEIPT, receipt)




def test_a_v1_check_artifact_still_validates() -> None:
    """A scenario driver still writes v1 artifact bytes, and that stays true.

    The scenario variant delegates to a driver, so its artifact format is not
    deprecated by anything in Plan 10. Validating one here is what proves the
    per-schema version map did not quietly move this schema to 2.
    """
    artifact = {
        "schema_version": 1,
        "description": "totals CLI behavior observed by running the real command",
        "scenarios": [
            {"id": "empty-cart", "result": "PASS", "observation": "printed 0"},
            {"id": "mixed-sign", "result": "PASS", "observation": "printed 3"},
        ],
    }
    validate("artifact", CHECK_ARTIFACT, artifact)
    assert schema_version(CHECK_ARTIFACT) == 1


def test_a_v1_run_report_still_validates() -> None:
    """The run report describes the process, and Plan 10 does not change it.

    A recorded v1 report is the durable evidence an old run leaves behind, so it
    has to keep validating under a build whose manifest is at version 2.
    """
    report = {
        "schema_version": 1, "run_id": "r1", "check_id": "totals-behavior",
        "lifecycle": "terminal", "started_at": "t", "ended_at": "t",
        "outcome": {"result": "PASS", "scenarios": [
            {"id": "empty-cart", "result": "PASS", "observation": "printed 0"}]},
        "source": {"head": "abc123", "inventory_digest": "d", "dirty": False},
        "command": {"argv": ["python", "verify.py"], "cwd": "."},
    }
    validate("report", RUN_REPORT, report)
    assert schema_version(RUN_REPORT) == 1


def test_the_version_map_refuses_a_schema_nobody_declared() -> None:
    """A default would be a way of shipping a version nobody chose."""
    assert schema_version(MANIFEST) == 1
    assert schema_version(MANIFEST_V2) == 2
    assert schema_version(RECEIPT) == 2
    with pytest.raises(KeyError) as caught:
        schema_version("no-such-schema.v1.json")
    assert "no-such-schema.v1.json" in str(caught.value)


def test_a_v1_manifest_still_parses_and_is_a_scenario_check(project) -> None:
    """An old manifest is still evidence, and it is still SCENARIO.

    A v1 check has no `kind`, and reading it as a scenario driver is the only
    reading it has. That is what makes a legacy driver's claim to another category
    unreachable rather than merely unchecked.
    """
    legacy = {
        "schema_version": 1,
        "description": "a manifest written before kinds existed",
        "checks": [{
            "id": "legacy", "command": ["python", "verify.py"],
            "timeout_seconds": 60, "required_scenarios": ["one", "two"],
            "artifact": "result.json",
        }],
    }
    manifest = parse_manifest_bytes(
        json.dumps(legacy).encode("utf-8"),
        project=project, run_dir=project.runs_root, origin="legacy",
    )
    spec = manifest.checks["legacy"]
    assert spec.kind is CheckKind.SCENARIO
    assert spec.evidence_kind() is ClaimCategory.SCENARIO
    assert spec.obligations() == (CaseObligation("one"), CaseObligation("two"))
    assert spec.claim_id == "legacy", (
        "a v1 manifest declares no claim id, so the check id stands in; inventing "
        "a different one would be a claim the manifest never made"
    )


def test_a_v1_and_a_v2_manifest_produce_different_digests(project) -> None:
    """Every manifest digest changes with the version, which is intended.

    Design §5.2 says the canonical form gains `kind`, `subject` and `claim_id`, and
    that this changes every `Manifest.digest()`. It is the same event as a policy
    change: runs recorded under the old shape no longer describe what is in force.
    """
    legacy = parse_manifest_bytes(
        json.dumps({"schema_version": 1, "checks": [{
            "id": "legacy", "command": ["python", "verify.py"], "timeout_seconds": 60,
            "required_scenarios": ["one"], "artifact": "result.json",
        }]}).encode("utf-8"),
        project=project, run_dir=project.runs_root, origin="legacy",
    )
    modern = parse(project, [{
        "id": "legacy", "kind": "scenario", "command": ["python", "verify.py"],
        "timeout_seconds": 60, "required_scenarios": ["one"],
        "artifact": "result.json", "subject": {"paths": [], "digest": None},
        "claim_id": "legacy",
    }])
    assert legacy.digest() != modern.digest()