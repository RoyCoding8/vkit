from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from vkit.operations.source_migrations import preview_typescript_symbol_rename


@pytest.fixture
def configured_typescript(monkeypatch: pytest.MonkeyPatch) -> Path:
    project = Path(__file__).parents[1]
    helper = project / "tools" / "typescript" / "migration-helper.cjs"
    lockfile = project / "tools" / "typescript" / "package-lock.json"
    node = shutil.which("node")
    if not node or not helper.is_file() or not (project / "tools" / "typescript" / "node_modules" / "typescript").is_dir():
        pytest.skip("the pinned TypeScript rename backend is not installed")
    monkeypatch.setenv("VKIT_TYPESCRIPT_RENAME_HELPER", str(helper))
    monkeypatch.setenv("VKIT_TYPESCRIPT_RENAME_HELPER_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    monkeypatch.setenv("VKIT_TYPESCRIPT_LOCKFILE", str(lockfile))
    monkeypatch.setenv("VKIT_TYPESCRIPT_LOCK_SHA256", hashlib.sha256(lockfile.read_bytes()).hexdigest())
    monkeypatch.setenv("VKIT_NODE_BIN", str(Path(node).resolve()))
    return helper


def _write_project(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _rename(root: Path, **kwargs) -> dict:
    return preview_typescript_symbol_rename(root, "src/api.ts", "oldName", "newName", **kwargs)


def test_rename_preview_preserves_explicit_import_alias_and_does_not_write_source(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": 'export function oldName(value: number): number { return value + 1; }\n',
        "src/use.ts": 'import { oldName as localName } from "./api";\nexport const result = localName(1);\n',
    })
    before = (tmp_path / "src/api.ts").read_bytes()
    result = _rename(tmp_path)
    assert result["status"] == "PREVIEW", result
    assert result["diagnostics_before"] == result["diagnostics_after"] == []
    assert result["binding_counts"]["renamed_sites"] == 2
    assert result["binding_counts"]["preserved_alias_sites"] >= 2
    assert len(result["matched_sites"]) == 2
    assert "export function newName" in result["patch"]
    assert "import { newName as localName }" in result["patch"]
    assert "result = localName(1)" in result["patch"]
    assert (tmp_path / "src/api.ts").read_bytes() == before


def test_shorthand_import_becomes_explicit_alias_so_local_binding_stays_stable(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": "export function oldName(value: number): number { return value; }\n",
        "src/use.ts": 'import { oldName } from "./api";\nexport const result = oldName(1);\n',
    })
    result = _rename(tmp_path)
    assert result["status"] == "PREVIEW", result
    assert "import { newName as oldName }" in result["patch"]
    assert "result = oldName(1)" in result["patch"]


def test_longer_rename_maps_shorthand_export_after_earlier_edits(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": (
            "function oldName(): number { return 1; }\n"
            "const first = oldName();\n"
            "export { oldName };\n"
        ),
    })
    result = preview_typescript_symbol_rename(tmp_path, "src/api.ts", "oldName", "longerName")
    assert result["status"] == "PREVIEW", result
    assert "function longerName" in result["patch"]
    assert "export { longerName as oldName }" in result["patch"]
    assert "const first = longerName()" in result["patch"]


def test_longer_rename_maps_shorthand_import_after_an_earlier_import_edit(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": "export function oldName(): number { return 1; }\n",
        "src/barrel.ts": (
            'export { oldName as primary } from "./api";\n'
            'export { oldName } from "./api";\n'
        ),
        "src/use.ts": (
            'import { oldName as localName } from "./api";\n'
            'import { oldName } from "./api";\n'
            "const result = localName() + oldName();\n"
            "export { oldName };\n"
        ),
    })
    result = preview_typescript_symbol_rename(tmp_path, "src/api.ts", "oldName", "longerName")
    assert result["status"] == "PREVIEW", result
    assert 'import { longerName as localName } from "./api"' in result["patch"]
    assert 'import { longerName as oldName } from "./api"' in result["patch"]
    assert 'export { longerName as primary } from "./api"' in result["patch"]
    assert 'export { longerName as oldName } from "./api"' in result["patch"]
    assert "const result = localName() + oldName()" in result["patch"]
    assert 'export { oldName };' in result["patch"]


def test_shadowed_same_spelling_is_not_renamed(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": (
            "export function oldName(value: number): number { return value; }\n"
            "export function wrapper(oldName: number): number { return oldName + 1; }\n"
        ),
    })
    result = _rename(tmp_path)
    assert result["status"] == "PREVIEW", result
    assert result["patch"].count("newName") == 1
    assert "wrapper(oldName: number)" in result["patch"]


def test_name_collision_is_refused_after_compiler_recheck(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": (
            "export function oldName(value: number): number { return value; }\n"
            "export function newName(value: number): number { return value + 1; }\n"
        ),
    })
    result = _rename(tmp_path)
    assert result["status"] == "UNSUPPORTED", result
    assert result["diagnostics_after"]
    assert "newName" not in result.get("patch", "")


def test_rename_does_not_capture_a_preexisting_reference(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": (
            "const newName = 1;\n"
            "export function wrapper(): number {\n"
            "  const oldName = 2;\n"
            "  return newName + oldName;\n"
            "}\n"
        ),
    })
    result = _rename(tmp_path)
    assert result["status"] == "UNSUPPORTED", result
    assert "changes the compiler binding" in result["detail"]
    assert "patch" not in result


def test_object_shorthand_dual_binding_is_rejected_without_a_preview(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": "const oldName = 2;\nexport const record = { oldName };\n",
    })
    result = _rename(tmp_path)
    assert result["status"] == "UNSUPPORTED", result
    assert "patch" not in result


@pytest.mark.parametrize("source, reason", [
    ('import { value } from "external-package";\nexport function oldName(x: number) { return x; }\n', "relative"),
    ('export async function oldName() { return import("./lazy"); }\n', "dynamic import"),
    ('export function oldName(x: number) { return x; }\nconst dynamic = require("./other");\n', "require"),
    ('export function oldName(x: number) { return x; }\nconst key = "oldName";\n', "string literal"),
])
def test_external_dynamic_and_reflection_edges_fail_closed(tmp_path, configured_typescript, source, reason):
    _write_project(tmp_path, {"src/api.ts": source})
    result = _rename(tmp_path)
    assert result["status"] == "UNSUPPORTED", result
    assert reason in result["detail"]


def test_preexisting_type_diagnostics_do_not_become_a_rename_preview(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": "export function oldName(value: number): number { return missingName; }\n",
    })
    result = _rename(tmp_path)
    assert result["status"] == "UNSUPPORTED", result
    assert result["diagnostics_before"]


def test_unicode_before_symbol_keeps_byte_patch_offsets_correct(tmp_path, configured_typescript):
    _write_project(tmp_path, {
        "src/api.ts": 'const marker = "🧪";\nexport function oldName(value: number): number { return value; }\n',
    })
    result = _rename(tmp_path)
    assert result["status"] == "PREVIEW", result
    assert "export function newName" in result["patch"]


def test_no_final_newline_preview_applies_as_git_patch_and_preserves_bytes(tmp_path, configured_typescript):
    original = b"export function oldName(): number { return 1; }"
    _write_project(tmp_path, {"src/api.ts": original.decode("utf-8")})
    result = preview_typescript_symbol_rename(tmp_path, "src/api.ts", "oldName", "longerName")
    assert result["status"] == "PREVIEW", result
    assert "\\ No newline at end of file" in result["patch"]

    apply_root = tmp_path / "apply-check"
    apply_root.mkdir()
    subprocess.run(["git", "init", str(apply_root)], check=True, capture_output=True)
    target = apply_root / "src" / "api.ts"
    target.parent.mkdir()
    target.write_bytes(original)
    patch = result["patch"].encode("utf-8")
    checked = subprocess.run(["git", "apply", "--check", "-"], cwd=apply_root,
                             input=patch, capture_output=True, check=False)
    assert checked.returncode == 0, checked.stderr.decode("utf-8", "replace")
    applied = subprocess.run(["git", "apply", "-"], cwd=apply_root,
                             input=patch, capture_output=True, check=False)
    assert applied.returncode == 0, applied.stderr.decode("utf-8", "replace")
    assert target.read_bytes() == b"export function longerName(): number { return 1; }"


def test_invalid_target_path_is_rejected_without_starting_node(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid target paths must be rejected before starting the helper")

    monkeypatch.setattr("vkit.operations.source_migrations.run", forbidden)
    result = preview_typescript_symbol_rename(tmp_path, "../api.ts", "oldName", "newName")
    assert result["status"] == "UNSUPPORTED"
