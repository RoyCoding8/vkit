from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkit.operations.contract import (
    FileInput,
    Operation,
    OperationContractError,
    OperationInputError,
    cli_arguments,
    invoke,
    operation_by_name,
    validate_arguments,
)


def test_catalog_descriptor_drives_cli_and_mcp(monkeypatch, tmp_path, capsys):
    from vkit import cli, mcp

    calls = []

    def handler(root, text, count, enabled, items, payload):
        calls.append((root, text))
        return {"status": "FAILED" if text == "fail" else "READY", "text": text,
                "count": count, "enabled": enabled,
                "items": items, "payload": payload}

    operation = Operation(
        version=1,
        name="tiny_probe",
        description="Run a tiny probe.",
        properties={
            "text": {"type": "string", "minLength": 1},
            "count": {"type": "integer", "minimum": 1, "default": 7},
            "enabled": {"type": "boolean", "default": False},
            "items": {"type": "array", "items": {"type": "string"}, "default": ["catalog"]},
            "payload": {"type": "object", "default": {"tag": "catalog"}},
        },
        required=("text",),
        handler=handler,
        outcomes={"READY": 0, "FAILED": 1},
    )

    parser = argparse.ArgumentParser()
    compute = parser.add_subparsers(dest="operation", required=True)
    cli._add_operation_parser(compute, operation)
    help_text = parser.format_help()
    assert "tiny-probe" in help_text
    parsed = parser.parse_args([
        "tiny-probe", "--text", "hello", "--count", "9", "--enabled", "true",
        "--items", '["one", "two"]', "--payload", '{"tag": "cli"}',
    ])
    assert (parsed.text, parsed.count, parsed.enabled, parsed.items, parsed.payload) == (
        "hello", 9, True, ["one", "two"], {"tag": "cli"})

    monkeypatch.setattr(cli, "OPERATIONS", (operation,))
    monkeypatch.setattr(cli, "_context", lambda _args: SimpleNamespace(project=SimpleNamespace(root=tmp_path)))
    assert cli.main(["compute", "tiny-probe", "--text", "from-cli", "--json"]) == 0
    assert '"count": 7' in capsys.readouterr().out
    assert calls[-1] == (tmp_path, "from-cli")
    assert cli.main(["compute", "tiny-probe", "--text", "fail", "--json"]) == 1
    assert calls[-1] == (tmp_path, "fail")

    definition = mcp.tool_definitions((operation,))[0]
    assert definition["name"] == "tiny_probe"
    assert definition["inputSchema"]["properties"]["count"]["default"] == 7
    monkeypatch.setattr(mcp, "BY_NAME", {operation.name: operation})
    body, is_error = mcp.Server(tmp_path).call(operation.name, {"text": "from-mcp"})
    assert not is_error
    assert body == {"status": "READY", "text": "from-mcp", "count": 7, "enabled": False,
                    "items": ["catalog"], "payload": {"tag": "catalog"}}
    assert calls[-1] == (tmp_path, "from-mcp")

    body, is_error = mcp.Server(tmp_path).call(operation.name, {"text": "", "extra": "refused"})
    assert is_error and "extra" in body["error"]
    assert len(calls) == 3


def test_operation_defaults_are_copied_and_shared_by_validation(tmp_path):
    def handler(_root, items):
        items.append("handler")
        return {"status": "READY", "items": items}

    operation = Operation(1, "copy_default", "Copy defaults.",
                          {"items": {"type": "array", "items": {"type": "string"},
                                     "default": ["catalog"]}}, (), handler, {"READY": 0})
    assert invoke(operation, tmp_path, {}) == {"status": "READY", "items": ["catalog", "handler"]}
    assert validate_arguments(operation, {}) == {"items": ["catalog"]}


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_inputs_are_refused_before_dispatch(value, tmp_path):
    operation = Operation(1, "finite_input", "Accept a finite number.",
                          {"value": {"type": "number"}}, ("value",),
                          lambda _root, value: {"status": "OK", "value": value}, {"OK": 0})
    assert invoke(operation, tmp_path, {"value": 1.5}) == {"status": "OK", "value": 1.5}
    with pytest.raises(OperationInputError, match="finite numbers"):
        invoke(operation, tmp_path, {"value": value})


def test_contract_rejects_bad_versions_and_duplicate_names():
    with pytest.raises(ValueError, match="version"):
        Operation(2, "new_op", "A new operation.", {}, (), lambda _root: {"status": "OK"}, {"OK": 0})
    operation = Operation(1, "same_name", "One operation.", {}, (),
                          lambda _root: {"status": "OK"}, {"OK": 0})
    with pytest.raises(ValueError, match="duplicate operation name"):
        operation_by_name((operation, operation))

    with pytest.raises(ValueError, match="invalid input schema"):
        Operation(1, "bad_schema", "An invalid schema.",
                  {"value": {"type": "string", "minLength": "one"}}, (),
                  lambda _root, value: {"status": value}, {"OK": 0})


@pytest.mark.parametrize("result", [
    ["READY"],
    {"value": 1},
    {"status": "NOT_DECLARED"},
    {"status": "READY", "value": object()},
    {"status": "READY", "value": math.nan},
])
def test_invoke_fails_closed_on_malformed_results(result, tmp_path):
    operation = Operation(1, "malformed_result", "Return a malformed result.", {}, (),
                          lambda _root: result, {"READY": 0})
    with pytest.raises(OperationContractError):
        invoke(operation, tmp_path, {})


@pytest.mark.parametrize("name", ["root", "command", "compute_command", "handler", "project", "json"])
def test_contract_rejects_transport_owned_inputs(name):
    with pytest.raises(ValueError):
        Operation(1, "collision", "A colliding operation.", {name: {"type": "string"}}, (),
                  lambda _root, **_args: {"status": "OK"}, {"OK": 0})


def test_cli_file_mapping_keeps_replacement_flag_bounded_and_utf8(tmp_path):
    from vkit.cli import build_parser
    from vkit.operations.catalog import OPERATIONS_BY_NAME

    operation = OPERATIONS_BY_NAME["check_rewrite"]
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes("é".encode("utf-8"))
    parsed = build_parser().parse_args([
        "compute", "check-rewrite", "--path", "subject.py", "--function", "f",
        "--replacement-file", str(replacement),
    ])
    values = cli_arguments(operation, vars(parsed))
    assert values["replacement"] == "é"
    assert "timeout_ms" not in values
    assert validate_arguments(operation, values)["timeout_ms"] == 2_000

    too_small = Operation(1, "file_probe", "Read a small file.", {"source": {"type": "string"}},
                          ("source",), lambda _root, source: {"status": "OK", "source": source},
                          {"OK": 0}, cli_files={"source": FileInput("source-file", 1)})
    with pytest.raises(OperationInputError, match="at most 1 bytes"):
        cli_arguments(too_small, {"source_file": replacement})
    replacement.write_bytes(b"\xff")
    with pytest.raises(OperationInputError, match="valid UTF-8"):
        cli_arguments(operation, {"replacement_file": replacement})


def test_undeclared_result_status_fails_closed_in_mcp_and_cli(monkeypatch, tmp_path, capsys):
    from vkit import cli, mcp

    operation = Operation(1, "bad_result", "Return a bad result.", {}, (),
                          lambda _root: {"status": "NOT_DECLARED"}, {"READY": 0})
    monkeypatch.setattr(mcp, "BY_NAME", {operation.name: operation})
    body, is_error = mcp.Server(tmp_path).call(operation.name, {})
    assert is_error and body["kind"] == "operation_contract_error"

    monkeypatch.setattr(cli, "OPERATIONS", (operation,))
    monkeypatch.setattr(cli, "_context", lambda _args: SimpleNamespace(project=SimpleNamespace(root=tmp_path)))
    assert cli.main(["compute", operation.cli_name, "--json"]) == 4
    assert capsys.readouterr().out.strip() == (
        '{"error": "bad_result returned undeclared status \'NOT_DECLARED\'", "exit_code": 4}')


def test_cli_keeps_operation_input_errors_at_exit_two(monkeypatch, tmp_path, capsys):
    from vkit import cli

    replacement = tmp_path / "replacement.py"
    replacement.write_text("def f(x):\n    return x\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_context", lambda _args: SimpleNamespace(project=SimpleNamespace(root=tmp_path)))
    assert cli.main([
        "compute", "check-rewrite", "--path", "../outside.py", "--function", "f",
        "--replacement-file", str(replacement), "--json",
    ]) == 2
    assert '"exit_code": 2' in capsys.readouterr().out

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert cli.main([
        "compute", "reduce-failure", "--run-id", "missing", "--path", "input.txt", "--json",
    ]) == 2
    assert '"exit_code": 2' in capsys.readouterr().out


def test_importing_transports_does_not_import_optional_engines():
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ, PYTHONPATH=str(source_root))
    script = (
        "import json, sys; import vkit.cli; import vkit.mcp; "
        "args = vkit.cli.build_parser().parse_args(['compute', 'compare-matchsets', "
        "'--old-pattern', 'a', '--new-pattern', 'a', '--alphabet', 'a']); "
        "print(json.dumps({'engines': sorted({'cvc5', 'egglog', 'greenery'} & sys.modules.keys()), "
        "'command': args.compute_command, 'tool': next(t['name'] for t in vkit.mcp.tool_definitions() "
        "if t['name'] == 'compare_matchsets')}))"
    )
    completed = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                               text=True, check=True)
    assert json.loads(completed.stdout) == {
        "engines": [], "command": "compare-matchsets", "tool": "compare_matchsets"}
