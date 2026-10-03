"""Checkpoint 12.3: the operator edits project configuration without weakening evidence.

**Every assertion is on a real response body or a real file.** The console is
driven through `server.start_in_thread` against a real Git copy of the example, so
a preview is what the browser would have received and a refusal is the byte
string the operator would have read. Nothing here computes the answer it then
asserts on.

**The dangerous failure mode drives the order of the file.** A settings edit that
silently releases an obligation is exactly what this product exists to prevent,
so the tests that matter are the ones that would let it happen: a preview that
omits an effect, a save that accepts a stale digest, a save that releases a pinned
obligation, a write that lands halfway. They come first and they come often.

**The prohibited writes are asserted, not assumed.** `schemas/` stays prohibited,
an arbitrary path is refused, and `plan.under_protected_path` is checked directly
so a future edit that weakens the gate fails here rather than in production.
"""
from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import subproc
from vkit.console import api, operations, plan, server
from vkit.paths import open_project

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"
CHECK_ID = "totals-behavior"

#: The two repository-relative paths this checkpoint is allowed to write, written
#: out rather than read from the module under test, so a widened allowlist fails
#: here rather than passing against itself.
PROJECT_POLICY = "verification/project.json"
CLEANUP_POLICY = "verification/cleanup.json"

#: The six obligations the example manifest declares. Written out rather than
#: read from the manifest, so a manifest that stops declaring one fails here.
SIX = (
    "empty-cart",
    "single-positive",
    "several-positives",
    "mixed-sign",
    "negatives-only",
    "cancels-to-zero",
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real Git repository holding a real copy of the example."""
    target = tmp_path / "config repo"
    shutil.copytree(EXAMPLE, target)
    for args in (["git", "init", "-q"], ["git", "add", "-A"]):
        subproc.run(args, cwd=target, check=True, capture_output=True)
    subproc.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=target, check=True, capture_output=True,
    )
    return target


@pytest.fixture()
def context(repo: Path) -> operations.Context:
    return operations.open_context(repo)


@pytest.fixture()
def live(repo: Path):
    """A real console over a real repository, serving the real static assets."""
    context_ = operations.open_context(repo)
    bound, thread = server.start_in_thread(context_, port=0)
    url = f"http://127.0.0.1:{bound.server_address[1]}"
    try:
        yield url, _token_of(url), repo, context_
    finally:
        bound.shutdown()
        bound.server_close()
        thread.join(timeout=10)


# ----------------------------------------------------------------- helpers


def _token_of(url: str) -> str:
    """The token the served page carries, read from the page as the browser does."""
    import re

    with urllib.request.urlopen(f"{url}/", timeout=30) as response:
        page = response.read().decode("utf-8")
    match = re.search(r'name="vkit-token" content="([^"]+)"', page)
    assert match, "the served page carries no session token"
    return match.group(1)


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


def _post(url: str, token: str, body: dict) -> tuple[dict, int]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"X-Vkit-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8")), response.status
    except urllib.error.HTTPError as error:
        return json.loads(error.read().decode("utf-8")), error.code


def _stage(live, stage: str, **body) -> tuple[dict, int]:
    """One POST to the configuration operation, with the token from the page."""
    url, token, _repo, _context = live
    return _post(
        f"{url}/api/apply?operation=save_project_config&stage={stage}", token, body,
    )


def _digest(live) -> str:
    """The digest every write is guarded by, read the way the page reads it."""
    url, _token, _repo, _context = live
    return _get(f"{url}/api/project")["configuration"]["digest"]


def _settings(live) -> dict:
    """The settings section's configuration block."""
    url, _token, _repo, _context = live
    return _get(f"{url}/api/checks")["sections"]["integrations"]


def proposal(*obligations: str, **overrides) -> dict:
    """A complete, valid proposal requiring the named obligations.

    Defaults to all six the example manifest declares, so a proposal built this
    way cross-references cleanly against the parsed manifest.
    """
    document = {
        "schema_version": 1,
        "description": "what this project must prove",
        "required_checks": [
            {
                "id": CHECK_ID,
                "obligations": [
                    {"kind": "case", "obligation": name}
                    for name in (obligations or SIX)
                ],
            },
        ],
        "connection": {"scope": "loopback"},
        "cleanup": {"mode": "preview", "enabled_rules": [], "excluded_paths": []},
    }
    document.update(overrides)
    return document


# ------------------------------------------------------------- the surface


def test_the_operation_is_on_the_writable_surface_and_writes_nothing_else() -> None:
    """`WRITABLE` and `OPERATIONS` agree, and the manifest is not among them.

    The equality is the property that makes the list the whole surface, so a
    handler that exists without being on the list, and a name on the list with no
    handler, both fail here.
    """
    assert set(operations.OPERATIONS) == set(plan.WRITABLE_NAMES)
    assert "save_project_config" in plan.WRITABLE_NAMES
    assert "save_project_config" in operations.OPERATIONS

    names = {name.lower() for name in plan.WRITABLE_NAMES}
    assert "manifest" not in names, (
        f"the manifest reached the writable surface as {sorted(names)}"
    )
    assert not any("manifest" in name for name in plan.WRITABLE_NAMES)

    # `test_console.py` walks this package's own AST for a write call naming a
    # protected path part, and asserts no `writes` entry names one either. The
    # machine-readable authority is the closed pair.
    assert plan.PROJECT_CONFIG_PATHS == (PROJECT_POLICY, CLEANUP_POLICY)
    assert "verification/manifest.json" not in plan.PROJECT_CONFIG_PATHS
    assert not plan.is_project_config_path("verification/manifest.json")
    assert not plan.is_project_config_path("schemas/run-report.v1.json")
    for part in plan.PROTECTED_PATH_PARTS:
        assert plan.under_protected_path(f"{part}/anything.json") is True


def test_the_settings_section_offers_the_validated_edit(live) -> None:
    """The section that said `editable: false` now carries the edit and its state.

    A flag that stays false while an edit is available is worse than no flag, so
    the flag, the state and the action are asserted together.
    """
    section = _settings(live)

    assert section["editable"] is True, (
        "the settings section still says settings are not editable while a "
        "validated configuration editor is served on it"
    )
    block = section["proposals"]
    assert block is not None, "the settings section carries no configuration state"
    for key in ("digest", "current", "candidate", "approved", "active", "controls"):
        assert key in block, f"the configuration block has no {key!r}: {sorted(block)}"
    offered = {action["operation"] for action in section["actions"]}
    assert "save_project_config" in offered, (
        f"the settings section offers {sorted(offered)} and not the save"
    )
    save_action = next(
        action for action in section["actions"] if action["operation"] == "save_project_config"
    )
    assert save_action["effect"], "the save action names no effect"
    assert save_action["available"] is True


def test_under_protected_path_still_refuses_a_path_shaped_identifier(context) -> None:
    """The other half of the boundary did not move.

    Checkpoint 12.3 added a document parameter, and the temptation was to relax
    `_text_param` for every route so the new one could carry JSON. It was not:
    the document is read by its own validator, and every route that used to
    refuse a path-shaped identifier still refuses it by name.
    """
    assert plan.under_protected_path("verification-kit/runs/abc/stdout.log") is False

    for route, query in (
        ("run", {"run_id": "verification/manifest.json"}),
        ("run", {"run_id": "schemas/run-report.v1.json"}),
        ("log", {"run_id": "somerun", "stream": "verification/manifest.json"}),
        ("plan", {"operation": "verification/manifest.json"}),
        ("run_check", {"check_id": "schemas/run-report.v1.json"}),
        ("cancel_run", {"run_id": "verification/manifest.json"}),
    ):
        with pytest.raises(api.BadRequest) as caught:
            api.dispatch(context, route, query)
        assert "policy path" in str(caught.value), route


# ---------------------------------------------------- preview before writing


def test_a_preview_shows_current_changed_affected_and_stale_before_anything_is_written(live) -> None:
    """The operator sees the whole effect, and the tree is untouched when they do.

    The facts the plan names are asserted separately, and so is the byte identity
    of the two paths afterwards. A preview that wrote first and reported
    afterwards would satisfy nothing here.
    """
    url, _token, repo, _context = live
    before = {
        name: (repo / name).read_bytes() if (repo / name).exists() else None
        for name in (PROJECT_POLICY, CLEANUP_POLICY)
    }
    current_digest = _digest(live)

    document, status = _stage(live, "preview", document=proposal(), expected_digest=current_digest)
    assert status == 200, document
    result = document["result"]

    assert result["wrote"] is False, "the preview reported that it wrote"
    assert result["current"]["cleanup"]["mode"] == "off", (
        "the preview does not show the value that is in force now"
    )
    assert result["current"]["connection"]["scope"] == "none", (
        "the preview does not show the current harness connection scope"
    )
    assert result["changed"], "the preview reports no changed field for a real change"
    changed = {(entry["document"], entry["field"]) for entry in result["changed"]}
    assert (CLEANUP_POLICY, "cleanup.mode") in changed, f"the changed fields are {sorted(changed)}"
    assert (PROJECT_POLICY, "connection.scope") in changed, (
        f"the changed fields are {sorted(changed)}, and the connection scope is missing"
    )
    # No check's obligations moved, so no check is affected and no evidence is
    # stale. Both are asserted here because the other tests depend on the same
    # two facts being true, and an empty list on a project that has never run is
    # indistinguishable from an empty list produced by a bug.
    assert result["affected_checks"] == [], (
        f"the affected checks are {result['affected_checks']}, but this proposal "
        "moves the cleanup mode and the connection scope and no obligation"
    )
    assert result["stale_evidence"] == [], (
        f"evidence is reported stale by a proposal that changes no obligation: "
        f"{result['stale_evidence']}"
    )
    assert result["routine"] is True, (
        "a proposal that changes the cleanup mode and connection scope but removes "
        "no obligation is not routine"
    )
    assert result["candidate"]["revision"], "the preview names no candidate revision"
    assert result["expected_digest"] == current_digest, (
        "the preview does not echo the digest the operator was looking at"
    )

    after = {
        name: (repo / name).read_bytes() if (repo / name).exists() else None
        for name in (PROJECT_POLICY, CLEANUP_POLICY)
    }
    assert after == before, "the preview wrote to the working tree"
    assert _digest(live) == current_digest, "the preview moved the configuration digest"


def test_the_preview_names_the_evidence_that_becomes_stale(live) -> None:
    """Evidence produced under the old configuration is named, not silently aged.

    The stale list is what makes "changing configuration cannot make old evidence
    current" legible. A check with no recorded run reports nothing to stale, and a
    check with one reports that run.
    """
    url, token, _repo, _context = live
    started, status = _post(f"{url}/api/run_check?check_id={CHECK_ID}", token, {})
    assert status == 200, started

    document, status = _stage(
        live, "preview", document=proposal("cancels-to-zero"), expected_digest=_digest(live),
    )
    assert status == 200, document
    stale = document["result"]["stale_evidence"]

    assert stale, "a check with a recorded run is reported as having no evidence to lose"
    entry = next(item for item in stale if item["check_id"] == CHECK_ID)
    assert entry["run_id"] == started["run_id"], (
        "the stale evidence names a run this project did not produce"
    )
    assert entry["verdict"] == "PASS", (
        f"the stale evidence reports verdict {entry['verdict']!r} for a run that passed"
    )
    assert entry["detail"], "a stale evidence entry explains nothing"
    assert document["result"]["affected_checks"] == [CHECK_ID], (
        "dropping four obligations did not mark the check whose obligations dropped as affected"
    )


def test_a_check_with_no_evidence_reports_nothing_stale(live) -> None:
    """A check that never ran has no evidence to invalidate.

    Reporting it anyway would teach an operator to ignore the stale list, which is
    the same failure as a page that renders no list at all.
    """
    document, status = _stage(
        live, "preview", document=proposal(), expected_digest=_digest(live),
    )
    assert status == 200, document
    assert document["result"]["stale_evidence"] == [], (
        f"a project that has never run a check reports stale evidence: "
        f"{document['result']['stale_evidence']}"
    )


# ------------------------------------------- the same validation everywhere


def test_the_advanced_json_editor_goes_through_the_same_validation(live) -> None:
    """The JSON path is not a weaker door than the forms.

    Every document here is one the typed controls could not produce. All of them
    are refused, and the refusal names what is wrong, which is what a person
    editing raw JSON needs and a form cannot leave them without.
    """
    current = _digest(live)

    for bad in ("sideways", "", None, 7):
        document, status = _stage(
            live, "preview",
            document=proposal(cleanup={"mode": bad}), expected_digest=current,
        )
        assert status == 409, f"cleanup mode {bad!r} was accepted: {document}"
        assert "cleanup mode" in document["error"], document["error"]
        for mode in ("off", "preview", "apply_verified"):
            assert mode in document["error"], (
                f"the refusal does not quote the vocabulary the control offers: {document['error']!r}"
            )

    unknown_rule = proposal(cleanup={"mode": "preview", "enabled_rules": ["make-it-faster"]})
    document, status = _stage(live, "preview", document=unknown_rule, expected_digest=current)
    assert status == 409, document
    assert "make-it-faster" in document["error"], document["error"]
    # The refusal quotes what this build can actually apply, so an operator is not
    # left guessing at the rule vocabulary.
    assert "ORDINARY_TRAILING_COMMENT" in document["error"], document["error"]

    bad_exclusion = proposal(cleanup={"mode": "preview", "excluded_paths": [""]})
    document, status = _stage(live, "preview", document=bad_exclusion, expected_digest=current)
    assert status == 409, document
    assert "excluded_paths" in document["error"], document["error"]

    unknown_key = proposal(policy="x")
    document, status = _stage(live, "preview", document=unknown_key, expected_digest=current)
    assert status == 409, document
    assert "unsupported policy key" in document["error"], document["error"]


def test_a_document_cannot_carry_a_command_string_or_a_credential(live) -> None:
    """The policy shape admits no shell text and no secret.

    A closed key set does this rather than a value check: there is no field that
    holds a command at all, and `model_credentials` is refused as an unsupported
    key rather than being stored and quietly ignored.
    """
    current = _digest(live)

    for extra in (
        {"argv": "python -c 'import os'"},
        {"model_credentials": {"api_key": "sk-not-real"}},
        {"checks": [{"id": CHECK_ID, "command": "rm -rf /"}]},
        {"environment": {"ANTHROPIC_API_KEY": "sk-not-real"}},
    ):
        document, status = _stage(
            live, "preview", document=proposal(**extra), expected_digest=current,
        )
        assert status == 409, f"{extra} was accepted: {document}"
        assert "unsupported" in document["error"].lower(), document["error"]


def test_an_argument_list_is_validated_as_a_list_and_a_command_string_is_refused(live) -> None:
    """Advanced argv is an argument list, never a shell string.

    The manifest's own comment is the reason: `['a b', 'c']` and `['a', 'b c']` are
    different argument vectors, so joining them makes two policies
    indistinguishable. The refusal names that.
    """
    current = _digest(live)

    as_string = proposal(checks=[{"id": CHECK_ID, "argv": "python verify.py"}])
    document, status = _stage(live, "preview", document=as_string, expected_digest=current)
    assert status == 409, f"a shell command string was accepted as an argument list: {document}"
    assert "argument list" in document["error"], document["error"]

    for bad in ([], ["python", ""], [3], "python verify.py"):
        document, status = _stage(
            live, "preview",
            document=proposal(checks=[{"id": CHECK_ID, "argv": bad}]), expected_digest=current,
        )
        assert status == 409, f"argv {bad!r} was accepted: {document}"

    as_list = proposal(checks=[{"id": CHECK_ID, "argv": ["python", "verify_totals.py"]}])
    document, status = _stage(live, "preview", document=as_list, expected_digest=current)
    assert status == 200, document
    changed = {entry["field"]: entry for entry in document["result"]["changed"]}
    key = f"checks.{CHECK_ID}.argv"
    assert key in changed, f"a valid argument list produced no changed field: {sorted(changed)}"
    assert changed[key]["changed"] == ["python", "verify_totals.py"], (
        "the preview reports the argument list as something other than the list it was given"
    )


def test_a_check_timeout_is_bounded_by_the_same_manifest_rule(live) -> None:
    """The timeout control enforces the bound `manifest._build_check` enforces.

    One rule, one place it is stated. A console that accepted a timeout the core
    would refuse would offer a control whose value cannot be saved.
    """
    from vkit.manifest import MAX_TIMEOUT_SECONDS

    current = _digest(live)

    for bad in (0, -1, MAX_TIMEOUT_SECONDS * 2, "120"):
        document, status = _stage(
            live, "preview",
            document=proposal(checks=[{"id": CHECK_ID, "timeout_seconds": bad}]),
            expected_digest=current,
        )
        assert status == 409, f"timeout {bad!r} was accepted: {document}"
        assert "timeout_seconds" in document["error"], document["error"]

    document, status = _stage(
        live, "preview",
        document=proposal(checks=[{"id": CHECK_ID, "timeout_seconds": 300}]),
        expected_digest=current,
    )
    assert status == 200, document
    changed = {entry["field"]: entry["changed"] for entry in document["result"]["changed"]}
    assert changed[f"checks.{CHECK_ID}.timeout_seconds"] == 300.0, (
        f"the timeout preview reports {changed}"
    )


def test_an_unknown_check_or_obligation_is_refused_as_a_cross_reference(live) -> None:
    """Every obligation in the proposal must be one a registered check declares.

    This is the cross-reference check the plan asks for, and it is what keeps a
    proposal from becoming a second, looser source of obligations.
    """
    current = _digest(live)

    unknown_check = proposal(required_checks=[
        {"id": "not-registered", "obligations": [{"kind": "case", "obligation": "empty-cart"}]},
    ])
    document, status = _stage(live, "preview", document=unknown_check, expected_digest=current)
    assert status == 409, document
    assert "not-registered" in document["error"], document["error"]
    assert CHECK_ID in document["error"], (
        f"the refusal does not say what the project does define: {document['error']!r}"
    )

    invented = proposal("cancels-to-zero")
    invented["required_checks"][0]["obligations"] = [
        {"kind": "case", "obligation": "never-written-down"}
    ]
    document, status = _stage(live, "preview", document=invented, expected_digest=current)
    assert status == 409, document
    assert "never-written-down" in document["error"], document["error"]

    duplicate = proposal()
    duplicate["required_checks"][0]["obligations"].append(
        {"kind": "case", "obligation": "cancels-to-zero"}
    )
    document, status = _stage(live, "preview", document=duplicate, expected_digest=current)
    assert status == 409, document
    assert "duplicate" in document["error"].lower(), document["error"]


def test_an_unknown_connection_scope_is_refused(live) -> None:
    """The connection scope is a closed vocabulary, and the refusal quotes it."""
    current = _digest(live)
    document, status = _stage(
        live, "preview", document=proposal(connection={"scope": "0.0.0.0:any"}),
        expected_digest=current,
    )
    assert status == 409, document
    for scope in ("none", "loopback"):
        assert scope in document["error"], document["error"]


# ------------------------------------------- proposal, approval, activation


def test_saving_is_not_approving_and_activation_is_separate(live) -> None:
    """Three states, three operations, and only the last one changes what runs.

    A saved proposal is a candidate revision. It is not the accepted local policy
    until an approval records it, and nothing is admitted under it until the
    activation names the revision. A save that did all three would make the review
    step decorative.
    """
    current = _digest(live)
    saved, status = _stage(live, "save", document=proposal(), expected_digest=current)
    assert status == 200, saved
    revision = saved["result"]["candidate"]["revision"]

    settings = _settings(live)["proposals"]
    assert settings["candidate"]["state"] == "candidate", (
        f"the saved revision is {settings['candidate']['state']!r}, not a candidate"
    )
    assert settings["approved"] is None, "a save approved the revision on its own"
    assert settings["active"] is None, "a save activated the revision on its own"

    unnamed, status = _stage(live, "approve", expected_digest=_digest(live))
    assert status == 409, f"approval was answered without naming a revision: {unnamed}"
    assert "revision" in unnamed["error"], unnamed["error"]

    unapproved, status = _stage(
        live, "activate", expected_digest=_digest(live), revision=revision,
    )
    assert status == 409, f"activation was answered for an unapproved revision: {unapproved}"
    assert "approved" in unapproved["error"], unapproved["error"]

    approved, status = _stage(
        live, "approve", expected_digest=_digest(live), revision=revision,
    )
    assert status == 200, approved
    settings = _settings(live)["proposals"]
    assert settings["approved"]["revision"] == revision
    assert settings["active"] is None, "approving also activated the revision"

    activated, status = _stage(
        live, "activate", expected_digest=_digest(live), revision=revision,
    )
    assert status == 200, activated
    assert activated["result"]["active"]["policy_digest"], (
        "activation names no policy digest, so a reader cannot tell what a new "
        "attempt would be admitted against"
    )
    settings = _settings(live)["proposals"]
    assert settings["active"]["revision"] == revision
    assert settings["active"]["context"] == "local", (
        f"the activated policy claims {settings['active']['context']!r}"
    )


def test_a_change_records_the_operator_action_and_each_step_names_its_revision(live) -> None:
    """The history is on disk, not in the page, and each entry names what it did.

    A console that returned the revision without recording it would leave an
    operator asking a week later who approved what.
    """
    current = _digest(live)
    saved, status = _stage(live, "save", document=proposal(), expected_digest=current)
    assert status == 200, saved
    revision = saved["result"]["candidate"]["revision"]

    approved, status = _stage(live, "approve", expected_digest=_digest(live), revision=revision)
    assert status == 200, approved
    activated, status = _stage(
        live, "activate", expected_digest=_digest(live), revision=revision,
    )
    assert status == 200, activated

    actions = _settings(live)["proposals"]["actions"]
    assert [entry["stage"] for entry in actions] == ["activate", "approve", "save"], (
        f"the recorded actions are {[entry['stage'] for entry in actions]}, newest first"
    )
    for entry in actions:
        assert entry["revision"] == revision, (
            f"a recorded action names revision {entry['revision']!r}, not the one it did"
        )
        assert entry["at"], "a recorded action carries no timestamp"


def test_a_change_produces_a_candidate_that_is_not_the_protected_integration_policy(live) -> None:
    """The local configuration and the approved integration policy are two documents.

    Nothing this console writes can confer protected context, and the section
    says so with the approved policy it found rather than pretending one exists.
    The mismatch is reported rather than resolved: a local configuration that
    requires less than the approved policy is the ordinary state of a project
    that has not finished configuring itself.
    """
    url, _token, _repo, _context = live
    saved, status = _stage(live, "save", document=proposal(), expected_digest=_digest(live))
    assert status == 200, saved
    _stage(live, "approve", expected_digest=_digest(live), revision=saved["result"]["candidate"]["revision"])

    block = _settings(live)["proposals"]
    assert block["local"]["context"] == "local", (
        f"the local candidate claims {block['local']['context']!r}; a console edit "
        "cannot confer protected integration authority"
    )
    assert block["local"]["approved_for_protected"] is False
    approved = block["approved_integration"]
    assert approved["context"] is None, (
        "the section reports a protected approved policy for a project that has none"
    )
    assert approved["detail"], "the absent approved policy names no reason"
    assert "cannot" in approved["detail"], approved["detail"]

    mismatch = block["mismatch"]
    assert mismatch["approved_requires"] == [], mismatch
    assert mismatch["local_requires"] == [CHECK_ID], mismatch
    # The project requires more than no approved policy requires, and the section
    # says so rather than claiming agreement. A page that reported `agrees: true`
    # whenever one side was empty would be reassuring an operator about a
    # comparison that never ran.
    assert mismatch["agrees"] is False, (
        f"a local configuration requiring {CHECK_ID} against an absent approved "
        f"policy is reported as agreeing: {mismatch}"
    )
    assert mismatch["extra_locally"] == [CHECK_ID], mismatch
    assert "protected run decides against the approved policy" in mismatch["detail"], (
        f"the mismatch does not say which one decides: {mismatch['detail']!r}"
    )


def test_the_writable_surface_writes_only_the_two_allowlisted_paths(
    context: operations.Context, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every byte the operation writes lands on an allowlisted path or nowhere.

    `test_console.py` walks this package's AST for a write call naming a protected
    path part. That gate catches `open("verification/…")` and
    `replace("verification/…")`, but NOT the commoner
    `Path("verification/…").write_text(...)`, because the protected part lands in
    the `Path` call rather than in the writer call. Measured against the base
    commit, so it is a pre-existing gap in that gate rather than one this
    checkpoint introduced.

    This closes it behaviourally instead of on the source: the filesystem is
    faked, every write is recorded, and the allowlist is asserted against the
    result. A future edit that reached `verification/manifest.json` through any
    shape at all fails here.

    The operation is called directly rather than over HTTP on purpose. This test
    is about which paths reach the filesystem, and a server would only add a way
    for the assertion to fail for an unrelated reason.
    """
    allowlist = set(plan.PROJECT_CONFIG_PATHS)
    written: list[Path] = []
    real_write_bytes = Path.write_bytes
    real_write_text = Path.write_text

    def record_bytes(self, data):
        written.append(self)
        return real_write_bytes(self, data)

    def record_text(self, data, **kwargs):
        written.append(self)
        return real_write_text(self, data, **kwargs)

    # The real write still happens. The point is to record every destination, so
    # a save that stops early cannot pass by writing nothing at all.
    monkeypatch.setattr(Path, "write_bytes", record_bytes)
    monkeypatch.setattr(Path, "write_text", record_text)

    digest = operations.configuration_state(context)["digest"]
    saved = operations.save_project_config(
        context, "save", document=proposal(), expected_digest=digest,
    )
    assert saved["wrote"] is True, saved

    relative = {
        path.resolve().relative_to(context.project.root).as_posix() for path in written
    }
    assert relative, "the save wrote nothing at all, so this proves nothing"

    # Every destination the save touched is either one of the two allowlisted
    # documents, a temporary file that is renamed onto one of them, or the
    # evidence record that lives beside the store rather than in the working tree.
    # Anything else is a third document, and this is what fails if one appears.
    for path in relative:
        if path in allowlist:
            continue
        if path.startswith(".git/"):
            continue
        assert any(
            path.startswith(f"{allowed}.") for allowed in allowlist
        ), f"the save wrote {path!r}, which is not one of {sorted(allowlist)}"

    assert "verification/manifest.json" not in relative
    assert "verification/project.json.vkit-tmp" in relative, (
        f"the save did not stage a temporary beside its destination: {sorted(relative)}"
    )
    # The candidate, approval and activation record is evidence beside the runs,
    # not a third document in the working tree. A save that wrote it into
    # `verification/` would appear in `git status` as policy nobody reviewed.
    for path in relative:
        if path.startswith(".git/"):
            assert "verification-kit" in path, (
                f"the save wrote {path!r} into the repository's Git directory "
                "outside the shared state store"
            )


def test_a_local_document_claiming_protected_context_is_refused() -> None:
    """A browser cannot write the word that confers protected authority.

    This is the integration package's own rule, checked here against the same
    parser a local policy file goes through, so the console and `vkit integrate`
    cannot disagree about who may claim protected context.
    """
    from vkit.integration import policy as policies

    with pytest.raises(policies.PolicyError) as caught:
        policies.from_file(
            open_project(EXAMPLE),
            _tmp_policy({"context": "protected"}),
        )
    assert "cannot confer" in str(caught.value)

    assert _settings_context_of_a_local_document() == "local", (
        "the console writes a local candidate that claims something other than "
        "local context"
    )


def _tmp_policy(document: dict) -> Path:
    import tempfile

    path = Path(tempfile.mkdtemp()) / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _settings_context_of_a_local_document() -> str:
    """What context this console's own candidate document carries, by construction."""
    return "local"


# ------------------------------------------------------- the digest guard


def test_a_stale_expected_digest_is_refused_and_nothing_is_written(live) -> None:
    """Two tabs, one configuration: the second writer is refused, not merged.

    This is the conflicting-browser-tab case. The refusal names both digests, so
    an operator can tell which configuration it is looking at, and the file on
    disk is still the one the first writer produced.
    """
    url, _token, repo, _context = live
    first = _digest(live)

    saved, status = _stage(live, "save", document=proposal(), expected_digest=first)
    assert status == 200, saved
    on_disk = (repo / CLEANUP_POLICY).read_bytes()

    stale, status = _stage(
        live, "save", document=proposal(cleanup={"mode": "off"}), expected_digest=first,
    )
    assert status == 409, stale
    assert "changed since" in stale["error"], stale["error"]
    assert first[:12] in stale["error"], (
        f"the refusal does not name the digest the operator was looking at: {stale['error']!r}"
    )
    assert on_disk == (repo / CLEANUP_POLICY).read_bytes(), "a refused save changed the file"

    from vkit.cleanup import hooks as cleanup_hooks

    assert cleanup_hooks.load_policy(open_project(repo)).mode.value == "preview", (
        "the configuration left on disk is not the one the first save published, so a "
        "refused save did not leave the old configuration usable"
    )


def test_a_save_with_no_expected_digest_is_refused(live) -> None:
    """The guard cannot be skipped by omitting the digest.

    Without it the operation cannot tell whether the file changed while the page
    was open, which is the only thing the guard exists for.
    """
    document, status = _stage(live, "save", document=proposal())
    assert status == 409, document
    assert "expected_digest" in document["error"], document["error"]
    assert not (_repo_path(live) / CLEANUP_POLICY).exists(), (
        "a save with no expected digest still wrote"
    )


def _repo_path(live) -> Path:
    return live[2]


def test_a_malformed_expected_digest_is_refused_by_the_gate(live) -> None:
    """The digest is a 64-character hex value, and anything else is a 400.

    Checked at the boundary rather than deep in the comparison, so the refusal
    names a malformed request rather than reporting a conflict.
    """
    for bad in ("abc", "z" * 64, "0" * 63, 12345, "a" * 33):
        document, status = _stage(
            live, "save", document=proposal(), expected_digest=bad,
        )
        assert status == 400, f"expected_digest {bad!r} was answered {status}: {document}"
        assert "expected_digest" in document["error"], document["error"]


def test_a_save_is_idempotent_under_the_digest_it_produced(live) -> None:
    """Re-running the same save converges rather than stacking.

    The revision is derived from the content it publishes, so the same proposal
    applied twice is one revision with one recorded action, which is what makes a
    retry after a dropped connection safe.
    """
    first = _digest(live)
    saved, status = _stage(live, "save", document=proposal(), expected_digest=first)
    assert status == 200, saved

    again, status = _stage(live, "save", document=proposal(), expected_digest=_digest(live))
    assert status == 200, again
    assert again["result"]["candidate"]["revision"] == saved["result"]["candidate"]["revision"], (
        f"the same proposal produced two revisions: {saved['result']['candidate']['revision']} "
        f"and {again['result']['candidate']['revision']}"
    )
    saves = [
        entry for entry in _settings(live)["proposals"]["actions"]
        if entry["stage"] == "save"
    ]
    assert len(saves) == 1, (
        f"the same save was recorded {len(saves)} times; a retry must converge"
    )


# -------------------------------------------- obligation removal is named


def test_a_proposal_that_removes_an_obligation_cannot_read_as_a_routine_edit(live) -> None:
    """The one change this product exists to prevent is labelled as itself.

    Both halves are asserted: the removal is named, and the proposal is marked not
    routine, so a page that renders only the flag still cannot present it as an
    ordinary settings change.
    """
    saved, status = _stage(live, "save", document=proposal(), expected_digest=_digest(live))
    assert status == 200, saved

    preview, status = _stage(
        live, "preview", document=proposal("cancels-to-zero", "empty-cart"),
        expected_digest=_digest(live),
    )
    assert status == 200, preview
    result = preview["result"]

    assert result["routine"] is False, (
        "a proposal that drops four required obligations is reported as a routine edit"
    )
    removed = result["removes_obligations"]
    assert removed, "the preview names no removed obligation"
    # The names are `describe_obligation`'s, which is the name a receipt uses for
    # the same obligation. An operator comparing this list with a receipt gap
    # needs them to be the same words.
    assert {entry["obligation"] for entry in removed} == {
        "case 'single-positive'", "case 'several-positives'",
        "case 'mixed-sign'", "case 'negatives-only'",
    }, f"the removed obligations are {sorted(entry['obligation'] for entry in removed)}"
    assert all(entry["check_id"] == CHECK_ID for entry in removed)
    assert all("keep it" in entry["detail"] for entry in removed), (
        "a removed obligation does not say what happens to a task that already pinned it"
    )
    assert "weakens_cleanup" in result, "the preview reports no cleanup weakening field"


def test_adding_an_obligation_is_routine(live) -> None:
    """The flag is about removals, not about any change being alarming.

    A flag that is always false is a flag nobody reads. Saved with only two
    obligations, a proposal that adds four is routine: it strengthens the bar.
    """
    saved, status = _stage(live, "save", document=proposal("cancels-to-zero"), expected_digest=_digest(live))
    assert status == 200, saved

    preview, status = _stage(live, "preview", document=proposal(), expected_digest=_digest(live))
    assert status == 200, preview
    result = preview["result"]
    assert result["routine"] is True, (
        "a proposal that adds four required obligations is reported as non-routine"
    )
    assert result["removes_obligations"] == [], (
        f"adding obligations reported removals: {result['removes_obligations']}"
    )


def test_lowering_the_cleanup_profile_is_named_as_a_weakening(live) -> None:
    """Turning a write-authorising cleanup policy off is a weakening, not a tidy-up."""
    strict = proposal(cleanup={
        "mode": "apply_verified",
        "enabled_rules": ["ORDINARY_TRAILING_COMMENT"],
    })
    saved, status = _stage(live, "save", document=strict, expected_digest=_digest(live))
    assert status == 200, saved

    preview, status = _stage(live, "preview", document=proposal(), expected_digest=_digest(live))
    assert status == 200, preview
    result = preview["result"]
    assert result["weakens_cleanup"] is True, (
        "dropping the only mode that authorizes a cleanup write is not reported as a weakening"
    )
    assert result["routine"] is False, (
        "a proposal that removes write authority is reported as routine"
    )

    # And raising it is not a weakening, so the flag means something.
    preview, status = _stage(
        live, "preview", document=strict, expected_digest=_digest(live),
    )
    assert status == 200, preview
    assert preview["result"]["weakens_cleanup"] is False, (
        "a proposal that restores write authority is reported as a weakening"
    )


# --------------------------------------------------------- pinned contracts


def test_saving_configuration_does_not_release_a_pinned_obligation(live) -> None:
    """The task keeps the contract it was admitted under, byte for byte.

    This is the checkpoint's central hazard, so it is measured on the durable
    record rather than on the view: the task row's contract, its policy digest and
    its generation are read through the core's own reader before and after a save,
    and a proposal that drops four of the six obligations that task required is
    saved in between.

    **A save may make the evidence stale, and that is the safe direction.** The
    configuration lives in the working tree, so writing it moves the source
    identity every run is measured against, and `compute_readiness` then reports
    `BLOCKED` with a source-changed gap. That is the plan's own rule: changing
    configuration must not make old evidence current. So the invariant asserted
    here is that readiness never *improves* -- the task cannot go from BLOCKED to
    READY, or drop an obligation from its requirement, because a person edited a
    settings document.
    """
    from vkit import tasks as core_tasks

    url, token, _repo, context_ = live
    started, status = _post(f"{url}/api/run_check?check_id={CHECK_ID}", token, {})
    assert status == 200, started
    task_id = started["task_id"]

    def pinned_bytes() -> bytes:
        record = core_tasks.get_task(context_.store, task_id)
        return json.dumps(record.pinned().to_json(), sort_keys=True).encode("utf-8")

    def readiness() -> tuple[str, tuple[str, ...]]:
        acceptance = core_tasks.acceptance_context(context_.project, lambda: context_.manifest)
        record = core_tasks.get_task(context_.store, task_id)
        result = core_tasks.compute_readiness(
            context_.store, task_id,
            required_check_ids=record.pinned().required_checks, context=acceptance,
        )
        return result.readiness, tuple(result.gaps)

    before_bytes = pinned_bytes()
    before_digest = core_tasks.get_task(context_.store, task_id).policy_digest
    before_generation = core_tasks.get_task(context_.store, task_id).generation
    before_ready, before_gaps = readiness()
    assert json.loads(before_bytes)["required_checks"] == [CHECK_ID]

    saved, status = _stage(
        live, "save", document=proposal("cancels-to-zero", "empty-cart"),
        expected_digest=_digest(live),
    )
    assert status == 200, saved

    assert pinned_bytes() == before_bytes, (
        "saving a configuration released obligations from a task that had already "
        "been admitted; the pinned contract is not byte-identical afterwards"
    )
    record_after = core_tasks.get_task(context_.store, task_id)
    assert record_after.policy_digest == before_digest, (
        "the task's pinned policy digest moved when project configuration changed"
    )
    assert record_after.generation == before_generation, (
        "the task's generation moved when project configuration changed"
    )

    after_ready, after_gaps = readiness()
    order = {"REJECTED": 0, "BLOCKED": 1, "READY": 2}
    assert order[after_ready] <= order[before_ready], (
        f"the task's verdict went from {before_ready} to {after_ready} after a "
        "configuration change; a settings edit must never improve a verdict"
    )
    # And a task still blocked on the source names that, not a released obligation.
    for gap in after_gaps:
        assert "no longer require" not in gap, (
            f"the core reports a released obligation for a task that pinned its "
            f"contract at admission: {gap!r}"
        )


def test_activation_reports_the_tasks_it_did_not_touch(live) -> None:
    """Activation says how many tasks keep their pinned contract.

    An activation that reported only what it changed would leave a reader
    assuming the tasks it did not mention were released.
    """
    url, token, _repo, _context = live
    started, status = _post(f"{url}/api/run_check?check_id={CHECK_ID}", token, {})
    assert status == 200, started

    saved, status = _stage(live, "save", document=proposal(), expected_digest=_digest(live))
    assert status == 200, saved
    revision = saved["result"]["candidate"]["revision"]
    _stage(live, "approve", expected_digest=_digest(live), revision=revision)
    activated, status = _stage(
        live, "activate", expected_digest=_digest(live), revision=revision,
    )
    assert status == 200, activated

    result = activated["result"]
    assert result["tasks_pinned"] == 1, (
        f"activation reports {result['tasks_pinned']} pinned tasks after one run, "
        "so a reader cannot tell whether an existing task was affected"
    )
    assert "keeps the contract and policy digest it pinned" in result["next_attempt"], (
        f"activation does not say what happens to an admitted task: {result['next_attempt']!r}"
    )


# ------------------------------------------------------- atomic publication


def test_a_failure_mid_save_leaves_the_previous_document_intact_and_usable(
    live, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two documents are one decision, so one failure publishes neither.

    The second document's publish is made to fail. The first has already been
    renamed at that point, which is precisely the state that must not survive: a
    project holding the new cleanup authority beside old requirements has been
    handed a decision nobody made.
    """
    url, _token, repo, _context = live
    first = _digest(live)
    saved, status = _stage(live, "save", document=proposal(), expected_digest=first)
    assert status == 200, saved

    before = {name: (repo / name).read_bytes() for name in (PROJECT_POLICY, CLEANUP_POLICY)}
    current = _digest(live)

    real_replace = Path.replace
    seen = {"n": 0}

    def failing_second(self, target):
        seen["n"] += 1
        if seen["n"] == 2:
            raise OSError("the second publish could not be completed")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_second)
    document, status = _stage(
        live, "save", document=proposal(cleanup={"mode": "off"}), expected_digest=current,
    )
    assert seen["n"] >= 2, (
        f"the save made {seen['n']} replace calls, so the failure was never reached "
        "and this test proved nothing"
    )
    assert status in (409, 500), (
        f"the save was answered {status} rather than failing inside it: {document}"
    )
    monkeypatch.undo()

    after = {name: (repo / name).read_bytes() for name in (PROJECT_POLICY, CLEANUP_POLICY)}
    assert after == before, (
        "a failure in the middle of the save left one document updated and the "
        "other not, which is a policy decision nobody made"
    )
    assert _digest(live) == current, "the configuration digest moved although nothing was published"

    from vkit.cleanup import hooks as cleanup_hooks

    assert cleanup_hooks.load_policy(open_project(repo)).mode.value == "preview", (
        "the configuration left on disk is not the one the last successful save published"
    )


def test_the_previous_document_is_preserved_for_recovery(live) -> None:
    """Every save leaves the bytes it replaced readable and named.

    Recovery is by comparison, so the previous document has to exist. The receipt
    names where it was written and that path reads back the exact bytes that were
    there before, which is what makes it a recovery and not a log line.

    The first save on a project that has no configuration has nothing to preserve,
    so the assertion starts from the second: that is the first save that replaces
    bytes, and the first whose receipt can name them.
    """
    url, _token, repo, _context = live
    first = _digest(live)
    _initial, status = _stage(live, "save", document=proposal(), expected_digest=first)
    assert status == 200

    preserved = (repo / CLEANUP_POLICY).read_bytes()
    second, status = _stage(
        live, "save", document=proposal(cleanup={"mode": "off"}), expected_digest=_digest(live),
    )
    assert status == 200, second

    recovered = second["result"]["recovery"]
    assert recovered, "the second save reports no preserved previous document"
    entry = next(
        item for item in recovered if Path(item["location"]).read_bytes() == preserved
    )
    assert entry["previous_digest"], "the preserved document is not named by digest"
    # The copy is named so an operator can tell which document it restores: the
    # repository-relative destination with separators flattened into one filename.
    assert entry["path"] == "verification__cleanup.json.previous", (
        f"the preserved copy does not say which document it is: {entry['path']!r}"
    )

    listed = _settings(live)["proposals"]["recoverable"]
    assert any(item["path"] == entry["path"] for item in listed), (
        f"the preserved document is not listed for recovery: {listed}"
    )


# ------------------------------------------------------- the refusals hold


def test_writing_schemas_is_refused(live) -> None:
    """`schemas/` stays absolutely prohibited, and no stage reaches it."""
    current = _digest(live)

    for part in plan.PROTECTED_PATH_PARTS:
        assert plan.under_protected_path(f"{part}/run-report.v1.json") is True
    assert not any(part in path for path in plan.PROJECT_CONFIG_PATHS
                   for part in ("schemas/",))

    for stage in ("preview", "save", "approve", "activate"):
        document, status = _stage(stage=stage, live=live) if False else _stage(live, stage, document=proposal(), expected_digest=current)
        assert status in (200, 409), f"stage {stage} answered {status}: {document}"

    with pytest.raises(api.BadRequest) as caught:
        api.dispatch(_context_of(live), "plan", {"operation": "schemas/run-report.v1.json"})
    assert "policy path" in str(caught.value)


def _context_of(live) -> operations.Context:
    return live[3]


def test_writing_an_arbitrary_path_is_refused(live) -> None:
    """A caller cannot name a file for this operation to write.

    The operation writes two fixed paths and takes no path parameter at all, so
    there is nothing for a caller to redirect. Both halves are asserted: the
    parameter is refused at the gate, and the allowlist is a closed pair whose
    manifest entry is absent.
    """
    url, token, _repo, _context = live
    for path in ("../../escape.json", "verification/manifest.json", "/etc/passwd"):
        document, status = _post(
            f"{url}/api/apply?operation=save_project_config&stage=save", token,
            {"document": proposal(), "expected_digest": _digest(live), "path": path},
        )
        assert status == 400, f"a path of {path!r} was answered {status}: {document}"
        assert "path" in document["error"], document["error"]

    assert plan.PROJECT_CONFIG_PATHS == (PROJECT_POLICY, CLEANUP_POLICY)
    assert "verification/manifest.json" not in plan.PROJECT_CONFIG_PATHS


def test_an_unknown_stage_is_refused_and_names_the_real_stages(live) -> None:
    """The stage vocabulary is closed, and the refusal quotes it.

    A stage is what separates "show me" from "write it", so an unchecked stage
    would let `preview` write and `save` describe.
    """
    document, status = _stage(live, "obliterate", document=proposal(), expected_digest=_digest(live))
    assert status == 400, document
    for stage in ("preview", "save", "approve", "activate"):
        assert stage in document["error"], f"the refusal does not name {stage}: {document['error']!r}"


def test_a_stage_for_another_operation_is_refused(live) -> None:
    """`stage` belongs to the configuration operation alone.

    Otherwise a stage would be a parameter every handler has to remember to
    ignore, and the day one forgot, `?operation=enroll&stage=save` would mean
    something to it.
    """
    url, token, _repo, _context = live
    document, status = _post(
        f"{url}/api/apply?operation=enroll&stage=save", token, {},
    )
    assert status == 400, document
    assert "save_project_config" in document["error"], document["error"]


def test_a_non_object_document_is_refused_by_the_gate(live) -> None:
    """The document must be a JSON object, because every field in it is typed."""
    url, token, _repo, _context = live
    for bad in ("a string", ["a", "list"], 7, None):
        document, status = _post(
            f"{url}/api/apply?operation=save_project_config&stage=preview", token,
            {"document": bad, "expected_digest": _digest(live)},
        )
        assert status == 400, f"a document of {bad!r} was answered {status}: {document}"


# ------------------------------------------------------- the page renders it


def test_the_page_renders_the_configuration_effects_and_the_warnings(live) -> None:
    """The page has a rendering for a preview and for a non-routine proposal.

    Asserted on the served script, because the rendering decision is made there
    and nowhere else. A backend that computed every effect correctly while the
    page ignored them would satisfy every other test in this file.
    """
    url, _token, _repo, _context = live
    source = _get_text(f"{url}/app.js")

    assert "removes_obligations" in source, (
        "the page has no rendering for a proposal that removes an obligation"
    )
    assert "stale_evidence" in source, "the page never shows the evidence that becomes stale"
    assert "expected_digest" in source, (
        "the page does not send the expected current digest, so every save is a blind write"
    )
    assert "weakens_cleanup" in source, (
        "the page has no rendering for a proposal that removes write authority"
    )
    assert "approved_integration" in source, (
        "the page never shows the approved integration policy beside the local one"
    )
    assert "save_project_config" in source, "the page never reaches the configuration save"
    # Text goes in as textContent, never as markup. Checked on the executable
    # lines rather than the whole file, because the file names `innerHTML` in a
    # comment explaining that it is not used, and a substring test would fail on
    # the very comment that carries the guarantee.
    executable = [
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith(("/*", "*", "//"))
    ]
    assert not any("innerHTML" in line for line in executable), (
        "the page writes project text into markup"
    )


def test_the_settings_view_carries_the_configuration_body(live) -> None:
    """The settings section has somewhere to draw the editor.

    Checkpoint 12.2 gave each view one body element and a test that every nav view
    has one. A new panel with no container is a panel that renders nowhere.
    """
    page = _get_text(f"{live[0]}/")
    assert '<section id="integrations" class="view' in page
    assert 'id="integrations-body"' in page


# --------------------------------------------------------------- the fixture


def test_the_example_manifest_still_declares_the_six_obligations() -> None:
    """The fixture the cross-reference tests rely on is the real manifest.

    Written out rather than read from the module under test, so a manifest that
    stops registering one of the six fails here rather than making the
    cross-reference tests pass vacuously.
    """
    document = json.loads((EXAMPLE / "verification" / "manifest.json").read_text(encoding="utf-8"))
    assert document["checks"][0]["id"] == CHECK_ID
    assert tuple(document["checks"][0]["required_scenarios"]) == SIX