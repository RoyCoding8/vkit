"""The category cannot be declared, only derived, and a promotion is refused.

`docs/plans-10-evidence-contract-design.md` §7 asks the hard question: where does
`property` classification happen? Its answer is that a declared category is a
label the candidate controls, so there is no field to declare one in. These
tests take that from both directions.

**Promotion.** A manifest cannot carry `evidence_kind` at all; the schema refuses
the key before a variant exists. A `property` claim cannot be made without the
generator settings that make it meaningful. And the policy's own assertion is
cross-checked against the derivation, so a candidate who writes `property` in
their policy and `scenario` in their manifest is refused rather than believed.

**Anti-promotion by construction.** A check whose tests are Hypothesis tests but
whose kind is `pytest` is SCENARIO evidence, no matter that hypothesis is
importable in the child. That is the failure `plans/10-native-verifiers.md:44`
guards against and the reason the classification cannot come from a runner
detecting an import.

The refusal tested here is the public one: a policy check entry claiming
`theorem_checking` for a check the approved manifest declares as `scenario` goes
through `integration.policy.compare`, the same function every integration run
calls, and comes back as a finding rather than an exception.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkit.claimkind import ClaimCategory  # noqa: E402
from vkit.integration.policy import (  # noqa: E402
    PolicyError,
    compare,
    from_file,
)
from vkit.manifest import Manifest, ManifestError, parse_manifest_bytes  # noqa: E402
from vkit.paths import open_project  # noqa: E402

SCENARIO_CHECK = {
    "id": "ownership-theorems",
    "kind": "scenario",
    "description": "",
    "cwd": ".",
    "timeout_seconds": 60,
    "command": ["python", "verify.py"],
    "required_scenarios": ["empty-cart"],
    "artifact": "result.json",
    "subject": {"paths": [], "digest": None},
    "claim_id": "acceptance-decides-readiness",
}
PYTEST_FIELDS = {
    "timeout_seconds": 60,
    "artifact": "pytest-report.json",
    "subject": {"paths": [], "digest": None},
    "claim_id": "store-survives-restart",
    "required_tests": ["tests/test_formal_correspondence.py::test_core_matches_reference"],
    "runner": {"executable": "{{python}}", "base_argv": ["-m", "pytest"]},
    "report_format": "pytest_json_report",
    "expect_report_version": 1,
}
GENERATOR = {
    "max_examples": 200, "stateful_step_count": 25,
    "deadline": None, "suppress_health_check": ["too_slow"],
}


@pytest.fixture
def project(tmp_path: Path) -> object:
    (tmp_path / "verification").mkdir()
    (tmp_path / "verification" / "manifest.json").write_text("{}", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return open_project(tmp_path)


def manifest_of(project, checks: list[dict]) -> Manifest:
    return parse_manifest_bytes(
        json.dumps({"schema_version": 2, "checks": checks}).encode("utf-8"),
        project=project, run_dir=project.runs_root, origin="manifest.json",
    )


def a_policy(tmp_path: Path, checks: list[dict]) -> object:
    """A policy file on disk, read through the same `from_file` a CLI caller uses."""
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps({"schema_version": 1, "description": "the bar",
                    "required_checks": checks}),
        encoding="utf-8",
    )
    return from_file(open_project(tmp_path), path)


# ------------------------------------------------------------- no field exists


def test_a_manifest_may_not_declare_an_evidence_kind(project) -> None:
    """The candidate cannot set the category because there is nowhere to set it.

    This is the first of the three obstacles in design §7, and it is the one that
    does the work: no key named `evidence_kind` exists on any branch, so the
    attempt is refused by the schema rather than by a rule that could be
    forgotten.
    """
    with pytest.raises(ManifestError) as caught:
        manifest_of(project, [{**SCENARIO_CHECK, "evidence_kind": "property"}])
    assert "evidence_kind" in str(caught.value), str(caught.value)


def test_a_manifest_may_not_declare_theorem_checking_for_a_scenario_check(project) -> None:
    """Plan 10 §34: a legacy driver claiming `theorem_checking` must be refused.

    The refusal happens in `parse_manifest_bytes`, which is the public parser
    every manifest enters through, and the message comes from the scenario
    branch's `additionalProperties: false` rather than from a rule written for
    this case.
    """
    with pytest.raises(ManifestError) as caught:
        manifest_of(project, [{**SCENARIO_CHECK, "evidence_kind": "theorem_checking"}])
    message = str(caught.value)
    assert "evidence_kind" in message
    assert "rejected" in message, message


# ------------------------------------- a policy assertion is checked, not trusted


def test_a_policy_claiming_theorem_checking_for_a_scenario_check_is_refused(
    project, tmp_path: Path
) -> None:
    """The second of the three obstacles, through the public comparison.

    `policy.compare` is the function every integration run calls, and it is where
    the policy's declared category is cross-checked against `evidence_kind(spec)`
    for the candidate's manifest. The finding is named `evidence_kind_downgraded`
    rather than reusing `required_scenario_removed`, because nothing went missing
    here: the variant changed, and a reader grepping for the old name would not
    find the rule that matters.
    """
    candidate = manifest_of(project, [{**SCENARIO_CHECK,
                                       "required_scenarios": ["empty-cart"]}])
    policy = a_policy(tmp_path, [{
        "id": "ownership-theorems",
        "obligations": [{"kind": "case", "obligation": "empty-cart"}],
        "evidence_kind": "theorem_checking",
    }])

    findings = compare(candidate, policy, approved=None)
    kinds = {f.kind for f in findings}
    assert "evidence_kind_downgraded" in kinds, [f.to_json() for f in findings]
    finding = next(f for f in findings if f.kind == "evidence_kind_downgraded")
    assert finding.severity == "REJECT"
    assert "theorem_checking" in finding.detail
    assert "scenario" in finding.detail


def test_a_policy_cannot_promote_a_scenario_check_to_property(project, tmp_path: Path) -> None:
    """The same check refuses in the other direction.

    A candidate that wrote `property` in its own policy would be claiming a
    stronger category than its manifest's variant licenses. Refusing both
    directions is what makes the policy an assertion rather than a grant.
    """
    candidate = manifest_of(project, [dict(
        PYTEST_FIELDS, id="correspondence", kind="pytest",
    )])
    policy = a_policy(tmp_path, [{
        "id": "correspondence",
        "obligations": [{"kind": "case",
                         "obligation": "tests/test_formal_correspondence.py::test_core_matches_reference"}],
        "evidence_kind": "property",
    }])

    findings = compare(candidate, policy, approved=None)
    assert "evidence_kind_downgraded" in {f.kind for f in findings}, findings


def test_a_policy_may_not_name_a_category_the_vocabulary_does_not_have(
    project, tmp_path: Path
) -> None:
    """The refusal names the four categories rather than listing what was tried."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "schema_version": 1, "description": "the bar",
        "required_checks": [{"id": "c", "obligations": [],
                             "evidence_kind": "property_checking"}],
    }), encoding="utf-8")
    with pytest.raises(PolicyError) as caught:
        from_file(project, path)
    message = str(caught.value)
    assert "property_checking" in message
    for category in ClaimCategory:
        assert category.value in message


# ------------------------------------------------ the anti-promotion scenario


def test_a_pytest_run_over_hypothesis_tests_is_scenario_unless_the_manifest_says_otherwise(
    project,
) -> None:
    """Design §7's exact scenario: the category is not read off the package.

    The test file is a real Hypothesis test in this repository, and hypothesis is
    importable in the parent venv, so every wrong answer to "where does `property`
    classification happen" is available to be wrong here. A `pytest` kind parses
    to SCENARIO; only the same check re-declared `property` with a generator block
    reaches PROPERTY.
    """
    hypothesis_installed = importlib_available("hypothesis")
    assert hypothesis_installed, (
        "this scenario is only meaningful where the runner would really report a "
        "property profile; without hypothesis installed there is nothing to "
        "infer wrongly"
    )

    as_pytest = manifest_of(project, [dict(PYTEST_FIELDS, id="correspondence",
                                          kind="pytest")])
    assert as_pytest.checks["correspondence"].evidence_kind() is ClaimCategory.SCENARIO, (
        "a pytest check must not be read as property evidence merely because the "
        "package is installed"
    )

    as_property = manifest_of(project, [dict(
        PYTEST_FIELDS, id="correspondence", kind="property", generator=GENERATOR,
    )])
    assert as_property.checks["correspondence"].evidence_kind() is ClaimCategory.PROPERTY


def test_declaring_the_kind_changes_the_digest_so_old_evidence_stops_matching(
    project,
) -> None:
    """Design §7: changing the kind changes `Manifest.digest()`, so every run
    recorded before the change fails the policy identity comparison.

    This is the existing identity guard doing the work, not a new rule, and it is
    the honest account of what stops a mid-flight promotion: the runs recorded
    under the old kind no longer describe what is in force.
    """
    as_pytest = manifest_of(project, [dict(PYTEST_FIELDS, id="correspondence",
                                          kind="pytest")])
    as_property = manifest_of(project, [dict(
        PYTEST_FIELDS, id="correspondence", kind="property", generator=GENERATOR,
    )])
    assert as_pytest.digest() != as_property.digest()


def test_the_kind_is_inside_the_digest_so_a_rename_cannot_preserve_it(project) -> None:
    """A manifest that re-declares the same commands as a different kind is a
    different policy, and one that drops a theorem is a different policy again."""
    scenario = manifest_of(project, [{**SCENARIO_CHECK}])
    same_commands_other_claim = manifest_of(project, [
        {**SCENARIO_CHECK, "claim_id": "something-else"}])
    assert scenario.digest() != same_commands_other_claim.digest()


def importlib_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None