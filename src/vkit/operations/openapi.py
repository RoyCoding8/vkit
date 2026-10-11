"""Compare repository-local OpenAPI contracts with pinned oasdiff."""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

OASDIFF_VERSION = "1.33.0"
MAX_TIMEOUT_SECONDS = 60
MAX_OPENAPI_FILES = 256
MAX_OPENAPI_FILE_BYTES = 2_097_152
MAX_OPENAPI_TOTAL_BYTES = 16_777_216
MAX_BACKEND_OUTPUT_BYTES = 8_388_608
_BIN_ENV = "VKIT_OASDIFF_BIN"
_SHA_ENV = "VKIT_OASDIFF_SHA256"
_MODEL_SCOPE = (
    "oasdiff 1.33.0 OpenAPI 3 breaking checks at ERR and WARN severity after "
    "oasdiff validation; this compares declared contracts, not runtime behavior."
)
_LEVELS = {1: "info", 2: "warning", 3: "error"}
_SUPPORTED_SUFFIXES = {".json", ".yaml", ".yml"}
_SUPPORTED_VERSIONS = {"3.0.0", "3.0.1", "3.0.2", "3.0.3", "3.0.4", "3.1.0", "3.1.1", "3.1.2"}
_SUPPORTED_DIALECTS = {"https://spec.openapis.org/oas/3.1/dialect/base",
                       "https://json-schema.org/draft/2020-12/schema"}


class _UnsupportedDocument(Exception):
    pass


class _ParserUnavailable(Exception):
    pass


class _DeadlineExceeded(Exception):
    pass


class _BackendOutputExceeded(Exception):
    pass


def _runtime() -> tuple[Path, str] | None:
    raw_path = os.environ.get(_BIN_ENV)
    expected = os.environ.get(_SHA_ENV, "").lower()
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


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _DeadlineExceeded


def _read_source(path: Path, deadline: float) -> bytes:
    _check_deadline(deadline)
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_OPENAPI_FILE_BYTES + 1)
    except OSError as exc:
        raise _UnsupportedDocument("an OpenAPI source file cannot be read") from exc
    _check_deadline(deadline)
    if len(content) > MAX_OPENAPI_FILE_BYTES:
        raise _UnsupportedDocument("an OpenAPI source file exceeds the size limit")
    return content


def _checked_relative_file(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or ".." in parts:
        raise _UnsupportedDocument("a local reference escapes the repository")
    path = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise _UnsupportedDocument("OpenAPI source paths cannot contain symlinks")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _UnsupportedDocument(f"a referenced OpenAPI file cannot be read: {relative}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise _UnsupportedDocument("local references must identify regular repository files")
    if resolved.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise _UnsupportedDocument("OpenAPI documents and references must use JSON or YAML files")
    return resolved


def _source_file(root: Path, path: str) -> tuple[Path, str]:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("OpenAPI source path must be a repository-relative JSON or YAML file")
    relative = Path(path)
    if relative.is_absolute() or relative.drive or relative.root \
            or any(part in {"..", ""} for part in relative.parts):
        raise ValueError("OpenAPI source path must be repository-relative")
    normalized = PurePosixPath(*relative.parts).as_posix()
    resolved = _checked_relative_file(root, normalized)
    return resolved, normalized


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _UnsupportedDocument(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _parse_document(relative: str, content: bytes, entry: bool) -> Any:
    suffix = Path(relative).suffix.lower()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _UnsupportedDocument(f"{relative} is not UTF-8 text") from exc
    if suffix == ".json":
        try:
            document = json.loads(text, object_pairs_hook=_json_object)
        except _UnsupportedDocument:
            raise
        except RecursionError as exc:
            raise _UnsupportedDocument(f"{relative} is nested too deeply") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise _UnsupportedDocument(f"{relative} is not valid JSON") from exc
    else:
        try:
            import yaml
        except ImportError as exc:
            raise _ParserUnavailable("YAML input requires the PyYAML dependency") from exc

        class UniqueSafeLoader(yaml.SafeLoader):
            def construct_mapping(self, node, deep=False):
                mapping = {}
                for key_node, value_node in node.value:
                    if key_node.tag == "tag:yaml.org,2002:merge":
                        raise _UnsupportedDocument("YAML merge keys are not supported")
                    key = self.construct_object(key_node, deep=deep)
                    try:
                        duplicate = key in mapping
                    except TypeError as exc:
                        raise yaml.constructor.ConstructorError(
                            "while constructing a mapping", node.start_mark,
                            "found an unhashable key", key_node.start_mark,
                        ) from exc
                    if duplicate:
                        raise yaml.constructor.ConstructorError(
                            "while constructing a mapping", node.start_mark,
                            f"found duplicate key {key!r}", key_node.start_mark,
                        )
                    mapping[key] = self.construct_object(value_node, deep=deep)
                return mapping

        try:
            depth = 0
            aliases = 0
            events = 0
            for event in yaml.parse(text, Loader=yaml.SafeLoader):
                events += 1
                if events > 100_000:
                    raise _UnsupportedDocument(f"{relative} contains too many YAML values")
                if isinstance(event, yaml.events.AliasEvent):
                    aliases += 1
                    if aliases > 64:
                        raise _UnsupportedDocument(f"{relative} contains too many YAML aliases")
                if isinstance(event, (yaml.events.MappingStartEvent,
                                      yaml.events.SequenceStartEvent)):
                    depth += 1
                    if depth > 128:
                        raise _UnsupportedDocument(f"{relative} is nested too deeply")
                elif isinstance(event, (yaml.events.MappingEndEvent,
                                        yaml.events.SequenceEndEvent)):
                    depth -= 1
            document = yaml.load(text, Loader=UniqueSafeLoader)
        except yaml.YAMLError as exc:
            raise _UnsupportedDocument(f"{relative} is not valid unambiguous YAML") from exc
        except RecursionError as exc:
            raise _UnsupportedDocument(f"{relative} is nested too deeply") from exc
    if entry and (not isinstance(document, dict) or not isinstance(document.get("openapi"), str)
                  or document["openapi"] not in _SUPPORTED_VERSIONS):
        raise _UnsupportedDocument(f"{relative} must declare a supported OpenAPI 3.0 or 3.1 version")
    return document


def _references(document: Any, deadline: float) -> list[str]:
    found: list[str] = []
    active: set[int] = set()
    visited: set[int] = set()
    stack: list[tuple[Any, bool]] = [(document, False)]
    while stack:
        _check_deadline(deadline)
        value, exiting = stack.pop()
        if not isinstance(value, (dict, list)):
            continue
        identity = id(value)
        if exiting:
            active.remove(identity)
            visited.add(identity)
            continue
        if identity in active:
            raise _UnsupportedDocument("recursive YAML aliases are not supported")
        if identity in visited:
            continue
        active.add(identity)
        stack.append((value, True))
        if isinstance(value, dict):
            if "$id" in value:
                raise _UnsupportedDocument("JSON Schema $id base URI changes are not supported")
            if any(key in value for key in ("$dynamicRef", "$recursiveRef", "$dynamicAnchor")):
                raise _UnsupportedDocument("JSON Schema $dynamicRef, $recursiveRef and $dynamicAnchor are not supported")
            for key in ("$schema", "jsonSchemaDialect"):
                if key in value and (not isinstance(value[key], str) or value[key] not in _SUPPORTED_DIALECTS):
                    raise _UnsupportedDocument("custom JSON Schema dialects are not supported")
            components = value.get("components")
            if isinstance(components, dict) and "pathItems" in components:
                raise _UnsupportedDocument("reusable components.pathItems are not supported by the backend")
            if "$ref" in value:
                reference = value["$ref"]
                if not isinstance(reference, str) or not reference:
                    raise _UnsupportedDocument("every $ref must be a non-empty string")
                found.append(reference)
            stack.extend((child, False) for child in value.values())
        else:
            stack.extend((child, False) for child in value)
    return found


def _reference_target(root: Path, current: str, reference: str) -> str | None:
    try:
        uri = urlsplit(reference)
    except ValueError as exc:
        raise _UnsupportedDocument("a $ref is not a valid URI reference") from exc
    if uri.scheme or uri.netloc or reference.startswith("//"):
        raise _UnsupportedDocument("remote $refs are not supported")
    if uri.query or "\\" in uri.path or re.search(r"%(?![0-9a-fA-F]{2})", uri.path):
        raise _UnsupportedDocument("a $ref must use a repository-local URI path")
    if not uri.path:
        return None
    path = unquote(uri.path)
    if "\\" in path or "\x00" in path:
        raise _UnsupportedDocument("a $ref must use a repository-local URI path")
    if path.startswith("/") or PureWindowsPath(path).drive:
        raise _UnsupportedDocument("absolute $refs are not supported")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(current), path))
    if target == "." or target == ".." or target.startswith("../"):
        raise _UnsupportedDocument("a local reference escapes the repository")
    _checked_relative_file(root, target)
    return target


def _snapshot(root: Path, entry: str, destination: Path, deadline: float,
              entry_content: bytes) -> tuple[str, int]:
    contents: dict[str, bytes] = {}
    pending = [entry]
    total_bytes = 0
    while pending:
        _check_deadline(deadline)
        relative = pending.pop()
        if relative in contents:
            continue
        source = _checked_relative_file(root, relative)
        content = entry_content if relative == entry else _read_source(source, deadline)
        _check_deadline(deadline)
        if len(content) > MAX_OPENAPI_FILE_BYTES:
            raise _UnsupportedDocument(f"OpenAPI file {relative} exceeds the size limit")
        total_bytes += len(content)
        if total_bytes > MAX_OPENAPI_TOTAL_BYTES:
            raise _UnsupportedDocument("OpenAPI reference closure exceeds the total size limit")
        if len(contents) >= MAX_OPENAPI_FILES:
            raise _UnsupportedDocument("OpenAPI reference closure contains too many files")
        document = _parse_document(relative, content, relative == entry)
        contents[relative] = content
        for reference in _references(document, deadline):
            target = _reference_target(root, relative, reference)
            if target is not None and target not in contents:
                pending.append(target)

    digest = hashlib.sha256()
    for relative, content in sorted(contents.items()):
        _check_deadline(deadline)
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return digest.hexdigest(), len(contents)


def _process_env(work: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
           if key in os.environ}
    env["HOME"] = str(work)
    env["USERPROFILE"] = str(work)
    env["XDG_CONFIG_HOME"] = str(work / "config")
    return env


def _run(binary: Path, args: list[str], cwd: Path, env: dict[str, str],
         deadline: float) -> subprocess.CompletedProcess[bytes]:
    _check_deadline(deadline)
    remaining = deadline - time.monotonic()
    command = [str(binary), *args]
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    stdout = bytearray()
    stderr = bytearray()
    exceeded = threading.Event()

    def drain(stream, destination: bytearray) -> None:
        while chunk := stream.read(65_536):
            if len(destination) + len(chunk) > MAX_BACKEND_OUTPUT_BYTES:
                exceeded.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            destination.extend(chunk)

    assert process.stdout is not None and process.stderr is not None
    readers = (threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True))
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise subprocess.TimeoutExpired(command, remaining)
    finally:
        for reader in readers:
            reader.join()
    _check_deadline(deadline)
    if exceeded.is_set():
        raise _BackendOutputExceeded
    return subprocess.CompletedProcess(command, returncode, bytes(stdout), bytes(stderr))


def _severity(level: Any) -> str:
    if type(level) is not int or level not in _LEVELS:
        raise ValueError("oasdiff returned an unknown severity")
    return _LEVELS[level]


def _source(source: Any, side: str, snapshot: Path) -> dict[str, Any] | None:
    if source is None:
        return None
    if not isinstance(source, dict) or not isinstance(source.get("file"), str):
        raise ValueError("oasdiff returned a malformed source location")
    raw_file = Path(source["file"])
    try:
        file = raw_file.resolve().relative_to(snapshot.resolve()).as_posix()
    except (OSError, ValueError):
        if raw_file.is_absolute() or ".." in raw_file.parts:
            raise ValueError("oasdiff returned a source outside the snapshot")
        file = raw_file.as_posix()
    result: dict[str, Any] = {"side": side, "file": file}
    for key in ("line", "column", "endLine", "endColumn"):
        if key in source:
            if type(source[key]) is not int or source[key] < 1:
                raise ValueError("oasdiff returned a malformed source location")
            result[key] = source[key]
    return result


def _validation_diagnostics(stdout: bytes, side: str, snapshot: Path) -> list[dict[str, Any]]:
    payload = json.loads(stdout.decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("oasdiff validation output was not a findings array")
    diagnostics = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) \
                or not item["id"] or not isinstance(item.get("text"), str):
            raise ValueError("oasdiff returned a malformed validation finding")
        diagnostic = {"rule_id": item["id"], "message": item["text"],
                      "severity": _severity(item.get("level"))}
        source = _source(item.get("source"), side, snapshot)
        if source is not None:
            diagnostic["source"] = source
        for key in ("operation", "path"):
            if key in item:
                if not isinstance(item[key], str):
                    raise ValueError("oasdiff returned malformed operation details")
                diagnostic[key] = item[key]
        diagnostics.append(diagnostic)
    return diagnostics


def _breaking_diagnostics(stdout: bytes, old_snapshot: Path,
                          new_snapshot: Path) -> list[dict[str, Any]]:
    payload = json.loads(stdout.decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("oasdiff breaking output was not a findings array")
    diagnostics = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) \
                or not item["id"] or not isinstance(item.get("text"), str):
            raise ValueError("oasdiff returned a malformed breaking finding")
        severity = _severity(item.get("level"))
        if severity not in {"error", "warning"}:
            raise ValueError("oasdiff breaking returned a non-breaking severity")
        diagnostic: dict[str, Any] = {"rule_id": item["id"], "message": item["text"],
                                      "severity": severity}
        for key in ("operation", "operationId", "path"):
            if key in item:
                if not isinstance(item[key], str):
                    raise ValueError("oasdiff returned malformed operation details")
                diagnostic[key] = item[key]
        old_source = _source(item.get("baseSource"), "old", old_snapshot)
        new_source = _source(item.get("revisionSource"), "new", new_snapshot)
        if old_source is not None:
            diagnostic["old_source"] = old_source
        if new_source is not None:
            diagnostic["new_source"] = new_source
        diagnostics.append(diagnostic)
    return diagnostics


def check_openapi_compatibility(root: Path, old_path: str, new_path: str,
                                timeout_seconds: int = 20) -> dict[str, Any]:
    """Compare two bounded repository-local OpenAPI 3 reference closures."""
    started = time.monotonic()
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_TIMEOUT_SECONDS}]")
    deadline = started + timeout_seconds
    base: dict[str, Any] = {
        "old_path": old_path,
        "new_path": new_path,
        "model_scope": _MODEL_SCOPE,
        "backend": f"oasdiff/{OASDIFF_VERSION}",
        "backend_sha256": None,
    }
    try:
        repo = root.resolve(strict=True)
        if not repo.is_dir():
            raise ValueError("repository root must be a directory")
        old_source, old_relative = _source_file(repo, old_path)
        new_source, new_relative = _source_file(repo, new_path)
        base.update({"old_path": old_relative, "new_path": new_relative})
        old_content = _read_source(old_source, deadline)
        new_content = _read_source(new_source, deadline)
        base["old_source_sha256"] = hashlib.sha256(old_content).hexdigest()
        base["new_source_sha256"] = hashlib.sha256(new_content).hexdigest()
        with tempfile.TemporaryDirectory(prefix="vkit-oasdiff-") as temporary:
            work = Path(temporary)
            old_snapshot = work / "old"
            new_snapshot = work / "new"
            runner = work / "runner"
            old_snapshot.mkdir()
            new_snapshot.mkdir()
            runner.mkdir()
            old_digest, old_count = _snapshot(repo, old_relative, old_snapshot, deadline, old_content)
            new_digest, new_count = _snapshot(repo, new_relative, new_snapshot, deadline, new_content)
            base.update({"old_snapshot_sha256": old_digest, "new_snapshot_sha256": new_digest,
                         "old_snapshot_file_count": old_count, "new_snapshot_file_count": new_count})
            runtime = _runtime()
            if runtime is None:
                return {**base, "status": "UNAVAILABLE", "diagnostics": [],
                        "detail": "the configured oasdiff 1.33.0 binary is unavailable or failed its SHA-256 check"}
            binary, backend_sha256 = runtime
            base["backend_sha256"] = backend_sha256
            env = _process_env(work)
            version = _run(binary, ["--version"], runner, env, deadline)
            if version.returncode != 0 or version.stdout.decode("utf-8", "replace").strip() \
                    != f"oasdiff version {OASDIFF_VERSION}" or version.stderr:
                return {**base, "status": "UNAVAILABLE", "diagnostics": [],
                        "detail": f"the configured executable did not identify itself as oasdiff {OASDIFF_VERSION}"}

            diagnostics: list[dict[str, Any]] = []
            config = work / "fixed-config.yaml"
            config.write_text("{}\n", encoding="utf-8", newline="\n")
            for side, snapshot, entry in (("old", old_snapshot, old_relative),
                                          ("new", new_snapshot, new_relative)):
                result = _run(binary, ["validate", "--format=json", "--fail-on=WARN",
                                       "--allow-external-refs=true", f"--config={config}",
                                       "--", str(snapshot.joinpath(*PurePosixPath(entry).parts))],
                              runner, env, deadline)
                if result.returncode == 102:
                    return {**base, "status": "UNSUPPORTED", "diagnostics": diagnostics,
                            "detail": "oasdiff could not load or validate an OpenAPI document"}
                if result.stderr:
                    return {**base, "status": "UNKNOWN", "diagnostics": diagnostics,
                            "detail": "oasdiff validation emitted unexpected error output"}
                try:
                    found = _validation_diagnostics(result.stdout, side, snapshot)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    return {**base, "status": "UNKNOWN", "diagnostics": [],
                            "detail": "oasdiff returned malformed or incomplete validation output"}
                blockers = [item for item in found if item["severity"] in {"error", "warning"}]
                if result.returncode not in (0, 1) or (result.returncode == 0 and blockers) \
                        or (result.returncode == 1 and not blockers):
                    return {**base, "status": "UNKNOWN", "diagnostics": found,
                            "detail": "oasdiff validation exit status did not match its findings"}
                diagnostics.extend(found)
                if blockers:
                    return {**base, "status": "UNSUPPORTED", "diagnostics": diagnostics,
                            "detail": "oasdiff validation found errors or warnings outside the accepted input model"}

            result = _run(binary, ["breaking", "--format=json", "--fail-on=WARN",
                                   "--allow-external-refs=true", f"--config={config}", "--",
                                   str(old_snapshot.joinpath(*PurePosixPath(old_relative).parts)),
                                   str(new_snapshot.joinpath(*PurePosixPath(new_relative).parts))],
                          runner, env, deadline)
            if result.stderr:
                return {**base, "status": "UNKNOWN", "diagnostics": diagnostics,
                        "detail": "oasdiff breaking emitted unexpected error output"}
            try:
                breaking = _breaking_diagnostics(result.stdout, old_snapshot, new_snapshot)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return {**base, "status": "UNKNOWN", "diagnostics": diagnostics,
                        "detail": "oasdiff returned malformed or incomplete breaking output"}
            expected_code = 1 if breaking else 0
            if result.returncode != expected_code:
                return {**base, "status": "UNKNOWN", "diagnostics": diagnostics + breaking,
                        "detail": "oasdiff breaking exit status did not match its findings"}
            diagnostics.extend(breaking)
            status = "BREAKING" if breaking else "COMPATIBLE"
            return {**base, "status": status, "diagnostics": diagnostics}
    except _ParserUnavailable as exc:
        return {**base, "status": "UNAVAILABLE", "diagnostics": [], "detail": str(exc)}
    except _UnsupportedDocument as exc:
        return {**base, "status": "UNSUPPORTED", "diagnostics": [], "detail": str(exc)}
    except _DeadlineExceeded:
        return {**base, "status": "UNKNOWN", "diagnostics": [],
                "detail": "the compatibility check exceeded its total time budget"}
    except subprocess.TimeoutExpired:
        return {**base, "status": "UNKNOWN", "diagnostics": [],
                "detail": "the compatibility check exceeded its total time budget"}
    except _BackendOutputExceeded:
        return {**base, "status": "UNKNOWN", "diagnostics": [],
                "detail": "oasdiff output exceeded the size limit"}
    except OSError:
        return {**base, "status": "UNAVAILABLE", "diagnostics": [],
                "detail": "the configured oasdiff executable could not be started"}
