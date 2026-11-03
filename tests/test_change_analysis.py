from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import git
from test_paired import _git_output, _json_bytes, _repository
from vkit import change_analysis as change_analysis_module
from vkit.change_analysis import run_change_analysis
from vkit.certification import DifferencePolicy, IntentRevision, assess_differences
from vkit.intent import _prepare_change_request
from vkit.inputs import canonical_digest
from vkit.paths import open_project
from vkit.schemas import SchemaValidationError, validate


def _policy(intent_sha256: str, outputs: tuple[str, ...] = ("1",)) -> dict:
    return {
        "schema_version": 1,
        "kind": "finite_differences",
        "intent_sha256": intent_sha256,
        "guards": [{
            "id": "zero-case",
            "requirements": [{"input": "0", "outputs": outputs}],
        }],
    }


def _candidate_commit(
    root: Path,
    base_oid: str,
    source: str,
    policy_bytes: bytes | None = None,
    policy_path: str | None = None,
) -> str:
    git(root, "checkout", "--detach", base_oid)
    (root / "candidate.py").write_text(source, encoding="utf-8")
    git(root, "add", "candidate.py")
    if policy_bytes is not None and policy_path is not None:
        retained_policy = root / policy_path
        retained_policy.parent.mkdir(parents=True, exist_ok=True)
        retained_policy.write_bytes(policy_bytes)
        git(root, "add", policy_path)
    git(root, "commit", "-qm", "candidate for analysis")
    return _git_output(root, "rev-parse", "HEAD")


def _with_target_policy(
    root: Path,
    target_oid: str,
    request: dict,
    policy_raw: dict,
    *,
    candidate_source: str,
    candidate_policy_bytes: bytes | None = None,
) -> tuple[str, dict]:
    git(root, "checkout", "--detach", target_oid)
    path = root / "verification/change-intents/differences" / f"{request['intent_sha256']}.v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(policy_raw))
    git(root, "add", str(path.relative_to(root)))
    git(root, "commit", "-qm", "target protects finite differences")
    policy_target = _git_output(root, "rev-parse", "HEAD")
    candidate = _candidate_commit(
        root, policy_target, candidate_source, policy_bytes=candidate_policy_bytes
    )
    updated = dict(request)
    updated["base_oid"] = policy_target
    updated["candidate_oid"] = candidate
    return policy_target, updated


def _run(
    tmp_path,
    *,
    candidate_source: str,
    outputs: dict[str, list[str]] | None = None,
    policy: dict | None = None,
    input_tokens: tuple[str, ...] = ("0", "1"),
    repetitions: int = 2,
    max_shrink_trials: int = 0,
    claim_outputs: dict[str, list[str]] | None = None,
    timeout_seconds: float = 10,
    semantic_changes: dict[str, object] | None = None,
):
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs=outputs or {"0": ["0", "1"], "1": ["1", "2"]},
        semantic_changes=semantic_changes,
    )
    if policy is not None:
        policy_target, request = _with_target_policy(
            root,
            target_oid,
            request,
            _policy(request["intent_sha256"], policy),
            candidate_source=candidate_source,
        )
        target_oid = policy_target
    else:
        candidate = _candidate_commit(root, target_oid, candidate_source)
        request = dict(request)
        request["candidate_oid"] = candidate
    if claim_outputs is not None:
        request = json.loads(json.dumps(request))
        request["claim"]["contract"]["allowed_outputs"] = [
            {"input": token, "outputs": values}
            for token, values in sorted(claim_outputs.items())
        ]
    result = run_change_analysis(
        open_project(root),
        request,
        trusted_target_oid=target_oid,
        output_dir=tmp_path / "analysis",
        input_tokens=input_tokens,
        repetitions=repetitions,
        timeout_seconds=timeout_seconds,
        max_shrink_trials=max_shrink_trials,
    )
    return root, target_oid, request, result


def _weaker_claim_repository(tmp_path):
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    git(root, "checkout", "--detach", target_oid)
    accepted_path = root / "verification/change-intents/accepted.v1.json"
    raw_path = root / "verification/change-intents" / f"{request['intent_sha256']}.v1.json"
    raw = json.loads(raw_path.read_bytes())
    raw["contract"]["admissible_inputs"] = ["0"]
    raw["contract"]["allowed_outputs"] = [
        row for row in raw["contract"]["allowed_outputs"] if row["input"] == "0"
    ]
    revision = IntentRevision.from_json(raw)
    raw_path = root / "verification/change-intents" / f"{revision.digest}.v1.json"
    raw_path.write_bytes(_json_bytes(revision.to_json()))
    accepted_path.write_bytes(_json_bytes({"schema_version": 1, "accepted": [revision.digest]}))
    git(root, "add", str(raw_path.relative_to(root)), str(accepted_path.relative_to(root)))
    git(root, "commit", "-qm", "target narrows admissible intent inputs")
    trusted_target = _git_output(root, "rev-parse", "HEAD")
    candidate = _candidate_commit(root, trusted_target, "def convert(value):\n    return value\n")
    request["base_oid"] = trusted_target
    request["candidate_oid"] = candidate
    request["intent_sha256"] = revision.digest
    claim = revision.contract.to_json()
    claim["admissible_inputs"] = ["0", "1"]
    claim["allowed_outputs"].append({"input": "1", "outputs": ["1", "2"]})
    request["claim"]["contract"] = claim
    return root, trusted_target, request


def test_guarded_change_and_unguarded_equality_use_target_policy(tmp_path):
    root, target_oid, request, result = _run(
        tmp_path,
        candidate_source="def convert(value):\n    return 1 if value == 0 else value\n",
        policy=["1"],
    )

    assert result.report["status"] == "observed_conformance"
    assert result.report["scope"] == "sampled_change_analysis"
    assert result.report["source_proof"] is False
    assert result.report["approval_authority"] is False
    assert result.policy_identity["default"] is False
    assert result.policy_identity["target_tree_oid"] == result.bound.target_tree_oid
    policy_artifact = tmp_path / "analysis" / "policy" / "difference-policy.json"
    assert hashlib.sha256(policy_artifact.read_bytes()).hexdigest() == result.policy_identity["raw_sha256"]
    assert result.report["policy"]["artifact"]["path"] == "policy/difference-policy.json"
    assert result.assessment.to_json()["findings"] == [
        {
            "input": "0",
            "kind": "guarded_requirement_met",
            "guard_ids": ["zero-case"],
            "base_output": "0",
            "candidate_output": "1",
            "reason": "candidate meets the intersected guard requirements",
        },
        {
            "input": "1",
            "kind": "preserved",
            "guard_ids": [],
            "base_output": "1",
            "candidate_output": "1",
            "reason": "base and candidate observations are equal",
        },
    ]
    retained = json.loads((tmp_path / "analysis" / "change-analysis.json").read_bytes())
    assert retained == result.report
    assert request["base_oid"] == target_oid
    assert result.report["paired_report"]["sha256"] == hashlib.sha256(
        (tmp_path / "analysis" / "paired-report.json").read_bytes()
    ).hexdigest()


def test_weaker_claim_flags_do_not_hide_standing_intent_violation(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source="def convert(value):\n    return 1 if value == 0 else value\n",
        outputs={"0": ["0"], "1": ["1", "2"]},
        input_tokens=("0",),
        claim_outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )

    paired_sample = result.paired_execution.report["samples"][0]
    assert paired_sample["candidate_within_contract"] is True
    assert result.assessment.status == "observed_violation"
    assert result.assessment.findings[0].reason == "candidate violates standing target intent"


def test_unsafe_base_repair_satisfies_standing_target_requirement(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source="def convert(value):\n    return 1 if value == 0 else value\n",
        outputs={"0": ["1"], "1": ["1", "2"]},
        claim_outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )

    assert result.assessment.status == "observed_conformance"
    assert result.assessment.findings[0].kind == "standing_intent_repair"


def test_unguarded_change_is_a_violation_even_when_both_outputs_are_allowed(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source=(
            "def convert(value):\n"
            "    return 1 if value == 0 else value + 1\n"
        ),
        policy=["1"],
    )

    assert result.assessment.status == "observed_violation"
    finding = result.assessment.findings[1]
    assert finding.input == "1"
    assert finding.base_output == "1"
    assert finding.candidate_output == "2"
    assert finding.reason == "unguarded difference is not a standing-intent repair"


def test_overlapping_guards_use_their_intersection(tmp_path):
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    policy = {
        "schema_version": 1,
        "kind": "finite_differences",
        "intent_sha256": request["intent_sha256"],
        "guards": [
            {"id": "first", "requirements": [{"input": "0", "outputs": ["0", "1"]}]},
            {"id": "second", "requirements": [{"input": "0", "outputs": ["1"]}]},
        ],
    }
    target_oid, request = _with_target_policy(
        root,
        target_oid,
        request,
        policy,
        candidate_source="def convert(value):\n    return 1 if value == 0 else value\n",
    )
    result = run_change_analysis(
        open_project(root), request, trusted_target_oid=target_oid,
        output_dir=tmp_path / "analysis", input_tokens=None, repetitions=1,
    )

    assert result.assessment.status == "observed_conformance"
    assert result.assessment.findings[0].guard_ids == ("first", "second")
    assert result.assessment.findings[0].candidate_output == "1"


def test_selected_subset_and_timeout_remain_unknown(tmp_path):
    _, _, _, partial = _run(
        tmp_path / "partial",
        candidate_source="def convert(value):\n    return value\n",
        input_tokens=("1",),
        repetitions=1,
    )
    assert partial.assessment.status == "unknown"
    assert partial.assessment.missing_inputs == ("0",)

    _, _, _, timed_out = _run(
        tmp_path / "timeout",
        candidate_source=(
            "import time\n"
            "def convert(value):\n"
            "    time.sleep(0.1)\n"
            "    return value\n"
        ),
        input_tokens=("0",),
        repetitions=1,
        timeout_seconds=0.001,
    )
    assert timed_out.paired_execution.status in {"unknown", "infrastructure_failure"}
    assert timed_out.assessment.status == "unknown"


def test_default_selection_uses_accepted_intent_domain_and_explicit_extra_fails_early(tmp_path):
    root, target_oid, request = _weaker_claim_repository(tmp_path / "default")
    result = run_change_analysis(
        open_project(root), request, trusted_target_oid=target_oid,
        output_dir=tmp_path / "default-analysis", repetitions=1,
    )
    assert result.paired_execution.report["input_selection"]["selected"] == ["0"]
    assert result.paired_execution.report["input_selection"]["admissible_count"] == 2
    assert result.assessment.coverage_complete is True
    assert result.assessment.status == "observed_conformance"
    assert result.policy_identity["default"] is True
    retained_policy = json.loads(
        (tmp_path / "default-analysis" / "policy" / "difference-policy.json").read_bytes()
    )
    assert retained_policy["guards"] == []
    assert result.policy_identity["artifact"]["sha256"] == result.policy_identity["raw_sha256"]

    root, target_oid, request = _weaker_claim_repository(tmp_path / "explicit")
    with pytest.raises(ValueError, match="accepted intent domain"):
        run_change_analysis(
            open_project(root), request, trusted_target_oid=target_oid,
            output_dir=tmp_path / "explicit-analysis", input_tokens=("1",), repetitions=1,
        )
    assert not (tmp_path / "explicit-analysis").exists()


def test_known_violation_survives_a_later_timeout(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source=(
            "import time\n"
            "def convert(value):\n"
            "    if value == 1:\n"
            "        time.sleep(2)\n"
            "    return 1 if value == 0 else value\n"
        ),
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
        input_tokens=("0", "1"),
        repetitions=1,
        timeout_seconds=0.5,
    )

    assert result.paired_execution.status == "unknown"
    assert result.assessment.status == "observed_violation"
    assert result.assessment.findings[0].reason == "unguarded difference is not a standing-intent repair"


def test_failed_process_stdout_does_not_erase_earlier_known_violation(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source=(
            "import os\n"
            "def convert(value):\n"
            "    if value == 0:\n"
            "        return 1\n"
            "    os.write(1, b'2\\n')\n"
            "    raise RuntimeError('after direct stdout write')\n"
        ),
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
        input_tokens=("0", "1"),
        repetitions=2,
    )

    assert result.paired_execution.status == "infrastructure_failure"
    assert result.assessment.status == "observed_violation"
    assert result.assessment.findings[0].input == "0"
    assert result.assessment.findings[0].kind == "violation"
    failed_output = result.paired_execution.report["samples"][1]["runs"][0]["candidate"]
    assert failed_output["outcome"] == "incomplete"
    assert failed_output["value"] is None
    assert failed_output["stdout"] is not None


def test_failed_process_stdout_alone_remains_unknown(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source=(
            "import os\n"
            "def convert(value):\n"
            "    os.write(1, b'2\\n')\n"
            "    raise RuntimeError('after direct stdout write')\n"
        ),
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
        input_tokens=("1",),
        repetitions=1,
    )

    assert result.paired_execution.status == "infrastructure_failure"
    assert result.assessment.status == "unknown"
    assert any(
        finding.input == "1" and finding.kind == "unknown"
        and finding.reason == "incomplete_output"
        for finding in result.assessment.findings
    )


@pytest.mark.parametrize("drift", ["deleted", "newline"])
def test_candidate_must_preserve_target_policy_bytes(tmp_path, drift):
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    target_policy = _policy(request["intent_sha256"])
    path = root / "verification/change-intents/differences" / f"{request['intent_sha256']}.v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _json_bytes(target_policy)
    path.write_bytes(raw)
    git(root, "add", str(path.relative_to(root)))
    git(root, "commit", "-qm", "target policy")
    target_oid = _git_output(root, "rev-parse", "HEAD")
    candidate_source = "def convert(value):\n    return value\n"
    git(root, "checkout", "--detach", target_oid)
    if drift == "deleted":
        path.unlink()
        git(root, "add", "-u", str(path.relative_to(root)))
    else:
        path.write_bytes(raw + b"\n")
        git(root, "add", str(path.relative_to(root)))
    (root / "candidate.py").write_text(candidate_source, encoding="utf-8")
    git(root, "add", "candidate.py")
    git(root, "commit", "-qm", "candidate drifts from target policy")
    request["base_oid"] = target_oid
    request["candidate_oid"] = _git_output(root, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="candidate difference policy differs"):
        run_change_analysis(
            open_project(root), request, trusted_target_oid=target_oid,
            output_dir=tmp_path / "analysis", input_tokens=("0",), repetitions=1,
        )
    assert not (tmp_path / "analysis").exists()


def test_candidate_cannot_add_policy_when_target_uses_default(tmp_path):
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    added = _json_bytes(_policy(request["intent_sha256"]))
    candidate = _candidate_commit(
        root,
        request["candidate_oid"],
        "def convert(value):\n    return 1\n",
        added,
        f"verification/change-intents/differences/{request['intent_sha256']}.v1.json",
    )
    request["candidate_oid"] = candidate

    with pytest.raises(ValueError, match="candidate adds a difference policy"):
        run_change_analysis(
            open_project(root), request, trusted_target_oid=target_oid,
            output_dir=tmp_path / "analysis", input_tokens=("0",), repetitions=1,
        )
    assert not (tmp_path / "analysis").exists()


def test_difference_policy_schema_rejects_newline_digest_and_extra_fields(tmp_path):
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    bound, _, _ = _prepare_change_request(
        open_project(root), request, trusted_target_oid=target_oid
    )
    raw = _policy(bound.intent.digest)
    raw["intent_sha256"] += "\n"
    with pytest.raises((ValueError, SchemaValidationError)):
        DifferencePolicy.from_json(raw, intent=bound.intent)
    raw = _policy(bound.intent.digest)
    raw["candidate_may_authorize"] = True
    with pytest.raises(SchemaValidationError):
        DifferencePolicy.from_json(raw, intent=bound.intent)


def test_partial_selected_coverage_is_not_filled_by_shrink_sample(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source=(
            "def convert(value):\n"
            "    return 3 if value == 1 else value\n"
        ),
        outputs={"0": ["0"], "1": ["1", "2"]},
        input_tokens=("1",),
        repetitions=2,
        max_shrink_trials=1,
    )

    assert [sample["purpose"] for sample in result.paired_execution.report["samples"]] == [
        "selected", "shrink"
    ]
    assert result.assessment.status == "observed_violation"
    assert result.assessment.coverage_complete is False
    assert result.assessment.missing_inputs == ("0",)


def test_infrastructure_failure_blocks_conformance(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source="def convert(value):\n    return True\n",
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
        input_tokens=("0",),
        repetitions=1,
    )

    assert result.paired_execution.status == "infrastructure_failure"
    assert result.assessment.status == "unknown"
    assert result.report["status"] == "unknown"


def test_unsupported_semantic_declaration_returns_unknown_analysis(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source="def convert(value):\n    return value\n",
        input_tokens=("0",),
        repetitions=1,
        semantic_changes={"codec": {"schema_version": 1, "kind": "unsupported_codec"}},
    )

    assert result.paired_execution.status == "unknown"
    assert result.paired_execution.binding is None
    assert result.assessment.status == "unknown"
    assert result.report["status"] == "unknown"


def test_analysis_schema_binds_status_and_policy_path(tmp_path):
    _, _, _, result = _run(
        tmp_path,
        candidate_source="def convert(value):\n    return value\n",
        input_tokens=("0", "1"),
        repetitions=1,
    )

    mismatched_status = copy.deepcopy(result.report)
    mismatched_status["assessment"]["status"] = "unknown"
    with pytest.raises(SchemaValidationError):
        validate("change analysis report", change_analysis_module._ANALYSIS_SCHEMA,
                 mismatched_status)

    escaped_policy_path = copy.deepcopy(result.report)
    escaped_policy_path["policy"]["path"] = "../../outside.json"
    with pytest.raises(SchemaValidationError):
        validate("change analysis report", change_analysis_module._ANALYSIS_SCHEMA,
                 escaped_policy_path)


def test_report_import_ignores_claim_flags_and_checks_stdout_input_and_binding(tmp_path):
    root, target_oid, request, result = _run(
        tmp_path,
        candidate_source="def convert(value):\n    return 1 if value == 0 else value\n",
        policy=["1"],
        input_tokens=("0", "1"),
    )
    bound, target, _ = _prepare_change_request(
        open_project(root), request, trusted_target_oid=target_oid
    )
    report_path = tmp_path / "analysis" / "paired-report.json"
    report = json.loads(report_path.read_bytes())
    report["samples"][0]["candidate_within_contract"] = False
    forged_flags = _json_bytes(report)
    observations = change_analysis_module._report_observations(
        tmp_path / "analysis",
        forged_flags,
        bound=bound,
        target=target,
        expected_binding=result.paired_execution.binding,
        expected_selected=("0", "1"),
        repetitions=2,
    )
    assert assess_differences(
        bound.intent, result.policy, observations, repetitions=2
    ).status == "observed_conformance"

    swapped_identity = json.loads(report_path.read_bytes())
    swapped_identity["identities"]["runner"] = swapped_identity["identities"]["runtime"]
    with pytest.raises(ValueError, match="runner identity differs"):
        change_analysis_module._report_observations(
            tmp_path / "analysis",
            _json_bytes(swapped_identity),
            bound=bound,
            target=target,
            expected_binding=result.paired_execution.binding,
            expected_selected=("0", "1"),
            repetitions=2,
        )

    wrong_binding = SimpleNamespace(to_json=lambda: {"forged": True})
    with pytest.raises((ValueError, SchemaValidationError)):
        change_analysis_module._report_observations(
            tmp_path / "analysis",
            report_path.read_bytes(),
            bound=bound,
            target=target,
            expected_binding=wrong_binding,
            expected_selected=("0", "1"),
            repetitions=2,
        )

    sample = report["samples"][0]
    sample["runs"][0]["base"]["value"] = 99
    forged_output = _json_bytes(report)
    with pytest.raises(ValueError, match="differs from retained stdout"):
        change_analysis_module._report_observations(
            tmp_path / "analysis",
            forged_output,
            bound=bound,
            target=target,
            expected_binding=result.paired_execution.binding,
            expected_selected=("0", "1"),
            repetitions=2,
        )

    sample["runs"][0]["base"]["value"] = 0
    forged_input = _json_bytes(report)
    input_path = tmp_path / "analysis" / "inputs" / "0.u32le"
    original_input = input_path.read_bytes()
    input_path.write_bytes((1).to_bytes(4, "little"))
    with pytest.raises(ValueError, match="retained artifacts changed"):
        change_analysis_module._report_observations(
            tmp_path / "analysis",
            forged_input,
            bound=bound,
            target=target,
            expected_binding=result.paired_execution.binding,
            expected_selected=("0", "1"),
            repetitions=2,
        )
    input_path.write_bytes(original_input)

    valid_wrong_binding = result.paired_execution.binding.to_json()
    valid_wrong_binding["runner_identity_sha256"] = "0" * 64
    valid_wrong_binding["binding_sha256"] = canonical_digest({
        key: value for key, value in valid_wrong_binding.items()
        if key != "binding_sha256"
    })
    report["binding"] = valid_wrong_binding
    with pytest.raises(ValueError, match="live execution binding"):
        change_analysis_module._report_observations(
            tmp_path / "analysis",
            _json_bytes(report),
            bound=bound,
            target=target,
            expected_binding=result.paired_execution.binding,
            expected_selected=("0", "1"),
            repetitions=2,
        )

    report["source"]["candidate_oid"] = "0" * 40
    with pytest.raises(ValueError, match="source identities"):
        change_analysis_module._report_observations(
            tmp_path / "analysis",
            _json_bytes(report),
            bound=bound,
            target=target,
            expected_binding=result.paired_execution.binding,
            expected_selected=("0", "1"),
            repetitions=2,
        )
    extra_sample = json.loads(report_path.read_bytes())
    extra = copy.deepcopy(extra_sample["samples"][0])
    extra["purpose"] = "shrink"
    extra_sample["samples"].append(extra)
    with pytest.raises(ValueError, match="repeats input"):
        change_analysis_module._report_observations(
            tmp_path / "analysis",
            _json_bytes(extra_sample),
            bound=bound,
            target=target,
            expected_binding=result.paired_execution.binding,
            expected_selected=("0", "1"),
            repetitions=2,
        )


def test_public_analysis_rejects_stale_retained_report(tmp_path, monkeypatch):
    original = change_analysis_module._run_prepared_paired_execution

    def stale_report(*args, **kwargs):
        execution = original(*args, **kwargs)
        path = Path(kwargs["output_dir"]) / "paired-report.json"
        report = json.loads(path.read_bytes())
        extra = copy.deepcopy(report["samples"][0])
        extra["purpose"] = "shrink"
        report["samples"].append(extra)
        path.write_bytes(_json_bytes(report))
        return execution

    monkeypatch.setattr(
        change_analysis_module, "_run_prepared_paired_execution", stale_report
    )
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    candidate = _candidate_commit(root, request["candidate_oid"], "def convert(value):\n    return value\n")
    request["candidate_oid"] = candidate

    with pytest.raises(ValueError, match="differs from its live result"):
        run_change_analysis(
            open_project(root), request, trusted_target_oid=target_oid,
            output_dir=tmp_path / "analysis", input_tokens=("0",), repetitions=1,
        )
    assert not (tmp_path / "analysis" / "change-analysis.json").exists()


@pytest.mark.parametrize("artifact", ["paired-report.json", "change-analysis.json"])
def test_candidate_cannot_precreate_analysis_reports(tmp_path, artifact):
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    candidate_source = (
        "from pathlib import Path\n"
        "root = Path(__file__).parents[2]\n"
        f"(root / {artifact!r}).write_bytes(b'candidate')\n"
        "def convert(value):\n"
        "    return value\n"
    )
    request["candidate_oid"] = _candidate_commit(
        root, request["candidate_oid"], candidate_source
    )

    with pytest.raises(FileExistsError):
        run_change_analysis(
            open_project(root), request, trusted_target_oid=target_oid,
            output_dir=tmp_path / "analysis", input_tokens=("0",), repetitions=1,
        )
    assert (tmp_path / "analysis" / artifact).read_bytes() == b"candidate"
