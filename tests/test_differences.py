from __future__ import annotations

from copy import deepcopy

import pytest

from vkit.certification import (
    DifferencePolicy,
    InputDifference,
    IntentRevision,
    PairedObservation,
    assess_differences,
)
from vkit.schemas import SchemaValidationError


def _intent(*, admissible=("x", "y"), outputs=None, domain_inputs=None):
    if outputs is None:
        outputs = {token: ["good", "other"] for token in admissible}
    domain_inputs = domain_inputs or tuple(sorted(set(admissible) | {"extra"}))
    domain = {
        "binding": "example::convert",
        "inputs": list(domain_inputs),
        "observations": ["bad", "good", "other"],
        "codec_sha256": "a" * 64,
        "observation_sha256": "b" * 64,
        "environment_sha256": "c" * 64,
    }
    contract = {
        "domain": domain,
        "admissible_inputs": list(admissible),
        "allowed_outputs": [
            {"input": token, "outputs": values}
            for token, values in outputs.items()
        ],
    }
    raw = {
        "schema_version": 1,
        "requirement": {
            "source": "requirements.md#convert",
            "statement": "Return one of the listed values for each input.",
        },
        "contract": contract,
        "artifacts": {
            "binding": {"path": "binding.json", "sha256": "d" * 64},
            "codec": {"path": "codec.json", "sha256": domain["codec_sha256"]},
            "observation": {
                "path": "observation.json",
                "sha256": domain["observation_sha256"],
            },
            "environment": {
                "path": "environment.json",
                "sha256": domain["environment_sha256"],
            },
        },
    }
    return IntentRevision.from_json(raw)


def _guard(guard_id, requirements):
    return {
        "id": guard_id,
        "requirements": [
            {"input": token, "outputs": values}
            for token, values in requirements.items()
        ],
    }


def _policy(intent, guards=()):
    return DifferencePolicy.from_json(
        {
            "schema_version": 1,
            "kind": "finite_differences",
            "intent_sha256": intent.digest,
            "guards": list(guards),
        },
        intent=intent,
    )


def _observations(intent, values=None, repetitions=2):
    if values is None:
        values = {token: ("good", "good") for token in intent.contract.admissible_inputs}
    return tuple(
        PairedObservation(token, (base,) * repetitions, (candidate,) * repetitions)
        for token, (base, candidate) in values.items()
    )


def test_policy_digest_and_json_are_canonical_across_guard_order():
    intent = _intent()
    first = _policy(intent, (
        _guard("z-case", {"y": ["other", "good"]}),
        _guard("a-case", {"x": ["other", "good"]}),
    ))
    reordered = _policy(intent, (
        _guard("a-case", {"x": ["good", "other"]}),
        _guard("z-case", {"y": ["good", "other"]}),
    ))

    assert first.digest == reordered.digest
    assert first.to_json() == {
        "schema_version": 1,
        "kind": "finite_differences",
        "intent_sha256": intent.digest,
        "guards": [
            {"id": "a-case", "requirements": [{"input": "x", "outputs": ["good", "other"]}]},
            {"id": "z-case", "requirements": [{"input": "y", "outputs": ["good", "other"]}]},
        ],
    }


def test_policy_rejects_wrong_intent_boolean_version_and_extra_fields():
    intent = _intent()
    raw = {
        "schema_version": 1,
        "kind": "finite_differences",
        "intent_sha256": intent.digest,
        "guards": [],
    }

    wrong_intent = deepcopy(raw)
    wrong_intent["intent_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="different intent"):
        DifferencePolicy.from_json(wrong_intent, intent=intent)

    boolean_version = deepcopy(raw)
    boolean_version["schema_version"] = True
    with pytest.raises((SchemaValidationError, ValueError)):
        DifferencePolicy.from_json(boolean_version, intent=intent)

    extra_field = {**raw, "authority": True}
    with pytest.raises(SchemaValidationError):
        DifferencePolicy.from_json(extra_field, intent=intent)


def test_policy_rejects_duplicate_ids_rows_and_tokens():
    intent = _intent()
    duplicate_ids = [
        _guard("same", {"x": ["good"]}),
        _guard("same", {"y": ["good"]}),
    ]
    with pytest.raises(ValueError, match="duplicate difference guard id"):
        _policy(intent, duplicate_ids)

    duplicate_rows = [
        {
            "id": "row-case",
            "requirements": [
                {"input": "x", "outputs": ["good"]},
                {"input": "x", "outputs": ["other"]},
            ],
        }
    ]
    with pytest.raises(ValueError, match="duplicate input rows"):
        _policy(intent, duplicate_rows)

    duplicate_outputs = [_guard("token-case", {"x": ["good", "good"]})]
    with pytest.raises(SchemaValidationError):
        _policy(intent, duplicate_outputs)


def test_policy_rejects_guard_id_with_final_newline():
    intent = _intent()

    with pytest.raises(SchemaValidationError):
        _policy(intent, [_guard("zero\n", {"x": ["good"]})])


def test_policy_guards_cannot_expand_domain_or_weaken_standing_outputs():
    intent = _intent()

    with pytest.raises(ValueError, match="expands the accepted input domain"):
        _policy(intent, [_guard("outside", {"extra": ["good"]})])

    with pytest.raises(ValueError, match="weakens standing target intent"):
        _policy(intent, [_guard("weak", {"x": ["bad"]})])


def test_compatible_overlapping_guards_intersect_their_requirements():
    intent = _intent(outputs={"x": ["good", "other"], "y": ["good"]})
    policy = _policy(intent, (
        _guard("first", {"x": ["good", "other"]}),
        _guard("second", {"x": ["other"]}),
    ))

    allowed = assess_differences(
        intent, policy,
        _observations(intent, {"x": ("good", "other"), "y": ("good", "good")}),
        repetitions=2,
    )
    rejected = assess_differences(
        intent, policy,
        _observations(intent, {"x": ("good", "good"), "y": ("good", "good")}),
        repetitions=2,
    )

    assert allowed.status == "observed_conformance"
    assert allowed.findings[0].kind == "guarded_requirement_met"
    assert allowed.findings[0].guard_ids == ("first", "second")
    assert rejected.status == "observed_violation"
    assert rejected.findings[0].reason == "candidate violates an overlapping guard requirement"


def test_conflicting_overlapping_guards_are_invalid_before_assessment():
    intent = _intent(outputs={"x": ["good", "other"], "y": ["good"]})

    with pytest.raises(ValueError, match="overlapping difference guards conflict"):
        _policy(intent, (
            _guard("first", {"x": ["good"]}),
            _guard("second", {"x": ["other"]}),
        ))


def test_unguarded_refactor_and_guarded_feature_conform():
    intent = _intent()
    unguarded = assess_differences(
        intent, _policy(intent), _observations(intent), repetitions=2
    )
    guarded = assess_differences(
        intent,
        _policy(intent, [_guard("feature", {"x": ["other"]})]),
        _observations(intent, {"x": ("good", "other"), "y": ("good", "good")}),
        repetitions=2,
    )

    assert unguarded.status == "observed_conformance"
    assert [finding.kind for finding in unguarded.findings] == ["preserved", "preserved"]
    assert guarded.status == "observed_conformance"
    assert [finding.kind for finding in guarded.findings] == [
        "guarded_requirement_met", "preserved"
    ]


def test_unguarded_allowed_but_changed_behavior_is_a_violation():
    intent = _intent()
    result = assess_differences(
        intent, _policy(intent),
        _observations(intent, {"x": ("good", "other"), "y": ("good", "good")}),
        repetitions=2,
    )

    assert result.status == "observed_violation"
    assert result.findings[0].kind == "violation"
    assert result.findings[0].reason == "unguarded difference is not a standing-intent repair"


def test_unsafe_base_repair_is_allowed_but_unchanged_unsafe_result_is_not():
    intent = _intent(outputs={"x": ["good"], "y": ["good"]})
    repaired = assess_differences(
        intent, _policy(intent),
        _observations(intent, {"x": ("bad", "good"), "y": ("good", "good")}),
        repetitions=2,
    )
    unchanged = assess_differences(
        intent, _policy(intent),
        _observations(intent, {"x": ("bad", "bad"), "y": ("good", "good")}),
        repetitions=2,
    )

    assert repaired.status == "observed_conformance"
    assert repaired.findings[0].kind == "standing_intent_repair"
    assert unchanged.status == "observed_violation"
    assert unchanged.findings[0].kind == "violation"
    assert unchanged.findings[0].reason == "candidate violates standing target intent"


def test_candidate_must_meet_both_standing_and_guarded_requirements():
    intent = _intent()
    policy = _policy(intent, [_guard("feature", {"x": ["other"]})])
    outside_standing = assess_differences(
        intent, policy,
        _observations(intent, {"x": ("good", "bad"), "y": ("good", "good")}),
        repetitions=2,
    )
    outside_guard = assess_differences(
        intent, policy,
        _observations(intent, {"x": ("good", "good"), "y": ("good", "good")}),
        repetitions=2,
    )

    assert outside_standing.status == "observed_violation"
    assert outside_standing.findings[0].reason == "candidate violates standing target intent"
    assert outside_guard.status == "observed_violation"
    assert outside_guard.findings[0].reason == "candidate violates an overlapping guard requirement"


@pytest.mark.parametrize(
    ("observation", "reason", "base_output", "candidate_output"),
    [
        (None, "missing_observation", None, None),
        (PairedObservation("x", ("good",), ("good", "good")), "wrong_repetition_count", None, None),
        (PairedObservation("x", ("good", "other"), ("good", "good")), "unstable_base_observations", None, None),
        (PairedObservation("x", ("good", "good"), ("good", "other")), "unstable_candidate_observations", None, None),
        (PairedObservation("x", ("good", "good"), ("unmodeled", "unmodeled")), "out_of_model_observation", "good", "unmodeled"),
        (PairedObservation("x", ("good", "good"), ("good", "good"), "timeout"), "timeout", None, None),
    ],
)
def test_incomplete_or_unstable_observations_are_unknown(
    observation, reason, base_output, candidate_output
):
    intent = _intent(admissible=("x",))
    observations = () if observation is None else (observation,)

    result = assess_differences(intent, _policy(intent), observations, repetitions=2)

    assert result.status == "unknown"
    assert result.coverage_complete is False
    assert result.findings == (
        InputDifference("x", "unknown", (), base_output, candidate_output, reason),
    )
    assert result.missing_inputs == ("x",)


def test_duplicate_observation_is_unknown_not_arbitrarily_selected():
    intent = _intent(admissible=("x",))
    sample = PairedObservation("x", ("good", "good"), ("good", "good"))

    result = assess_differences(intent, _policy(intent), (sample, sample), repetitions=2)

    assert result.status == "unknown"
    assert result.findings[0].reason == "duplicate_observations"
    assert result.missing_inputs == ("x",)


def test_nonselected_violation_is_visible_without_filling_selected_coverage():
    intent = _intent()
    observations = (
        PairedObservation("x", ("good", "good"), ("other", "other"), selected=False),
        PairedObservation("y", ("good", "good"), ("good", "good")),
    )

    result = assess_differences(intent, _policy(intent), observations, repetitions=2)

    assert result.status == "observed_violation"
    assert result.coverage_complete is False
    assert result.findings == (
        InputDifference("x", "unknown", (), None, None, "missing_observation"),
        InputDifference(
            "x", "violation", (), "good", "other",
            "unguarded difference is not a standing-intent repair",
        ),
        InputDifference("y", "preserved", (), "good", "good", "base and candidate observations are equal"),
    )
    assert result.missing_inputs == ("x",)


def test_nonselected_conforming_observation_cannot_complete_coverage():
    intent = _intent(admissible=("x",))
    observation = PairedObservation(
        "x", ("good", "good"), ("good", "good"), selected=False
    )

    result = assess_differences(intent, _policy(intent), (observation,), repetitions=2)

    assert result.status == "unknown"
    assert result.coverage_complete is False
    assert result.findings == (
        InputDifference("x", "unknown", (), None, None, "missing_observation"),
    )
    assert result.missing_inputs == ("x",)


@pytest.mark.parametrize(
    ("observation", "reason", "base_output", "candidate_output"),
    [
        (
            PairedObservation("x", ("good", "good"), ("good", "other"), selected=False),
            "unstable_candidate_observations", None, None,
        ),
        (
            PairedObservation("x", ("good", "good"), ("good", "good"), "timeout", False),
            "timeout", None, None,
        ),
        (
            PairedObservation("x", ("good", "good"), ("unmodeled", "unmodeled"), selected=False),
            "out_of_model_observation", "good", "unmodeled",
        ),
    ],
)
def test_inconclusive_nonselected_sample_is_unknown_without_losing_coverage(
    observation, reason, base_output, candidate_output
):
    intent = _intent(admissible=("x",))
    selected = PairedObservation("x", ("good", "good"), ("good", "good"))

    result = assess_differences(
        intent, _policy(intent), (selected, observation), repetitions=2
    )

    assert result.status == "unknown"
    assert result.coverage_complete is True
    assert result.findings == (
        InputDifference("x", "preserved", (), "good", "good", "base and candidate observations are equal"),
        InputDifference("x", "unknown", (), base_output, candidate_output, reason),
    )
    assert result.missing_inputs == ()


def test_nonselected_unknown_does_not_mask_known_violation():
    intent = _intent(admissible=("x",))
    selected = PairedObservation("x", ("good", "good"), ("other", "other"))
    nonselected = PairedObservation(
        "x", ("good", "good"), ("good", "good"), "timeout", selected=False
    )

    result = assess_differences(
        intent, _policy(intent), (selected, nonselected), repetitions=2
    )

    assert result.status == "observed_violation"
    assert result.coverage_complete is True
    assert result.findings == (
        InputDifference(
            "x", "violation", (), "good", "other",
            "unguarded difference is not a standing-intent repair",
        ),
        InputDifference("x", "unknown", (), None, None, "timeout"),
    )
    assert result.missing_inputs == ()


def test_definite_violation_remains_visible_with_incomplete_coverage():
    intent = _intent()
    observations = (
        PairedObservation("x", ("good", "good"), ("other", "other")),
    )

    result = assess_differences(intent, _policy(intent), observations, repetitions=2)

    assert result.status == "observed_violation"
    assert result.coverage_complete is False
    assert [finding.kind for finding in result.findings] == ["violation", "unknown"]
    assert result.missing_inputs == ("y",)


def test_unknown_extra_input_does_not_expand_or_replace_coverage():
    intent = _intent(admissible=("x",))
    observations = (
        PairedObservation("x", ("good", "good"), ("good", "good")),
        PairedObservation("extra", ("bad", "bad"), ("bad", "bad")),
    )

    result = assess_differences(intent, _policy(intent), observations, repetitions=2)

    assert result.status == "observed_conformance"
    assert result.coverage_complete is True
    assert [finding.input for finding in result.findings] == ["x"]
    assert result.missing_inputs == ()


def test_empty_admissible_domain_is_unknown_not_vacuous_conformance():
    intent = _intent(admissible=(), outputs={})

    result = assess_differences(intent, _policy(intent), (), repetitions=1)

    assert result.status == "unknown"
    assert result.coverage_complete is False
    assert result.findings == ()
    assert result.missing_inputs == ()


def test_assessment_serialization_marks_finite_observation_scope_and_no_authority():
    intent = _intent(admissible=("x",))
    result = assess_differences(
        intent, _policy(intent), _observations(intent), repetitions=2
    )

    assert result.to_json() == {
        "scope": "finite_paired_observations",
        "source_proof": False,
        "approval_authority": False,
        "status": "observed_conformance",
        "coverage_complete": True,
        "findings": [{
            "input": "x",
            "kind": "preserved",
            "guard_ids": [],
            "base_output": "good",
            "candidate_output": "good",
            "reason": "base and candidate observations are equal",
        }],
        "missing_inputs": [],
    }


@pytest.mark.parametrize("repetitions", [0, -1, True, 1.5])
def test_assessment_requires_positive_integer_repetitions(repetitions):
    intent = _intent(admissible=("x",))

    with pytest.raises(ValueError, match="positive integer"):
        assess_differences(intent, _policy(intent), (), repetitions=repetitions)
