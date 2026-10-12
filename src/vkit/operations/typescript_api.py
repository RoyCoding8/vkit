"""Compare captured TypeScript declaration APIs with the pinned compiler."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from ._runtime import pinned_node_helper

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
_HELPER_ENV = "VKIT_TYPESCRIPT_HELPER"
_HELPER_SHA_ENV = "VKIT_TYPESCRIPT_HELPER_SHA256"
_LOCK_ENV = "VKIT_TYPESCRIPT_LOCKFILE"
_LOCK_SHA_ENV = "VKIT_TYPESCRIPT_LOCK_SHA256"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SKIP_DIRECTORIES = {".git", ".hg", "node_modules", ".venv", "dist", "build", "coverage"}
_MODEL_SCOPE = (
    "TypeScript 5.9.3 declaration API compatibility for the closed captured .d.ts import closure. "
    "Each old exported type and value is checked with the pinned compiler's strict assignability relation; "
    "this does not prove runtime compatibility or universal source compatibility in the presence of TypeScript any/unsoundness."
)


class _UnsupportedDeclarations(ValueError):
    pass


def _relative(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"{label} must be a repository-relative POSIX .d.ts path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.suffix != ".ts":
        raise ValueError(f"{label} must identify a repository-relative declaration file")
    if not value.endswith(".d.ts"):
        raise ValueError(f"{label} must end in .d.ts")
    return path


def _reparse(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & _REPARSE_POINT)
    except AttributeError:
        return False


def _capture(root: Path, value: str, deadline: float) -> tuple[dict[str, Any], str, int]:
    relative = _relative(value, "declaration path")
    base = root.resolve()
    target = base.joinpath(*relative.parts)
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or _reparse(current):
            raise _UnsupportedDeclarations("declaration paths cannot contain symlinks or reparse points")
    try:
        entry = target.resolve(strict=True)
    except OSError as exc:
        raise _UnsupportedDeclarations("declaration entry cannot be read") from exc
    if not entry.is_relative_to(base) or not entry.is_file() or not entry.name.endswith(".d.ts"):
        raise _UnsupportedDeclarations("entry must be a regular .d.ts file inside the repository")
    capture_root = entry.parent
    files: list[tuple[str, Path]] = []
    pending = [capture_root]
    entries = 0
    while pending:
        if time.monotonic() >= deadline:
            raise TimeoutError
        directory = pending.pop()
        if directory.is_symlink() or _reparse(directory):
            raise _UnsupportedDeclarations("declaration trees cannot contain symlinks or reparse points")
        try:
            with os.scandir(directory) as children:
                for child in children:
                    entries += 1
                    if entries > MAX_TREE_ENTRIES:
                        raise _UnsupportedDeclarations("declaration directory exceeds the tree entry limit")
                    candidate = Path(child.path)
                    if child.is_symlink() or _reparse(candidate):
                        raise _UnsupportedDeclarations("declaration trees cannot contain symlinks or reparse points")
                    if child.is_dir(follow_symlinks=False):
                        if child.name.casefold() not in _SKIP_DIRECTORIES:
                            pending.append(candidate)
                    elif child.is_file(follow_symlinks=False) and child.name.endswith(".d.ts"):
                        files.append((candidate.relative_to(capture_root).as_posix(), candidate))
        except OSError as exc:
            raise _UnsupportedDeclarations("a declaration directory cannot be enumerated") from exc
    files.sort(key=lambda item: item[0])
    if len(files) > MAX_FILES:
        raise _UnsupportedDeclarations(f"declaration tree has more than {MAX_FILES} files")
    digest = hashlib.sha256()
    contents: list[dict[str, str]] = []
    total = 0
    for name, path in files:
        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            before = path.lstat()
            if path.is_symlink() or _reparse(path) or not path.is_file():
                raise _UnsupportedDeclarations("declaration files must be regular files")
            with path.open("rb") as source:
                data = source.read(MAX_FILE_BYTES + 1)
            after = path.lstat()
        except OSError as exc:
            raise _UnsupportedDeclarations("a declaration file cannot be read") from exc
        if len(data) > MAX_FILE_BYTES:
            raise _UnsupportedDeclarations(f"declaration file {name!r} exceeds {MAX_FILE_BYTES} bytes")
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise _UnsupportedDeclarations("a declaration changed while it was being captured")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _UnsupportedDeclarations(f"declaration file {name!r} is not UTF-8") from exc
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise _UnsupportedDeclarations("captured declaration tree exceeds the byte limit")
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        contents.append({"path": name, "text": text})
    entry_name = entry.relative_to(capture_root).as_posix()
    if entry_name not in {item["path"] for item in contents}:
        raise _UnsupportedDeclarations("captured declaration tree does not contain its entry")
    return {"entry": entry_name, "files": contents}, digest.hexdigest(), len(files)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("helper response contains a duplicate JSON field")
        result[key] = value
    return result


def _backend_result(raw: Any, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("protocol_version") != PROTOCOL_VERSION \
            or raw.get("typescript_version") != TYPESCRIPT_VERSION:
        raise ValueError("compiler helper identity is invalid")
    if raw.get("status") not in {"COMPATIBLE", "BREAKING", "UNSUPPORTED", "UNKNOWN"}:
        raise ValueError("compiler helper returned an invalid status")
    closure = raw.get("closure")
    if closure is not None:
        if not isinstance(closure, dict) or set(closure) != {"old", "new"}:
            raise ValueError("compiler helper returned an invalid closure")
        for key, capture in (("old", old), ("new", new)):
            names = closure[key]
            allowed = {item["path"] for item in capture["files"]}
            if not isinstance(names, list) or names != sorted(set(names)) or not set(names) <= allowed:
                raise ValueError("compiler helper closure escaped the captured declarations")
    exports = raw.get("exports", [])
    diagnostics = raw.get("diagnostics", [])
    detail = raw.get("reason", "")
    if not isinstance(exports, list) or len(exports) > 10_000 or not isinstance(diagnostics, list) or len(diagnostics) > 100:
        raise ValueError("compiler helper evidence exceeds limits")
    if not isinstance(detail, str) or len(detail) > 500:
        raise ValueError("compiler helper reason is malformed")
    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
           or type(item.get("compatible")) is not bool or not isinstance(item.get("reason"), str)
           for item in exports):
        raise ValueError("compiler helper export evidence is malformed")
    if len({item["name"] for item in exports}) != len(exports):
        raise ValueError("compiler helper returned duplicate export evidence")
    if any(not isinstance(item, dict) or type(item.get("code")) is not int
           or not isinstance(item.get("message"), str) for item in diagnostics):
        raise ValueError("compiler helper diagnostics are malformed")
    if raw["status"] == "COMPATIBLE" and (closure is None or diagnostics
                                             or any(not item["compatible"] for item in exports)):
        raise ValueError("compiler helper compatibility status contradicts its evidence")
    if raw["status"] == "BREAKING" and (closure is None or
                                          (not diagnostics and all(item["compatible"] for item in exports))):
        raise ValueError("compiler helper breaking status lacks a failing obligation")
    return {"status": raw["status"], "exports": exports, "diagnostics": diagnostics,
            "detail": detail, "declaration_closure": closure}


def check_typescript_api_compatibility(root: Path, old_path: str, new_path: str,
                                       timeout_seconds: int = 20) -> dict[str, Any]:
    """Check old exported .d.ts types/values against a new declaration closure."""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    base: dict[str, Any] = {"operation": "compare_contracts", "profile": "typescript-api",
                            "status": "UNKNOWN", "old_path": old_path, "new_path": new_path,
                            "backend": f"typescript/{TYPESCRIPT_VERSION}", "model_scope": _MODEL_SCOPE}
    started = time.monotonic()
    deadline = started + timeout_seconds
    try:
        old, old_digest, old_count = _capture(Path(root), old_path, deadline)
        new, new_digest, new_count = _capture(Path(root), new_path, deadline)
    except TimeoutError:
        return {**base, "status": "UNKNOWN", "detail": "declaration capture exceeded the deadline"}
    except (ValueError, OSError) as exc:
        return {**base, "status": "UNSUPPORTED", "detail": str(exc)}
    base["source_identity"] = {"old_sha256": old_digest, "new_sha256": new_digest,
                               "old_captured_file_count": old_count, "new_captured_file_count": new_count}
    runtime = pinned_node_helper(_HELPER_ENV, _HELPER_SHA_ENV, _LOCK_ENV, _LOCK_SHA_ENV)
    if runtime is None:
        return {**base, "status": "UNAVAILABLE", "detail": "trusted TypeScript helper, lockfile, or Node path is unavailable or failed its SHA-256 check"}
    helper, helper_digest, lockfile, lock_digest, node = runtime
    base["backend_identity"] = {"typescript": TYPESCRIPT_VERSION, "node": NODE_VERSION,
                                "helper_sha256": helper_digest, "package_lock_sha256": lock_digest}
    with tempfile.TemporaryDirectory(prefix="vkit-typescript-api-") as temporary:
        workspace = Path(temporary)
        env = {"PATH": os.environ.get("PATH", ""), "SystemRoot": os.environ.get("SystemRoot", ""),
               "TEMP": str(workspace), "TMP": str(workspace), "NODE_OPTIONS": "--disable-proto=throw"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**base, "status": "UNKNOWN", "detail": "declaration capture exceeded the deadline"}
        try:
            version = subprocess.run([str(node), "--version"], cwd=workspace, env=env, capture_output=True,
                                     timeout=remaining, check=False,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired):
            return {**base, "status": "UNAVAILABLE", "detail": "the configured Node executable could not be started"}
        if version.returncode != 0 or version.stdout.decode("utf-8", "replace").strip() != NODE_VERSION:
            return {**base, "status": "UNAVAILABLE", "detail": f"the configured Node executable must be {NODE_VERSION}"}
        request = json.dumps({"protocol_version": PROTOCOL_VERSION, "typescript_version": TYPESCRIPT_VERSION,
                              "old": old, "new": new}, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(request) > MAX_REQUEST_BYTES:
            return {**base, "status": "UNSUPPORTED", "detail": "captured declaration request exceeds the backend limit"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**base, "status": "UNKNOWN", "detail": "declaration capture exceeded the deadline"}
        try:
            completed = subprocess.run([str(node), str(helper)], input=request, cwd=workspace, env=env,
                                       capture_output=True, timeout=remaining, check=False,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            return {**base, "status": "UNKNOWN", "detail": "TypeScript API check exceeded the deadline"}
        except OSError:
            return {**base, "status": "UNAVAILABLE", "detail": "the pinned TypeScript helper could not be started"}
        if time.monotonic() >= deadline:
            return {**base, "status": "UNKNOWN", "detail": "TypeScript API check exceeded the deadline"}
        if completed.returncode != 0 or len(completed.stdout) > MAX_OUTPUT_BYTES:
            return {**base, "status": "UNKNOWN", "detail": "TypeScript helper failed or exceeded the output limit"}
        try:
            raw = json.loads(completed.stdout.decode("utf-8"), object_pairs_hook=_object)
            parsed = _backend_result(raw, old, new)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return {**base, "status": "UNKNOWN", "detail": "TypeScript helper returned malformed or unbounded evidence"}
    return {**base, **parsed}
