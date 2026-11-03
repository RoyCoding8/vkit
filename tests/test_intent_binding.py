from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import subprocess

import pytest

from helpers import git, NO_WINDOW
from vkit.certification import IntentRevision
from vkit.intent import preflight_change_request
from vkit.paths import open_project, ProjectError
from vkit.store import Store, write_json


POLICY = "verification/change-intents/accepted.v1.json"


def _git_output(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.email=t@vkit", "-c", "user.name=t", *args],
        cwd=root, text=True, **NO_WINDOW,
    ).strip()


def _commit(root: Path, message: str) -> str:
    git(root, "add", ".")
    git(root, "commit", "-qm", message)
    return _git_output(root, "rev-parse", "HEAD")


@pytest.fixture
def intent_repository(tmp_path):
    def build(*, object_format="sha1", change_revision=None, change_target=None):
        root = tmp_path / "repo"
        root.mkdir()
        git(root, "init", "-q", f"--object-format={object_format}")
        artifacts = {}
        for role in ("binding", "codec", "observation", "environment"):
            content = f'{{"role":"{role}"}}\n'.encode()
            path = f"verification/semantics/{role}.json"
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            artifacts[role] = {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
        revision_raw = {
            "schema_version": 1,
            "requirement": {"source": "requirements.md#choice", "statement": "Either allow or deny is legal."},
            "contract": {
                "domain": {
                    "binding": "example::choose",
                    "inputs": ["x", "y"],
                    "observations": ["allow", "deny", "other"],
                    **{f"{role}_sha256": artifacts[role]["sha256"]
                       for role in ("codec", "observation", "environment")},
                },
                "admissible_inputs": ["x"],
                "allowed_outputs": [{"input": "x", "outputs": ["allow", "deny"]}],
            },
            "artifacts": artifacts,
        }
        if change_revision:
            change_revision(revision_raw)
        revision = IntentRevision.from_json(revision_raw)
        revision_path = f"verification/change-intents/{revision.digest}.v1.json"
        write_json(root / revision_path, revision.to_json())
        write_json(root / POLICY, {"schema_version": 1, "accepted": [revision.digest]})
        (root / "candidate.rs").write_text("pub fn choose() -> bool { true }\n")
        if change_target:
            change_target(root, revision_path, revision_raw)
        target_oid = _commit(root, "accept intent in target")
        (root / "candidate.rs").write_text("pub fn choose() -> bool { false }\n")
        candidate_oid = _commit(root, "change application source")
        request = {
            "schema_version": 1, "base_oid": target_oid, "candidate_oid": candidate_oid,
            "intent_sha256": revision.digest,
            "claim": {"kind": "finite_contract", "contract": deepcopy(revision_raw["contract"])},
        }
        return root, target_oid, request, revision_raw, revision_path
    return build


def _bind(root, target_oid, request):
    return preflight_change_request(open_project(root), request, trusted_target_oid=target_oid)


@pytest.mark.parametrize("object_format,oid_length", [("sha1", 40), ("sha256", 64)])
def test_real_commits_bind_an_accepted_revision_without_certifying_source(intent_repository, object_format, oid_length):
    root, target, request, raw, _ = intent_repository(object_format=object_format)

    bound = _bind(root, target, request)

    assert len(bound.request.base_oid) == oid_length
    assert bound.request.candidate_oid == request["candidate_oid"]
    assert bound.intent.requirement_statement == "Either allow or deny is legal."
    assert bound.intent.contract.outputs_for("x") == frozenset({"allow", "deny"})
    assert bound.to_json()["scope"] == "request_preflight"
    assert bound.to_json()["candidate_behavior_checked"] is False
    assert bound.intent.to_json() == raw
    assert _bind(root, target, request).digest == bound.digest
    assert not (open_project(root).git_common_dir / "vkit").exists()


def test_candidate_and_local_approval_cannot_accept_a_new_intent(intent_repository):
    root, target, request, raw, _ = intent_repository()
    proposed_raw = deepcopy(raw)
    proposed_raw["requirement"]["statement"] = "The candidate approves itself."
    proposed = IntentRevision.from_json(proposed_raw)
    write_json(root / POLICY, {"schema_version": 1, "accepted": [proposed.digest]})
    write_json(root / f"verification/change-intents/{proposed.digest}.v1.json", proposed.to_json())
    store = Store.of(open_project(root))
    write_json(store.root / "approved.json", {"schema_version": 2, "records": {
        proposed.digest: {"kind": "change_intent", "accepted": True}}})
    request["candidate_oid"] = _commit(root, "candidate self-approval")
    request["intent_sha256"] = proposed.digest

    with pytest.raises(ValueError, match="accept"):
        _bind(root, target, request)


def test_candidate_policy_replacement_is_rejected(intent_repository):
    root, target, request, raw, revision_path = intent_repository()
    raw["requirement"]["statement"] = "Unreviewed replacement."
    write_json(root / revision_path, raw)
    (root / POLICY).unlink()
    request["candidate_oid"] = _commit(root, "replace revision and delete acceptance")

    with pytest.raises(ValueError, match="candidate accepted-intents document differs from trusted target"):
        _bind(root, target, request)


def test_an_unchanged_candidate_can_bind_without_granting_a_pass(intent_repository):
    root, target, request, _, _ = intent_repository()
    request["candidate_oid"] = target

    bound = _bind(root, target, request)

    assert bound.request.base_oid == bound.request.candidate_oid == target
    assert bound.to_json()["candidate_behavior_checked"] is False


@pytest.mark.parametrize("role", ["binding", "codec", "observation", "environment"])
@pytest.mark.parametrize("mutation", ["delete", "change_raw_newlines"])
def test_changed_declared_semantic_artifacts_require_new_acceptance(intent_repository, role, mutation):
    root, target, request, raw, _ = intent_repository()
    path = root / raw["artifacts"][role]["path"]
    if mutation == "delete":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    request["candidate_oid"] = _commit(root, f"change {role}")

    with pytest.raises(ValueError, match=role):
        _bind(root, target, request)


@pytest.mark.parametrize("mutation", ["missing_index", "not_accepted", "missing_revision", "changed_revision", "wrong_artifact_hash", "missing_artifact", "duplicate_key", "malformed_json"])
def test_invalid_target_policy_fails_closed(intent_repository, mutation):
    def change_target(root, revision_path, raw):
        if mutation == "missing_index":
            (root / POLICY).unlink()
        elif mutation == "not_accepted":
            write_json(root / POLICY, {"schema_version": 1, "accepted": []})
        elif mutation == "missing_revision":
            (root / revision_path).unlink()
        elif mutation == "changed_revision":
            raw["requirement"]["statement"] = "Changed without a new digest."
            write_json(root / revision_path, raw)
        elif mutation == "wrong_artifact_hash":
            (root / raw["artifacts"]["binding"]["path"]).write_bytes(b"wrong bytes")
        elif mutation == "missing_artifact":
            (root / raw["artifacts"]["codec"]["path"]).unlink()
        elif mutation == "duplicate_key":
            original = (root / POLICY).read_text()
            (root / POLICY).write_text(original.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1'))
        else:
            (root / POLICY).write_text("{unparseable")
    root, target, request, _, _ = intent_repository(change_target=change_target)

    with pytest.raises((ValueError, ProjectError)):
        _bind(root, target, request)


def test_wrong_base_and_noncommit_or_unrelated_candidate_are_rejected(intent_repository):
    root, target, request, _, _ = intent_repository()
    with pytest.raises(ValueError, match="base|target"):
        _bind(root, request["candidate_oid"], request)
    with pytest.raises(ValueError, match="base|target"):
        _bind(root, "HEAD", request)
    request["candidate_oid"] = _git_output(root, "rev-parse", "HEAD:candidate.rs")
    with pytest.raises(ProjectError):
        _bind(root, target, request)
    tree = _git_output(root, "rev-parse", "HEAD^{tree}")
    request["candidate_oid"] = _git_output(root, "commit-tree", tree, "-m", "unrelated root")
    with pytest.raises(ValueError, match="ancestor|descend"):
        _bind(root, target, request)


def test_dirty_checkout_and_git_replacement_refs_do_not_replace_authority(intent_repository):
    root, target, request, raw, revision_path = intent_repository()
    original = _bind(root, target, request)
    raw["requirement"]["statement"] = "Replacement commit policy."
    write_json(root / revision_path, raw)
    replacement = _commit(root, "replacement authority")
    git(root, "replace", target, replacement)
    (root / POLICY).write_text("invalid dirty policy")
    (root / raw["artifacts"]["codec"]["path"]).write_bytes(b"dirty codec")

    bound = _bind(root, target, request)

    assert bound.digest == original.digest
    assert bound.intent.requirement_statement == "Either allow or deny is legal."


def test_alternate_worktree_uses_the_same_exact_target_authority(intent_repository, tmp_path):
    root, target, request, _, _ = intent_repository()
    worktree = tmp_path / "alternate"
    git(root, "worktree", "add", "--detach", str(worktree), request["candidate_oid"])
    (worktree / POLICY).write_text("dirty worktree policy")
    try:
        bound = _bind(worktree, target, request)
        assert bound.intent.requirement_statement == "Either allow or deny is legal."
        assert bound.digest == _bind(root, target, request).digest
    finally:
        worktree.resolve().relative_to(tmp_path.resolve())
        git(root, "worktree", "remove", "--force", str(worktree))


@pytest.mark.parametrize("change", ["domain", "legal_input", "legal_output", "vacuous_intent"])
def test_domain_drift_and_unsupported_entailment_cannot_bind(intent_repository, change):
    def change_revision(raw):
        raw["contract"]["admissible_inputs"] = []
        raw["contract"]["allowed_outputs"] = []
    root, target, request, _, _ = intent_repository(
        change_revision=change_revision if change == "vacuous_intent" else None)
    claim = request["claim"]["contract"]
    if change == "domain":
        claim["domain"]["observation_sha256"] = "0" * 64
    elif change == "legal_input":
        claim["admissible_inputs"] = []
        claim["allowed_outputs"] = []
    elif change == "legal_output":
        claim["allowed_outputs"][0]["outputs"] = ["allow"]

    with pytest.raises(ValueError):
        _bind(root, target, request)


def test_request_identity_changes_for_a_weaker_entailed_claim(intent_repository):
    root, target, request, _, _ = intent_repository()
    original = _bind(root, target, request)
    request["claim"]["contract"]["allowed_outputs"][0]["outputs"].append("other")

    bound = _bind(root, target, request)

    assert bound.request.claim.outputs_for("x") == frozenset({"allow", "deny", "other"})
    assert bound.digest != original.digest
    assert bound.to_json()["candidate_behavior_checked"] is False


def test_excluded_legal_output_is_named_in_the_rejection(intent_repository):
    root, target, request, _, _ = intent_repository()
    request["claim"]["contract"]["allowed_outputs"][0]["outputs"] = ["allow"]

    with pytest.raises(ValueError, match="legal output 'deny' for input 'x'"):
        _bind(root, target, request)
