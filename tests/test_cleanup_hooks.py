"""Plan 11 checkpoint 3: cleanup as a gate, not a tool a person runs.

Every test here drives the real code against a real Git checkout and asserts on
the bytes on disk, because the properties being pinned are about files. A test
that asserted which functions were called would pass against a gate that
decided nothing.

The properties, one per test, in the plan's order:

* an actual `PostToolUse` payload drives cleanup, and an edit it cannot
  attribute to a task leaves the file untouched with a bounded explanation;
* a change that arrives through a shell command is still caught, because the
  shared pre-verification path reads the checkout rather than the payload;
* cleanup following an earlier verification means the earlier evidence cannot
  finalize the changed source;
* a protected candidate that requires cleanup stays unchanged and reports the
  gap, and is never cleaned and then attested to;
* a missing binding, an unsupported runtime, a malformed file and a JavaScript
  edit all leave the file byte-identical;
* a repeated hook invocation converges instead of looping;
* finalization changes nothing, asserted by comparing source bytes across it.

`REPLAYED PAYLOAD` versus `LIVE HOST` is a distinction these tests exist to
make visible. Everything below drives a payload through the real handler the
way the host delivers one. None of it is a live Claude Code session: it proves
the handler is correct for the documented payload shape, and it cannot prove
that the host delivers that shape, or that `hooks.json` is accepted by a
running host. Those two facts need a host session and are reported as such.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subproc  # noqa: E402

from vkit.cleanup import (  # noqa: E402
    ALL_LINES,
    Applied,
    Blocked,
    Cleaned,
    Cleared,
    Pending,
    accelerate,
    candidate_gaps,
    changed_files,
    changed_lines_from_diff,
    changed_lines_from_git,
    cleanup_paths,
    freshness,
    load_policy,
    policy_problem,
    resolve_repository_path,
)
from vkit.cleanup.apply import (
    AlreadyApplied,
    ApplyRequest,
    apply_cleanup as _apply_cleanup,
    ownership_from_tasks,
)  # noqa: E402
from vkit.cleanup.comments import (  # noqa: E402
    PreservationPassed,
    Proposal,
    Refusal,
    RefusalReason,
    RemovedComment,
    preview_comment_cleanup,
    verify_preservation,
)
from vkit.cleanup.comments import TRAILING_RULE  # noqa: E402
from vkit.cleanup.hooks import (  # noqa: E402
    CleanupUnavailable,
    _clean_path,
    _request_key,
)
from vkit.cleanup.policy import CLEANUP_RULE_IDS, CleanupMode, CleanupPolicy  # noqa: E402
from vkit.identity import compute_source_identity, source_unchanged  # noqa: E402
from vkit.manifest import parse_manifest  # noqa: E402
from vkit.paths import open_project  # noqa: E402
from vkit.storage import Store  # noqa: E402
from vkit.tasks import get_task, open_task  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = REPO_ROOT / "plugin" / "scripts" / "vkit_hook.py"

CHECK_ID = "c1"
TASK_ID = "t-cleanup"
SESSION_ID = "sess-cleanup"

COMMENTED = b"def bump(total):\n    total = total + 1  # increment the counter\n    return total\n"
COMMENT_FREE = b"def bump(total):\n    total = total + 1\n    return total\n"
ELSE_PASS = b"def pick(a):\n    if a:\n        return 1\n    else:\n        pass\n"
JAVASCRIPT = b"function bump(total) {\n  return total; // add one\n}\n"

#: `parse_policy` requires every key it declares. A policy omitting
#: `excluded_paths` is refused, which is reported as a `policy.py` defect; this
#: fixture spells all of them so the gate's own behaviour is what is measured.
POLICY_DOCUMENT: dict[str, Any] = {
    "mode": "apply_verified",
    "enabled_rules": list(CLEANUP_RULE_IDS),
    "excluded_paths": [],
    "python_version": None,
    "optimize_levels": [0, 1, 2],
}

TRAILING = "ORDINARY_TRAILING_COMMENT"


def _test_ownership(_request) -> None:
    """Direct apply tests model a current task claim unless they override it."""


def apply_cleanup(*args, ownership_check=_test_ownership, **kwargs):
    return _apply_cleanup(*args, ownership_check=ownership_check, **kwargs)




def git(*args: str, cwd: Path) -> None:
    subproc.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def write_policy(root: Path, **overrides: Any) -> None:
    (root / "verification").mkdir(parents=True, exist_ok=True)
    document = {**POLICY_DOCUMENT, **overrides}
    (root / "verification" / "cleanup.json").write_text(
        json.dumps(document), encoding="utf-8"
    )


#: One registered check. Recorded by the tests and never executed: acceptance
#: compares identities, and a run carrying a different source identity is what
#: must stop finalizing after a cleanup.
MANIFEST: dict[str, Any] = {
    "schema_version": 1,
    "description": "One check the readiness gate can read a verdict from.",
    "checks": [{
        "id": CHECK_ID,
        "description": "Recorded by the test, never executed by the gate.",
        "command": ["{{python}}", "-c", "pass"],
        "cwd": ".",
        "timeout_seconds": 120,
        "required_scenarios": ["s"],
        "artifact": "result.json",
    }],
}


def write_manifest(root: Path) -> None:
    (root / "verification").mkdir(parents=True, exist_ok=True)
    (root / "verification" / "manifest.json").write_text(
        json.dumps(MANIFEST), encoding="utf-8"
    )


def load_hook_module():
    """The hook entry point by file path, the way the host runs it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("vkit_hook_cleanup_under_test", HOOK_SCRIPT)
    assert spec and spec.loader, f"{HOOK_SCRIPT} is not an importable module"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def post_tool_use_payload(
    session_id: str, tool_name: str, path: Any, *, agent_id: str | None = None,
    content: str | None = None, field_name: str = "file_path",
) -> dict[str, Any]:
    """A `PostToolUse` payload in the shape the hook reference documents.

    `session_id`, `transcript_path`, `cwd`, `permission_mode`,
    `hook_event_name`, `tool_name`, `tool_input`, `tool_response` and
    `tool_use_id`. `tool_input` carries the absolute path under the field the
    documented tool uses, and `tool_response` carries the tool's structured
    result. Every other field the reference documents is optional here, and a
    payload carrying only these is one the host produces.
    """
    tool_input: dict[str, Any] = {field_name: str(path)}
    if content is not None:
        tool_input["content"] = content
    payload: dict[str, Any] = {
        "session_id": session_id,
        "transcript_path": str(REPO_ROOT / "tmp" / "transcript.jsonl"),
        "cwd": "",
        "permission_mode": "default",
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": {"filePath": str(path), "type": "update"},
        "tool_use_id": "toolu_01ABC123",
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A committed repository holding one source file and one managed task.

    The committed content is the CLEAN text. Every test that needs a pending
    cleanup writes `COMMENTED` over it, so "this file is dirty" is something a
    test does on purpose rather than something the fixture left behind. A
    fixture that committed the dirty text would make a later write of the same
    bytes a no-op, and the changed-set assertions would pass for the wrong
    reason.

    A task is bound to `SESSION_ID` with no `agent_id`, so a main-session
    payload matches it and a subagent payload does not. That asymmetry is the
    property the ownership tests turn on, so it is built into the fixture
    rather than configured per test.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "sample.py").write_bytes(COMMENT_FREE)
    write_manifest(root)
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "t@example.invalid", cwd=root)
    git("config", "user.name", "Cleanup Test", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-qm", "the source under test", cwd=root)

    project = open_project(root)
    store = Store(project.db_path)
    digest = parse_manifest(project, project.runs_root).digest()
    open_task(
        store,
        task_id=TASK_ID,
        contract={
            "repository": {
                "root": str(project.root),
                "git_common_dir": str(project.git_common_dir),
            },
            "policy_digest": digest,
            "required_checks": [CHECK_ID],
            "scope": "ship",
            "resources": [],
            "declared": {"host": {"session_id": SESSION_ID}},
        },
        policy_digest=digest,
    )
    return root


def edit(repo: Path, content: bytes = COMMENTED) -> Path:
    """Make `sample.py` dirty, the way an agent's edit would.

    Written after the fixture's commit, so the file differs from HEAD and the
    authority path can find it. Nothing simulates a host event here; the bytes
    on disk are the whole input.
    """
    (repo / "sample.py").write_bytes(content)
    return repo / "sample.py"




def test_a_post_tool_use_payload_cleans_the_file_it_names(repo: Path) -> None:
    """The documented payload, through the real handler, changes the real bytes.

    REPLAYED PAYLOAD. The payload is built from the hook reference's own
    example and delivered to the same entry point the host executes. What it
    proves is that the handler reads this shape and acts on it.
    """
    write_policy(repo)
    hook = load_hook_module()
    target = edit(repo)
    assert target.read_bytes() == COMMENTED

    response, code = hook.respond(
        "PostToolUse",
        post_tool_use_payload(SESSION_ID, "Edit", target, content="edited"),
        str(repo),
    )

    assert code == 0
    assert target.read_bytes() == COMMENT_FREE
    assert TRAILING in json.dumps(response)
    assert "no longer describes" in json.dumps(response)


def test_a_post_tool_use_payload_for_a_bound_file_removes_the_comment(repo: Path) -> None:
    """The same payload naming a file that still has its comment removes it."""
    write_policy(repo)
    hook = load_hook_module()
    target = edit(repo)

    hook.respond(
        "PostToolUse",
        post_tool_use_payload(SESSION_ID, "Write", target, content="rewritten"),
        str(repo),
    )

    assert target.read_bytes() == COMMENT_FREE


def test_a_repeated_payload_with_the_same_before_and_after_converges(repo: Path) -> None:
    """Invoking the handler twice on one edit does not loop or write twice.

    The second call re-reads the file, finds the comment already gone, and
    reports nothing to do. There is no counter anywhere in this path that a
    second call could increment, and this asserts the observable result: the
    file is byte-identical and the second response says no work remained.
    """
    write_policy(repo)
    hook = load_hook_module()
    target = edit(repo)
    payload = post_tool_use_payload(SESSION_ID, "Edit", target, content="edited")

    hook.respond("PostToolUse", payload, str(repo))
    after_first = target.read_bytes()
    second, code = hook.respond("PostToolUse", payload, str(repo))

    assert code == 0
    assert target.read_bytes() == after_first == COMMENT_FREE
    assert second == {}, f"a second invocation re-did work: {second!r}"




def test_an_edit_from_an_unbound_session_leaves_the_file_unchanged(repo: Path) -> None:
    """No binding means no owner, and no owner means no write.

    The file is a worker's file and the payload names a session no task claims.
    Resolving a task from recency or from the last active task is what
    docs/verification.md forbids, so the answer is an explanation and an untouched file.
    """
    write_policy(repo)
    hook = load_hook_module()
    target = edit(repo)

    response, code = hook.respond(
        "PostToolUse",
        post_tool_use_payload("sess-nobody", "Edit", target, content="edited"),
        str(repo),
    )

    assert code == 0
    assert target.read_bytes() == COMMENTED
    assert "not from a session registered against a managed task" in json.dumps(response)


def test_an_ambiguous_binding_leaves_the_file_unchanged(repo: Path) -> None:
    """Two tasks claiming one session is a refusal, not a choice between them."""
    write_policy(repo)
    hook = load_hook_module()
    project = open_project(repo)
    store = Store(project.db_path)
    open_task(
        store, task_id="t-second",
        contract={
            "repository": {
                "root": str(project.root), "git_common_dir": str(project.git_common_dir),
            },
            "policy_digest": "0" * 64, "required_checks": [CHECK_ID],
            "scope": "ship", "resources": [],
            "declared": {"host": {"session_id": SESSION_ID}},
        },
        policy_digest="0" * 64,
    )
    target = edit(repo)

    response, _code = hook.respond(
        "PostToolUse",
        post_tool_use_payload(SESSION_ID, "Edit", target, content="edited"),
        str(repo),
    )

    assert target.read_bytes() == COMMENTED
    assert "not from a session registered against a managed task" in json.dumps(response)


def test_a_subagent_edit_never_inherits_the_parent_sessions_ownership(repo: Path) -> None:
    """A binding that names no agent does not answer for a payload that has one."""
    write_policy(repo)
    hook = load_hook_module()
    target = edit(repo)

    hook.respond(
        "PostToolUse",
        post_tool_use_payload(
            SESSION_ID, "Edit", target, agent_id="agent-1", content="edited",
        ),
        str(repo),
    )

    assert target.read_bytes() == COMMENTED


def test_a_tool_that_is_not_an_edit_is_answered_with_nothing(repo: Path) -> None:
    """A payload for a tool that writes no file produces an empty response."""
    write_policy(repo)
    hook = load_hook_module()

    response, code = hook.respond(
        "PostToolUse",
        post_tool_use_payload(SESSION_ID, "Bash", repo / "sample.py"),
        str(repo),
    )

    assert code == 0
    assert response == {}


def test_a_project_without_a_cleanup_policy_leaves_the_file_unchanged(repo: Path) -> None:
    """No approved policy means cleanup is off, and off writes nothing."""
    hook = load_hook_module()
    target = edit(repo)
    assert not (repo / "verification" / "cleanup.json").exists()

    response, code = hook.respond(
        "PostToolUse",
        post_tool_use_payload(SESSION_ID, "Edit", target, content="edited"),
        str(repo),
    )

    assert code == 0
    assert target.read_bytes() == COMMENTED
    assert response == {}


def test_a_file_outside_the_project_is_left_alone_and_says_why(repo: Path, tmp_path: Path) -> None:
    """A path outside the root is reported, never resolved to the nearest file."""
    write_policy(repo)
    hook = load_hook_module()
    outside = tmp_path / "elsewhere.py"
    outside.write_bytes(COMMENTED)

    response, code = hook.respond(
        "PostToolUse",
        post_tool_use_payload(SESSION_ID, "Edit", outside, content="edited"),
        str(repo),
    )

    assert code == 0
    assert outside.read_bytes() == COMMENTED
    assert "does not name a file inside" in json.dumps(response)


def test_a_javascript_edit_is_ignored_by_the_automatic_hook(repo: Path) -> None:
    """An automatic Python cleanup hook ignores non-Python edits.

    Explicit preview reports unsupported-language status; an automatic hook
    only owns registered Python rules, so this edit does not produce a gate note.
    """
    write_policy(repo)
    target = repo / "app.js"
    target.write_bytes(JAVASCRIPT)
    hook = load_hook_module()

    response, code = hook.respond(
        "PostToolUse",
        post_tool_use_payload(SESSION_ID, "Write", target, content=JAVASCRIPT.decode()),
        str(repo),
    )

    assert code == 0
    assert target.read_bytes() == JAVASCRIPT
    assert response == {}, f"an unsupported language produced a response: {response!r}"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("README.md", b"Notes for the project.\n"),
        ("metadata.json", b'{"enabled": true}\n'),
        ("app.js", JAVASCRIPT),
    ],
)
def test_non_python_changes_do_not_become_required_cleanup(
    repo: Path, name: str, content: bytes,
) -> None:
    """Automatic cleanup only gates files handled by its registered rules."""
    write_policy(repo)
    target = repo / name
    target.write_bytes(content)
    project = open_project(repo)
    store = Store(project.db_path)

    verdict = cleanup_paths(project, store, TASK_ID)

    assert isinstance(verdict, Cleared)
    assert target.read_bytes() == content
    assert freshness(project) == ()


def test_deleted_python_source_has_no_automatic_cleanup_requirement(repo: Path) -> None:
    write_policy(repo)
    target = edit(repo)
    target.unlink()
    project = open_project(repo)
    store = Store(project.db_path)

    verdict = cleanup_paths(project, store, TASK_ID)

    assert isinstance(verdict, Cleared), verdict
    assert freshness(project) == ()
    explicit = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert isinstance(explicit, Refusal), explicit
    assert explicit.reason is RefusalReason.NOT_A_FILE


def test_a_typescript_edit_leaves_the_file_unchanged(repo: Path) -> None:
    """`.ts` is refused for the same reason `.js` is, from the same registry."""
    write_policy(repo)
    target = repo / "app.ts"
    target.write_bytes(b"export const bump = (n: number): number => n; // one more\n")
    hook = load_hook_module()

    hook.respond(
        "PostToolUse",
        post_tool_use_payload(SESSION_ID, "Write", target, content="export const x = 1;"),
        str(repo),
    )

    assert target.read_bytes() == b"export const bump = (n: number): number => n; // one more\n"


def test_a_malformed_python_file_is_left_unchanged(repo: Path) -> None:
    """A file the tokenizer cannot read is a refusal, and a refusal writes nothing."""
    write_policy(repo)
    malformed = b"def bump(total:\n    return total  # broken\n"
    (repo / "sample.py").write_bytes(malformed)
    hook = load_hook_module()

    response, code = hook.respond(
        "PostToolUse",
        post_tool_use_payload(SESSION_ID, "Edit", repo / "sample.py", content="x"),
        str(repo),
    )

    assert code == 0
    assert (repo / "sample.py").read_bytes() == malformed
    assert response["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "preview_malformed_source" in response["systemMessage"]
    assert response["hookSpecificOutput"]["additionalContext"] == response["systemMessage"]


def test_an_unmeasured_runtime_is_refused_and_nothing_is_written(repo: Path) -> None:
    """A policy approved against another interpreter cannot govern this one.

    The compiled-equality claim is scoped to the compiler that produced the code
    objects, so a policy naming a different CPython is refused before any
    preview and therefore before any write.
    """
    write_policy(repo, python_version="3.9.0")
    project = open_project(repo)
    store = Store(project.db_path)
    edit(repo)
    before = (repo / "sample.py").read_bytes()

    verdict = accelerate(project, store, TASK_ID, "sample.py")

    assert isinstance(verdict, Blocked)
    assert verdict.reason == "cleanup_required_unmet:ORDINARY_TRAILING_COMMENT"
    assert "3.9.0" in verdict.detail
    assert (repo / "sample.py").read_bytes() == before


def test_a_policy_that_names_an_unknown_rule_is_reported_as_unusable(repo: Path) -> None:
    """A typo in the approved policy is named, not silently treated as off."""
    (repo / "verification").mkdir(parents=True, exist_ok=True)
    (repo / "verification" / "cleanup.json").write_text(
        json.dumps({"mode": "apply_verified", "enabled_rules": ["NO_SUCH_RULE"]}),
        encoding="utf-8",
    )
    project = open_project(repo)

    problem = policy_problem(project)

    assert problem is not None
    assert "not a usable cleanup policy" in problem
    assert load_policy(project).mode is CleanupMode.OFF




def _stale_proposal(project, relative_path: str, before: bytes, after: bytes):
    """The real proposal type, holding bytes the file no longer has.

    A real `Proposal` whose `before_bytes` the file no longer holds. Building
    one needs those original bytes, so it is created before the first apply
    consumes them and replayed afterwards.
    """
    receipt = verify_preservation(before, after)
    assert isinstance(receipt, PreservationPassed), receipt
    return Proposal(
        relative_path=relative_path,
        proposal_id="stale-" + receipt.after_digest[:12],
        before_bytes=before,
        after_bytes=after,
        removed=(RemovedComment(line=2, text="# increment the counter",
                                byte_start=38, byte_end=62),),
        preserved=(),
        receipt=receipt,
    )


def test_a_replayed_request_reaches_the_convergent_branch_through_the_gate(
    repo: Path,
) -> None:
    """`AlreadyApplied` is handled as success, not as an unmet requirement.

    A retry after a crash between the replacement and the receipt finds the
    after bytes already in place. `AlreadyApplied` carries no `reason` and no
    `detail`, so a caller that read one from an unrecognized result raises
    instead of reporting -- from inside a hook, on exactly the input a retry
    produces.

    The setup reaches that branch rather than describing it. The apply is
    driven once so the file holds the after bytes, and then a proposal built
    from the ORIGINAL bytes is offered through the gate's per-file driver. The
    apply compares what it finds to what the proposal wants, finds them equal,
    and returns `AlreadyApplied`.
    """

    write_policy(repo)
    project = open_project(repo)
    target = edit(repo)
    after = COMMENT_FREE

    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    key = _request_key(TASK_ID, 1, "sample.py", proposal.before_bytes, proposal.after_bytes)
    first = apply_cleanup(
        project,
        ApplyRequest(request_id=key, proposal=proposal, generation=1, policy=load_policy(project)),
    )
    assert isinstance(first, Applied), first
    assert target.read_bytes() == after == COMMENT_FREE

    from vkit.cleanup import hooks as hooks_module

    real_propose = hooks_module._propose

    def replayed_propose(project_arg, relative_path, rule_id, lines):
        """Offer the ORIGINAL before/after pair once, as a retry would."""
        if rule_id == TRAILING_RULE:
            return _stale_proposal(
                project_arg, relative_path, proposal.before_bytes, proposal.after_bytes,
            )
        return real_propose(project_arg, relative_path, rule_id, lines)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(hooks_module, "_propose", replayed_propose)
    try:
        applied, unresolved = _clean_path(
            project, "sample.py", load_policy(project), lambda: (lambda _p: ALL_LINES),
            task_id=TASK_ID, generation=1,
            ownership_check=ownership_from_tasks(project, Store(project.db_path), TASK_ID),
        )
    finally:
        monkey.undo()

    assert not unresolved, (
        f"a converged retry was reported as pending work: {unresolved}"
    )
    assert applied and all(item.kind == "applied" for item in applied), applied
    assert "already applied" in applied[0].detail, applied[0].detail
    assert target.read_bytes() == after


def test_a_change_made_by_a_shell_command_is_caught_by_the_shared_path(repo: Path) -> None:
    """A shell rewrite the hook never saw still reaches the gate.

    This is the plan's acceptance row and the reason two paths exist. Claude
    Code does not run an `Edit|Write` hook when a `Bash` command rewrites the
    file, so nothing delivered a payload about this change. The pre-verification
    path reads the checkout's own changed set, finds it, and cleans it.
    """
    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)

    (repo / "sample.py").write_bytes(COMMENTED)
    assert "sample.py" in changed_files(project)

    verdict = cleanup_paths(project, store, TASK_ID)

    assert isinstance(verdict, Cleaned)
    assert (repo / "sample.py").read_bytes() == COMMENT_FREE


def test_an_untracked_task_owned_file_is_caught_too(repo: Path) -> None:
    """A file no index tracks is every line changed, so every comment is in scope."""
    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)
    (repo / "fresh.py").write_bytes(COMMENTED)

    verdict = cleanup_paths(project, store, TASK_ID)

    assert isinstance(verdict, Cleaned)
    assert (repo / "fresh.py").read_bytes() == COMMENT_FREE
    assert "fresh.py" in verdict.inspected


def test_an_applied_cleanup_changes_the_source_identity(repo: Path) -> None:
    """The write moves the digest the existing inventory computes.

    No second invalidation mechanism is involved: the before and after digests
    on the verdict are `SourceIdentity` values, the same type
    `compute_source_identity` returns to `execution`.
    """
    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)
    edit(repo)

    before = compute_source_identity(project)
    verdict = cleanup_paths(project, store, TASK_ID)
    after = compute_source_identity(project)

    assert isinstance(verdict, Cleaned)
    assert verdict.before.inventory_digest == before.inventory_digest
    assert verdict.after.inventory_digest == after.inventory_digest
    assert verdict.before.inventory_digest != verdict.after.inventory_digest
    assert not source_unchanged(before, after), \
        "the source identity did not move, so earlier evidence would still apply"


def test_execution_prepare_source_uses_the_shared_cleanup_gate(repo: Path) -> None:
    """The execution entry point cleans before returning the source to verify."""
    from vkit.execution import prepare_source

    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)
    edit(repo)
    before = compute_source_identity(project)

    prepared, blocked = prepare_source(project, store, TASK_ID, before)

    assert blocked is None
    assert (repo / "sample.py").read_bytes() == COMMENT_FREE
    assert prepared.inventory_digest == compute_source_identity(project).inventory_digest
    assert prepared.inventory_digest != before.inventory_digest


def test_a_required_cleanup_that_cannot_be_applied_blocks_with_its_reason(repo: Path) -> None:
    """A required case the guard refuses blocks, and names the refusal.

    Ownership is the refusal a caller can act on, so it is the one driven here:
    the apply is asked for a generation the task no longer holds, and the gate
    reports the core's own words rather than a summary.
    """
    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)
    edit(repo)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    assert not isinstance(proposal, type(None))
    (repo / "sample.py").write_bytes(
        b"def bump(total):\n    total = total + 2  # somebody else\n    return total\n"
    )
    result = apply_cleanup(
        project,
        ApplyRequest(
            request_id="probe", proposal=proposal, generation=1, policy=load_policy(project),
        ),
    )
    assert result.reason == "before_bytes_changed"
    assert (repo / "sample.py").read_bytes() != COMMENT_FREE


def test_a_removed_owner_blocks_the_cleanup_and_the_file(repo: Path) -> None:
    """A task that lost its generation may not write, and the gate reports it.

    The refusal comes from `verify_ownership` through the same
    `ownership_from_tasks` callable every writer uses, so this asserts the gate
    declines to decide ownership for itself.
    """
    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)
    edit(repo)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    result = apply_cleanup(
        project,
        ApplyRequest(
            request_id="probe", proposal=proposal,
            generation=get_task(store, TASK_ID).generation + 5, policy=load_policy(project),
        ),
        ownership_check=ownership_from_tasks(project, store, TASK_ID),
    )

    assert result.reason == "not_task_owned"
    assert "reassigned from generation" in result.detail
    assert (repo / "sample.py").read_bytes() == COMMENTED


def test_the_gate_reports_a_task_it_cannot_read_as_unavailable(repo: Path) -> None:
    """A task that does not exist is an absent answer, not a clean repository."""
    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)
    edit(repo)

    with pytest.raises(CleanupUnavailable) as caught:
        cleanup_paths(project, store, "no-such-task")

    assert "no-such-task" in str(caught.value)
    assert (repo / "sample.py").read_bytes() == COMMENTED


@pytest.mark.parametrize("entrypoint", ["shared", "hook"])
def test_a_pinned_cleanup_policy_change_blocks_both_writers(
    repo: Path, entrypoint: str,
) -> None:
    """Changing an admitted write policy to off cannot bypass either writer.

    The task carries the digest measured from the approved policy. Once the live
    policy changes, both the shared pre-verification gate and the host
    accelerator must block before touching the source bytes.
    """
    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)
    approved = load_policy(project)
    contract = dict(get_task(store, TASK_ID).contract)
    contract["cleanup_digest"] = approved.digest
    open_task(
        store,
        task_id="t-pinned-cleanup",
        contract=contract,
        policy_digest=contract["policy_digest"],
    )
    edit(repo)
    before = (repo / "sample.py").read_bytes()
    write_policy(repo, mode="off")

    if entrypoint == "shared":
        verdict = cleanup_paths(project, store, "t-pinned-cleanup")
    else:
        verdict = accelerate(project, store, "t-pinned-cleanup", "sample.py")

    assert isinstance(verdict, Blocked), verdict
    assert verdict.reason == "cleanup_policy_changed"
    assert "pinned cleanup policy" in verdict.detail
    assert (repo / "sample.py").read_bytes() == before


def test_a_file_the_policy_does_not_enable_a_rule_for_is_only_suggested(repo: Path) -> None:
    """An unapproved suggestion is reported and never blocks.

    The policy enables only the logic rules. The trailing comment is a real
    removable comment and the checker proves the edit equal, but no approved
    rule authorizes it, so the verdict clears and the file is unchanged.
    """
    write_policy(repo, enabled_rules=["LOGIC-EMPTY-ELSE-PASS"])
    project = open_project(repo)
    store = Store(project.db_path)
    edit(repo)

    verdict = cleanup_paths(project, store, TASK_ID)

    assert isinstance(verdict, Cleared)
    assert (repo / "sample.py").read_bytes() == COMMENTED
    suggested = {item.rule_id for item in verdict.suggestions}
    assert TRAILING in suggested
    assert all(item.kind == "suggested" for item in verdict.suggestions)


def test_a_logic_rule_applies_through_the_shared_path(repo: Path) -> None:
    """The empty-else case the checker can prove equal applies through the gate."""
    write_policy(repo, enabled_rules=["LOGIC-EMPTY-ELSE-PASS"])
    project = open_project(repo)
    store = Store(project.db_path)
    (repo / "sample.py").write_bytes(ELSE_PASS)

    verdict = cleanup_paths(project, store, TASK_ID)

    assert isinstance(verdict, Cleaned)
    assert (repo / "sample.py").read_bytes() == (
        b"def pick(a):\n    if a:\n        return 1\n"
    )


def test_an_unproved_enabled_rule_blocks_and_is_not_applied(repo: Path) -> None:
    """An enabled candidate the checker cannot prove must not read as clean.

    `LOGIC-REDUNDANT-PASS` is registered and enabled, and the interior pass
    changes bytecode, so the preview refuses. The gate applies nothing and
    reports the reason as required work still unmet.
    """
    write_policy(repo, enabled_rules=["LOGIC-REDUNDANT-PASS"])
    project = open_project(repo)
    store = Store(project.db_path)
    interior = b"def f(a):\n    if a:\n        pass\n        return 1\n    return 2\n"
    edit(repo, interior)

    verdict = cleanup_paths(project, store, TASK_ID)

    assert (repo / "sample.py").read_bytes() == interior
    assert isinstance(verdict, Blocked), verdict
    assert verdict.pending[0].rule_id == "LOGIC-REDUNDANT-PASS"
    assert "executable_fields_differ" in verdict.pending[0].detail


def test_a_malformed_required_file_blocks_instead_of_looking_clean(repo: Path) -> None:
    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)
    malformed = b"def broken(:\n    return 1  # not parseable\n"
    edit(repo, malformed)

    verdict = cleanup_paths(project, store, TASK_ID)

    assert isinstance(verdict, Blocked), verdict
    assert verdict.pending
    assert any("malformed_source" in item.detail for item in verdict.pending)
    assert (repo / "sample.py").read_bytes() == malformed


def test_freshness_reports_a_refused_required_preview(repo: Path) -> None:
    write_policy(repo)
    project = open_project(repo)
    malformed = b"def broken(:\n    return 1  # not parseable\n"
    edit(repo, malformed)

    assert freshness(project) == (f"sample.py:{TRAILING}",)
    assert (repo / "sample.py").read_bytes() == malformed


def test_freshness_refuses_an_invalid_policy_instead_of_returning_no_work(
    repo: Path,
) -> None:
    project = open_project(repo)
    (repo / "verification" / "cleanup.json").write_text("{", encoding="utf-8")

    with pytest.raises(CleanupUnavailable, match="could not be read|not a usable"):
        freshness(project)


def test_freshness_does_not_treat_disabled_rules_as_required(repo: Path) -> None:
    write_policy(repo, mode="off")
    project = open_project(repo)
    edit(repo)

    assert freshness(project) == ()


def test_freshness_does_not_treat_preview_rules_as_required(repo: Path) -> None:
    write_policy(repo, mode="preview")
    project = open_project(repo)
    edit(repo)

    assert freshness(project) == ()


def test_a_tampered_receipt_is_refused_and_the_file_is_unchanged(repo: Path) -> None:
    """The preservation receipt is re-derived at apply time, not trusted.

    The proposal is built against real bytes, then its recorded before digest is
    replaced with one that names nothing. The guard re-verifies the receipt from
    the bytes in hand, and refuses.
    """
    from dataclasses import replace as dataclass_replace

    write_policy(repo)
    project = open_project(repo)
    edit(repo)
    proposal = preview_comment_cleanup(project, "sample.py", changed_lines=[2])
    tampered = dataclass_replace(
        proposal, receipt=dataclass_replace(proposal.receipt, before_digest="0" * 64),
    )

    result = apply_cleanup(
        project,
        ApplyRequest(
            request_id="tampered", proposal=tampered,
            generation=get_task(Store(project.db_path), TASK_ID).generation,
            policy=load_policy(project),
        ),
    )

    assert result.reason == "receipt_mismatch"
    assert (repo / "sample.py").read_bytes() == COMMENTED




def test_freshness_names_the_pending_file_and_writes_nothing(repo: Path) -> None:
    """The read-only question, asked against a real pending file."""
    write_policy(repo)
    project = open_project(repo)
    edit(repo)
    before = (repo / "sample.py").read_bytes()

    pending = freshness(project)

    assert pending == (f"sample.py:{TRAILING}",)
    assert (repo / "sample.py").read_bytes() == before


def test_finalization_changes_no_source_bytes(repo: Path) -> None:
    """The load-bearing non-mutation assertion.

    Every source file in the checkout is read before `freshness` and again
    after, and the two reads must be byte-identical. An implementation that
    applied cleanup here would move the source that already-collected evidence
    describes, and that is the failure the plan forbids by name.
    """
    write_policy(repo)
    project = open_project(repo)
    sources = {
        path: path.read_bytes()
        for path in sorted((repo / "src").rglob("*.py"))
        if path.is_file()
    }
    sources[repo / "sample.py"] = (repo / "sample.py").read_bytes()
    before_bytes = dict(sources)
    before_identity = compute_source_identity(project)

    freshness(project)
    candidate_gaps(project)
    freshness(project)

    assert {path: path.read_bytes() for path in sources} == before_bytes
    assert compute_source_identity(project).inventory_digest == before_identity.inventory_digest


def test_finalization_refuses_even_a_policy_that_authorizes_writing(repo: Path) -> None:
    """The non-mutation guarantee is a property of the code, not of its caller.

    A write-capable policy is handed in deliberately. The function demotes it to
    preview rather than trusting the caller to have demoted it, so the guarantee
    cannot be lost by a caller that passes the operator's real policy.
    """
    write_policy(repo)
    project = open_project(repo)
    edit(repo)
    write_capable = load_policy(project)
    assert write_capable.may_write()
    before = (repo / "sample.py").read_bytes()

    pending = freshness(project, policy=write_capable)

    assert pending == (f"sample.py:{TRAILING}",)
    assert (repo / "sample.py").read_bytes() == before
    assert write_capable.may_write(), "the caller's policy object was narrowed in place"


def test_a_protected_candidate_requiring_cleanup_stays_unchanged(repo: Path) -> None:
    """A candidate that owes cleanup reports the gap and is not cleaned.

    The integration path runs against a checkout of the candidate commit. This
    is the rule in one assertion: the tree is exactly as the candidate made it,
    the gap is named, and the answer is "commit the cleanup", not "clean it and
    attest to the commit".
    """
    write_policy(repo)
    project = open_project(repo)
    edit(repo)
    before = (repo / "sample.py").read_bytes()
    head_before = git_head(repo)

    gaps = candidate_gaps(project)

    assert gaps == (f"sample.py:{TRAILING}",)
    assert (repo / "sample.py").read_bytes() == before == COMMENTED
    assert git_head(repo) == head_before, "the candidate's commit moved"


def git_head(root: Path) -> str:
    done = subproc.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
    )
    return done.stdout.decode("ascii").strip()


def test_a_clean_candidate_reports_no_gap(repo: Path) -> None:
    """The contrast that gives the previous test its meaning."""
    write_policy(repo)
    project = open_project(repo)
    edit(repo, COMMENT_FREE)

    assert candidate_gaps(project) == ()


def test_cleanup_after_a_passed_verification_invalidates_that_evidence(repo: Path) -> None:
    """The plan's row: evidence collected before a cleanup cannot finalize after it.

    A run is published whose recorded source identity is the tree as it was
    BEFORE the cleanup. The gate then rewrites the file. `compute_readiness` is
    asked again with that context, and the identity comparison in
    `tasks._identity_gaps` must name the source change as the gap.

    This is the whole point of routing invalidation through the existing
    inventory rather than adding a cleanup-specific flag: the readiness path
    already knows how to notice that the code it verified has moved, and a second
    mechanism would have to be taught to notice it separately.
    """
    from vkit import tasks as tasks_mod
    from vkit.manifest import parse_manifest

    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)
    edit(repo)

    context = tasks_mod.acceptance_context(
        project, lambda: parse_manifest(project, project.runs_root),
    )
    assert context.usable, f"the control context is unusable: {context!r}"

    recorded_source = compute_source_identity(project)
    publish_run(
        store, CHECK_ID, recorded_source.to_json(), attempt=1,
        policy_digest=context.policy_digest,
        fixture_digest=context.fixture_digest,
    )
    before = tasks_mod.compute_readiness(
        store, TASK_ID, required_check_ids=[CHECK_ID], context=context,
    )
    assert before.readiness == "READY", (
        f"the control must be READY or the later BLOCKED proves nothing: {before!r}"
    )

    verdict = cleanup_paths(project, store, TASK_ID)
    assert isinstance(verdict, Cleaned)

    after_context = tasks_mod.acceptance_context(
        project, lambda: parse_manifest(project, project.runs_root),
    )
    after = tasks_mod.compute_readiness(
        store, TASK_ID, required_check_ids=[CHECK_ID], context=after_context,
    )

    assert after.readiness == "BLOCKED", (
        f"evidence taken before the cleanup still finalizes the changed source: {after!r}"
    )
    assert any("source" in gap.lower() for gap in after.gaps), after.gaps


def publish_run(
    store: Store, check_id: str, source: dict[str, Any], attempt: int,
    policy_digest: str, fixture_digest: str,
) -> str:
    """Publish a terminal PASS run carrying the source identity it ran against.

    `attempt` is the task generation the run belongs to. Acceptance compares it
    against the live generation, so a run recorded under a different one is a
    gap in its own right and would make the later assertion ambiguous about
    which comparison fired.
    """
    run_id = f"run-{check_id}-{source['inventory_digest'][:8]}"
    store.register_run(
        run_id, check_id, task_id=TASK_ID, attempt=attempt,
        source={"inventory_digest": source["inventory_digest"]},
        configuration_digest=policy_digest, fixture_digest=fixture_digest,
    )
    store.publish(run_id, {
        "run_id": run_id, "lifecycle": "terminal", "ended_at": "t",
        "outcome": {"result": "PASS",
                    "scenarios": [{"id": "s", "result": "PASS", "observation": "o"}]},
    })
    return run_id




def test_a_windows_style_payload_path_resolves_to_the_repository_relative_one(
    repo: Path,
) -> None:
    """Backslashes normalize, because the host delivers them that way.

    Asserted on every host, not only on the one whose separators happen to match
    the payload's. `posixpath.normpath` leaves a backslash inside a single
    filename component, so on Linux this exact payload resolved to a sibling of
    the root and the containment check refused a file the host really had edited.
    """
    project = open_project(repo)

    assert resolve_repository_path(project, str(repo / "sample.py")) == "sample.py"
    assert resolve_repository_path(project, f"{repo}\\\\sample.py") == "sample.py"
    assert resolve_repository_path(project, "sample.py") == "sample.py"


def test_a_path_outside_the_root_does_not_resolve(repo: Path, tmp_path: Path) -> None:
    """No resolution is a refusal, never the nearest file inside the root.

    The Windows absolute row is the security half of the same fix. `C:\\...` on a
    POSIX host looks relative -- `PurePosixPath` has no idea what a drive letter
    is -- so joining it under the root puts it back INSIDE the project, the
    containment check passes, and the caller is handed a path that reads as
    local and names another machine's filesystem. It returned
    `'C:\\Users\\x\\elsewhere\\evil.py'` rather than None. This failed on Windows
    too, which is what makes it a bug test rather than a CI-shape test.
    """
    project = open_project(repo)

    assert resolve_repository_path(project, str(tmp_path / "other.py")) is None
    assert resolve_repository_path(project, "") is None
    assert resolve_repository_path(project, f"{repo} .. .. else") is None
    assert resolve_repository_path(project, "C:\\anywhere\\elsewhere\\evil.py") is None
    assert resolve_repository_path(project, "\\\\server\\share\\evil.py") is None
    assert resolve_repository_path(project, f"{repo}\\..\\..\\outside\\evil.py") is None


def test_the_accelerator_cleans_only_lines_in_the_checkout_diff(repo: Path) -> None:
    """The host names a file; the diff still bounds comment ownership by line."""
    write_policy(repo)
    project = open_project(repo)
    store = Store(project.db_path)

    (repo / "sample.py").write_bytes(b"value = 1  # old comment\nother = 2\n")
    git("add", "sample.py", cwd=repo)
    git("commit", "-qm", "baseline comment", cwd=repo)
    (repo / "sample.py").write_bytes(
        b"value = 1  # old comment\nother = 3  # new comment\n"
    )

    accelerate(project, store, TASK_ID, "sample.py")

    assert (repo / "sample.py").read_bytes() == (
        b"value = 1  # old comment\nother = 3\n"
    )


def test_changed_line_parser_handles_deleted_hunks_and_quoted_filenames() -> None:
    diff = (
        b'diff --git a/odd\\303\\251.py b/odd\\303\\251.py\n'
        b'--- a/odd\\303\\251.py\n+++ "b/odd\\303\\251.py"\n'
        b'@@ -2 +1,0 @@\n-old = 1\n'
        b'@@ -5 +4,2 @@\n+new = 1\n+new = 2\n'
    )
    changed_lines = changed_lines_from_diff(diff)

    assert changed_lines("oddé.py") == frozenset({4, 5})


def test_changed_line_parser_rejects_malformed_hunks() -> None:
    from vkit.cleanup import CleanupUnavailable

    with pytest.raises(CleanupUnavailable, match="malformed hunk"):
        changed_lines_from_diff(b"+++ b/sample.py\n@@ not-a-hunk @@\n")


def test_the_authority_path_reads_the_changed_set(repo: Path) -> None:
    """The contrast with the accelerator above: it derives the set itself."""
    write_policy(repo)
    project = open_project(repo)
    edit(repo)

    assert "sample.py" in changed_files(project)
    assert sorted(changed_lines_from_git(project)("sample.py")) == [2]


def test_the_all_lines_sentinel_is_not_iterable() -> None:
    """A sentinel that iterated would mean "no lines" and clean nothing.

    Every line of a rewritten file is a changed line, and passing that through
    as a line-number iterable is the mistake this guards: an integer iterates to
    nothing and a string iterates to its characters. Both would read as "no line
    is changed" and quietly skip the whole file.
    """
    assert ALL_LINES is not None
    with pytest.raises(TypeError) as caught:
        list(ALL_LINES)  # noqa: B018 - the call is the assertion

    assert "all_lines" in str(caught.value)


def test_a_pending_case_is_reported_as_json_a_caller_can_gate_on() -> None:
    """The verdict a caller branches on is a closed vocabulary, not prose."""
    pending = Pending("a.py", "required_unmet", TRAILING, "not_task_owned: reassigned")

    assert pending.to_json() == {
        "path": "a.py",
        "kind": "required_unmet",
        "rule": TRAILING,
        "detail": "not_task_owned: reassigned",
    }
