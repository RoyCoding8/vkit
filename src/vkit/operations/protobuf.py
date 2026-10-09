"""Check local Protobuf sources with a pinned Buf breaking ruleset."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

MAX_PROTO_FILES = 500
MAX_TREE_ENTRIES = 10_000
MAX_PROTO_FILE_BYTES = 1_048_576
MAX_PROTO_TOTAL_BYTES = 16_777_216
MAX_TIMEOUT_SECONDS = 60
BUF_VERSION = "1.73.0"
BUF_CATEGORIES = frozenset({"FILE", "PACKAGE", "WIRE_JSON", "WIRE"})
_BUF_BIN_ENV = "VKIT_BUF_BIN"
_BUF_SHA_ENV = "VKIT_BUF_SHA256"
_RULE_ID = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CONFIG_TEMPLATE = "version: v2\nbreaking:\n  use:\n    - {category}\n"
_MODEL_SCOPE = (
    "Buf breaking rules in the selected category only; this does not establish "
    "runtime or application behavior compatibility."
)


def _runtime() -> tuple[Path, str] | None:
    raw_path = os.environ.get(_BUF_BIN_ENV)
    expected = os.environ.get(_BUF_SHA_ENV, "").lower()
    if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return None
    binary = Path(raw_path).expanduser().resolve()
    try:
        if not binary.is_file():
            return None
        with binary.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
    except OSError:
        return None
    return (binary, digest) if digest == expected else None


def _source_directory(root: Path, path: str) -> tuple[Path, str]:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("source path must be a repository-relative directory")
    relative = Path(path)
    if relative.is_absolute() or relative.drive or relative.root \
            or any(part in {"..", ""} for part in relative.parts):
        raise ValueError("source path must be repository-relative")
    base = root.resolve()
    target = base / relative
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("source paths cannot contain symlinks")
    resolved = target.resolve()
    if not resolved.is_relative_to(base) or not resolved.is_dir():
        raise ValueError("source path must identify a directory inside the repository")
    return resolved, relative.as_posix()


def _snapshot(source: Path, destination: Path, deadline: float) -> tuple[str, int]:
    files: list[tuple[str, Path]] = []
    pending = [source]
    entries_seen = 0
    while pending:
        current = pending.pop()
        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    entries_seen += 1
                    if entries_seen > MAX_TREE_ENTRIES:
                        raise ValueError(f"source tree has more than {MAX_TREE_ENTRIES} entries")
                    path = Path(entry.path)
                    if entry.is_symlink():
                        raise ValueError("source trees cannot contain symlinks")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        if path.suffix == ".proto":
                            raise ValueError(f"Protobuf source {path.name!r} must be a regular file")
                        continue
                    if path.suffix == ".proto":
                        files.append((path.relative_to(source).as_posix(), path))
                        if len(files) > MAX_PROTO_FILES:
                            raise ValueError(f"source tree has more than {MAX_PROTO_FILES} .proto files")
        except OSError as exc:
            raise ValueError(f"cannot scan Protobuf source directory: {exc}") from exc
    if not files:
        raise ValueError("source tree must contain at least one .proto file")

    digest = hashlib.sha256()
    total = 0
    for relative, path in sorted(files):
        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
                raise ValueError("source tree changed or escaped while being snapshotted")
            with path.open("rb") as source_file:
                content = source_file.read(MAX_PROTO_FILE_BYTES + 1)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError(f"cannot read Protobuf source {relative!r}: {exc}") from exc
        if len(content) > MAX_PROTO_FILE_BYTES:
            raise ValueError(f"Protobuf source {relative!r} exceeds {MAX_PROTO_FILE_BYTES} bytes")
        total += len(content)
        if total > MAX_PROTO_TOTAL_BYTES:
            raise ValueError(f"source tree exceeds {MAX_PROTO_TOTAL_BYTES} total Protobuf bytes")
        if time.monotonic() >= deadline:
            raise TimeoutError
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return digest.hexdigest(), len(files)


def _json_diagnostics(stdout: bytes, source_roots: tuple[Path, ...]) -> list[dict[str, Any]] | None:
    try:
        lines = [line for line in stdout.decode("utf-8").splitlines() if line.strip()]
        diagnostics = []
        for line in lines:
            item = json.loads(line)
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) \
                    or not isinstance(item.get("type"), str) or not _RULE_ID.fullmatch(item["type"]) \
                    or type(item.get("start_line")) is not int or item["start_line"] < 1 \
                    or not isinstance(item.get("message"), str):
                return None
            path = Path(item["path"])
            if path.is_absolute():
                relative_path = None
                for source_root in source_roots:
                    try:
                        relative_path = path.resolve().relative_to(source_root.resolve()).as_posix()
                        break
                    except (OSError, ValueError):
                        continue
                if relative_path is None:
                    return None
            else:
                if ".." in path.parts:
                    return None
                relative_path = path.as_posix()
            diagnostics.append({"rule_id": item["type"], "file": relative_path,
                                "line": item["start_line"], "message": item["message"]})
        return diagnostics
    except (UnicodeDecodeError, ValueError):
        return None


def check_proto_compatibility(root: Path, old_path: str, new_path: str,
                              category: str = "WIRE_JSON", timeout_seconds: int = 10) -> dict[str, Any]:
    """Compare two bounded repository-local Protobuf trees with pinned Buf."""
    started = time.monotonic()
    if not isinstance(category, str) or category not in BUF_CATEGORIES:
        raise ValueError("category must be FILE, PACKAGE, WIRE_JSON, or WIRE")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    deadline = started + timeout_seconds
    old_source, old_relative = _source_directory(root, old_path)
    new_source, new_relative = _source_directory(root, new_path)
    base: dict[str, Any] = {
        "operation": "check_proto_compatibility",
        "old_path": old_relative,
        "new_path": new_relative,
        "category": category,
        "model_scope": _MODEL_SCOPE,
        "backend": f"buf/{BUF_VERSION}",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="vkit-buf-") as temporary:
            work = Path(temporary)
            old_snapshot = work / "old"
            new_snapshot = work / "new"
            old_snapshot.mkdir()
            new_snapshot.mkdir()
            old_digest, old_count = _snapshot(old_source, old_snapshot, deadline)
            new_digest, new_count = _snapshot(new_source, new_snapshot, deadline)
            base.update({"old_proto_sha256": old_digest, "new_proto_sha256": new_digest,
                         "old_proto_file_count": old_count, "new_proto_file_count": new_count})
            runtime = _runtime()
            if runtime is None:
                return {**base, "status": "UNAVAILABLE", "backend_sha256": None,
                        "detail": "the configured Buf 1.73.0 binary is unavailable or failed its SHA-256 check"}
            binary, backend_sha256 = runtime
            base["backend_sha256"] = backend_sha256
            runner = work / "runner"
            runner.mkdir()
            config = runner / "buf.yaml"
            config.write_text(_CONFIG_TEMPLATE.format(category=category), encoding="utf-8", newline="\n")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
                   if key in os.environ}
            env["BUF_CONFIG_DIR"] = str(work / "buf-config")
            env["BUF_CACHE_DIR"] = str(work / "buf-cache")
            version = subprocess.run([str(binary), "--version"], cwd=runner, env=env,
                                     capture_output=True, timeout=remaining, check=False)
            if version.returncode != 0 or version.stdout.decode("utf-8", "replace").strip() != BUF_VERSION:
                return {**base, "status": "UNAVAILABLE",
                        "detail": "the configured executable did not identify itself as Buf 1.73.0"}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            command = [str(binary), "breaking", str(new_snapshot), "--against", str(old_snapshot),
                       "--config", str(config), "--error-format=json", "--disable-symlinks",
                       f"--timeout={max(1, int(remaining * 1000))}ms"]
            result = subprocess.run(command, cwd=runner, env=env, capture_output=True,
                                    timeout=remaining, check=False)
            if time.monotonic() >= deadline:
                raise TimeoutError
            diagnostics = _json_diagnostics(result.stdout, (new_snapshot, old_snapshot))
            if diagnostics is not None and any(item["rule_id"] == "COMPILE" for item in diagnostics):
                return {**base, "status": "UNSUPPORTED",
                        "detail": "Buf could not compile the local Protobuf sources", "diagnostics": []}
            if result.returncode == 0:
                if diagnostics is None or diagnostics:
                    return {**base, "status": "UNKNOWN",
                            "detail": "Buf returned unrecognized output with a success exit code"}
                return {**base, "status": "COMPATIBLE", "diagnostics": []}
            if diagnostics:
                return {**base, "status": "BREAKING", "diagnostics": diagnostics}
            return {**base, "status": "UNKNOWN",
                    "detail": "Buf did not return recognized machine-readable results"}
    except TimeoutError:
        return {**base, "status": "UNKNOWN", "detail": "the compatibility check exceeded its total time budget"}
    except subprocess.TimeoutExpired:
        return {**base, "status": "UNKNOWN", "detail": "the compatibility check exceeded its total time budget"}
    except OSError:
        return {**base, "status": "UNAVAILABLE", "detail": "the configured Buf executable could not be started"}
