from copy import deepcopy
import json

import pytest

from helpers import git
from test_intent_binding import _bind, _commit
from vkit.certification import DelegationBasis, intent_entails_claim
from vkit.store import write_json


REGISTRY = "verification/change-claims/accepted.v1.json"
pytest_plugins = ["test_intent_binding"]


def _delegating_repository(build, *, narrow=False):
    root, target, request, intent_raw, _ = build()
    source = (root / "candidate.rs").read_bytes()
    git(root, "checkout", "--detach", target)
    baseline = deepcopy(intent_raw["contract"])
    baseline["allowed_outputs"][0]["outputs"] = ["allow", "deny", "other"]
    assumptions = deepcopy(baseline)
    if narrow:
        assumptions["allowed_outputs"][0]["outputs"] = ["allow"]
        request["claim"]["contract"]["allowed_outputs"][0]["outputs"] = ["allow"]
    basis = DelegationBasis.from_json({
        "schema_version": 1, "policy": "finite_strengthening_v1",
        "scope": "restricted_captured_rust_kani_model", "intent_sha256": request["intent_sha256"],
        "baseline": baseline, "assumptions": assumptions,
    })
    path = f"verification/change-claims/{basis.digest}.v1.json"
    write_json(root / path, basis.to_json())
    write_json(root / REGISTRY, {"schema_version": 1, "accepted": [basis.digest]})
    target = _commit(root, "accept delegation basis")
    (root / "candidate.rs").write_bytes(source)
    request.update(base_oid=target, candidate_oid=_commit(root, "candidate"))
    request["delegation"] = {
        "basis_sha256": basis.digest, "near_miss": {"input": "x", "output": "other"},
    }
    return root, target, request, path


def test_target_owned_basis_binds_without_accepting_a_claim(intent_repository):
    root, target, request, _ = _delegating_repository(intent_repository)
    bound = _bind(root, target, request)

    assert bound.delegation.basis.digest == request["delegation"]["basis_sha256"]
    assert bound.delegation.strictness_witness == ("x", "other")
    assert bound.to_json()["candidate_behavior_checked"] is False
    assert "accepted_by" not in bound.to_json()["delegation"]


def test_target_assumptions_authorize_a_claim_the_intent_alone_does_not_entail(intent_repository):
    root, target, request, _ = _delegating_repository(intent_repository, narrow=True)
    bound = _bind(root, target, request)

    assert intent_entails_claim(bound.intent.contract, bound.request.claim).kind.value == "not_entailed"
    assert bound.delegation.claim.outputs_for("x") == frozenset({"allow"})


@pytest.mark.parametrize("file,operation", [
    ("registry", "change"), ("registry", "delete"), ("basis", "change"), ("basis", "delete"),
])
def test_candidate_cannot_replace_or_remove_the_target_basis(intent_repository, file, operation):
    root, target, request, basis_path = _delegating_repository(intent_repository)
    path = root / (REGISTRY if file == "registry" else basis_path)
    if operation == "delete":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"\n")
    request["candidate_oid"] = _commit(root, "tamper with authority")

    with pytest.raises(ValueError, match="candidate .* (differs|missing)"):
        _bind(root, target, request)


def test_unaccepted_basis_cannot_be_selected_by_the_candidate(intent_repository):
    root, target, request, _ = _delegating_repository(intent_repository)
    request["delegation"]["basis_sha256"] = "f" * 64
    write_json(root / REGISTRY, {"schema_version": 1, "accepted": ["f" * 64]})
    request["candidate_oid"] = _commit(root, "candidate accepts its own basis")

    with pytest.raises(ValueError, match="not accepted by the trusted target"):
        _bind(root, target, request)


def test_target_basis_digest_is_checked_even_when_candidate_copies_match(intent_repository):
    root, _, request, path = _delegating_repository(intent_repository)
    raw = DelegationBasis.from_json(json.loads((root / path).read_text())).to_json()
    raw["assumptions"]["allowed_outputs"][0]["outputs"] = ["allow"]
    write_json(root / path, raw)
    target = _commit(root, "corrupt digest named basis")
    request.update(base_oid=target, candidate_oid=target)

    with pytest.raises(ValueError, match="basis digest"):
        _bind(root, target, request)
