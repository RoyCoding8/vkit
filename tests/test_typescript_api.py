from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest
from helpers import McpClient, vkit

from vkit.operations.typescript_api import check_typescript_api_compatibility


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Path:
    project = Path(__file__).resolve().parents[1]
    helper = project / "tools" / "typescript" / "helper.cjs"
    lockfile = project / "tools" / "typescript" / "package-lock.json"
    node = shutil.which("node")
    if not node or not helper.is_file() or not (project / "tools" / "typescript" / "node_modules" / "typescript").is_dir():
        assert not os.environ.get("VKIT_REQUIRE_TYPESCRIPT_ENGINE"), "CI must install the pinned TypeScript engine"
        pytest.skip("pinned TypeScript helper is not installed")
    monkeypatch.setenv("VKIT_TYPESCRIPT_HELPER", str(helper))
    monkeypatch.setenv("VKIT_TYPESCRIPT_HELPER_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    monkeypatch.setenv("VKIT_TYPESCRIPT_LOCKFILE", str(lockfile))
    monkeypatch.setenv("VKIT_TYPESCRIPT_LOCK_SHA256", hashlib.sha256(lockfile.read_bytes()).hexdigest())
    monkeypatch.setenv("VKIT_NODE_BIN", str(Path(node).resolve()))
    return project


def test_typescript_profile_cli_and_mcp(make_project, backend):
    project = make_project({"old/index.d.ts": "export declare function parse(x: string): string;",
                            "new/index.d.ts": "export declare function parse(x: unknown): string;"}, [], accept=False)
    code, body = vkit(project, "compute", "compare-contracts", "--old-path", "old/index.d.ts",
                      "--new-path", "new/index.d.ts", "--profile", "typescript-api")
    assert (code, body["status"], body["profile"]) == (0, "COMPATIBLE", "typescript-api")
    client = McpClient(project)
    try:
        body, error = client.call("compare_contracts", old_path="old/index.d.ts", new_path="new/index.d.ts",
                                 profile="typescript-api")
        assert not error and body["status"] == "COMPATIBLE"
    finally:
        client.close()


def _compare(tmp_path: Path, backend: Path, old: str, new: str) -> dict:
    (tmp_path / "old").mkdir(parents=True, exist_ok=True)
    (tmp_path / "new").mkdir(parents=True, exist_ok=True)
    (tmp_path / "old" / "index.d.ts").write_text(old, encoding="utf-8")
    (tmp_path / "new" / "index.d.ts").write_text(new, encoding="utf-8")
    return check_typescript_api_compatibility(tmp_path, "old/index.d.ts", "new/index.d.ts")


def test_function_compatibility_uses_strict_parameter_and_return_assignability(tmp_path, backend):
    old = "export declare function parse(value: string | number): string;\n"
    widened_parameter = "export declare function parse(value: unknown): string;\n"
    narrowed_parameter = "export declare function parse(value: string): string;\n"
    assert _compare(tmp_path, backend, old, widened_parameter)["status"] == "COMPATIBLE"
    assert _compare(tmp_path, backend, old, narrowed_parameter)["status"] == "BREAKING"
    assert _compare(tmp_path, backend, old, "export declare function parse(value: unknown): unknown;\n")["status"] == "BREAKING"


def test_type_only_exports_are_checked_and_additive_members_remain_assignable(tmp_path, backend):
    old = "export interface Result { id: string }\nexport type Token = string;\n"
    additive = "export interface Result { id: string; label: string }\nexport type Token = string;\n"
    removed_type = "export interface Result { id: string; label: string }\n"
    assert _compare(tmp_path, backend, old, additive)["status"] == "COMPATIBLE"
    result = _compare(tmp_path, backend, old, removed_type)
    assert result["status"] == "BREAKING"
    assert any(item["name"] == "Token" and not item["compatible"] for item in result["exports"])


def test_generic_constraints_and_arity_are_checked(tmp_path, backend):
    old = "export interface Box<T extends string> { value: T }\n"
    assert _compare(tmp_path, backend, old, old)["status"] == "COMPATIBLE"
    stricter = "export interface Box<T extends 'fixed'> { value: T }\n"
    assert _compare(tmp_path, backend, old, stricter)["status"] == "BREAKING"
    broader = "export interface Box<T> { value: T }\n"
    assert _compare(tmp_path, backend, old, broader)["status"] == "COMPATIBLE"
    extra_parameter = "export interface Box<T extends string, U = number> { value: T }\n"
    assert _compare(tmp_path, backend, old, extra_parameter)["status"] == "BREAKING"
    generic_method = "export interface Handler<T extends string> { handle(value: T): T }\n"
    assert _compare(tmp_path, backend, generic_method, generic_method)["status"] == "COMPATIBLE"


def test_method_parameters_do_not_use_typescripts_bivariant_method_exception(tmp_path, backend):
    old = "export interface Handler { handle(value: string | number): string }\n"
    narrowed = "export interface Handler { handle(value: string): string }\n"
    widened = "export interface Handler { handle(value: unknown): string }\n"
    assert _compare(tmp_path, backend, old, narrowed)["status"] == "BREAKING"
    assert _compare(tmp_path, backend, old, widened)["status"] == "COMPATIBLE"


def test_construct_signatures_are_checked_as_public_values(tmp_path, backend):
    old = "export declare class Client { constructor(url: string); request(): string }\n"
    compatible = "export declare class Client { constructor(url: string | URL); request(): string }\n"
    breaking = "export declare class Client { constructor(url: 'https'); request(): string }\n"
    assert _compare(tmp_path, backend, old, compatible)["status"] == "COMPATIBLE"
    assert _compare(tmp_path, backend, old, breaking)["status"] == "BREAKING"


def test_overload_and_optional_argument_changes_preserve_old_calls(tmp_path, backend):
    old = "export declare function read(path: string): string;\nexport declare function read(path: string, encoding?: 'utf8'): string;\n"
    added = old + "export declare function read(path: URL): Uint8Array;\n"
    breaking = "export declare function read(path: string, encoding: 'utf8'): string;\n"
    assert _compare(tmp_path, backend, old, added)["status"] == "COMPATIBLE"
    assert _compare(tmp_path, backend, old, breaking)["status"] == "BREAKING"


def test_local_declaration_closure_is_captured_and_reported(tmp_path, backend):
    for side in ("old", "new"):
        directory = tmp_path / side
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "model.d.ts").write_text("export interface Model { value: string }\n", encoding="utf-8")
    (tmp_path / "old" / "index.d.ts").write_text("export { Model } from './model';\n", encoding="utf-8")
    (tmp_path / "new" / "index.d.ts").write_text("export { Model } from './model';\n", encoding="utf-8")
    result = check_typescript_api_compatibility(tmp_path, "old/index.d.ts", "new/index.d.ts")
    assert result["status"] == "COMPATIBLE"
    assert result["declaration_closure"] == {"old": ["index.d.ts", "model.d.ts"],
                                              "new": ["index.d.ts", "model.d.ts"]}
    assert result["source_identity"]["old_sha256"] == result["source_identity"]["new_sha256"]


def test_external_declaration_import_is_unsupported_not_a_pass(tmp_path, backend):
    result = _compare(tmp_path, backend, "import type { X } from 'external-package';\nexport type Api = X;\n",
                      "import type { X } from 'external-package';\nexport type Api = X;\n")
    assert result["status"] == "UNSUPPORTED"


def test_any_is_refused_instead_of_masking_assignability(tmp_path, backend):
    result = _compare(tmp_path, backend, "export declare function value(): any;\n",
                      "export declare function value(): string;\n")
    assert result["status"] == "UNSUPPORTED"


def test_typescript_diagnostics_do_not_become_compatibility_evidence(tmp_path, backend):
    result = _compare(tmp_path, backend, "export declare const value: MissingType;\n",
                      "export declare const value: MissingType;\n")
    assert result["status"] == "UNKNOWN"
    assert result["diagnostics"]


def test_helper_identity_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("VKIT_TYPESCRIPT_HELPER", raising=False)
    path = tmp_path / "index.d.ts"
    path.write_text("export type Value = string;\n", encoding="utf-8")
    result = check_typescript_api_compatibility(tmp_path, path.name, path.name)
    assert result["status"] == "UNAVAILABLE"
    assert result["source_identity"]["old_sha256"]
