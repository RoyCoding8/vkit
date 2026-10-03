"""The counterexamples the R1 probes produced, as regressions.

Every test here has a probe behind it. The probes are disposable — they wrote
their observations into `review/*.json` and kept nothing — so a failure they
demonstrated would otherwise return unnoticed. Each test states its expectation
from the invariant rather than from the value the current build happens to
print, so a later edit cannot turn a defect into the expectation by copying it.

The invariant these defend, in one sentence: one core admission/readiness
authority, a floor frozen at admission that callers may only widen, claims
acquired atomically or admission refused, and acceptance that compares the
identities a pass actually tested.

The remaining probe counterexamples are absent because they are not this
subsystem's to fix, and a regression asserting a behavior another worker is
still repairing would fail for the wrong reason: `check_start_is_synchronous`
(F05, execution) and `foreign_origin_get_mutation` (F07, the console server).
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"
sys.path.insert(0, str(REPO_ROOT / "src"))

from vkit.console import operations  # noqa: E402
from vkit.console.plan import Refused  # noqa: E402
from vkit.enroll import read_enrollment  # noqa: E402
from vkit.identity import compute_source_identity  # noqa: E402
from vkit.manifest import parse_manifest, _relative  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.storage import ConflictError, Store  # noqa: E402
from vkit.tasks import (  # noqa: E402
    AdmissionRefused,
    TaskContract,
    TaskError,
    acceptance_context,
    admit,
    compute_readiness,
    finalize,
    get_task,
    supersede_task,
    verify_ownership,
)

CHECK_ID = "totals-behavior"

#: The policy digest the seeded runs record, so a seeded pass is a pass against
#: the policy actually in force rather than against a placeholder. The identity
#: comparison is only meaningful when the one field under test is the one that
#: differs.
_POLICY_DIGEST: dict[str, str] = {}
_FIXTURE_DIGEST: dict[str, str] = {}


def _git_init(root: Path) -> None:
    """A repository, because that is what every one of these reads to find state."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init", "--allow-empty"],
                   cwd=root, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway Git repository holding a real copy of the example.

    A Git repository because that is what every one of these operations reads to
    decide where state lives, and a real policy because a synthetic manifest
    would test the fixture rather than the rule.
    """
    root = tmp_path / "repo"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    _git_init(root)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=root, check=True, capture_output=True,
    )
    return root


def _context(repo: Path):
    project = open_project(repo)

    def loader():
        manifest = parse_manifest(project, project.runs_root)
        _POLICY_DIGEST["value"] = manifest.digest()
        fixture = manifest.fixture_identity()
        if fixture is not None:
            _FIXTURE_DIGEST["value"] = fixture.digest
        return manifest

    return acceptance_context(project, loader)


def _store(repo: Path) -> Store:
    return Store(open_project(repo).db_path)


def _admit(repo: Path, task_id: str, **kwargs):
    return admit(_store(repo), task_id, context=_context(repo), **kwargs)


def _policy_checks(repo: Path) -> list[str]:
    return sorted(parse_manifest(open_project(repo), open_project(repo).runs_root).checks)


_RUNS = itertools.count()


def _current_source(repo: Path) -> dict:
    """The `source` block a run recorded today would carry.

    A pass has to be recorded against the identities in force to be eligible at
    all, and a test that seeds an unrelated digest is testing the mismatch rather
    than whatever it names.
    """
    return {"inventory_digest": compute_source_identity(open_project(repo)).inventory_digest}


def _publish_pass(store: Store, task_id: str, attempt: int, source: dict | None = None,
                  policy_digest: str | None = None,
                  fixture_digest: str | None | object = Ellipsis) -> None:
    """One terminal passing run, the way a completed check records itself.

    Fixture identity is measured from the same policy as admission.
    """
    run_id = f"r-{task_id}-{attempt}-{next(_RUNS)}"
    if policy_digest is None:
        policy_digest = _POLICY_DIGEST.get("value", "pd")
    store.register_run(run_id, CHECK_ID, task_id=task_id, attempt=attempt,
                       source=source or {"inventory_digest": "an-unrelated-inventory"},
                       configuration_digest=policy_digest,
                       fixture_digest=(
                           _FIXTURE_DIGEST["value"] if fixture_digest is Ellipsis
                           else fixture_digest
                       ))
    # A real run's adapter writes a receipt into the run directory, and
    # `Store.publish` reads the evidence kind and obligations out of that file
    # rather than out of the report. Without one, a run has established nothing
    # about WHAT KIND of evidence it is, and acceptance refuses to let it
    # discharge a requirement that names a kind. See `storage.Store.publish`.
    receipt = store.run_dir(run_id) / "receipt.v2.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "schema_version": 2,
        "status": "PASS",
        "evidence_kind": "scenario",
        "check_id": CHECK_ID,
        "satisfied": [],
        "counterexamples": [],
    }), encoding="utf-8")
    store.publish(run_id, {
        "run_id": run_id, "lifecycle": "terminal", "ended_at": "t",
        "outcome": {"result": "PASS", "scenarios": [
            {"id": "s", "result": "PASS", "observation": "seeded"}]},
    })


# --- F01: the floor is frozen, and a caller may only widen it ---------------

def test_the_caller_cannot_subtract_from_the_approved_floor(repo: Path) -> None:
    """A contract naming a subset of the policy is frozen at the whole policy.

    The contract is the caller's selection. What is mandatory is what the
    approved policy registers, and the caller is not the authority on that.
    """
    admitted = _admit(repo, "t1", required_checks=[CHECK_ID])
    assert admitted.contract.required_checks == tuple(_policy_checks(repo)), (
        f"the frozen floor is {admitted.contract.required_checks}, but the policy "
        f"registers {_policy_checks(repo)}"
    )
    assert CHECK_ID in admitted.contract.required_checks


def test_a_caller_widening_the_floor_keeps_what_it_added(repo: Path) -> None:
    """Adding to the floor is allowed; the addition is kept, not discarded."""
    path = open_project(repo).manifest_path
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["checks"].append(dict(manifest["checks"][0], id="mandatory-second"))
    path.write_text(json.dumps(manifest), encoding="utf-8")

    admitted = _admit(repo, "t1", required_checks=[CHECK_ID])
    assert set(admitted.contract.required_checks) == set(_policy_checks(repo))
    assert "mandatory-second" in admitted.contract.required_checks


def test_a_selection_naming_an_unregistered_check_is_refused(repo: Path) -> None:
    """A selection the policy does not register is refused, not dropped.

    Silently ignoring it would admit a task that looks like it was asked for
    something it is not.
    """
    with pytest.raises(AdmissionRefused) as raised:
        _admit(repo, "t1", required_checks=["no-such-check"])
    assert "unknown check" in str(raised.value)


def test_both_adapters_freeze_the_same_floor(repo: Path) -> None:
    """CLI and MCP derive one floor, so neither can be the laxer adapter.

    A divergence here means one adapter kept a private rule beside the core's,
    which is the defect the adapters had.
    """
    from vkit.mcp import Server

    server = Server(repo)
    opened = server.call_tool("task_begin", {
        "contract": {"required_checks": [CHECK_ID]}, "request_id": "mcp-floor",
    }).content
    assert opened.get("admitted") is True, f"admission was refused: {opened}"

    via_server = get_task(server._store(), opened["task_id"]).pinned().required_checks
    via_core = _admit(repo, "t2", required_checks=[CHECK_ID]).contract.required_checks
    assert via_server == via_core, (
        f"the adapters froze different floors: server {via_server}, core {via_core}"
    )


# --- F02: a missing, malformed or empty policy blocks -----------------------

def test_a_deleted_policy_blocks_instead_of_emptying_the_floor(repo: Path) -> None:
    """A removed manifest is a diagnostic refusal, not an empty baseline.

    An empty floor is a permanent silent failure: nothing can ever satisfy it and
    it reads as success. A refusal names the missing file, which is actionable.
    """
    open_project(repo).manifest_path.unlink()
    context = _context(repo)

    assert not context.usable
    assert "no usable policy" in context.refusal
    with pytest.raises(AdmissionRefused):
        _admit(repo, "t1")


def test_a_malformed_policy_blocks_instead_of_emptying_the_floor(repo: Path) -> None:
    """Unparseable JSON is the same refusal as no file, and says so."""
    open_project(repo).manifest_path.write_text("{ not json", encoding="utf-8")
    assert not _context(repo).usable
    with pytest.raises(AdmissionRefused):
        _admit(repo, "t1")


def test_a_policy_registering_nothing_cannot_admit(repo: Path) -> None:
    """A policy with an empty check set admits nothing at all."""
    path = open_project(repo).manifest_path
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["checks"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")

    context = _context(repo)
    assert not context.usable, "a policy with no checks is not a usable context"
    assert "non-empty" in context.refusal
    with pytest.raises(AdmissionRefused):
        _admit(repo, "t1")


def test_an_unenrolled_project_cannot_admit_a_zero_check_task(tmp_path: Path) -> None:
    """No policy at all is the strongest form of the same refusal."""
    root = tmp_path / "bare"
    root.mkdir()
    _git_init(root)
    context = _context(root)

    assert not context.usable
    with pytest.raises(AdmissionRefused):
        admit(_store(root), "t1", context=context)


def test_a_declared_input_that_cannot_be_read_is_refused_not_assumed(repo: Path) -> None:
    """A fixture identity that cannot be measured blocks; it is never a null.

    Writing null and continuing would report that the fixtures were tested when
    nothing was read at all.
    """
    path = open_project(repo).manifest_path
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["checks"][0]["inputs"] = ["fixtures/does-not-exist.json"]
    path.write_text(json.dumps(manifest), encoding="utf-8")

    context = _context(repo)
    assert not context.usable
    assert "cannot be read" in context.refusal
    assert context.fixture_digest is None


def test_an_unreadable_policy_does_not_erase_a_pinned_floor(repo: Path) -> None:
    """The frozen floor outlives the file it was derived from.

    Deleting the policy after admission must not shrink what the task must
    prove; it blocks instead, with the floor still intact.
    """
    store = _store(repo)
    floor = _admit(repo, "t1", required_checks=[CHECK_ID]).contract.required_checks

    open_project(repo).manifest_path.unlink()
    result = finalize(store, "t1", context=_context(repo))

    assert result.readiness == "BLOCKED"
    assert any("no usable policy" in gap for gap in result.gaps)
    assert get_task(store, "t1").pinned().required_checks == floor


# --- F03: claims are acquired atomically, or admission is refused -----------

def test_a_claim_conflict_refuses_admission_and_leaves_no_task(repo: Path) -> None:
    """A contended resource refuses the second task, not admits it degraded.

    The refused task must leave nothing behind. A row that reads admitted while
    holding nothing is the exact state the single transaction exists to prevent.
    """
    store = _store(repo)
    _admit(repo, "t1", resources=[{"key": "checkout", "kind": "exclusive"}])

    with pytest.raises(ConflictError):
        admit(store, "t2", context=_context(repo),
              resources=[{"key": "checkout", "kind": "exclusive"}])
    with pytest.raises(TaskError):
        get_task(store, "t2")
    assert get_task(store, "t1").generation == 1


def test_a_partially_available_resource_set_refuses_the_whole_set(repo: Path) -> None:
    """All-or-nothing: one unavailable resource refuses the admission.

    A caller that declared two requirements does not get one, which would leave
    a task believing it holds a protection it never took.
    """
    store = _store(repo)
    _admit(repo, "t1", resources=[{"key": "checkout", "kind": "exclusive"}])

    with pytest.raises(ConflictError):
        admit(store, "t2", context=_context(repo), resources=[
            {"key": "free-resource", "kind": "exclusive"},
            {"key": "checkout", "kind": "exclusive"},
        ])
    with pytest.raises(TaskError):
        get_task(store, "t2")


def test_a_claim_conflict_is_rechecked_before_launch(repo: Path) -> None:
    """Ownership is verified again at the launch boundary, not only at admission.

    A conflict found at admission can stop being found: recovery may release the
    resource while the attempt is still open, and an attempt whose resource
    vanished under it must not go on to run.
    """
    from vkit.claims import release

    store = _store(repo)
    admitted = _admit(repo, "t1", resources=[{"key": "checkout", "kind": "exclusive"}])

    release(store, "t1", admitted.generation)

    with pytest.raises(ConflictError):
        verify_ownership(store, "t1", admitted.generation, project=open_project(repo))


def test_a_superseded_attempt_cannot_publish_a_verdict(repo: Path) -> None:
    """A verdict computed by a superseded attempt is refused when recorded.

    The attempt is reassigned after it computed its verdict. Recording compares
    the generation that decided against the generation in force, so the stale
    verdict cannot be stamped on the task its predecessor owned.
    """
    from vkit.tasks import compute_readiness, record_readiness

    store = _store(repo)
    _admit(repo, "t1")
    verdict = compute_readiness(store, "t1", required_check_ids=[CHECK_ID])
    assert verdict.context["generation"] == 1

    supersede_task(store, "t1")

    with pytest.raises(ConflictError):
        record_readiness(store, "t1", verdict)
    assert get_task(store, "t1").readiness is None


def test_a_superseded_attempt_no_longer_owns_its_resources(repo: Path) -> None:
    """Reassignment invalidates the previous attempt's ownership authority."""
    store = _store(repo)
    admitted = _admit(repo, "t1", resources=[{"key": "checkout", "kind": "exclusive"}])

    supersede_task(store, "t1")

    with pytest.raises(ConflictError):
        verify_ownership(store, "t1", admitted.generation, project=open_project(repo))
    with pytest.raises(ConflictError) as fresh:
        verify_ownership(store, "t1", 2, project=open_project(repo))
    assert "checkout" in str(fresh.value), (
        "the successor is expected to re-take the resource its predecessor held; "
        f"the refusal does not name it: {fresh.value}"
    )


# --- F04: the bindings are derived, never supplied -------------------------

def test_a_contract_must_bind_a_repository_and_a_policy_digest(repo: Path) -> None:
    """The two fields acceptance compares against cannot be left out.

    A contract with no repository binding has nothing to check the evidence
    belongs to this checkout, and one with no policy digest has nothing to
    compare the tested policy against.
    """
    project = open_project(repo)
    repository = {"root": str(project.root), "git_common_dir": str(project.git_common_dir)}

    with pytest.raises(AdmissionRefused):
        TaskContract.from_json({**repository, "required_checks": [CHECK_ID]})
    with pytest.raises(AdmissionRefused):
        TaskContract.from_json({**repository, "policy_digest": "pd",
                                "required_checks": [CHECK_ID], "scope": "", "resources": []})


def test_the_pinned_policy_digest_is_the_policys_own(repo: Path) -> None:
    """The digest is derived, so a changed policy changes what the task is bound to."""
    project = open_project(repo)
    first = _admit(repo, "t1").contract.policy_digest

    assert first == parse_manifest(project, project.runs_root).digest()
    assert first not in ("", "caller-supplied-policy")


def test_an_empty_mandatory_floor_is_never_a_valid_contract(repo: Path) -> None:
    """A contract with no mandatory checks cannot be constructed at all.

    The floor being empty is the condition requirement 1 exists to prevent, and
    it is refused at the contract rather than at each caller that might have
    forgotten to check.
    """
    project = open_project(repo)
    with pytest.raises(AdmissionRefused):
        TaskContract.from_json({
            "repository": {"root": str(project.root), "git_common_dir": str(project.git_common_dir)},
            "policy_digest": "pd", "required_checks": [], "scope": "", "resources": [],
        })


# --- F09: a mismatch is a gap for this attempt; history is never erased -----

def test_a_pass_from_a_superseded_attempt_cannot_satisfy_a_fresh_one(repo: Path) -> None:
    """A fresh attempt does not inherit the previous attempt's pass.

    The recorded history says which attempt produced what, and the current
    attempt is decided only on its own runs.
    """
    store = _store(repo)
    _admit(repo, "t1")
    _publish_pass(store, "t1", 1)
    supersede_task(store, "t1")

    result = finalize(store, "t1", context=_context(repo))

    assert result.readiness == "BLOCKED", (
        "generation 2 reached READY on generation 1's evidence; it produced no "
        "runs of its own"
    )
    assert any("attempt" in line for line in result.history), (
        f"the superseded run was not reported as history: {result.history}"
    )
    assert not any("attempt" in gap for gap in result.gaps), (
        f"a stale trace was reported as a gap, which makes it look like this "
        f"attempt's own evidence is missing: {result.gaps}"
    )


def test_history_is_reported_separately_and_never_erased(repo: Path) -> None:
    """A prior attempt's runs are history, not a verdict, and not deleted.

    The gap and the history are two different things: one says this attempt
    cannot be accepted, the other says what happened before. Collapsing them is
    what turns a stale trace into a permanent veto.
    """
    store = _store(repo)
    _admit(repo, "t1")
    _publish_pass(store, "t1", 1)
    supersede_task(store, "t1")

    result = finalize(store, "t1", context=_context(repo))

    assert result.history, "a superseded attempt's runs must remain visible"
    assert not set(result.history) & set(result.gaps), (
        f"the same statement is serving as both a gap and as history: {result.gaps}"
    )
    assert any(run["attempt"] == 1 for run in store.list_runs(task_id="t1")), (
        "acceptance deleted the old attempt's records"
    )


def test_an_unusable_context_blocks_instead_of_comparing_nothing(repo: Path) -> None:
    """A refused context is not the same as no context at all.

    `compute_readiness` takes a context so an adapter that can reach the
    repository can compare identities. It used to drop a context that arrived
    unusable and fall through to the branch that compares nothing, so an adapter
    that had built one and found it unreadable still decided READY — the exact
    shape of the hook defect, one layer down. Only a caller with no repository at
    all may omit the context, and it says so by omitting it.

    `finalize` already refused here; the two entry points disagreed about what an
    unresolved measurement means, and the laxer one is the one an adapter reaches.
    """
    store = _store(repo)
    _admit(repo, "t1")
    _publish_pass(store, "t1", 1, source=_current_source(repo),
                  policy_digest=_POLICY_DIGEST["value"])
    (repo / "verification" / "manifest.json").unlink()
    refused = _context(repo)
    assert not refused.usable, "the fixture is not exercising a refused context"

    with_context = compute_readiness(
        store, "t1", required_check_ids=[CHECK_ID], context=refused
    )
    without = compute_readiness(store, "t1", required_check_ids=[CHECK_ID])

    assert without.readiness == "READY", (
        "a caller with no repository is the one case that may decide without an "
        "identity comparison, and that case must keep working"
    )
    assert with_context.readiness == "BLOCKED", (
        "a context that could not be measured was dropped and the verdict was "
        f"reached on no comparison at all: {with_context.gaps}"
    )
    assert any("no usable policy" in gap for gap in with_context.gaps), (
        f"the refusal does not name the measurement that failed: {with_context.gaps}"
    )


def test_an_identity_mismatch_blocks_the_attempt_and_is_not_a_veto(repo: Path) -> None:
    """A pass recorded against another source blocks this attempt.

    It does not veto the task: re-running the check against the source now in
    force clears it, and the gap is a statement about this attempt's evidence.
    """
    store = _store(repo)
    _admit(repo, "t1")
    _publish_pass(store, "t1", 1)

    stale = finalize(store, "t1", context=_context(repo))
    assert stale.readiness == "BLOCKED"
    assert any("different source" in gap for gap in stale.gaps), (
        f"the gap does not name the identity that changed: {stale.gaps}"
    )
    assert stale.readiness != "REJECTED", (
        "changed identity is insufficient evidence, not a failing check"
    )

    # The same task, re-run against the source now in force, is accepted.
    _publish_pass(store, "t1", 1, source={
        "inventory_digest": compute_source_identity(open_project(repo)).inventory_digest,
    })
    fresh = finalize(store, "t1", context=_context(repo))
    assert fresh.readiness == "READY", (
        f"a fresh pass against the current identity was not accepted: {fresh.gaps}"
    )


def test_a_missing_fixture_identity_blocks_until_the_check_runs_again(repo: Path) -> None:
    """A passing run without fixture provenance cannot satisfy acceptance."""
    store = _store(repo)
    _admit(repo, "t1")
    _publish_pass(store, "t1", 1, source={
        "inventory_digest": compute_source_identity(open_project(repo)).inventory_digest,
    }, fixture_digest=None)

    result = finalize(store, "t1", context=_context(repo))

    assert result.readiness == "BLOCKED"
    assert any("no recorded fixtures identity" in gap for gap in result.gaps)
    assert "fixtures" in result.context["unverified_identities"], (
        f"the uncompared identity was not named: {result.context}"
    )


# --- F10/F11: the manifest digest is structured, and a duplicate is refused --

def test_a_manifest_digest_is_boundary_safe(repo: Path) -> None:
    """Argv boundaries, inputs and prerequisites all change the digest.

    A joined string cannot distinguish `("a b", "c")` from `("a", "b c")`, so
    two different checks could share one fingerprint. The digest is a structured
    hash of the check, and every field that changes what is run is in it.
    """
    project = open_project(repo)
    manifest = parse_manifest(project, project.runs_root)
    check = next(iter(manifest.checks.values()))

    def digest(**fields) -> str:
        changed = dataclasses.replace(check, **fields)
        return dataclasses.replace(manifest, checks={changed.id: changed}).digest()

    assert digest(argv=("python", "a b", "c")) != digest(argv=("python", "a", "b c"))
    assert digest(inputs=("one.json",)) != digest(inputs=("two.json",))
    assert digest(prerequisites=()) != digest(prerequisites=check.prerequisites)


def test_a_policy_digest_ignores_the_checkout_it_was_read_from(repo: Path) -> None:
    """The same policy read from two checkouts has one fingerprint.

    The digest identifies the policy, not the directory it was read from. A
    digest that moved with the checkout would fail every worktree of one
    repository against the evidence the other produced, which is the property
    this row exists to keep.
    """
    project = open_project(repo)
    manifest = parse_manifest(project, project.runs_root)
    check = next(iter(manifest.checks.values()))
    relative = _relative(check.cwd, project.root)

    sibling = project.root.parent / "elsewhere"
    sibling.mkdir(parents=True)
    _git_init(sibling)
    sibling.joinpath("verification").mkdir(exist_ok=True)
    sibling.joinpath("verification/manifest.json").write_text(
        project.manifest_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    elsewhere = open_project(sibling)

    digests = {parse_manifest(root, root.runs_root).digest()
               for root in (project, elsewhere)}
    assert len(digests) == 1, (
        f"one policy produced {len(digests)} digests across two checkouts; the "
        f"working directory is expressed as {relative!r} in both"
    )


def test_a_duplicate_check_id_is_a_named_manifest_error(repo: Path) -> None:
    """Two checks with one id is refused, by name and by origin.

    The duplicate was a `NameError` deep in the parser, so a malformed policy
    reported an internal error rather than the policy's own defect.
    """
    from vkit.manifest import ManifestError, parse_manifest_bytes

    project = open_project(repo)
    raw = json.loads(project.manifest_path.read_text(encoding="utf-8"))
    raw["checks"].append(raw["checks"][0])

    with pytest.raises(ManifestError) as raised:
        parse_manifest_bytes(json.dumps(raw).encode(), project=project,
                             run_dir=project.runs_root / "duplicate",
                             origin="audit duplicate")
    assert "duplicate check id" in str(raised.value)


# --- F06: the console has no enrollment authority of its own ----------------

def test_console_enrollment_agrees_with_the_core(repo: Path) -> None:
    """The console reports the core's state, and proposes what the core proposes.

    The two disagreed — the console could report a state the core did not hold.
    One authority means the console reads the core's record rather than keeping
    a second one.
    """
    context = operations.open_context(repo)

    proposed = operations.enroll(context, accepted=False)

    # The console reports the policy and writes nothing, so the state it reports
    # is the state the core holds rather than the state the call implied.
    assert proposed["state"] == read_enrollment(context.project).state.value == "not_enrolled"
    assert proposed["enrolled"] is False


def test_the_console_cannot_promote_onto_a_hand_written_manifest(repo: Path) -> None:
    """Accepting without a proposal is refused, and the policy is untouched.

    The example ships a manifest a person maintains. The core refuses to accept
    what was never proposed, and the console reports that refusal as its own
    boundary error rather than inventing a receipt.
    """
    context = operations.open_context(repo)
    before = open_project(repo).manifest_path.read_text(encoding="utf-8")

    with pytest.raises(Refused) as raised:
        operations.enroll(context, accepted=True)

    assert "no proposal" in str(raised.value)
    assert open_project(repo).manifest_path.read_text(encoding="utf-8") == before
    assert read_enrollment(context.project).state.value == "not_enrolled"
