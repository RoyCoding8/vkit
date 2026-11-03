from __future__ import annotations

import pytest

from vkit.certification import (
    DifferencePolicy,
    IntentRevision,
    PairedObservation,
    assess_differences,
)


def _intent(*, admissible=("x",), allowed=None):
    if allowed is None:
        allowed = {token: ["good", "other"] for token in admissible}
    domain = {
        "binding": "example::convert",
        "inputs": sorted(set(admissible) | {"extra"}),
        "observations": ["bad", "good", "other", "alien"],
        "codec_sha256": "a" * 64,
        "observation_sha256": "b" * 64,
        "environment_sha256": "c" * 64,
    }
    return IntentRevision.from_json({
        "schema_version": 1,
        "requirement": {
            "source": "requirements.md#convert",
            "statement": "Return an allowed observation for each accepted input.",
        },
        "contract": {
            "domain": domain,
            "admissible_inputs": list(admissible),
            "allowed_outputs": [
                {"input": token, "outputs": outputs}
                for token, outputs in allowed.items()
            ],
        },
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
    })


def _policy(intent, guards=()):
    return DifferencePolicy.from_json({
        "schema_version": 1,
        "kind": "finite_differences",
        "intent_sha256": intent.digest,
        "guards": list(guards),
    }, intent=intent)


def _guard(guard_id, input_token, outputs):
    return {
        "id": guard_id,
        "requirements": [{"input": input_token, "outputs": list(outputs)}],
    }


def _observation(input_token, base, candidate, *, selected=True, failure=None):
    return PairedObservation(
        input_token, tuple(base), tuple(candidate), failure=failure, selected=selected
    )


def test_selected_equal_pair_is_conformance_with_complete_finite_coverage():
    intent = _intent()
    result = assess_differences(
        intent,
        _policy(intent),
        (_observation("x", ("good", "good"), ("good", "good")),),
        repetitions=2,
    )

    assert result.status == "observed_conformance"
    assert result.coverage_complete is True
    assert [(item.input, item.kind) for item in result.findings] == [("x", "preserved")]
    assert result.missing_inputs == ()


def test_shrink_observation_cannot_fill_selected_coverage():
    intent = _intent()
    result = assess_differences(
        intent,
        _policy(intent),
        (_observation("x", ("good", "good"), ("good", "good"), selected=False),),
        repetitions=2,
    )

    assert result.status == "unknown"
    assert result.coverage_complete is False
    assert [(item.input, item.kind, item.reason) for item in result.findings] == [
        ("x", "unknown", "missing_observation")
    ]
    assert result.missing_inputs == ("x",)


def test_invalid_shrink_keeps_complete_coverage_but_blocks_conformance():
    intent = _intent()
    result = assess_differences(
        intent,
        _policy(intent),
        (
            _observation("x", ("good", "good"), ("good", "good")),
            _observation(
                "x", ("good", "good"), ("good", "other"), selected=False
            ),
        ),
        repetitions=2,
    )

    assert result.status == "unknown"
    assert result.coverage_complete is True
    assert [item.kind for item in result.findings] == ["preserved", "unknown"]
    assert result.findings[1].reason == "unstable_candidate_observations"
    assert result.missing_inputs == ()


def test_duplicate_identical_selected_guard_observations_do_not_fill_coverage():
    intent = _intent()
    guarded = _policy(intent, (_guard("required", "x", ("other",)),))
    sample = _observation("x", ("good", "good"), ("other", "other"))
    result = assess_differences(
        intent, guarded, (sample, sample), repetitions=2
    )

    assert result.status == "unknown"
    assert result.coverage_complete is False
    assert result.missing_inputs == ("x",)
    assert [(item.kind, item.reason) for item in result.findings] == [
        ("unknown", "duplicate_observations")
    ]


def test_known_violation_survives_missing_input_and_unknown_shrink():
    intent = _intent(
        admissible=("x", "y"),
        allowed={"x": ["good", "other"], "y": ["good"]},
    )
    result = assess_differences(
        intent,
        _policy(intent),
        (
            _observation("x", ("good", "good"), ("other", "other")),
            _observation(
                "y", ("good", "good"), ("good", "other"), selected=False
            ),
        ),
        repetitions=2,
    )

    assert result.status == "observed_violation"
    assert result.coverage_complete is False
    assert result.missing_inputs == ("y",)
    assert [(item.input, item.kind) for item in result.findings] == [
        ("x", "violation"),
        ("y", "unknown"),
        ("y", "unknown"),
    ]


def test_overlapping_guards_use_the_intersection_and_reject_empty_intersections():
    intent = _intent()
    guarded = _policy(intent, (
        _guard("wide", "x", ("good", "other")),
        _guard("narrow", "x", ("other",)),
    ))
    rejected = assess_differences(
        intent,
        guarded,
        (_observation("x", ("good", "good"), ("good", "good")),),
        repetitions=2,
    )

    assert rejected.status == "observed_violation"
    assert rejected.findings[0].guard_ids == ("narrow", "wide")
    assert rejected.findings[0].reason == "candidate violates an overlapping guard requirement"

    with pytest.raises(ValueError, match="overlapping difference guards conflict"):
        _policy(intent, (
            _guard("left", "x", ("good",)),
            _guard("right", "x", ("other",)),
        ))


def test_standing_intent_and_unguarded_equality_remain_required():
    standing = _intent(allowed={"x": ["good", "other"]})
    outside = assess_differences(
        standing,
        _policy(standing),
        (_observation("x", ("good", "good"), ("alien", "alien")),),
        repetitions=2,
    )
    changed = assess_differences(
        standing,
        _policy(standing),
        (_observation("x", ("good", "good"), ("other", "other")),),
        repetitions=2,
    )

    assert outside.status == "observed_violation"
    assert outside.findings[0].reason == "candidate violates standing target intent"
    assert changed.status == "observed_violation"
    assert changed.findings[0].reason == "unguarded difference is not a standing-intent repair"


def test_standing_repair_is_accepted_but_bad_candidate_is_not():
    intent = _intent(allowed={"x": ["good"]})
    repaired = assess_differences(
        intent,
        _policy(intent),
        (_observation("x", ("bad", "bad"), ("good", "good")),),
        repetitions=2,
    )
    bad_candidate = assess_differences(
        intent,
        _policy(intent),
        (_observation("x", ("bad", "bad"), ("bad", "bad")),),
        repetitions=2,
    )

    assert repaired.status == "observed_conformance"
    assert repaired.coverage_complete is True
    assert repaired.findings[0].kind == "standing_intent_repair"
    assert bad_candidate.status == "observed_violation"
    assert bad_candidate.findings[0].kind == "violation"


def test_empty_domain_is_unknown_and_assessment_has_no_authority():
    intent = _intent(admissible=(), allowed={})
    result = assess_differences(intent, _policy(intent), (), repetitions=2)

    assert result.to_json() == {
        "scope": "finite_paired_observations",
        "source_proof": False,
        "approval_authority": False,
        "status": "unknown",
        "coverage_complete": False,
        "findings": [],
        "missing_inputs": [],
    }
