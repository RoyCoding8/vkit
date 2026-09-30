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
    NotImplementedInBuild,
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


def test_the_unimplemented_operations_refuse_rather_than_pretend(
    context: operations.Context,
) -> None:
    for name in ("install", "repair", "remove", "enroll"):
        with pytest.raises(NotImplementedInBuild) as caught:
            operations.OPERATIONS[name](context)
        assert name in str(caught.value)
        assert "not implemented in this build" in str(caught.value)


def test_the_console_wrote_nothing_while_refusing(context: operations.Context) -> None:
    """An honest gap leaves the repository byte-identical.

    The point of refusing is that nothing happened. A stub that created a
    plausible directory would pass the exception assertion above and still have
    a real blast radius, so the tree is compared before and after.
    """
    project = open_project(context.project.root)

    def fingerprint() -> list[tuple[str, int]]:
        return sorted(
            (str(p.relative_to(project.root)), p.stat().st_size)
            for p in project.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )

    before = fingerprint()
    for name in ("install", "repair", "remove", "enroll"):
        with pytest.raises(NotImplementedInBuild):
            operations.OPERATIONS[name](context)
    assert fingerprint() == before


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

    Every request parameter is an identifier, a count or a stream name. None of
    them is a path, so there is no value a caller could supply that turns into a
    repository path the console would then read or write.
    """
    assert api.PARAM_NAMES == frozenset({
        "limit", "run_id", "check_id", "stream", "max_bytes", "operation",
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


def test_an_unimplemented_operation_over_http_is_a_501(
    context: operations.Context,
) -> None:
    """Applying an operation the core lacks is 501, and it is on the surface.

    Not a 404. The operation is named in the writable list and reachable through
    the API; it refuses because the core has not grown it, which is a different
    fact from the console not offering it.
    """
    bound, _thread = server.start_in_thread(context, port=0)
    port = bound.server_address[1]
    try:
        for name in ("install", "repair", "remove", "enroll"):
            document, status = _post(f"http://127.0.0.1:{port}/api/apply?operation={name}")
            assert status == 501, name
            assert document["implemented"] is False
            assert document["operation"] == name
            assert document["writable_surface"] == list(WRITABLE_NAMES)
            assert "not implemented in this build" in document["error"]
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
    planned = operations.plan_change_set(context, "install").to_json()
    assert planned["implemented"] is False
    assert planned["changes"] == []
    assert "no core operation" in planned["note"].lower() or planned["note"]


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
