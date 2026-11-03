from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Literal, TypeAlias

from .inputs import _validate_relative_path
from .paths import ProjectError

Language: TypeAlias = Literal["rust", "python", "typescript"]
ToolRole: TypeAlias = Literal["compiler", "runtime"]

_REQUIRED_TOOLS: dict[Language, frozenset[str]] = {
    "rust": frozenset({"compiler"}),
    "python": frozenset({"runtime"}),
    "typescript": frozenset({"compiler", "runtime"}),
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ENVIRONMENT_NAMES = {
    "path": "PATH",
    "systemroot": "SystemRoot",
    "windir": "WINDIR",
    "systemdrive": "SystemDrive",
    "temp": "TEMP",
    "tmp": "TMP",
    "tmpdir": "TMPDIR",
    "lang": "LANG",
    "lc_all": "LC_ALL",
    "tz": "TZ",
    "pythonhashseed": "PYTHONHASHSEED",
}


@dataclass(frozen=True)
class U32Binding:
    language: Language
    source: str
    symbol: str


@dataclass(frozen=True)
class FilePin:
    path: str
    sha256: str


@dataclass(frozen=True)
class ExecutionEnvironment:
    """Pinned local tools, files, and allowlisted non-secret process variables."""

    tools: tuple[tuple[str, FilePin], ...]
    dependencies: tuple[FilePin, ...]
    variables: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProgramPlan:
    build_argv: tuple[str, ...] | None
    run_argv: tuple[str, ...]
    cwd: Path
    generated_files: tuple[Path, ...]


def _object(raw: object, label: str, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
    return raw


def _string(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise ValueError(f"{label} must be a non-empty string without NUL")
    return raw


def _language(raw: object, label: str) -> Language:
    if raw == "rust":
        return "rust"
    if raw == "python":
        return "python"
    if raw == "typescript":
        return "typescript"
    raise ValueError(f"{label} must be rust, python, or typescript")


def _pin_json(pin: FilePin) -> dict[str, str]:
    return {"path": pin.path, "sha256": pin.sha256}


def _environment_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("environment variable names must be strings")
    canonical = _ENVIRONMENT_NAMES.get(raw.casefold())
    if canonical is None:
        raise ValueError(f"unsupported environment variable: {raw!r}")
    return canonical


def _parse_pin(raw: object, label: str) -> FilePin:
    obj = _object(raw, label, frozenset({"path", "sha256"}))
    path = _string(obj["path"], f"{label}.path")
    digest = _string(obj["sha256"], f"{label}.sha256")
    if not Path(path).is_absolute():
        raise ValueError(f"{label}.path must be absolute")
    if _SHA256.fullmatch(digest) is None:
        raise ValueError(f"{label}.sha256 must be 64 lowercase hex digits")
    return FilePin(path, digest)


def _binding_source(raw: object) -> str:
    source = _string(raw, "binding.source")
    try:
        _validate_relative_path(source)
    except ProjectError as exc:
        raise ValueError("binding.source must be a normalized relative path") from exc
    if PurePosixPath(source).as_posix() != source:
        raise ValueError("binding.source must use normalized POSIX separators")
    return source


def parse_binding(raw: object) -> U32Binding:
    obj = _object(
        raw,
        "binding",
        frozenset({"schema_version", "kind", "language", "source", "symbol"}),
    )
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
        raise ValueError("binding.schema_version must be 1")
    if obj["kind"] != "u32_function":
        raise ValueError("binding.kind must be 'u32_function'")
    language = _language(obj["language"], "binding.language")
    symbol = _string(obj["symbol"], "binding.symbol")
    if symbol.isascii() is False or _SYMBOL.fullmatch(symbol) is None:
        raise ValueError("binding.symbol must be an ASCII identifier")
    return U32Binding(language, _binding_source(obj["source"]), symbol)


def parse_environment(raw: object, language: Language) -> ExecutionEnvironment:
    """Parse the closed local-process declaration and canonicalize allowed variable names."""
    language = _language(language, "language")
    obj = _object(
        raw,
        "environment",
        frozenset({"schema_version", "kind", "tools", "dependencies", "variables"}),
    )
    if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
        raise ValueError("environment.schema_version must be 1")
    if obj["kind"] != "local_process":
        raise ValueError("environment.kind must be 'local_process'")

    raw_tools = obj["tools"]
    required_tools = _REQUIRED_TOOLS[language]
    if not isinstance(raw_tools, dict) or set(raw_tools) != required_tools:
        raise ValueError(f"environment.tools for {language} must contain {sorted(required_tools)}")
    tools = tuple(
        (role, _parse_pin(pin, f"environment.tools.{role}"))
        for role, pin in sorted(raw_tools.items())
    )

    raw_dependencies = obj["dependencies"]
    if not isinstance(raw_dependencies, list):
        raise ValueError("environment.dependencies must be an array")
    dependencies = tuple(
        _parse_pin(pin, f"environment.dependencies[{index}]")
        for index, pin in enumerate(raw_dependencies)
    )
    if len({pin.path for pin in dependencies}) != len(dependencies):
        raise ValueError("environment.dependencies contains duplicate paths")
    if {pin.path for _, pin in tools} & {pin.path for pin in dependencies}:
        raise ValueError("environment.dependencies repeats a tool path")

    raw_variables = obj["variables"]
    if not isinstance(raw_variables, dict):
        raise ValueError("environment.variables must be an object")
    variables: list[tuple[str, str]] = []
    names: set[str] = set()
    for name, value in raw_variables.items():
        canonical_name = _environment_name(name)
        if _VARIABLE.fullmatch(canonical_name) is None:
            raise ValueError(f"invalid environment variable name: {name!r}")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"environment variable {canonical_name!r} must be a string without NUL")
        if canonical_name in names:
            raise ValueError(f"duplicate environment variable name: {canonical_name!r}")
        names.add(canonical_name)
        variables.append((canonical_name, value))

    return ExecutionEnvironment(
        tools,
        tuple(sorted(dependencies, key=lambda pin: pin.path)),
        tuple(sorted(variables)),
    )


def _pin_file(path: Path, label: str) -> FilePin:
    if not path.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    try:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"{label} must be an absolute regular file")
        resolved = path.resolve(strict=True)
        content = resolved.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} cannot be read: {path}") from exc
    return FilePin(str(resolved), hashlib.sha256(content).hexdigest())


def identify_environment(
    language: Language,
    *,
    compiler: Path | None = None,
    runtime: Path | None = None,
    dependencies: tuple[Path, ...] = (),
) -> dict[str, object]:
    """Pin the supplied tools and files and copy present allowlisted host variables.

    This does not detect secrets or establish a transitive dependency closure. Callers
    must ensure copied variable values contain no secrets.
    """
    language = _language(language, "language")
    supplied_tools = {"compiler": compiler, "runtime": runtime}
    required_tools = _REQUIRED_TOOLS[language]
    if {role for role, path in supplied_tools.items() if path is not None} != required_tools:
        raise ValueError(f"{language} requires exactly these tools: {sorted(required_tools)}")

    tool_pins = {
        role: _pin_file(path, f"{language} {role}")
        for role, path in supplied_tools.items()
        if role in required_tools and path is not None
    }
    dependency_paths = list(dependencies)
    compiler_pin = tool_pins.get("compiler")
    if language == "typescript" and compiler_pin is not None:
        dependency_paths.extend(
            sorted(
                Path(compiler_pin.path).parent.glob("lib*.d.ts"),
                key=lambda path: str(path),
            )
        )
    pins: dict[str, FilePin] = {}
    for path in dependency_paths:
        pin = _pin_file(path, f"{language} dependency")
        previous = pins.get(pin.path)
        if previous is not None and previous != pin:
            raise ValueError(f"dependency path has conflicting pins: {pin.path}")
        pins[pin.path] = pin
    if set(pins) & {pin.path for pin in tool_pins.values()}:
        raise ValueError("environment dependencies repeat a tool path")

    variables: dict[str, str] = {}
    for name, value in os.environ.items():
        canonical_name = _ENVIRONMENT_NAMES.get(name.casefold())
        if canonical_name is None:
            continue
        previous = variables.get(canonical_name)
        if previous is not None and previous != value:
            raise ValueError(f"host has conflicting values for {canonical_name}")
        variables[canonical_name] = value
    return {
        "schema_version": 1,
        "kind": "local_process",
        "tools": {role: _pin_json(pin) for role, pin in sorted(tool_pins.items())},
        "dependencies": [_pin_json(pin) for _, pin in sorted(pins.items())],
        "variables": dict(sorted(variables.items())),
    }


def validate_environment(environment: ExecutionEnvironment) -> None:
    """Check the measured bytes of declared pins; this does not close dependencies."""
    roles = [role for role, _ in environment.tools]
    if len(set(roles)) != len(roles) or not set(roles) <= {"compiler", "runtime"}:
        raise ValueError("environment contains invalid or duplicate tool roles")
    pins = [pin for _, pin in environment.tools] + list(environment.dependencies)
    paths = [pin.path for pin in pins]
    if len(set(paths)) != len(paths):
        raise ValueError("environment contains duplicate file pins")
    variable_names: set[str] = set()
    for name, value in environment.variables:
        canonical_name = _environment_name(name)
        if name != canonical_name or canonical_name in variable_names:
            raise ValueError(f"invalid or duplicate environment variable: {name!r}")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"environment variable {name!r} must be a string without NUL")
        variable_names.add(canonical_name)
    for pin in pins:
        if _SHA256.fullmatch(pin.sha256) is None:
            raise ValueError(f"invalid SHA-256 pin for {pin.path}")
        path = Path(pin.path)
        if not path.is_absolute():
            raise ValueError(f"file pin path must be absolute: {pin.path}")
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(f"file pin is not a regular file: {pin.path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError(f"file pin cannot be read: {pin.path}") from exc
        if digest != pin.sha256:
            raise ValueError(f"file pin SHA-256 mismatch: {pin.path}")


def _source_file(source_root: Path, source: str) -> Path:
    if not source_root.is_absolute():
        raise ValueError("source_root must be absolute")
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError("source_root must be a plain directory")
    root = source_root.resolve(strict=True)
    path = root.joinpath(*source.split("/"))
    try:
        current = root
        for part in source.split("/"):
            current = current / part
            if current.is_symlink():
                raise ValueError(f"source path contains a symlink: {source}")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(f"binding source is not a regular file: {source}")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"binding source is unavailable: {source}") from exc
    if not resolved.is_relative_to(root):
        raise ValueError("binding source escapes source_root")
    return resolved


def _tool(environment: ExecutionEnvironment, role: ToolRole) -> Path:
    tools = dict(environment.tools)
    pin = tools.get(role)
    if pin is None:
        raise ValueError(f"environment is missing the {role} tool")
    return Path(pin.path)


def _write_driver(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    return path


def _rust_plan(binding: U32Binding, source: Path, build_root: Path,
               environment: ExecutionEnvironment) -> ProgramPlan:
    compiler = _tool(environment, "compiler")
    driver = build_root / "driver.rs"
    executable = build_root / ("vkit-u32.exe" if os.name == "nt" else "vkit-u32")
    source_literal = json.dumps(source.as_posix(), ensure_ascii=False)
    content = (
        f"#[path = {source_literal}] mod candidate;\n"
        "use std::io::Read;\n"
        "fn main() {\n"
        "    let mut bytes = [0u8; 4];\n"
        "    std::io::stdin().read_exact(&mut bytes).expect(\"expected four input bytes\");\n"
        "    let value = u32::from_le_bytes(bytes);\n"
        f"    let result: u32 = candidate::{binding.symbol}(value);\n"
        "    println!(\"{result}\");\n"
        "}\n"
    )
    _write_driver(driver, content)
    return ProgramPlan(
        (str(compiler), "--edition=2021", str(driver), "-o", str(executable)),
        (str(executable),),
        build_root,
        (driver,),
    )


def _python_plan(binding: U32Binding, source: Path, build_root: Path,
                 environment: ExecutionEnvironment) -> ProgramPlan:
    runtime = _tool(environment, "runtime")
    compile_driver = build_root / "compile_candidate.py"
    run_driver = build_root / "run_candidate.py"
    compile_source = (
        "from pathlib import Path\n"
        f"source = Path({str(source)!r})\n"
        "compile(source.read_bytes(), str(source), 'exec', dont_inherit=True)\n"
    )
    run_source = (
        "import importlib.util\n"
        "import struct\n"
        "import sys\n"
        "from pathlib import Path\n"
        "raw = sys.stdin.buffer.read()\n"
        "if len(raw) != 4:\n"
        "    raise ValueError('expected four input bytes')\n"
        "value = struct.unpack('<I', raw)[0]\n"
        f"source = Path({str(source)!r})\n"
        f"spec = importlib.util.spec_from_file_location('vkit_candidate', source)\n"
        "if spec is None or spec.loader is None:\n"
        "    raise ImportError('could not load captured module')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module\n"
        "spec.loader.exec_module(module)\n"
        f"function = getattr(module, {binding.symbol!r})\n"
        "result = function(value)\n"
        "if type(result) is not int or not 0 <= result <= 0xffffffff:\n"
        "    raise TypeError('function must return a u32 integer')\n"
        "sys.stdout.buffer.write(f'{result}\\n'.encode('ascii'))\n"
    )
    _write_driver(compile_driver, compile_source)
    _write_driver(run_driver, run_source)
    return ProgramPlan(
        (str(runtime), "-I", "-B", str(compile_driver)),
        (str(runtime), "-I", "-B", str(run_driver)),
        build_root,
        (compile_driver, run_driver),
    )


def _typescript_plan(binding: U32Binding, source: Path, source_root: Path,
                     build_root: Path, environment: ExecutionEnvironment) -> ProgramPlan:
    compiler = _tool(environment, "compiler")
    runtime = _tool(environment, "runtime")
    relative_js = source.relative_to(source_root).with_suffix(".js")
    output_root = build_root / "js"
    if output_root.is_symlink():
        raise ValueError("TypeScript output directory must not be a symlink")
    output_root.mkdir(parents=True, exist_ok=True)
    compiled_module = output_root / relative_js
    metadata = output_root / "package.json"
    driver = build_root / "driver.cjs"
    module_literal = json.dumps(str(compiled_module), ensure_ascii=False)
    content = (
        "'use strict';\n"
        "const fs = require('node:fs');\n"
        "const input = fs.readFileSync(0);\n"
        "if (input.length !== 4) throw new Error('expected four input bytes');\n"
        "const value = input.readUInt32LE(0);\n"
        f"const moduleExports = require({module_literal});\n"
        f"const result = moduleExports[{json.dumps(binding.symbol)}](value);\n"
        "if (!Number.isInteger(result) || result < 0 || result > 0xffffffff) {\n"
        "  throw new TypeError('function must return a u32 integer');\n"
        "}\n"
        "process.stdout.write(`${result}\\n`);\n"
    )
    _write_driver(driver, content)
    _write_driver(metadata, '{"type":"commonjs"}\n')
    return ProgramPlan(
        (
            str(compiler),
            "--module", "commonjs",
            "--target", "es2022",
            "--rootDir", str(source_root),
            "--outDir", str(output_root),
            "--pretty", "false",
            "--skipLibCheck",
            "--noEmitOnError",
            "--noResolve",
            str(source),
        ),
        (str(runtime), str(driver)),
        build_root,
        (driver, metadata),
    )


def prepare_program(
    binding: U32Binding,
    source_root: Path,
    build_root: Path,
    environment: ExecutionEnvironment,
) -> ProgramPlan:
    if not build_root.is_absolute():
        raise ValueError("build_root must be absolute")
    if build_root.is_symlink():
        raise ValueError("build_root must not be a symlink")
    build_root.mkdir(parents=True, exist_ok=True)
    if not build_root.is_dir():
        raise ValueError("build_root must be a directory")
    source = _source_file(source_root, binding.source)
    expected_roles = _REQUIRED_TOOLS[binding.language]
    if {role for role, _ in environment.tools} != expected_roles:
        raise ValueError(f"{binding.language} environment needs {sorted(expected_roles)} tools")
    if binding.language == "rust":
        if source.suffix != ".rs":
            raise ValueError("Rust binding source must end in .rs")
        return _rust_plan(binding, source, build_root, environment)
    if binding.language == "python":
        if source.suffix != ".py":
            raise ValueError("Python binding source must end in .py")
        return _python_plan(binding, source, build_root, environment)
    if source.suffix != ".ts":
        raise ValueError("TypeScript binding source must end in .ts")
    return _typescript_plan(binding, source, source_root.resolve(strict=True), build_root, environment)
