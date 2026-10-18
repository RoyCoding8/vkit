from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vkit.operations.dependencies import analyze_dependencies


_ENV = {
    "helper": "VKIT_DEPENDENCY_HELPER",
    "helper_sha": "VKIT_DEPENDENCY_SHA256",
    "lockfile": "VKIT_DEPENDENCY_LOCKFILE",
    "lock_sha": "VKIT_DEPENDENCY_LOCK_SHA256",
    "node": "VKIT_NODE_BIN",
}


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def configured_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {name: os.environ.get(variable) for name, variable in _ENV.items()}
    if not any(values.values()):
        pytest.skip("the pinned dependency backend is not configured")
    if not all(values.values()):
        pytest.fail("the dependency backend requires helper, helper hash, lockfile, lock hash, and Node paths")
    helper = Path(values["helper"]).resolve()
    lockfile = Path(values["lockfile"]).resolve()
    if not helper.is_file() or hashlib.sha256(helper.read_bytes()).hexdigest() != values["helper_sha"].lower():
        pytest.fail("the configured dependency helper does not match its SHA-256")
    if not lockfile.is_file() or hashlib.sha256(lockfile.read_bytes()).hexdigest() != values["lock_sha"].lower():
        pytest.fail("the configured dependency package lock does not match its SHA-256")
    if not Path(values["node"]).is_file():
        pytest.fail("the configured Node executable does not exist")
    for name, value in values.items():
        monkeypatch.setenv(_ENV[name], str(helper if name == "helper" else lockfile if name == "lockfile" else value))


def _configure_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> None:
    helper = tmp_path / "fake-helper.mjs"
    lockfile = tmp_path / "package-lock.json"
    helper.write_text(source, encoding="utf-8")
    lockfile.write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    node = os.environ.get("VKIT_NODE_BIN") or shutil.which("node")
    if not node:
        pytest.skip("Node is required for subprocess protocol tests")
    monkeypatch.setenv(_ENV["helper"], str(helper))
    monkeypatch.setenv(_ENV["helper_sha"], hashlib.sha256(helper.read_bytes()).hexdigest())
    monkeypatch.setenv(_ENV["lockfile"], str(lockfile))
    monkeypatch.setenv(_ENV["lock_sha"], hashlib.sha256(lockfile.read_bytes()).hexdigest())
    monkeypatch.setenv(_ENV["node"], str(Path(node).resolve()))


def test_real_backend_resolves_typescript_esm_commonjs_and_external_edges(repository, configured_backend):
    _write(repository, "src/entry.ts", "import { branch } from './branch.js';\nimport type { Model } from './model.js';\nimport React from 'react';\nexport const result: Model = { branch, React };\n")
    _write(repository, "src/branch.ts", "import './shared.js';\nexport const branch = 1;\n")
    _write(repository, "src/shared.js", "import './leaf.js';\nexport const shared = 2;\n")
    _write(repository, "src/leaf.js", "export const leaf = 3;\n")
    _write(repository, "src/model.ts", "export interface Model { branch: number; React: unknown }\n")
    _write(repository, "src/import-equals.ts", "import Model = require('./model.js');\nexport type Alias = Model.Model;\n")
    _write(repository, "src/main.cjs", "module.exports = require('./service.cjs');\n")
    _write(repository, "src/service.cjs", "module.exports = require('./leaf.js');\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "ANALYZED"
    assert result["result"]["nodes"] == [
        "src/branch.ts", "src/entry.ts", "src/import-equals.ts", "src/leaf.js", "src/main.cjs",
        "src/model.ts", "src/service.cjs", "src/shared.js",
    ]
    assert [(edge["source"], edge["target"], edge["specifier"], edge["external"])
            for edge in result["result"]["edges"]] == [
        ("src/branch.ts", "src/shared.js", "./shared.js", False),
        ("src/entry.ts", None, "react", True),
        ("src/entry.ts", "src/branch.ts", "./branch.js", False),
        ("src/entry.ts", "src/model.ts", "./model.js", False),
        ("src/import-equals.ts", "src/model.ts", "./model.js", False),
        ("src/main.cjs", "src/service.cjs", "./service.cjs", False),
        ("src/service.cjs", "src/leaf.js", "./leaf.js", False),
        ("src/shared.js", "src/leaf.js", "./leaf.js", False),
    ]
    assert result["backend_identity"]["dependency_cruiser"] == "18.5.0"
    assert result["runtime_absence_proven"] is False
    assert result["graph"] == {"node_count": 8, "edge_count": 8}


def test_real_backend_queries_have_deterministic_paths_cycles_impact_and_unreachable(repository, configured_backend):
    _write(repository, "app/start.js", "import './one.js';\n")
    _write(repository, "app/one.js", "import './two.js';\n")
    _write(repository, "app/two.js", "import './one.js';\n")
    _write(repository, "app/dependent.js", "import './one.js';\n")
    _write(repository, "app/unreachable.js", "export const unused = true;\n")

    path_result = analyze_dependencies(repository, "app", "path", ["app/start.js"], "app/two.js")
    cycles = analyze_dependencies(repository, "app", "cycles")
    affected = analyze_dependencies(repository, "app", "affected", ["app/one.js"])
    unreachable = analyze_dependencies(repository, "app", "unreachable", ["app/start.js"])

    assert path_result["status"] == "ANALYZED"
    assert path_result["result"] == {
        "source": "app/start.js", "target": "app/two.js",
        "witness": ["app/start.js", "app/one.js", "app/two.js"],
    }
    assert cycles["result"]["cycles"] == [["app/one.js", "app/two.js"]]
    assert affected["result"] == {
        "changed": ["app/one.js"],
        "affected": ["app/dependent.js", "app/one.js", "app/start.js", "app/two.js"],
    }
    assert unreachable["result"] == {
        "entrypoints": ["app/start.js"],
        "reachable": ["app/one.js", "app/start.js", "app/two.js"],
        "unreachable": ["app/dependent.js", "app/unreachable.js"],
    }


def test_real_backend_marks_nonliteral_imports_unknown_and_keeps_partial_graph(repository, configured_backend):
    _write(repository, "src/entry.js", "export const load = (name) => import(name);\n")
    _write(repository, "src/unused.js", "export const unused = true;\n")

    result = analyze_dependencies(repository, "src", "unreachable", ["src/entry.js"])

    assert result["status"] == "UNKNOWN"
    assert result["partial_graph"]["nodes"] == ["src/entry.js", "src/unused.js"]
    assert any("nonliteral" in reason for reason in result["incomplete_reasons"])
    assert "result" not in result


def test_real_backend_deduplicates_import_and_reexport_of_same_specifier(repository, configured_backend):
    _write(repository, "src/entry.js", "import { value } from './shared.js';\nexport { value } from './shared.js';\n")
    _write(repository, "src/shared.js", "export const value = 1;\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "ANALYZED"
    assert [(edge["source"], edge["target"], edge["specifier"])
            for edge in result["result"]["edges"]] == [("src/entry.js", "src/shared.js", "./shared.js")]


def test_real_backend_marks_ambient_module_imports_incomplete(repository, configured_backend):
    _write(repository, "src/ambient.d.ts", 'declare module "virtual" { import value from "./local.js"; }\n')
    _write(repository, "src/local.js", "export default 1;\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "UNKNOWN"
    assert result["partial_graph"]["nodes"] == ["src/ambient.d.ts", "src/local.js"]
    assert any("ambient imports" in reason for reason in result["incomplete_reasons"])


@pytest.mark.parametrize("expression", [
    "import(name)", "require(name)", "require.resolve(name)",
])
def test_real_backend_rejects_nonliteral_import_and_require_forms(repository, configured_backend, expression):
    _write(repository, "src/entry.js", f"const load = (name) => {expression};\n")

    result = analyze_dependencies(repository, "src", "path", ["src/entry.js"], "src/entry.js")

    assert result["status"] == "UNKNOWN"
    assert result["partial_graph"]["nodes"] == ["src/entry.js"]
    assert result["incomplete_reasons"]


def test_project_config_is_not_loaded_and_only_source_files_enter_the_snapshot(repository, configured_backend):
    marker = repository / "config-was-executed.txt"
    _write(repository, ".dependency-cruiser.cjs",
           f"require('node:fs').writeFileSync({json.dumps(str(marker))}, 'executed');\nthrow new Error('config executed');\n")
    _write(repository, "tsconfig.json", '{"extends":"./throwing-config.js"}\n')
    _write(repository, "throwing-config.js", "throw new Error('tsconfig executed');\n")
    _write(repository, "src/main.ts", "export const answer: number = 42;\n")

    result = analyze_dependencies(repository, ".")

    assert result["status"] == "ANALYZED"
    assert result["result"]["nodes"] == [".dependency-cruiser.cjs", "src/main.ts", "throwing-config.js"]
    assert "tsconfig.json" not in result["result"]["nodes"]
    assert not marker.exists()


def test_real_backend_marks_typescript_parse_diagnostics_unknown(repository, configured_backend):
    _write(repository, "src/main.ts", "export const = ;\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "UNKNOWN"
    assert result["incomplete_reasons"] == ["main.ts: parser diagnostics"]
    assert result["partial_graph"]["nodes"] == ["src/main.ts"] or result["partial_graph"]["nodes"] == []


def test_unresolved_local_import_returns_unknown_with_partial_edges(repository, configured_backend):
    _write(repository, "src/main.js", "import './missing.js';\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "UNKNOWN"
    assert result["partial_graph"]["nodes"] == ["src/main.js"]
    assert result["incomplete_reasons"]
    assert result["unresolved_dependencies"] == [{
        "source": "src/main.js", "target": None, "specifier": "./missing.js",
        "dynamic": False, "external": False,
    }]


def test_malformed_helper_output_is_unknown(repository, monkeypatch, tmp_path):
    _configure_fake(monkeypatch, tmp_path, "process.stdout.write('{');\n")
    _write(repository, "src/main.js", "export const answer = 42;\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "UNKNOWN"
    assert "malformed" in result["detail"]


def test_helper_refusal_is_unknown_with_bounded_diagnostic(repository, monkeypatch, tmp_path):
    _configure_fake(monkeypatch, tmp_path, "process.stderr.write('refused request'); process.exitCode = 2;\n")
    _write(repository, "src/main.js", "export const answer = 42;\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "UNKNOWN"
    assert "helper failed" in result["detail"]


def test_missing_backend_is_unavailable(repository, monkeypatch):
    for variable in _ENV.values():
        monkeypatch.delenv(variable, raising=False)
    _write(repository, "src/main.js", "export const answer = 42;\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "UNAVAILABLE"
    assert result["backend_identity"] is None


def test_backend_version_mismatch_is_unavailable(repository, monkeypatch, tmp_path):
    response = {"protocol_version": 1, "backend_version": "18.4.0", "typescript_version": "5.9.3",
                "complete": True, "incomplete_reasons": [], "modules": []}
    _configure_fake(monkeypatch, tmp_path, f"process.stdout.write({json.dumps(json.dumps(response))});\n")
    _write(repository, "src/main.js", "export const answer = 42;\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "UNAVAILABLE"
    assert "identity" in result["detail"]


def test_helper_timeout_is_unknown(repository, monkeypatch, tmp_path):
    _configure_fake(monkeypatch, tmp_path, "await new Promise((resolve) => setTimeout(resolve, 2_000));\n")
    _write(repository, "src/main.js", "export const answer = 42;\n")

    result = analyze_dependencies(repository, "src", timeout_seconds=1)

    assert result["status"] == "UNKNOWN"
    assert "time budget" in result["detail"]


def test_untrusted_helper_digest_is_unavailable(repository, monkeypatch, tmp_path):
    _configure_fake(monkeypatch, tmp_path, "process.stdout.write('{}');\n")
    monkeypatch.setenv(_ENV["helper_sha"], "0" * 64)
    _write(repository, "src/main.js", "export const answer = 42;\n")

    result = analyze_dependencies(repository, "src")

    assert result["status"] == "UNAVAILABLE"
    assert result["backend_identity"] is None


def test_out_of_subset_query_path_is_unsupported(repository, configured_backend):
    _write(repository, "src/main.js", "export const answer = 42;\n")
    _write(repository, "src/data.json", '{"answer":42}\n')

    result = analyze_dependencies(repository, "src", "path", ["src/main.js"], "src/data.json")

    assert result["status"] == "UNSUPPORTED"
    assert "outside" in result["detail"]


def test_invalid_query_shape_is_rejected(repository):
    with pytest.raises(ValueError, match="exactly one source and a target"):
        analyze_dependencies(repository, ".", "path", ["src/a.js"])
    with pytest.raises(ValueError, match="repository-relative"):
        analyze_dependencies(repository, ".", "affected", ["../outside.js"])


def test_missing_source_directory_is_unsupported(repository):
    result = analyze_dependencies(repository, "missing/source")

    assert result["status"] == "UNSUPPORTED"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions require Windows")
def test_reparse_point_directory_is_rejected_before_snapshot(repository):
    import vkit.operations.dependencies as dependencies

    source = repository / "src"
    outside = repository / "outside"
    source.mkdir()
    outside.mkdir()
    _write(outside, "secret.js", "export const secret = true;\n")
    junction = source / "linked"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        pytest.skip("the test environment cannot create NTFS junctions")
    assert junction.is_dir()
    assert dependencies._is_reparse_point(junction)
    result = analyze_dependencies(repository, "src")
    assert result["status"] == "UNSUPPORTED"
    assert "reparse points" in result["detail"]


def test_symlink_directory_is_rejected_before_snapshot(repository):
    source = repository / "src"
    outside = repository / "outside"
    source.mkdir()
    outside.mkdir()
    _write(outside, "secret.js", "export const secret = true;\n")
    try:
        (source / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("the test environment cannot create directory symlinks")

    result = analyze_dependencies(repository, "src")
    assert result["status"] == "UNSUPPORTED"
    assert "symlinks" in result["detail"]
