"""The plugin package, exercised through the surface Claude Code actually uses.

These drive the hook entry point the way the host does: a JSON payload in, a
JSON object out. The claims being defended are the ones CONTRACT.md makes and a
reviewer cannot see by reading the manifest.

A hook for a BLOCKED run must not return an accepting response. That is the
whole reason this layer exists, and a test that only asserted "returns a dict"
would pass on the exact bug the product is built to prevent.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugin"
MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
HOOK_SCRIPT = PLUGIN_ROOT / "scripts" / "vkit_hook.py"

# The events hooks.json declares. Claude Code ignores a handler for an event the
# plugin does not register, so this list and hooks.json must not drift.
DECLARED_EVENTS = (
    "SessionStart", "SubagentStart", "PreToolUse",
    "Stop", "SubagentStop", "TaskCompleted",
)

# Only these three can withhold a stop. The other three can only add context, so
# treating them as accept-or-refuse would be a category error.
COMPLETION_EVENTS = ("Stop", "SubagentStop", "TaskCompleted")


sys.path.insert(0, str(REPO_ROOT / "src"))

from vkit.storage import Store  # noqa: E402
from vkit.tasks import open_task  # noqa: E402


def load_hook_module():
    """Import the hook entry point by file path, the way the host runs it."""
    spec = importlib.util.spec_from_file_location("vkit_hook_under_test", HOOK_SCRIPT)
    assert spec and spec.loader, f"{HOOK_SCRIPT} is not an importable module"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- a real store, so the hooks read records rather than a stub ------------

def _publish_run(store: Store, run_id: str, check_id: str, task_id: str,
                 result: str, reason: str | None = None) -> None:
    store.register_run(run_id, check_id, task_id=task_id, attempt=1,
                       source={"inventory_digest": "src-1"},
                       configuration_digest="pd", fixture_digest=None)
    if result == "BLOCKED":
        outcome: dict[str, Any] = {"result": "BLOCKED", "reason": reason or "timeout"}
    else:
        outcome = {"result": result,
                   "scenarios": [{"id": "s", "result": result, "observation": "o"}]}
    store.publish(run_id, {"run_id": run_id, "lifecycle": "terminal",
                           "ended_at": "t", "outcome": outcome})


def _managed_contract(session_id: str, *, agent_id: str | None = None,
                      required: tuple[str, ...] = ("c1",)) -> dict[str, Any]:
    """A contract in the shape `TaskContract.from_json` validates.

    The flat `{"goal": ..., "required_checks": [...]}` form these tests used to
    write is no longer a contract the core will read: it carries no repository
    binding and no pinned policy digest, so there would be nothing for acceptance
    to compare a pass against. The host binding is preserved verbatim in
    `declared` because that is where the hook reads it from.
    """
    return {
        "repository": {"root": ".", "git_common_dir": "."},
        "policy_digest": "pd",
        "required_checks": list(required),
        "scope": "ship",
        "resources": [],
        "declared": {"host": {"session_id": session_id, **(
            {"agent_id": agent_id} if agent_id else {})}},
    }


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A Git repository with a vkit store holding one bound managed task.

    The task is bound to `session_id` with no `agent_id`, so main-session events
    match it and a subagent payload does not. That asymmetry is the property
    under test elsewhere, so it is built here rather than faked per test.
    """
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init", "--allow-empty"], cwd=root, check=True)

    from vkit.paths import open_project
    store = Store(open_project(root).db_path)
    open_task(
        store, task_id="t1",
        contract=_managed_contract("sess-1"),
        policy_digest="pd",
    )
    return root


def _plugin_relative(declared: str) -> Path:
    """Resolve a manifest path, which the reference requires to start with `./`.

    `str.lstrip` would be the wrong tool here: it strips a character set, so it
    turns `./.mcp.json` into `mcp.json` and reports a real file as missing.
    """
    assert declared.startswith("./"), \
        f"{declared!r} is not plugin-root relative; the manifest reference requires a ./ prefix"
    return PLUGIN_ROOT / declared[2:]


def payload_for(event: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": "sess-1",
        "transcript_path": str(REPO_ROOT / "tmp" / "transcript.jsonl"),
        "cwd": "",
        "hook_event_name": event,
    }
    body.update(extra)
    return body


def accepts_the_finish(response: dict[str, Any]) -> bool:
    """Whether this response lets Claude treat a managed task as finished.

    Claude Code allows a Stop or SubagentStop when the hook sets no
    `decision: "block"`, so the ABSENCE of that field is the accepting response.
    Naming this helper by what it detects is what stops a future edit from
    inverting the polarity and turning a refusing gate into a passing one.
    """
    return response.get("decision") != "block"


def registration_note(response: dict[str, Any], event: str) -> str:
    """The context a completion response carries about its own registration.

    The three events that can withhold a stop differ in what Claude Code reads:
    `TaskCompleted` is documented with a `systemMessage` and no
    `additionalContext` field, so a note for it arrives as the message. Asking
    for the right field per event is what keeps this helper from being the place
    a bug hides.
    """
    output = response.get("hookSpecificOutput") or {}
    if output.get("hookEventName") == event and isinstance(
        output.get("additionalContext"), str
    ):
        return output["additionalContext"]
    return response.get("systemMessage", "")


# --- the manifest ----------------------------------------------------------

def test_the_manifest_is_valid_json_and_names_the_plugin() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == "vkit"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", manifest["name"]), \
        f"plugin name {manifest['name']!r} is not kebab-case"
    assert isinstance(manifest["version"], str) and manifest["version"]
    assert manifest["description"].strip()
    assert manifest["author"]["name"].strip()


def test_the_manifest_declares_the_mcp_server_and_the_hooks() -> None:
    """The manifest points at both by path; the files it points at must hold them.

    A manifest that names a file which does not exist, or a hooks file that
    declares nothing, is the failure `claude plugin validate` reports as
    `Path not found` at load time rather than in a diff.
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key in ("mcpServers", "hooks"):
        assert key in manifest, f"the manifest declares no {key}"

    mcp_path = _plugin_relative(manifest["mcpServers"])
    hooks_path = _plugin_relative(manifest["hooks"])
    assert mcp_path.is_file(), f"{manifest['mcpServers']} does not exist"
    assert hooks_path.is_file(), f"{manifest['hooks']} does not exist"

    assert "vkit" in json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
    declared = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"]
    assert set(declared) == set(DECLARED_EVENTS)


def test_the_mcp_config_starts_vkit_mcp_serve_and_hardcodes_no_project_root() -> None:
    """The server binds to one configured root, so the config cannot name a path.

    `${user_config.project_root}` is the only acceptable source for it. A literal
    path here would ship one developer's checkout to every user and would offer a
    second place to change the binding, which CONTRACT.md forbids.
    """
    raw = (PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8")
    server = json.loads(raw)["mcpServers"]["vkit"]

    assert server["command"] == "vkit"
    assert server["args"][:3] == ["mcp", "serve", "--project"], \
        f"the MCP server must be started by `vkit mcp serve --project`, got {server['args']}"
    assert server["args"][3] == "${user_config.project_root}"

    # No Windows drive letter, no POSIX absolute path, no home-relative path.
    # The declaration is checked separately so a violation names which one it was.
    for pattern, label in (
        (r"[A-Za-z]:[\\/]", "a Windows drive path"),
        (r"\\\\", "a UNC path"),
        (r"~[\\/]", "a home-relative path"),
        (r"^/[^/]", "a POSIX absolute path"),
    ):
        assert not re.search(pattern, raw, re.MULTILINE), \
            f".mcp.json contains {label}; the project root is user_config's alone"


# --- every hook answers every recorded state -------------------------------

@pytest.mark.parametrize("event", DECLARED_EVENTS)
def test_a_hook_is_importable_and_well_formed_for_every_recorded_state(
    event: str, project: Path
) -> None:
    """Each hook, driven as the host drives it, returns a well-formed response.

    The four states are the ones a completion gate must distinguish: evidence
    that passed, evidence that failed, evidence that could not decide, and no
    evidence at all. A hook that answered only some of them has a gap that only
    shows up in a real session.
    """
    hook = load_hook_module()
    from vkit.paths import open_project
    store = Store(open_project(project).db_path)

    # Each state gets its own task, so one state's recorded run cannot satisfy
    # or mask another's. A shared task would make the PASS case pass only
    # because it ran first, which tests ordering rather than the hook.
    states = {
        "terminal PASS": ("t-pass", "r-pass", "PASS", None),
        "terminal FAIL": ("t-fail", "r-fail", "FAIL", None),
        "terminal BLOCKED": ("t-blocked", "r-blocked", "BLOCKED", "timeout"),
        "a run that does not exist": ("t-absent", None, None, None),
    }

    for state, (task_id, run_id, result, reason) in states.items():
        open_task(store, task_id=task_id,
                  contract=_managed_contract(f"sess-{task_id}"),
                  policy_digest="pd")
        if run_id is not None:
            _publish_run(store, run_id, "c1", task_id, result, reason)

        body = payload_for(event, session_id=f"sess-{task_id}")
        payload, code = hook.respond(event, body, str(project))

        assert code == 0, f"{event}/{state} exited {code}"
        assert isinstance(payload, dict), f"{event}/{state} did not return an object"
        # The host parses stdout as JSON; a value it cannot parse is a hook error.
        assert json.loads(json.dumps(payload)) == payload, \
            f"{event}/{state} is not JSON round-trippable"
        if "decision" in payload:
            assert payload["decision"] == "block", \
                f"{event}/{state} returned decision {payload['decision']!r}"
            assert isinstance(payload.get("reason"), str) and payload["reason"].strip(), \
                f"{event}/{state} blocked without a reason"
        for name, value in payload.items():
            assert isinstance(name, str) and isinstance(value, (dict, str, bool, list)), \
                f"{event}/{state} has a field of an unsupported type: {name}"


@pytest.mark.parametrize("event", DECLARED_EVENTS)
def test_a_blocked_run_never_yields_an_accepting_response(event: str, project: Path) -> None:
    """The load-bearing assertion.

    A BLOCKED run is a refusal to decide. If a hook can read one and return a
    response that lets a managed task finish as though the evidence were
    sufficient, the product's central guarantee is broken and nothing else in
    the suite would notice.
    """
    hook = load_hook_module()
    from vkit.paths import open_project
    store = Store(open_project(project).db_path)
    _publish_run(store, "r-blocked", "c1", "t1", "BLOCKED", "timeout")

    for event_under_test in COMPLETION_EVENTS:
        response = hook.respond(event_under_test, payload_for(event_under_test),
                                str(project))[0]
        assert not accepts_the_finish(response), (
            f"{event_under_test} accepted a task whose only required check is BLOCKED: "
            f"{response!r}"
        )

    # The response must also say why, or it silently permits a stop.
    response = hook.respond("Stop", payload_for("Stop"), str(project))[0]
    reason = response.get("reason", "") + json.dumps(response)
    assert "timeout" in reason, f"a BLOCKED gate must name its reason, got {response!r}"


@pytest.mark.parametrize("event", COMPLETION_EVENTS)
def test_a_terminal_pass_is_the_only_state_that_releases_a_bound_task(
    event: str, project: Path
) -> None:
    """The contrast that makes the assertion above mean something.

    If every state blocked, the BLOCKED test would pass for the wrong reason.
    A task whose required check has a recorded PASS must be allowed to finish,
    or the gate is not reading evidence at all.
    """
    hook = load_hook_module()
    from vkit.paths import open_project
    store = Store(open_project(project).db_path)
    _publish_run(store, "r-pass", "c1", "t1", "PASS")

    response = hook.respond(event, payload_for(event), str(project))[0]
    assert accepts_the_finish(response), \
        f"{event} blocked a task whose required check has a recorded PASS: {response!r}"


def test_an_unregistered_session_is_reported_and_never_gated(project: Path) -> None:
    """A payload bound to no task is not accepted silently, and is not gated.

    Read-only chats, unenrolled projects, and host-internal agents all arrive
    here. Guessing a task from a recent file or the last active task is what
    CONTRACT.md forbids — but a bare `{}` is the other half of the same hole,
    because a reader cannot tell "nothing was registered" from "nothing was
    wrong". So the response is the one field that separates them: no
    `decision: "block"`, and context naming the registration that is missing.
    """
    hook = load_hook_module()
    for event in COMPLETION_EVENTS:
        response = hook.respond(event, payload_for(event, session_id="someone-else"),
                               str(project))[0]
        assert accepts_the_finish(response), \
            f"{event} gated a session bound to no task: {response!r}"
        assert registration_note(response, event), \
            f"{event} reported nothing about an unregistered session: {response!r}"


def test_a_sibling_subagent_never_inherits_another_workers_gate(project: Path) -> None:
    """One worker's binding cannot reach a sibling, and the sibling is told so.

    The task is bound to `sess-1` with no agent id, so a subagent under that
    session carries an `agent_id` the binding does not name. The BLOCKED run on
    `t1` must therefore gate nothing here, and the response must say which
    registration is absent rather than reading as a clean finish.
    """
    hook = load_hook_module()
    from vkit.paths import open_project
    store = Store(open_project(project).db_path)
    _publish_run(store, "r-blocked", "c1", "t1", "BLOCKED", "timeout")

    response = hook.respond("SubagentStop", payload_for("SubagentStop", agent_id="a7"),
                            str(project))[0]
    assert accepts_the_finish(response), \
        f"a sibling subagent inherited another worker's gate: {response!r}"
    assert "timeout" not in json.dumps(response), \
        f"a sibling subagent inherited another worker's findings: {response!r}"
    note = registration_note(response, "SubagentStop")
    assert note, f"a sibling subagent was not told what to register: {response!r}"
    assert "a7" in note, f"the note does not identify the unregistered subagent: {note!r}"


def test_an_ambiguous_binding_is_reported_rather_than_picked(project: Path) -> None:
    """Two tasks claiming one session gate nothing.

    Selecting the first match would resolve the task by table order, which is
    the guess CONTRACT.md forbids; a duplicate binding is ambiguous, and an
    ambiguous registration has no evidence to compare against. The response
    reports the same missing-registration note an absent binding produces, so
    the two are not confused with a gate that ran and found nothing.
    """
    hook = load_hook_module()
    from vkit.paths import open_project
    store = Store(open_project(project).db_path)
    open_task(store, task_id="t2", contract=_managed_contract("sess-1"),
              policy_digest="pd")
    _publish_run(store, "r-pass", "c1", "t1", "PASS")
    _publish_run(store, "r-blocked", "c1", "t2", "BLOCKED", "timeout")

    for event in COMPLETION_EVENTS:
        response = hook.respond(event, payload_for(event), str(project))[0]
        assert accepts_the_finish(response), \
            f"{event} gated a session with two tasks claiming it: {response!r}"
        assert registration_note(response, event), \
            f"{event} reported nothing about an ambiguous binding: {response!r}"


def test_a_bound_task_is_never_displaced_by_a_sibling_registration(project: Path) -> None:
    """The disjointness is on both fields, not a filter over one set.

    A second task bound to the same session but a different agent is not a
    duplicate — it is a sibling's own registration. So the main session keeps
    its binding, and the subagent is judged against the task that names it.
    """
    hook = load_hook_module()
    from vkit.paths import open_project
    store = Store(open_project(project).db_path)
    open_task(store, task_id="t2", contract=_managed_contract("sess-1", agent_id="a7"),
              policy_digest="pd")
    # t1 passes so the main session's gate accepts; t2 stays blocked so the
    # subagent's gate has something to say, and a subagent inheriting t1
    # instead of t2 would be visible as silence.
    _publish_run(store, "r-pass", "c1", "t1", "PASS")
    _publish_run(store, "r-blocked", "c1", "t2", "BLOCKED", "timeout")

    main = hook.respond("Stop", payload_for("Stop"), str(project))[0]
    assert accepts_the_finish(main), \
        f"the main session's own task stopped being gated: {main!r}"
    assert registration_note(main, "Stop") == "", \
        f"a registered main session was reported as unregistered: {main!r}"

    subagent = hook.respond("SubagentStop",
                            payload_for("SubagentStop", agent_id="a7"), str(project))[0]
    assert subagent.get("decision") == "block", \
        f"a subagent with a registered task and a blocked check was not gated: {subagent!r}"
    assert "t2" in subagent["reason"], \
        f"the gate was decided by the wrong task's record: {subagent['reason']!r}"


def test_a_second_blocked_turn_records_the_blocker_instead_of_looping(
    project: Path,
) -> None:
    """The host's continuation flag ends the loop without hiding the blocker.

    Feeding the same feedback back is how a gate that can never be satisfied
    becomes an infinite turn. The second turn must still carry the reason.
    """
    hook = load_hook_module()
    from vkit.paths import open_project
    store = Store(open_project(project).db_path)
    _publish_run(store, "r-blocked", "c1", "t1", "BLOCKED", "timeout")

    response = hook.respond("Stop", payload_for("Stop", stop_hook_active=True),
                            str(project))[0]
    assert accepts_the_finish(response), "a blocked task could not end its turn"
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "BLOCKED" in context and "timeout" in context, \
        f"the loop-ending turn lost the blocker: {context!r}"


# --- failure paths ---------------------------------------------------------

@pytest.mark.parametrize("event", DECLARED_EVENTS)
def test_an_error_path_returns_a_non_accepting_response_rather_than_raising(
    event: str, tmp_path: Path
) -> None:
    """A hook that cannot evaluate its gate says so, and does not raise.

    Two distinct failures are exercised: a directory that is not a vkit project,
    and a payload that is not the object the event documents. Both must come
    back as a response naming the failure.
    """
    hook = load_hook_module()
    not_a_project = tmp_path / "loose"
    not_a_project.mkdir()

    for detail, body in (
        ("not a project", payload_for(event, cwd=str(not_a_project))),
        ("not an object", "this is not JSON"),
    ):
        response, code = hook.respond(event, body, str(not_a_project))
        assert code == 0, f"{event}/{detail} exited {code}"
        assert isinstance(response, dict), f"{event}/{detail} raised or returned no object"
        message = response.get("systemMessage", "") + json.dumps(response)
        assert "acceptance not established" in message, \
            f"{event}/{detail} did not report that acceptance was not established: {response!r}"
        assert accepts_the_finish(response), \
            f"{event}/{detail} blocked a session it could not evaluate"


def test_a_hook_given_an_event_it_does_not_declare_reports_it(tmp_path: Path) -> None:
    hook = load_hook_module()
    response, code = hook.respond("NoSuchEvent", payload_for("Stop"), str(tmp_path))
    assert code == 0
    assert "NoSuchEvent" in json.dumps(response)


def test_the_executable_entry_point_never_raises_on_a_host_delivery(
    project: Path,
) -> None:
    """Drive the real process boundary, including a malformed payload.

    The host writes the payload to stdin and reads stdout. A traceback on
    stderr there is a hook error the user sees, and an empty stdout is a parse
    failure, so both have to come back as a parseable object.
    """
    for event, body in (
        ("Stop", {"session_id": "sess-1", "hook_event_name": "Stop", "cwd": ""}),
        ("Stop", None),
    ):
        done = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT), event, "--project", str(project)],
            input="not json at all" if body is None else json.dumps(body),
            capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 0, f"{event} exited {done.returncode}: {done.stderr}"
        response = json.loads(done.stdout)
        assert isinstance(response, dict), f"{event} printed {done.stdout!r}"
        if body is None:
            assert "acceptance not established" in json.dumps(response)


# --- the constraint this layer exists to hold ------------------------------

def test_no_hook_launches_a_check() -> None:
    """Nothing in the hook path spawns a process.

    A hook that runs tests is exactly the failure CONTRACT.md's "hooks are fast
    local checks over existing records" exists to prevent, and it would be
    invisible in review because the code would look like an ordinary call. The
    execution, process, and supervisor modules are named too, since importing
    one is how a spawn arrives later.
    """
    source = HOOK_SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for banned in ("subprocess", "Popen", "os.system", "os.exec", "os.spawn",
                   "multiprocessing", "run_check", "run_command", "start_run"):
        assert banned not in code, (
            f"the hook module references {banned!r}; a hook must read records, not launch work"
        )

    # The module is loaded in a fresh interpreter. `sys.modules` in this process
    # is shared with every other test in the suite, and an unrelated one that
    # imports vkit.procs would make this assertion fail for a reason that has
    # nothing to do with the hook.
    probe = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('hook_probe', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('vkit'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe, str(HOOK_SCRIPT)],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    loaded = json.loads(done.stdout)
    for banned_module in ("vkit.execution", "vkit.procs", "vkit.supervisor"):
        assert banned_module not in loaded, (
            f"importing the hook module pulled in {banned_module}: {loaded}"
        )


def test_hook_execution_stays_fast_enough_to_be_a_hook() -> None:
    """A hook is called on every session start and every turn end.

    A gate that adds seconds to each of those is one users disable, and a
    disabled gate is a gate that accepts everything. The bound is generous for a
    process launch and a store open, and far below any test suite.
    """
    import time

    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "SessionStart", "--project", str(REPO_ROOT)],
        input=json.dumps({"session_id": "x", "hook_event_name": "SessionStart"}),
        capture_output=True, text=True, timeout=60,
    )
    elapsed = time.monotonic() - started
    assert done.returncode == 0, done.stderr
    assert elapsed < 15.0, f"a hook took {elapsed:.1f}s; it is reading records, not running tests"


# --- skills ----------------------------------------------------------------

def test_every_skill_has_frontmatter_with_a_kebab_case_name_and_a_description() -> None:
    """Claude Code loads a skill whose frontmatter will not parse with empty
    metadata, so the file still appears and the skill simply never triggers."""
    skill_files = sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
    assert skill_files, "the plugin ships no skills"

    for skill in skill_files:
        text = skill.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{skill} has no frontmatter block"
        _, frontmatter, body = text.split("---", 2)
        assert body.strip(), f"{skill} has an empty body"

        fields = {}
        for line in frontmatter.strip().splitlines():
            if line.strip() and not line.startswith((" ", "\t")):
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()

        name = fields.get("name", "")
        assert name, f"{skill} declares no name"
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), \
            f"{skill} name {name!r} is not kebab-case"
        assert name == skill.parent.name, \
            f"{skill} name {name!r} does not match its directory {skill.parent.name!r}"
        assert fields.get("description", "").strip(), f"{skill} declares no description"


def test_no_skill_embeds_a_second_acceptance_algorithm() -> None:
    """Skills route to the app. They must not restate how a verdict is reached.

    A skill that explains when a task counts as done is a second place for that
    decision to live, and the two drift. The one thing they may do is tell the
    reader what the app's verdict means.
    """
    for skill in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")):
        text = skill.read_text(encoding="utf-8").lower()
        for banned in ("if all tests pass then", "consider the task complete",
                       "you may mark it accepted", "approve the merge"):
            assert banned not in text, f"{skill} embeds an acceptance rule: {banned!r}"
        assert "task_finalize" in text, f"{skill} never routes to the app's own verdict"
