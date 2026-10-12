"""Preview a compiler-checked TypeScript symbol rename in a closed project snapshot."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..proc import run
from ._runtime import pinned_binary

TYPESCRIPT_VERSION = "5.9.3"
NODE_VERSION = "v24.16.0"
PROTOCOL_VERSION = 1
MAX_FILES = 128
MAX_FILE_BYTES = 262_144
MAX_TREE_ENTRIES = 10_000
MAX_TOTAL_BYTES = 2_097_152
MAX_REQUEST_BYTES = 4_194_304
MAX_OUTPUT_BYTES = 1_048_576
MAX_TIMEOUT_SECONDS = 60
_HELPER_ENV = "VKIT_TYPESCRIPT_RENAME_HELPER"
_HELPER_SHA_ENV = "VKIT_TYPESCRIPT_RENAME_HELPER_SHA256"
_LOCK_ENV = "VKIT_TYPESCRIPT_LOCKFILE"
_LOCK_SHA_ENV = "VKIT_TYPESCRIPT_LOCK_SHA256"
_NODE_ENV = "VKIT_NODE_BIN"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_SKIP_DIRECTORIES = frozenset({".git", ".hg", "node_modules", ".venv", "dist", "build", "coverage"})
_MODEL_SCOPE = (
    "TypeScript 5.9.3 binding rename in the bounded captured TypeScript project under fixed strict compiler options. "
    "The checker validates captured symbol bindings and compile diagnostics, not reflection strings, consumers outside "
    "the snapshot, or runtime behavior."
)


class _Unsupported(ValueError):
    pass


@dataclass(frozen=True)
class _SourceFile:
    path: str
    text: str
    data: bytes


@dataclass(frozen=True)
class _Edit:
    path: str
    start: int
    length: int
    new_text: str


@dataclass(frozen=True)
class _Snapshot:
    target: str
    old_name: str
    new_name: str
    files: tuple[_SourceFile, ...]
    digest: str


def _relative(value: Any, label: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or "\x00" in value or "\\" in value
            or value.startswith("/")):
        raise ValueError(f"{label} must be a repository-relative TypeScript path")
    path = PurePosixPath(value)
    if (path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix not in {".ts", ".tsx"}):
        raise ValueError(f"{label} must identify a repository-relative .ts or .tsx file")
    if path.name.endswith(".d.ts"):
        raise ValueError(f"{label} must identify source, not a declaration file")
    return path


def _reparse(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & _REPARSE_POINT)
    except AttributeError:
        return False


def _capture(root: Path, target_value: Any, old_name: Any, new_name: Any,
             deadline: float) -> _Snapshot:
    target = _relative(target_value, "path")
    if not isinstance(old_name, str) or not _NAME.fullmatch(old_name):
        raise ValueError("old_name must be an ASCII TypeScript identifier")
    if not isinstance(new_name, str) or not _NAME.fullmatch(new_name):
        raise ValueError("new_name must be an ASCII TypeScript identifier")
    if old_name == new_name:
        raise ValueError("old_name and new_name must differ")

    base = root.resolve(strict=True)
    target_path = base.joinpath(*target.parts)
    current = base
    for part in target.parts:
        current /= part
        if current.is_symlink() or _reparse(current):
            raise _Unsupported("TypeScript source paths cannot contain symlinks or reparse points")
    if not target_path.is_file():
        raise _Unsupported("target source file is not a regular file")

    pending = [base]
    entries = 0
    paths: list[tuple[str, Path]] = []
    directories: list[tuple[Path, tuple[int, int, int]]] = []
    while pending:
        if time.monotonic() >= deadline:
            raise TimeoutError
        directory = pending.pop()
        if directory.is_symlink() or _reparse(directory):
            raise _Unsupported("captured source trees cannot contain symlinked directories")
        try:
            directory_before = directory.stat()
            with os.scandir(directory) as children:
                for child in children:
                    entries += 1
                    if entries > MAX_TREE_ENTRIES:
                        raise _Unsupported("project tree exceeds the entry limit")
                    if child.name.casefold() in _SKIP_DIRECTORIES and child.is_dir(follow_symlinks=False):
                        continue
                    candidate = Path(child.path)
                    if child.is_dir(follow_symlinks=False):
                        pending.append(candidate)
                    elif child.name.endswith((".ts", ".tsx")):
                        if child.is_symlink() or _reparse(candidate) or not child.is_file(follow_symlinks=False):
                            raise _Unsupported("captured TypeScript sources must be regular files")
                        paths.append((candidate.relative_to(base).as_posix(), candidate))
            directory_after = directory.stat()
        except OSError as exc:
            raise _Unsupported("project source tree cannot be enumerated") from exc
        before_identity = (directory_before.st_ino, directory_before.st_mtime_ns, directory_before.st_size)
        after_identity = (directory_after.st_ino, directory_after.st_mtime_ns, directory_after.st_size)
        if before_identity != after_identity:
            raise _Unsupported("project source tree changed during enumeration")
        directories.append((directory, after_identity))

    paths.sort(key=lambda item: item[0])
    if len(paths) > MAX_FILES:
        raise _Unsupported(f"project contains more than {MAX_FILES} TypeScript source files")
    if target.as_posix() not in {name for name, _ in paths}:
        raise _Unsupported("target source is not in the captured TypeScript project")

    digest = hashlib.sha256()
    files: list[_SourceFile] = []
    total = 0
    for name, path in paths:
        if time.monotonic() >= deadline:
            raise TimeoutError
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise _Unsupported("TypeScript source paths cannot contain control characters")
        try:
            before = path.lstat()
            if path.is_symlink() or _reparse(path) or not path.is_file():
                raise _Unsupported("captured TypeScript sources must be regular files")
            with path.open("rb") as source:
                data = source.read(MAX_FILE_BYTES + 1)
            after = path.lstat()
        except OSError as exc:
            raise _Unsupported("a TypeScript source file cannot be read") from exc
        if len(data) > MAX_FILE_BYTES:
            raise _Unsupported(f"source file {name!r} exceeds {MAX_FILE_BYTES} bytes")
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise _Unsupported("a TypeScript source changed while the project was captured")
        if b"\x00" in data:
            raise _Unsupported(f"source file {name!r} contains NUL bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _Unsupported(f"source file {name!r} is not UTF-8") from exc
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise _Unsupported("captured TypeScript project exceeds the byte limit")
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        files.append(_SourceFile(name, text, data))
    try:
        for directory, identity in directories:
            current = directory.stat()
            if identity != (current.st_ino, current.st_mtime_ns, current.st_size):
                raise _Unsupported("project source tree changed during capture")
    except OSError as exc:
        raise _Unsupported("a captured source directory changed during capture") from exc
    return _Snapshot(target.as_posix(), old_name, new_name, tuple(files), digest.hexdigest())


def _runtime() -> tuple[Path, str, Path, str, Path] | None:
    helper = pinned_binary(_HELPER_ENV, _HELPER_SHA_ENV)
    lockfile = pinned_binary(_LOCK_ENV, _LOCK_SHA_ENV)
    raw_node = os.environ.get(_NODE_ENV)
    if helper is None or lockfile is None or not raw_node:
        return None
    try:
        node = Path(raw_node).expanduser().resolve(strict=True)
        if not node.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return helper[0], helper[1], lockfile[0], lockfile[1], node


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("TypeScript helper response contains a duplicate field")
        result[key] = value
    return result


def _validated_edits(raw: Any, snapshot: _Snapshot) -> tuple[_Edit, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > 20_000:
        raise ValueError("TypeScript helper returned an invalid edit list")
    known = {item.path: item.data for item in snapshot.files}
    edits: list[_Edit] = []
    for item in raw:
        if (not isinstance(item, dict) or set(item) != {"path", "start_byte", "length_byte", "new_text"}
                or not isinstance(item["path"], str) or item["path"] not in known
                or type(item["start_byte"]) is not int or type(item["length_byte"]) is not int
                or not isinstance(item["new_text"], str)):
            raise ValueError("TypeScript helper returned a malformed edit")
        start, length = item["start_byte"], item["length_byte"]
        old = known[item["path"]]
        if start < 0 or length <= 0 or start + length > len(old):
            raise ValueError("TypeScript helper returned an out-of-range edit")
        if old[start:start + length].decode("utf-8") not in {snapshot.old_name}:
            raise ValueError("TypeScript helper edit does not cover the selected symbol spelling")
        edits.append(_Edit(item["path"], start, length, item["new_text"]))
    edits.sort(key=lambda item: (item.path, item.start, item.length))
    for before, after in zip(edits, edits[1:]):
        if before.path == after.path and before.start + before.length > after.start:
            raise ValueError("TypeScript helper returned overlapping edits")
    return tuple(edits)


def _apply_edits(snapshot: _Snapshot, edits: tuple[_Edit, ...]) -> dict[str, str]:
    contents = {item.path: item.data for item in snapshot.files}
    by_path: dict[str, list[_Edit]] = {}
    for edit in edits:
        by_path.setdefault(edit.path, []).append(edit)
    updated: dict[str, str] = {}
    for name, changes in by_path.items():
        data = contents[name]
        for edit in reversed(changes):
            data = data[:edit.start] + edit.new_text.encode("utf-8") + data[edit.start + edit.length:]
        updated[name] = data.decode("utf-8")
    return updated


def _patch(snapshot: _Snapshot, updated: dict[str, str]) -> str:
    before = {item.path: item.text for item in snapshot.files}
    blocks: list[str] = []
    for name in sorted(updated):
        old_lines = before[name].splitlines(keepends=True)
        new_lines = updated[name].splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{name}", tofile=f"b/{name}")
        for line in diff:
            if line.startswith((" ", "-", "+")) and not line.endswith("\n"):
                blocks.append(f"{line}\n\\ No newline at end of file\n")
            else:
                blocks.append(line)
    return "".join(blocks)


def preview_typescript_symbol_rename(root: Path, path: str, old_name: str, new_name: str,
                                     timeout_seconds: int = 20) -> dict[str, Any]:
    """Return a verified rename patch without writing to the candidate project."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    result: dict[str, Any] = {"operation": "preview_typescript_symbol_rename", "status": "UNKNOWN",
                              "path": path, "old_name": old_name, "new_name": new_name,
                              "backend": f"typescript/{TYPESCRIPT_VERSION}", "model_scope": _MODEL_SCOPE}
    deadline = time.monotonic() + timeout_seconds
    try:
        snapshot = _capture(Path(root), path, old_name, new_name, deadline)
    except TimeoutError:
        return {**result, "detail": "source capture exceeded the operation deadline"}
    except (ValueError, OSError) as exc:
        return {**result, "status": "UNSUPPORTED", "detail": str(exc)}
    result["source_identity"] = {"sha256": snapshot.digest, "file_count": len(snapshot.files)}

    runtime = _runtime()
    if runtime is None:
        return {**result, "status": "UNAVAILABLE",
                "detail": "trusted rename helper, TypeScript lockfile, or configured Node executable is unavailable or failed its SHA-256 check"}
    helper, helper_digest, lockfile, lock_digest, node = runtime
    result["backend_identity"] = {"typescript": TYPESCRIPT_VERSION, "node": NODE_VERSION,
                                  "helper_sha256": helper_digest, "package_lock_sha256": lock_digest}

    files = [{"path": item.path, "text": item.text} for item in snapshot.files]
    request = json.dumps({"protocol_version": PROTOCOL_VERSION, "typescript_version": TYPESCRIPT_VERSION,
                          "target": snapshot.target, "old_name": snapshot.old_name,
                          "new_name": snapshot.new_name, "files": files}, ensure_ascii=True,
                         separators=(",", ":")).encode("utf-8")
    if len(request) > MAX_REQUEST_BYTES:
        return {**result, "status": "UNSUPPORTED", "detail": "captured TypeScript project exceeds the backend request limit"}

    with tempfile.TemporaryDirectory(prefix="vkit-ts-rename-") as temporary:
        workspace = Path(temporary)
        request_path = workspace / "request.json"
        stdout_path = workspace / "stdout.json"
        stderr_path = workspace / "stderr.log"
        request_path.write_bytes(request)
        env = {"PATH": os.environ.get("PATH", ""), "SystemRoot": os.environ.get("SystemRoot", ""),
               "TEMP": str(workspace), "TMP": str(workspace), "NODE_OPTIONS": "--disable-proto=throw"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**result, "detail": "source capture exceeded the operation deadline"}
        version = run([str(node), "--version"], cwd=workspace, stdout_path=stdout_path, stderr_path=stderr_path,
                      timeout_seconds=min(5.0, remaining), env=env,
                      should_cancel=lambda: _file_too_large(stdout_path, 128) or _file_too_large(stderr_path, 1_024))
        if version.launch_error:
            return {**result, "status": "UNAVAILABLE", "detail": "configured Node executable could not be started"}
        if version.timed_out or version.cancelled:
            return {**result, "detail": "Node identity check exceeded its bound"}
        if version.code != 0 or stdout_path.read_bytes().decode("utf-8", "replace").strip() != NODE_VERSION:
            return {**result, "status": "UNAVAILABLE", "detail": f"configured Node executable must be {NODE_VERSION}"}

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**result, "detail": "TypeScript rename preview exceeded the operation deadline"}
        completed = run([str(node), str(helper), str(request_path)], cwd=workspace,
                        stdout_path=stdout_path, stderr_path=stderr_path, timeout_seconds=remaining,
                        env=env, should_cancel=lambda: _file_too_large(stdout_path, MAX_OUTPUT_BYTES)
                        or _file_too_large(stderr_path, MAX_OUTPUT_BYTES))
        if completed.launch_error:
            return {**result, "status": "UNAVAILABLE", "detail": "pinned TypeScript rename helper could not be started"}
        if completed.timed_out or completed.cancelled or time.monotonic() >= deadline:
            return {**result, "detail": "TypeScript rename preview exceeded its operation deadline or output bound"}
        try:
            output = stdout_path.read_bytes()
            if completed.code != 0 or len(output) > MAX_OUTPUT_BYTES:
                raise ValueError("TypeScript helper failed or exceeded the output limit")
            raw = json.loads(output.decode("utf-8"), object_pairs_hook=_object)
            if (not isinstance(raw, dict) or raw.get("protocol_version") != PROTOCOL_VERSION
                    or raw.get("typescript_version") != TYPESCRIPT_VERSION
                    or raw.get("status") not in {"PREVIEW", "UNSUPPORTED", "UNKNOWN"}
                    or not isinstance(raw.get("reason", ""), str) or len(raw.get("reason", "")) > 500):
                raise ValueError("TypeScript helper identity or status is invalid")
            if raw["status"] != "PREVIEW":
                return {**result, "status": raw["status"], "detail": raw.get("reason", "rename was not established"),
                        "diagnostics_before": raw.get("diagnostics_before", []),
                        "diagnostics_after": raw.get("diagnostics_after", [])}
            edits = _validated_edits(raw.get("edits"), snapshot)
            updated = _apply_edits(snapshot, edits)
            if raw.get("postconditions") != {"diagnostics_clear": True, "binding_graph_preserved": True,
                                             "rename_edits_complete": True}:
                raise ValueError("TypeScript helper did not establish every rename postcondition")
            counts = raw.get("binding_counts")
            if (not isinstance(counts, dict) or set(counts) != {"renamed_sites", "preserved_alias_sites"}
                    or type(counts["renamed_sites"]) is not int or not 1 <= counts["renamed_sites"] <= 20_000
                    or type(counts["preserved_alias_sites"]) is not int
                    or not 0 <= counts["preserved_alias_sites"] <= 20_000):
                raise ValueError("TypeScript helper returned malformed binding counts")
            diff = _patch(snapshot, updated)
            if not diff or len(diff.encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise ValueError("rename patch is empty or exceeds its output bound")
            preview = {**result, "status": "PREVIEW", "patch": diff, "edits": len(edits),
                       "changed_files": sorted(updated), "binding_counts": counts,
                       "matched_sites": [{"path": edit.path, "start_byte": edit.start,
                                          "length_byte": edit.length, "replacement": edit.new_text}
                                         for edit in edits],
                       "postconditions": raw["postconditions"], "diagnostics_before": [],
                       "diagnostics_after": []}
            if len(json.dumps(preview, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) > MAX_OUTPUT_BYTES:
                return {**result, "status": "UNKNOWN", "detail": "rename preview evidence exceeds the response limit"}
            return preview
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
            return {**result, "status": "UNKNOWN",
                    "detail": f"TypeScript helper returned malformed or inconsistent evidence: {type(exc).__name__}: {str(exc)[:250]}"}


def _file_too_large(path: Path, maximum: int) -> bool:
    try:
        return path.stat().st_size > maximum
    except FileNotFoundError:
        return False
