from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from vkit.paired_profiles import (
    ExecutionEnvironment,
    Language,
    identify_environment,
    parse_binding,
    parse_environment,
    prepare_program,
    validate_environment,
)


def _write_file(path: Path, content: bytes = b"tool") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _binding(language: str, source: str, symbol: str = "step") -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "u32_function",
        "language": language,
        "source": source,
        "symbol": symbol,
    }


def _environment(language: Language, root: Path) -> ExecutionEnvironment:
    compiler = _write_file(root / "tools" / "compiler.exe")
    runtime = _write_file(root / "tools" / "runtime.exe")
    raw = {
        "rust": identify_environment("rust", compiler=compiler),
        "python": identify_environment("python", runtime=runtime),
        "typescript": identify_environment(
            "typescript", compiler=compiler, runtime=runtime
        ),
    }[language]
    return parse_environment(raw, language)


@pytest.mark.parametrize(
    ("language", "source"),
    [("rust", "src/step.rs"), ("python", "src/step.py"), ("typescript", "src/step.ts")],
)
def test_binding_parses_closed_language_and_normalized_source(language, source):
    binding = parse_binding(_binding(language, source))

    assert (binding.language, binding.source, binding.symbol) == (language, source, "step")
    with pytest.raises(FrozenInstanceError):
        binding.source = "changed"


@pytest.mark.parametrize(
    "raw",
    [
        {**_binding("rust", "src/step.rs"), "extra": 1},
        {**_binding("rust", "src/../step.rs"), "source": "src/../step.rs"},
        {**_binding("rust", "src/step.rs"), "source": "src\\step.rs"},
        {**_binding("rust", "src//step.rs"), "source": "src//step.rs"},
        {**_binding("rust", "src/step.rs"), "symbol": "fünction"},
        {**_binding("rust", "src/step.rs"), "schema_version": True},
        {**_binding("other", "src/step.rs")},
    ],
)
def test_binding_rejects_unknown_or_unsafe_declarations(raw):
    with pytest.raises(ValueError):
        parse_binding(raw)


@pytest.mark.parametrize(
    ("language", "roles"),
    [
        ("rust", {"compiler"}),
        ("python", {"runtime"}),
        ("typescript", {"compiler", "runtime"}),
    ],
)
def test_environment_identifies_exact_tool_roles(language, roles, tmp_path):
    compiler = _write_file(tmp_path / "compiler.exe", b"compiler")
    runtime = _write_file(tmp_path / "runtime.exe", b"runtime")
    raw = identify_environment(
        language,
        compiler=compiler if "compiler" in roles else None,
        runtime=runtime if "runtime" in roles else None,
    )

    environment = parse_environment(raw, language)
    assert {role for role, _ in environment.tools} == roles
    validate_environment(environment)


def test_identify_environment_omits_non_allowlisted_host_variables(tmp_path, monkeypatch):
    runtime = _write_file(tmp_path / "python.exe")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("PYTHONPATH", "untrusted path")
    monkeypatch.setenv("NODE_OPTIONS", "--require untrusted.js")

    raw = identify_environment("python", runtime=runtime)

    assert raw["variables"].keys().isdisjoint(
        {"GITHUB_TOKEN", "PYTHONPATH", "NODE_OPTIONS"}
    )


def test_typescript_environment_pins_adjacent_libs_and_explicit_dependencies(tmp_path):
    compiler = _write_file(tmp_path / "typescript" / "lib" / "tsc.exe")
    runtime = _write_file(tmp_path / "node.exe")
    lib_a = _write_file(compiler.parent / "lib.es2022.d.ts", b"es2022")
    lib_b = _write_file(compiler.parent / "lib.dom.d.ts", b"dom")
    extra = _write_file(tmp_path / "typescript" / "typescript.d.ts", b"extra")

    raw = identify_environment(
        "typescript", compiler=compiler, runtime=runtime, dependencies=(extra,)
    )
    environment = parse_environment(raw, "typescript")

    assert {pin.path for pin in environment.dependencies} == {
        str(lib_a.resolve()), str(lib_b.resolve()), str(extra.resolve())
    }
    assert set(dict(environment.variables)) <= {
        "PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG",
        "LC_ALL", "TZ", "PYTHONHASHSEED",
    }
    validate_environment(environment)


@pytest.mark.parametrize("language", ["rust", "python", "typescript"])
def test_environment_rejects_wrong_roles_unpinned_variables_and_extra_fields(
    language, tmp_path
):
    compiler = _write_file(tmp_path / "compiler.exe")
    runtime = _write_file(tmp_path / "runtime.exe")
    raw = identify_environment(
        language,
        compiler=compiler if language != "python" else None,
        runtime=runtime if language != "rust" else None,
    )
    wrong_roles = dict(raw)
    wrong_roles["tools"] = {}
    with pytest.raises(ValueError, match="environment.tools"):
        parse_environment(wrong_roles, language)

    raw["extra"] = True
    with pytest.raises(ValueError):
        parse_environment(raw, language)

    raw.pop("extra")
    raw["variables"] = {"PYTHONPATH": "unexpected"}
    with pytest.raises(ValueError, match="unsupported environment variable"):
        parse_environment(raw, language)


def test_environment_canonicalizes_windows_variable_names_and_rejects_alias_duplicates(
    tmp_path,
):
    runtime = _write_file(tmp_path / "python.exe")
    raw = identify_environment("python", runtime=runtime)
    raw["variables"] = {"path": "C:/tools", "systemroot": "C:/Windows"}

    parsed = parse_environment(raw, "python")
    assert parsed.variables == (("PATH", "C:/tools"), ("SystemRoot", "C:/Windows"))

    raw["variables"] = {"PATH": "C:/a", "path": "C:/b"}
    with pytest.raises(ValueError, match="duplicate environment variable"):
        parse_environment(raw, "python")


@pytest.mark.parametrize(
    ("language", "source", "content", "needles"),
    [
        (
            "rust", "src/step.rs", b"pub fn step(value: u32) -> u32 { value.saturating_sub(1) }\n",
            ("from_le_bytes", "let result: u32 = candidate::step", "--edition=2021"),
        ),
        (
            "python", "src/step.py", b"def step(value):\n    return max(0, value - 1)\n",
            ("-I", "-B", "spec_from_file_location", "struct.unpack('<I'", "stdout.buffer.write"),
        ),
        (
            "typescript", "src/step.ts",
            b"export function step(value: number): number { return Math.max(0, value - 1); }\n",
            ("--module", "commonjs", "--noResolve", "readUInt32LE", "commonjs"),
        ),
    ],
)
def test_prepare_program_writes_fixed_driver_and_direct_commands(
    language, source, content, needles, tmp_path
):
    binding = parse_binding(_binding(language, source))
    source_root = tmp_path / "source"
    _write_file(source_root / source, content)
    environment = _environment(language, tmp_path / "tools")
    build_root = tmp_path / "build"

    plan = prepare_program(binding, source_root, build_root, environment)

    assert plan.cwd == build_root
    assert plan.build_argv is not None
    assert Path(plan.build_argv[0]).is_absolute()
    assert Path(plan.run_argv[0]).is_absolute()
    assert plan.generated_files
    generated = "\n".join(path.read_text(encoding="utf-8") for path in plan.generated_files)
    for needle in needles:
        assert needle in generated or needle in plan.build_argv
    if language == "typescript":
        assert plan.build_argv[0] == dict(environment.tools)["compiler"].path
        assert plan.run_argv[0] == dict(environment.tools)["runtime"].path
        assert all("npm" not in arg.lower() for arg in plan.build_argv + plan.run_argv)


def test_prepare_program_rejects_wrong_extension_and_symbol(tmp_path):
    source_root = tmp_path / "source"
    _write_file(source_root / "step.txt", b"not a profile source")
    environment = _environment("rust", tmp_path / "tools")
    binding = parse_binding(_binding("rust", "step.txt"))

    with pytest.raises(ValueError, match="end in .rs"):
        prepare_program(binding, source_root, tmp_path / "build", environment)


@pytest.mark.parametrize("language", ["rust", "python", "typescript"])
def test_validate_environment_detects_changed_tool_bytes(language, tmp_path):
    environment = _environment(language, tmp_path / "tools")
    pin = environment.tools[0][1]
    Path(pin.path).write_bytes(b"changed")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        validate_environment(environment)
