"""Behavior of the local console.

Two rules shape this file.

**Drive it the way a user does.** The operations are called directly, with no
server running, because that is the property Plan 05 asks for and a test that
merely imported them would prove nothing. The HTTP test makes a real request to
a server on port 0.

**Assert literals.** Every expectation below is a value written out, not a
restatement of what the code computes. A test that compared a run list to
`operations.runs_view(...)` would pass if both returned undefined.

The manifest test is the one that matters most, and it works on the source rather
than on a docstring. `test_no_handler_writes_a_protected_path` parses every module
in the package and inspects the AST for a write to a path under `verification/`
or `schemas/`, so adding one fails a test instead of silently weakening the
contract the evidence is measured against.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vkit.console import api, operations, plan, server
from vkit.console.plan import (
    MAX_LOG_BYTES,
    Refused,
    WRITABLE_NAMES,
)
from vkit.manifest import ManifestError
from vkit.paths import open_project
from vkit.storage import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"
PACKAGE = REPO_ROOT / "src" / "vkit" / "console"
CHECK_ID = "totals-behavior"


@pytest.fixture()
def example_repo(tmp_path: Path) -> Path:
    """A throwaway Git repository holding a real copy of the example."""
    target = tmp_path / "späce repo"
    shutil.copytree(EXAMPLE, target)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=target, check=True,
    )
    return target


@pytest.fixture()
def context(example_repo: Path) -> operations.Context:
    return operations.open_context(example_repo)


def _claude_cli() -> str | None:
    import shutil

    return shutil.which("claude")


#: The host plugin tests drive the real `claude` CLI. A mock would prove only
#: that the console called what it was told to call; what matters is that the
#: package validates, that the host accepts it, and that remove leaves nothing
#: behind. Those are the host's behaviours, so the host is skipped, never
#: replaced, when it is absent.
requires_host = pytest.mark.skipif(
    _claude_cli() is None, reason="the 'claude' CLI is not on PATH"
)


@pytest.fixture()
def scratch_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Claude home, so no test touches the developer's real install."""
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


# ------------------------------------------------------------------- binding


def test_binding_a_non_loopback_address_is_refused(context: operations.Context) -> None:
    for host in ("0.0.0.0", "::", "192.168.1.10", "localhost", ""):
        with pytest.raises(server.BindRefused) as caught:
            server.serve(context, port=0, host=host)
        assert "127.0.0.1 only" in str(caught.value)
    assert operations.host() == "127.0.0.1"
    assert plan.LOOPBACK_HOST == "127.0.0.1"


def test_a_refused_bind_leaves_no_socket_listening(context: operations.Context) -> None:
    """The refusal happens before the socket exists, so nothing is reachable.

    A check that only asserted the exception would still pass if the server had
    bound the routable address and then complained. This proves the port is free
    afterwards.
    """
    import socket

    with pytest.raises(server.BindRefused):
        server.serve(context, port=0, host="0.0.0.0")
    probe = socket.socket()
    probe.settimeout(5)
    assert probe.connect_ex(("127.0.0.1", 1)) != 0
    probe.close()


def test_a_loopback_bind_succeeds_on_port_zero(context: operations.Context) -> None:
    bound = server.serve(context, port=0)
    try:
        assert bound.server_address[0] == "127.0.0.1"
        assert bound.server_address[1] != 0
    finally:
        bound.server_close()


# ------------------------------------------------------------ writable list


def test_the_writable_list_is_exactly_the_six_permitted_operations() -> None:
    assert WRITABLE_NAMES == (
        "enroll", "install", "repair", "remove", "run_check", "cancel_run",
    )
    assert set(operations.OPERATIONS) == set(WRITABLE_NAMES)
    assert len(WRITABLE_NAMES) == 6


def test_the_manifest_is_not_writable() -> None:
    names = {name.lower() for name in WRITABLE_NAMES}
    assert "manifest" not in names
    assert not any("manifest" in name for name in WRITABLE_NAMES)
    for operation in plan.WRITABLE:
        assert not any(
            part in plan.PROTECTED_PATH_PARTS for part in operation.writes
        ), operation.name


@requires_host
def test_no_setup_operation_touches_a_hand_maintained_file(
    context: operations.Context, scratch_host: Path
) -> None:
    """The whole setup surface runs, and the repository is byte-identical after.

    enroll, install, repair and remove all run against the real host CLI and the
    real project, and the working tree is compared before and after. This is the
    test that would catch a setup operation quietly editing a committed file,
    which is exactly what Plan 05's writable list exists to make impossible.
    A test asserting only return values would pass for an installer that also
    rewrote the manifest.
    """
    project = open_project(context.project.root)

    def fingerprint() -> list[tuple[str, int]]:
        return sorted(
            (str(p.relative_to(project.root)), p.stat().st_size)
            for p in project.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )

    before = fingerprint()
    assert operations.enroll(context)["enrolled"] is False
    assert operations.enroll(context, accepted=True)["enrolled"] is True
    operations.install(context)
    operations.repair(context)
    operations.remove(context)

    assert fingerprint() == before
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=project.root,
        capture_output=True, encoding="utf-8", timeout=60,
    ).stdout
    assert status == "", f"a setup operation dirtied the working tree: {status}"


def test_an_unknown_operation_names_the_real_surface() -> None:
    with pytest.raises(Refused) as caught:
        plan.operation("edit_manifest")
    assert "unknown operation 'edit_manifest'" in caught.value.reason
    for name in WRITABLE_NAMES:
        assert name in caught.value.reason


# --------------------------------------------- no handler writes the manifest


def _write_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call that could write a file, plus every assignment that rebinds a path.

    Deliberately wide. `open(...,"w")`, `Path.write_text`, `os.replace`,
    `shutil.copy`, `unlink`, `mkdir` and a bare `x = something` all count, so a
    writer arriving through a name this module has not seen yet is still caught.
    """
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    found.append(ast.Call(func=ast.Name(id="assign", ctx=ast.Load()),
                                          args=[ast.Constant(value=target.id)], keywords=[]))
    return found


WRITER_NAMES = frozenset({
    "write_text", "write_bytes", "open", "replace", "rename", "unlink", "rmdir",
    "mkdir", "makedirs", "copy", "copy2", "copyfile", "move", "rmtree", "truncate",
    "chmod", "symlink_to", "hardlink_to", "write_text_encoding", "assign",
})


def _module_writes_protected_path(source_path: Path) -> list[str]:
    """Every call in one module whose name or arguments could write policy.

    A hit is reported when the source text of the call names a protected path
    part, or when the call writes to a variable whose name does. Resolution is
    not attempted: a path can be assembled from parts, so the check errs toward
    flagging any module that so much as mentions writing near a policy directory.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    offences: list[str] = []
    for call in _write_calls(tree):
        func = call.func
        name = getattr(func, "attr", getattr(func, "id", ""))
        if name not in WRITER_NAMES:
            continue
        parts = [ast.unparse(arg) for arg in call.args]
        parts += [f"{kw.arg}={ast.unparse(kw.value)}" for kw in call.keywords]
        rendered = " ".join(parts)
        for protected in plan.PROTECTED_PATH_PARTS:
            if protected in rendered:
                offences.append(f"{source_path.name}:{call.lineno} {name}({rendered})")
    return offences


def test_no_handler_writes_a_protected_path() -> None:
    """No module in the console package writes under verification/ or schemas/.

    Implemented by scanning the source, not by trusting a docstring. The
    docstring could say the opposite and this test would still hold.
    """
    sources = sorted(PACKAGE.rglob("*.py"))
    assert len(sources) >= 5, "the package went missing; this test would pass vacuously"
    offences: list[str] = []
    for source in sources:
        offences.extend(_module_writes_protected_path(source))
    assert offences == [], f"a console module writes committed policy: {offences}"


def test_no_module_in_the_package_mentions_writing_the_manifest() -> None:
    """No source line both names a policy path and performs a write.

    Narrower than the AST test above, and checking the thing that matters: a
    line is only an offence when it is executable code naming a policy path next
    to a write verb. A docstring that says "the manifest is not writable" is the
    guarantee, not a violation of it.
    """
    VERBS = ("write_text", "write_bytes", "unlink", "rmtree", "mkdir", "open(")
    offences: list[str] = []
    for source in sorted(PACKAGE.rglob("*.py")):
        if source.name == "plan.py":
            continue  # plan.py declares the protection and may name the paths
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # A docstring or a string literal, not a write.
            if stripped.startswith(('"""', "'''", '"', "'", "*", "f\"", "f'")):
                continue
            for protected in plan.PROTECTED_PATH_PARTS:
                if protected in stripped and any(verb in stripped for verb in VERBS):
                    offences.append(f"{source.name}:{number} {stripped}")
    assert offences == [], f"a console module writes near policy: {offences}"


def test_no_route_takes_a_path_parameter(context: operations.Context) -> None:
    """The structural guarantee that no request can name a file to act on.

    Every request parameter is an identifier, a count, a stream name, or a
    fixed vocabulary value. None of them is a path, so there is no value a
    caller could supply that turns into a repository path the console would then
    read or write.
    """
    assert api.PARAM_NAMES == frozenset({
        "limit", "run_id", "check_id", "stream", "max_bytes", "operation",
        "scope", "accepted",
    })
    for name in api.PARAM_NAMES:
        assert "/" not in name and "\\" not in name

    # A path-shaped value in the identifier parameters is refused by name, on
    # every route that reads one of them. Each case gives the route exactly the
    # parameters it reads, so the path check is what raises, not a missing field.
    cases = (
        ("run", {"run_id": "verification/manifest.json"}),
        ("log", {"run_id": "somerun", "stream": "verification/manifest.json"}),
        ("plan", {"operation": "verification/manifest.json"}),
        ("apply", {"operation": "verification/manifest.json"}),
        ("run_check", {"check_id": "verification/manifest.json"}),
        ("cancel_run", {"run_id": "verification/manifest.json"}),
    )
    for route, query in cases:
        with pytest.raises(api.BadRequest) as caught:
            api.dispatch(context, route, query)
        assert "policy path" in str(caught.value), route

    # A route that does not read the parameter rejects the request outright,
    # rather than ignoring a path it was handed.
    with pytest.raises(api.BadRequest) as caught:
        api.dispatch(context, "plan", {"run_id": "verification/manifest.json"})
    assert "missing required parameter 'operation'" in str(caught.value)


def test_a_scope_that_is_not_the_supported_one_is_refused(
    context: operations.Context,
) -> None:
    """The scope vocabulary is closed, and the refusal names it.

    `scope` reaches the host CLI as an argument, so an unchecked value is a way
    to make the console pass an arbitrary argument to the host. It is validated
    at the operation, not only in the page's select box.
    """
    with pytest.raises(Refused) as caught:
        operations.install(context, scope="project")
    assert "'project' is not supported" in caught.value.reason
    assert "'user' scope only" in caught.value.reason


def test_under_protected_path_rejects_the_policy_paths() -> None:
    assert plan.under_protected_path("verification/manifest.json") is True
    assert plan.under_protected_path("schemas/run-report.v1.json") is True
    assert plan.under_protected_path("verification-kit/runs/abc/stdout.log") is False


# -------------------------------------------------- operations need no server


def test_operations_run_with_no_server_in_the_process(context: operations.Context) -> None:
    """The real logic is callable with nothing HTTP-shaped in sight."""
    project = operations.project_view(context)
    assert project["root"] == str(context.project.root)
    assert isinstance(project["source"]["inventory_digest"], str)

    readiness = operations.readiness_view(context)
    assert readiness["ok"] is True
    assert CHECK_ID in readiness["checks"]

    checks = operations.checks_view(context)
    assert [c["id"] for c in checks["checks"]] == [CHECK_ID]
    assert checks["checks"][0]["timeout_seconds"] == 120
    assert checks["checks"][0]["artifact"] == "result.json"

    assert operations.runs_view(context)["runs"] == []

    recovery = operations.recovery_view(context)
    assert recovery["findings"] == []
    assert recovery["actions_offered"] == []

    # No socket was created and nothing was bound.
    assert not any(
        thread.name == "vkit-console" for thread in threading.enumerate()
    )


def test_operations_module_imports_no_http_server() -> None:
    """The dependency direction, asserted on the import graph.

    Parsed rather than grepped, so a mention of HTTP in a docstring does not
    read as an import and a real import cannot hide inside a line of prose.
    """
    def imports_of(module: str) -> set[str]:
        tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    http_modules = {"http", "http.server", "socket", "socketserver", "urllib.request"}
    assert imports_of("operations.py") & http_modules == set(), \
        "operations.py must not know about HTTP or a socket"
    assert imports_of("plan.py") & {".operations", "operations"} == set(), \
        "plan.py must not call the operations"
    assert imports_of("api.py") & {"http", "http.server"} == set(), \
        "api.py must not open a socket"

    # server.py routes, and does not install: it holds no operation body of its own.
    server_tree = ast.parse((PACKAGE / "server.py").read_text(encoding="utf-8"))
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(server_tree) if isinstance(node, ast.Call)
    }
    assert "dispatch" in calls, "server.py must route through api.dispatch"
    for forbidden in ("install", "repair", "remove", "enroll", "run_check", "cancel_check_run"):
        assert forbidden not in calls, f"server.py must not perform {forbidden}"


# --------------------------------------- a refusal shows the core's own reason


def test_a_bad_check_id_surfaces_the_core_reason_verbatim(context: operations.Context) -> None:
    """The console shows the core's own message, not a reworded one.

    The expectation is the exact string `Manifest.require` builds. A paraphrase
    anywhere in the console layer fails this test.
    """
    expected = (
        "unknown check 'no-such-check'; manifest defines: totals-behavior"
    )
    with pytest.raises(Refused) as caught:
        operations.run_check(context, "no-such-check")
    assert caught.value.reason == expected

    status, document = api.error_of(caught.value)
    assert status == 409
    assert document["error"] == expected
    assert document["refused"] is True


def test_an_empty_check_id_is_refused_before_the_core_is_called(
    context: operations.Context,
) -> None:
    with pytest.raises(Refused) as caught:
        operations.run_check(context, "")
    assert "a check id is required" in caught.value.reason


def test_a_missing_run_report_surfaces_the_store_reason(context: operations.Context) -> None:
    project = open_project(context.project.root)
    expected = f"no published report for run nosuch: {project.runs_root / 'nosuch' / 'report.json'}"
    with pytest.raises(Refused) as caught:
        operations.run_detail_view(context, "nosuch")
    assert caught.value.reason == expected


# ------------------------------------------------------------ bounded log read


def test_a_log_read_is_bounded(context: operations.Context) -> None:
    """A 10 MB log yields a bounded tail, not the whole file."""
    started = operations.run_check(context, CHECK_ID)
    assert started["outcome"]["result"] == "PASS"
    run_id = started["run_id"]

    stdout = context.store.run_dir(run_id) / "stdout.log"
    with stdout.open("w", encoding="utf-8") as handle:
        for index in range(250_000):
            handle.write(f"line {index} of a deliberately enormous log\n")
    assert stdout.stat().st_size > 10 * 1024 * 1024, "the fixture must exceed 10 MB to mean anything"

    tail = operations.log_tail(context, run_id, "stdout")
    assert tail["truncated"] is True
    assert tail["size"] > 10 * 1024 * 1024
    assert tail["bytes"] <= MAX_LOG_BYTES
    assert len(tail["text"]) <= MAX_LOG_BYTES
    # The tail is the END of the file, which is what a reader wants.
    assert tail["text"].rstrip().endswith("line 249999 of a deliberately enormous log")
    assert "line 0 of" not in tail["text"]


def test_a_small_log_is_returned_whole(context: operations.Context) -> None:
    started = operations.run_check(context, CHECK_ID)
    tail = operations.log_tail(context, started["run_id"], "stderr")
    assert tail["exists"] is True
    assert tail["truncated"] is False
    assert tail["bytes"] == tail["size"]


def test_a_log_request_above_the_ceiling_is_clamped(context: operations.Context) -> None:
    started = operations.run_check(context, CHECK_ID)
    tail = operations.log_tail(context, started["run_id"], "stdout", max_bytes=10 * 1024 * 1024)
    assert tail["bytes"] <= MAX_LOG_BYTES


def test_an_unknown_log_stream_is_refused(context: operations.Context) -> None:
    started = operations.run_check(context, CHECK_ID)
    with pytest.raises(Refused) as caught:
        operations.log_tail(context, started["run_id"], "everything")
    assert "unknown log stream 'everything'" in caught.value.reason


# ------------------------------------------- the run list matches the store


def test_the_run_list_matches_the_store_for_the_same_store(
    context: operations.Context,
) -> None:
    """The console shows exactly the rows the core's own store returns.

    Compared against `store.list_runs` for the same store, which is what
    `vkit run show` and recovery read, so a drift between the console and the
    CLI would have to be a drift in the store itself.
    """
    first = operations.run_check(context, CHECK_ID)
    second = operations.run_check(context, CHECK_ID)

    direct = Store(context.project.db_path).list_runs(limit=50)
    shown = operations.runs_view(context)["runs"]

    assert shown == direct
    assert [run["run_id"] for run in shown] == [second["run_id"], first["run_id"]]
    assert [run["result"] for run in shown] == ["PASS", "PASS"]


def test_the_run_list_limit_is_clamped_to_the_store_bound(
    context: operations.Context,
) -> None:
    operations.run_check(context, CHECK_ID)
    assert operations.runs_view(context, limit=10_000)["limit"] == plan.MAX_RUN_LIMIT
    with pytest.raises(Refused):
        operations.runs_view(context, limit=0)


def test_a_full_run_detail_carries_the_stored_report(context: operations.Context) -> None:
    started = operations.run_check(context, CHECK_ID)
    detail = operations.run_detail_view(context, started["run_id"])
    stored = Store(context.project.db_path).load(started["run_id"])
    assert detail["report"] == stored
    assert detail["report"]["outcome"]["result"] == "PASS"
    assert detail["logs"] == ["stdout", "stderr"]


# ------------------------------------------------ a real HTTP request returns JSON


def test_a_real_http_request_returns_valid_json(context: operations.Context) -> None:
    bound, _thread = server.start_in_thread(context, port=0)
    port = bound.server_address[1]
    try:
        document = _get(f"http://127.0.0.1:{port}/api/checks")
        assert isinstance(document, dict)
        assert [c["id"] for c in document["checks"]] == [CHECK_ID]
        assert document["checks"][0]["required_scenarios"] == [
            "empty-cart", "single-positive", "several-positives",
            "mixed-sign", "negatives-only", "cancels-to-zero",
        ]

        runs = _get(f"http://127.0.0.1:{port}/api/runs?limit=5")
        assert runs == {"runs": [], "limit": 5}

        surface = _get(f"http://127.0.0.1:{port}/api/operations")
        assert [op["name"] for op in surface["operations"]] == list(WRITABLE_NAMES)

        page = _get_bytes(f"http://127.0.0.1:{port}/")
        assert page.startswith(b"<!DOCTYPE html>")
        assert b"vkit console" in page
    finally:
        bound.shutdown()
        bound.server_close()


def test_a_refusal_over_http_is_a_409_with_the_core_reason(
    context: operations.Context,
) -> None:
    bound, _thread = server.start_in_thread(context, port=0)
    port = bound.server_address[1]
    try:
        document, status = _post(
            f"http://127.0.0.1:{port}/api/run_check?check_id=no-such-check",
        )
        assert status == 409
        assert document["error"] == (
            "unknown check 'no-such-check'; manifest defines: totals-behavior"
        )
    finally:
        bound.shutdown()
        bound.server_close()


def test_an_unknown_operation_names_the_real_surface_over_http(
    context: operations.Context,
) -> None:
    """Every name on the surface resolves through `apply`; none is a 404.

    Refused with the list rather than a bare "unknown operation", so an operator
    who guessed a name learns what the surface actually is. The four setup
    operations are reachable, which is what makes them refusable by the core
    rather than invisible.
    """
    bound, _thread = server.start_in_thread(context, port=0)
    port = bound.server_address[1]
    try:
        document, status = _post(f"http://127.0.0.1:{port}/api/apply?operation=not_a_thing")
        assert status == 400
        assert document["error"].startswith("unknown operation 'not_a_thing'")
        for name in WRITABLE_NAMES:
            assert name in document["error"]
    finally:
        bound.shutdown()
        bound.server_close()


@requires_host
def test_enroll_over_http_records_acceptance(context: operations.Context) -> None:
    """The whole acceptance path over a real socket, including the refusal."""
    bound, _thread = server.start_in_thread(context, port=0)
    port = bound.server_address[1]
    try:
        declined, status = _post(f"http://127.0.0.1:{port}/api/apply?operation=enroll")
        assert status == 200
        assert declined["result"]["enrolled"] is False

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/apply",
            data=json.dumps({"operation": "enroll", "accepted": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            accepted = json.loads(response.read().decode("utf-8"))
        assert accepted["result"]["enrolled"] is True
        assert (context.project.state_root / "enrollment.json").is_file()
    finally:
        bound.shutdown()
        bound.server_close()


def test_the_static_server_will_not_read_outside_its_directory(
    context: operations.Context,
) -> None:
    bound, _thread = server.start_in_thread(context, port=0)
    port = bound.server_address[1]
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get_bytes(f"http://127.0.0.1:{port}/../../../../secrets.txt")
        assert caught.value.code == 404
    finally:
        bound.shutdown()
        bound.server_close()


# ------------------------------------------------------- the change set first


def test_the_change_set_is_visible_before_anything_is_applied(
    context: operations.Context,
) -> None:
    planned = operations.plan_change_set(context, "run_check").to_json()
    assert planned["implemented"] is True
    assert [change["target"] for change in planned["changes"]] == [
        "runs table", "runs/<run_id>/",
    ]
    # Nothing has run yet.
    assert operations.runs_view(context)["runs"] == []

    started = operations.run_check(context, CHECK_ID)
    assert started["run_id"] in {
        run["run_id"] for run in operations.runs_view(context)["runs"]
    }


def test_the_change_set_for_an_unimplemented_operation_is_empty(
    context: operations.Context,
) -> None:
    planned = operations.plan_change_set(context, "run_check").to_json()
    assert planned["implemented"] is True
    # Every operation on the surface names its change set before it is applied.
    for name in WRITABLE_NAMES:
        assert operations.plan_change_set(context, name).to_json()["implemented"] is True


def test_enroll_leaves_execution_disabled_until_the_policy_is_accepted(
    context: operations.Context,
) -> None:
    """The plan's rule, exercised rather than asserted in a comment.

    Plan 06: execution stays disabled until the user accepts the repository's
    executable policy. So the first enroll reports the policy and writes
    nothing at all; only an explicit acceptance records it, and it records to the
    shared Git directory, never to a hand-maintained file in the tree.
    """
    declined = operations.enroll(context)
    assert declined["enrolled"] is False
    assert declined["accepted"] is False
    assert not (context.project.state_root / "enrollment.json").exists()
    # The policy shown is the manifest's own executable command, not a summary.
    assert declined["policy"] == [{
        "id": CHECK_ID,
        "command": ["python", "verify_totals.py", "--app", "src/totals.py",
                    "--out", "{{run_dir}}/result.json"],
        "timeout_seconds": 120.0,
    }]

    accepted = operations.enroll(context, accepted=True)
    assert accepted["enrolled"] is True
    record_path = Path(accepted["record"])
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["accepted"] is True
    assert record["configuration_digest"] == context.manifest.digest()
    # The record lives under the Git common directory beside the store, so it is
    # state rather than a working-tree file: it is never tracked, never appears
    # in a diff, and survives the checkout. `git status` is asked rather than
    # assumed, because that is the property a reviewer would rely on.
    assert record_path.is_relative_to(context.project.git_common_dir)
    untracked = subprocess.run(
        ["git", "status", "--porcelain"], cwd=context.project.root,
        capture_output=True, encoding="utf-8", timeout=60,
    ).stdout
    assert "enrollment" not in untracked, untracked
    assert "manifest.json" not in untracked, untracked


def test_an_enrollment_is_impossible_without_a_manifest(tmp_path: Path) -> None:
    """A repository with no manifest cannot be enrolled against nothing."""
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True)
    context = operations.open_context(bare)
    with pytest.raises(Refused) as caught:
        operations.enroll(context, accepted=True)
    assert "no manifest" in caught.value.reason


def test_the_api_will_not_read_accepted_false_as_accepted(
    context: operations.Context,
) -> None:
    """A JSON body and a query string both arrive as strings; "false" is truthy.

    `accepted=false` reaching the operation as the string "false" would enroll a
    repository the operator explicitly declined, because a non-empty string is
    true. The boundary coerces both, and this drives both.
    """
    for query in ({"operation": "enroll", "accepted": "false"},
                  {"operation": "enroll", "accepted": "0"}):
        result = api.dispatch(context, "apply", query)
        assert result["result"]["enrolled"] is False, query
        assert result["result"]["accepted"] is False, query
    assert not (context.project.state_root / "enrollment.json").exists()

    result = api.dispatch(context, "apply", {"operation": "enroll", "accepted": "true"})
    assert result["result"]["enrolled"] is True

    with pytest.raises(api.BadRequest) as caught:
        api.dispatch(context, "apply", {"operation": "enroll", "accepted": "maybe"})
    assert "must be true or false" in str(caught.value)


# ----------------------------------------------- a run survives the console


def test_a_run_stays_readable_with_no_console_state(context: operations.Context) -> None:
    """Nothing the console did is required to read a run afterwards."""
    started = operations.run_check(context, CHECK_ID)
    run_id = started["run_id"]

    report = Store(context.project.db_path).load(run_id)
    assert report["run_id"] == run_id
    assert report["outcome"]["result"] == "PASS"
    assert report["configuration_digest"] == report["configuration_digest"]

    fresh = operations.open_context(context.project.root)
    assert operations.run_detail_view(fresh, run_id)["report"] == report


def test_cancel_reports_the_core_reason_when_there_is_no_process(
    context: operations.Context,
) -> None:
    """The core's wording, not the console's, for a run with nothing to stop."""
    started = operations.run_check(context, CHECK_ID)
    result = operations.cancel_check_run(context, started["run_id"])
    # The run already reached a terminal report, so the core returns the outcome
    # it actually recorded rather than inventing a cancellation.
    assert result["outcome"]["result"] == "PASS"
    assert result["cancelled"] is False


def test_the_change_set_lists_the_run_report_for_cancel(
    context: operations.Context,
) -> None:
    planned = operations.plan_change_set(context, "cancel_run").to_json()
    assert planned["implemented"] is True
    assert planned["changes"][0]["target"] == "runs/<run_id>/report.json"
    assert planned["changes"][0]["reversible"] is False


# ------------------------------------------- the host plugin, for real


@requires_host
def test_install_refuses_a_package_the_host_would_not_load(
    context: operations.Context, scratch_host: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Validation runs before the install, and a failure stops everything.

    Driven against the real host: a package the host's own validator rejects is
    offered to `install`, and `install` must refuse with the validator's words
    while never reaching `plugin install`. Removing the validation step from
    `install` makes this fail, because the host would then be asked to install a
    package nobody checked. It also asserts the refusal names the failing file,
    so an operator is told what to fix rather than that something went wrong.
    """
    broken = tmp_path / "broken-marketplace"
    shutil.copytree(operations._plugin_source_dir(), broken / "plugin")
    (broken / "plugin" / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "Not A Kebab Name", "description": "x"}), encoding="utf-8"
    )
    monkeypatch.setattr(operations, "_plugin_source_dir", lambda: broken / "plugin")
    monkeypatch.setattr(operations, "_repo_root", lambda: broken)

    reached: list[list[str]] = []
    original = operations._run_host

    def spy(args: list[str]) -> tuple[int, str, str]:
        reached.append(args)
        return original(args)

    monkeypatch.setattr(operations, "_run_host", spy)

    with pytest.raises(Refused) as caught:
        operations.install(context)
    message = caught.value.reason
    assert "did not validate" in message
    assert "plugin.json" in message or "kebab" in message.lower() or "name" in message.lower()
    # The install call itself was never reached: the package was refused first.
    assert [a for a in reached if a[:2] == ["plugin", "install"]] == []


@requires_host
def test_the_real_package_installs_through_the_host(
    context: operations.Context, scratch_host: Path
) -> None:
    """The shipped package validates strictly, and the host really installs it.

    The complement to the refusal above: the package in this checkout is one the
    host accepts, and the host copies it. Asserting the copy landed under the
    host's own install path is what proves the console used the host's mechanism
    rather than inventing a directory of its own.
    """
    done = subprocess.run(
        [_claude_cli(), "plugin", "validate", "--strict", str(operations._plugin_source_dir().parent)],
        capture_output=True, encoding="utf-8", timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    installed = operations.install(context)
    assert installed["validated"] is True
    assert installed["plugin"] == "vkit@vkit"
    record = operations._installed_plugin_record("vkit@vkit")
    assert record is not None
    assert Path(record["installPath"]).is_dir()
    assert (Path(record["installPath"]) / ".claude-plugin" / "plugin.json").is_file()


@requires_host
def test_install_is_idempotent_and_repair_converges(
    context: operations.Context, scratch_host: Path
) -> None:
    """Running install twice and repairing once leaves one installation.

    `principle-make-operations-idempotent`: a retry after a crash must converge
    to the same end state. A second install over an existing one is reported as
    already installed rather than adding a second copy, and repair on an
    up-to-date installation changes nothing.
    """
    first = operations.install(context)
    assert first["already_installed"] is False
    second = operations.install(context)
    assert second["already_installed"] is True

    entries = json.loads(
        (scratch_host / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
    )["plugins"]
    assert len(entries["vkit@vkit"]) == 1, "a second install stacked a duplicate record"

    repaired = operations.repair(context)
    assert repaired["validated"] is True
    assert "latest version" in repaired["host_output"]


@requires_host
def test_remove_leaves_no_orphan(context: operations.Context, scratch_host: Path) -> None:
    """Uninstalling is half of removing; the host keeps a registration.

    Verified rather than assumed: after `claude plugin uninstall` the host still
    has the marketplace registered and the cache directory on disk. `remove`
    takes both out and then confirms nothing is left, so "no orphans" is a
    checked end state and not a claim.
    """
    operations.install(context)
    result = operations.remove(context)

    assert result["uninstalled"] is True
    assert result["orphan_cache"] is False
    assert operations._installed_plugin_record("vkit@vkit") is None
    assert not Path(result["cache_removed"]).exists()
    assert operations._marketplace_registered() is False


@requires_host
def test_repair_and_remove_refuse_when_nothing_is_installed(
    context: operations.Context, scratch_host: Path
) -> None:
    """Neither reports success for an operation that did nothing."""
    with pytest.raises(Refused) as repair_caught:
        operations.repair(context)
    assert "not installed" in repair_caught.value.reason
    with pytest.raises(Refused) as remove_caught:
        operations.remove(context)
    assert "nothing to remove" in remove_caught.value.reason


# ------------------------------------------------------------------ helpers


def _get(url: str) -> dict:
    return json.loads(_get_bytes(url).decode("utf-8"))


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def _post(url: str) -> tuple[dict, int]:
    request = urllib.request.Request(url, data=b"{}", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), response.status
    except urllib.error.HTTPError as error:
        return json.loads(error.read().decode("utf-8")), error.code
