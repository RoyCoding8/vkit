"""Interprocedural source-to-sink paths from a pinned Joern C frontend."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from ._data import encode_json
from ..proc import run as run_process

BACKEND = "joern"
BACKEND_VERSION = "4.0.653"
LINUX_X86_64_ARCHIVE_SHA256 = "6cf996f2787381cd789322df52d134dc0b189a2f072f208bb4674a7f09edfb73"
MAX_FILES = 200
MAX_TREE_ENTRIES = 5_000
MAX_FILE_BYTES = 256_000
MAX_TOTAL_BYTES = 4_000_000
MAX_OUTPUT_BYTES = 256 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_PATHS = 2
MAX_TIMEOUT_MS = 60_000
MAX_METHOD_NAME = 128
MAX_ARGUMENT_INDEX = 128
MAX_CPG_BYTES = 2_000_000_000
MAX_CAPTURE_SECONDS = 10
MAX_OUTPUT_LINE = 64_000
_HOME_ENV = "VKIT_JOERN_HOME"
_PREPROCESSOR_DIRECTIVE = re.compile(rb"#|\?\?=|%:")
_INCOMPLETE_FRONTEND = re.compile(r"(?i)(?:\[!\]|\b(?:error|failed|failure|exception|fatal)\b)")
_SOURCE_EXTENSIONS = {".c", ".h"}
_SKIP_DIRECTORIES = {
    ".git", ".hg", ".svn", ".venv", "build", "coverage", "dist", "node_modules", "out", "vendor",
}
_MODEL_SCOPE = (
    "Joern 4.0.653 C frontend interprocedural data-flow paths for one captured standalone C source tree. "
    "The source is one exact method formal parameter and the sink one exact call argument. "
    "External methods without semantics may conservatively propagate values. Parse-error UNKNOWN nodes invalidate "
    "negative results; no path means no path in this model, not that a vulnerability or exploit is absent."
)

Status = Literal["PATH_FOUND", "NO_PATH_IN_MODEL", "UNSUPPORTED", "UNKNOWN", "UNAVAILABLE"]

# This is deliberately fixed. Request data is passed as JSON parameters and can never become Scala/CPGQL.
_QUERY = r'''@main def exec(cpgFile: String, sourceMethod: String, sourceParameter: Int,
               sinkMethod: String, sinkArgument: Int, maxPaths: Int, capturedFiles: String) = {
  importCpg(cpgFile)
  import scala.jdk.CollectionConverters.*
  // Materialize each endpoint once. Joern traversals are lazy and reusing one after .size
  // can consume it, silently turning a real flow into an empty result.
  val sources = cpg.method.nameExact(sourceMethod).parameter.index(sourceParameter).l
  val sinks = cpg.call.nameExact(sinkMethod).argument(sinkArgument).l
  val sourceCount = sources.size
  val sinkCount = sinks.size
  val paths = sinks.reachableByFlows(sources).take(maxPaths).l
  val unresolvedCallCount = cpg.call.whereNot(_.callee).size
  val unknownNodeCount = cpg.unknown.size
  val decoder = java.util.Base64.getDecoder
  val expectedFiles = capturedFiles.split(",", -1).toList.map(name =>
    new String(decoder.decode(name), "UTF-8")).toSet
  val parsedFiles = cpg.file.name.l.filter(_.endsWith(".c")).toSet
  val missingFileCount = (expectedFiles -- parsedFiles).size
  println(s"VKIT_COUNTS|$sourceCount|$sinkCount|${paths.size}|$unresolvedCallCount|$unknownNodeCount|$missingFileCount")
  paths.foreach { path =>
    val header = "label\tline\tname\tcode"
    val rows = path.elements.map { node =>
      val properties = node.propertiesMap.asScala
      val fields = Seq(
        node.label,
        properties.getOrElse("LINE_NUMBER", "").toString,
        properties.getOrElse("NAME", "").toString,
        properties.getOrElse("CODE", "").toString
      )
      fields.map(_.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n"))
        .mkString("\t")
    }
    val table = (header +: rows).mkString("\n")
    val encoded = java.util.Base64.getEncoder.encodeToString(table.getBytes("UTF-8"))
    println(s"VKIT_FLOW_B64|$encoded")
  }
}'''


class _Unsupported(Exception):
    pass


def _valid_identifier(value: Any, label: str) -> str:
    if (not isinstance(value, str) or len(value) > MAX_METHOD_NAME
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)):
        raise ValueError(f"{label} must be an identifier of at most {MAX_METHOD_NAME} characters")
    return value


def _source_directory(root: Path, path: str) -> tuple[Path, str]:
    if (not isinstance(path, str) or not path or "\0" in path or "\\" in path
            or Path(path).is_absolute() or PurePosixPath(path).is_absolute()
            or PureWindowsPath(path).drive):
        raise ValueError("path must identify a repository-relative C source directory")
    relative = PurePosixPath(path)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("path must be a normalized repository-relative directory")
    if any(part.casefold() in _SKIP_DIRECTORIES for part in relative.parts):
        raise ValueError("path cannot select a build, dependency, or metadata directory")
    try:
        base = Path(root).resolve(strict=True)
        current = base
        for part in relative.parts:
            current /= part
            if current.is_symlink() or _is_reparse_point(current):
                raise ValueError("source paths cannot contain symlinks or reparse points")
        resolved = current.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve C source directory: {exc}") from exc
    if not resolved.is_relative_to(base) or not resolved.is_dir():
        raise ValueError("path must identify a directory inside the repository")
    return resolved, relative.as_posix()


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except AttributeError:
        return False


def _snapshot(source: Path, destination: Path, deadline: float) -> tuple[str, tuple[str, ...], int]:
    files: list[tuple[str, Path]] = []
    entries_seen = 0
    pending = [source]
    while pending:
        current = pending.pop()
        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            if current.is_symlink() or _is_reparse_point(current) \
                    or not current.resolve(strict=True).is_relative_to(source.resolve()):
                raise ValueError("source trees cannot traverse symlinks, reparse points, or escapes")
            with os.scandir(current) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    entries_seen += 1
                    if entries_seen > MAX_TREE_ENTRIES:
                        raise _Unsupported(f"source tree has more than {MAX_TREE_ENTRIES} entries")
                    candidate = Path(entry.path)
                    if entry.is_symlink() or _is_reparse_point(candidate):
                        raise ValueError("source trees cannot contain symlinks or reparse points")
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold() not in _SKIP_DIRECTORIES:
                            pending.append(candidate)
                    elif entry.is_file(follow_symlinks=False) and candidate.suffix.lower() in _SOURCE_EXTENSIONS:
                        files.append((candidate.relative_to(source).as_posix(), candidate))
        except OSError as exc:
            raise ValueError(f"cannot enumerate source tree: {exc}") from exc
    files.sort(key=lambda item: item[0])
    if not files:
        raise _Unsupported("source tree contains no C or header files")
    if not any(Path(relative).suffix.lower() == ".c" for relative, _ in files):
        raise _Unsupported("source tree contains no C translation units")
    if len(files) > MAX_FILES:
        raise _Unsupported(f"source tree has more than {MAX_FILES} supported source files")

    digest = hashlib.sha256()
    total = 0
    paths: list[str] = []
    for relative, source_file in files:
        if time.monotonic() >= deadline:
            raise TimeoutError
        before = source_file.lstat()
        if source_file.is_symlink() or _is_reparse_point(source_file) or not source_file.is_file():
            raise ValueError("source trees cannot contain symlinks, reparse points, or special files")
        with source_file.open("rb") as handle:
            content = handle.read(MAX_FILE_BYTES + 1)
        after = source_file.lstat()
        if len(content) > MAX_FILE_BYTES:
            raise _Unsupported(f"source file {relative!r} exceeds {MAX_FILE_BYTES} bytes")
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"source file {relative!r} changed while being captured")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _Unsupported(f"source file {relative!r} is not UTF-8") from exc
        if _PREPROCESSOR_DIRECTIVE.search(content):
            raise _Unsupported(f"source file {relative!r} contains unsupported preprocessor directives")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise _Unsupported(f"source tree exceeds {MAX_TOTAL_BYTES} total source bytes")
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        path_bytes = relative.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        paths.append(relative)
    return digest.hexdigest(), tuple(paths), total


def _home() -> tuple[Path, str] | None:
    raw = os.environ.get(_HOME_ENV)
    if not raw:
        return None
    try:
        home = Path(raw).expanduser().resolve(strict=True)
        metadata = json.loads((home / ".vkit-joern.json").read_text(encoding="utf-8"))
        cli = home / ("joern.bat" if sys.platform == "win32" else "joern")
        parse = home / ("joern-parse.bat" if sys.platform == "win32" else "joern-parse")
        if (not isinstance(metadata, dict) or metadata.get("version") != BACKEND_VERSION
                or not cli.is_file() or not parse.is_file()
                or metadata.get("archive_sha256") != LINUX_X86_64_ARCHIVE_SHA256):
            return None
        for binary, key in ((cli, "joern_sha256"), (parse, "joern_parse_sha256")):
            with binary.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != metadata.get(key):
                    return None
        return home, metadata["archive_sha256"]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _quote_table(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_OUTPUT_LINE:
        raise ValueError("flow table exceeds its per-path bound")
    return {"table": value}


def _run_bounded(command: list[str], *, cwd: Path, timeout_seconds: float,
                 output_limit: int, artifact_limit: tuple[Path, int] | None = None,
                 env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run via the shared process-tree boundary and bound its file-backed logs."""
    stdout_path, stderr_path = cwd / "joern.stdout", cwd / "joern.stderr"

    def output_exceeded() -> bool:
        try:
            logs_exceeded = stdout_path.stat().st_size + stderr_path.stat().st_size > output_limit
        except FileNotFoundError:
            logs_exceeded = False
        if logs_exceeded:
            return True
        if artifact_limit is not None:
            artifact, limit = artifact_limit
            try:
                return artifact.stat().st_size > limit
            except FileNotFoundError:
                return False
        return False

    result = run_process(command, cwd=cwd, stdout_path=stdout_path, stderr_path=stderr_path,
                         timeout_seconds=timeout_seconds, env=env, should_cancel=output_exceeded)
    if artifact_limit is not None:
        artifact, limit = artifact_limit
        try:
            if artifact.stat().st_size > limit:
                raise ValueError(f"Joern artifact exceeded its {limit}-byte limit")
        except FileNotFoundError:
            pass
    if output_exceeded():
        raise ValueError("Joern process output exceeded its byte limit")
    if result.timed_out:
        raise subprocess.TimeoutExpired(command, timeout_seconds)
    if result.cancelled:
        raise ValueError("Joern process was cancelled")
    if result.launch_error:
        raise OSError(result.launch_error)
    assert result.code is not None
    stdout = stdout_path.read_bytes().decode("utf-8", "replace")
    stderr = stderr_path.read_bytes().decode("utf-8", "replace")
    return result.code, stdout, stderr


def _joern_environment(workspace: Path) -> dict[str, str]:
    """Keep Java and Joern away from caller-controlled shell and home settings."""
    return {"PATH": "/usr/bin:/bin", "HOME": str(workspace), "TMPDIR": str(workspace),
            "LC_ALL": "C", "LANG": "C"}


def _frontend_incomplete(stdout: str, stderr: str) -> bool:
    return bool(_INCOMPLETE_FRONTEND.search(stdout) or _INCOMPLETE_FRONTEND.search(stderr))


def _parse_query(stdout: str, *, source_method: str, source_parameter: int,
                 sink_method: str, sink_argument: int) -> tuple[Status, dict[str, Any]]:
    if len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("Joern query output exceeded its byte limit")
    source_count = sink_count = flow_count = unresolved_call_count = unknown_node_count = missing_file_count = None
    flows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if line.startswith("VKIT_COUNTS|"):
            if source_count is not None:
                raise ValueError("Joern emitted duplicate endpoint counts")
            fields = line.split("|")
            if len(fields) != 7:
                raise ValueError("Joern emitted malformed endpoint counts")
            source_count, sink_count, flow_count, unresolved_call_count, unknown_node_count, missing_file_count = (
                int(field) for field in fields[1:]
            )
        elif line.startswith("VKIT_FLOW_B64|"):
            encoded = line.removeprefix("VKIT_FLOW_B64|")
            table = base64.b64decode(encoded, validate=True).decode("utf-8")
            flows.append(_quote_table(table))
            if len(flows) > MAX_PATHS:
                raise ValueError("Joern emitted more paths than the configured result limit")
    if (source_count is None or sink_count is None or flow_count is None or unresolved_call_count is None
            or unknown_node_count is None or missing_file_count is None):
        raise ValueError("Joern emitted no endpoint count evidence")
    if min(source_count, sink_count, flow_count, unresolved_call_count, unknown_node_count, missing_file_count) < 0:
        raise ValueError("Joern emitted a negative count")
    if missing_file_count:
        return "UNKNOWN", {"reason": "Joern CPG omitted captured C translation units",
                            "missing_c_file_count": missing_file_count,
                            "endpoint_counts": {"sources": source_count, "sinks": sink_count,
                                                "flows": flow_count}}
    if unknown_node_count:
        return "UNKNOWN", {"reason": "Joern retained parse-error UNKNOWN nodes in the CPG",
                            "unknown_node_count": unknown_node_count,
                            "endpoint_counts": {"sources": source_count, "sinks": sink_count,
                                                "flows": flow_count}}
    if source_count != 1 or sink_count != 1:
        return "UNSUPPORTED", {"reason": "selected source and sink must each resolve to exactly one node",
                               "endpoint_counts": {"sources": source_count, "sinks": sink_count}}
    if flow_count > MAX_PATHS or flow_count != len(flows):
        return "UNKNOWN", {"reason": "Joern flow list was truncated or disagreed with emitted paths",
                            "endpoint_counts": {"sources": source_count, "sinks": sink_count,
                                                "flows": flow_count}}
    result = {"source": {"method": source_method, "parameter_index": source_parameter},
              "sink": {"method": sink_method, "argument_index": sink_argument},
              "endpoint_counts": {"sources": source_count, "sinks": sink_count, "flows": flow_count},
              "unresolved_call_count": unresolved_call_count,
              "unknown_node_count": unknown_node_count,
              "missing_c_file_count": missing_file_count,
              "flows": flows}
    return ("PATH_FOUND" if flows else "NO_PATH_IN_MODEL"), result


def find_data_flows(root: Path, path: str, source_method: str, source_parameter: int,
                    sink_method: str, sink_argument: int, timeout_ms: int = 30_000) -> dict[str, Any]:
    """Find Joern C data-flow paths between one formal parameter and one call argument."""
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be an integer from 1 to {MAX_TIMEOUT_MS}")
    source_method = _valid_identifier(source_method, "source_method")
    sink_method = _valid_identifier(sink_method, "sink_method")
    for value, label in ((source_parameter, "source_parameter"), (sink_argument, "sink_argument")):
        if type(value) is not int or not 1 <= value <= MAX_ARGUMENT_INDEX:
            raise ValueError(f"{label} must be an integer from 1 to {MAX_ARGUMENT_INDEX}")
    source, relative_path = _source_directory(Path(root), path)
    digest: str | None = None
    file_count = byte_count = 0
    status: Status
    result: dict[str, Any]
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        status, result = "UNAVAILABLE", {"reason": "only the pinned Linux x86_64 Joern runner is supported"}
    else:
        installed = _home()
        if installed is None:
            status, result = "UNAVAILABLE", {"reason": "verified Joern 4.0.653 installation is not configured"}
        else:
            home, archive_digest = installed
            started = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(prefix="vkit-joern-") as temporary:
                    workspace = Path(temporary)
                    snapshot = workspace / "source"
                    snapshot.mkdir()
                    digest, files, byte_count = _snapshot(
                        source, snapshot, started + min(MAX_CAPTURE_SECONDS, timeout_ms / 1000),
                    )
                    file_count = len(files)
                    phase_timeout = max(0.001, timeout_ms / 1000 - (time.monotonic() - started))
                    try:
                        parse_code, parse_stdout, parse_stderr = _run_bounded(
                            [str(home / "joern-parse"), str(snapshot), "--language", "C",
                             "--output", str(workspace / "capture.cpg")], cwd=workspace,
                            timeout_seconds=phase_timeout, output_limit=MAX_OUTPUT_BYTES,
                            artifact_limit=(workspace / "capture.cpg", MAX_CPG_BYTES),
                            env=_joern_environment(workspace),
                        )
                    except subprocess.TimeoutExpired:
                        status, result = "UNKNOWN", {"reason": "Joern C frontend exceeded the time limit"}
                    except ValueError as exc:
                        status, result = "UNKNOWN", {"reason": str(exc)}
                    else:
                        if parse_code != 0 or not (workspace / "capture.cpg").exists():
                            status, result = "UNKNOWN", {"reason": "Joern C frontend did not create a CPG",
                                                         "frontend_exit_code": parse_code,
                                                         "frontend_diagnostics": (parse_stdout + parse_stderr)[-MAX_OUTPUT_LINE:]}
                        elif _frontend_incomplete(parse_stdout, parse_stderr):
                            status, result = "UNKNOWN", {
                                "reason": "Joern C frontend reported an incomplete or failed source parse",
                                "frontend_diagnostics": (parse_stdout + parse_stderr)[-MAX_OUTPUT_LINE:],
                            }
                        elif (workspace / "capture.cpg").stat().st_size > MAX_CPG_BYTES:
                            status, result = "UNKNOWN", {"reason": "Joern CPG exceeded its byte limit"}
                        else:
                            query_path = workspace / "query.sc"
                            query_path.write_text(_QUERY, encoding="utf-8", newline="\n")
                            captured_c_files = [name for name in files if Path(name).suffix.lower() == ".c"]
                            encoded_files = ",".join(
                                base64.b64encode(name.encode("utf-8")).decode("ascii")
                                for name in captured_c_files
                            )
                            phase_timeout = max(0.001, timeout_ms / 1000 - (time.monotonic() - started))
                            try:
                                query_code, query_stdout, query_stderr = _run_bounded(
                                    [str(home / "joern"), "--script", str(query_path), "--param",
                                     f"cpgFile={workspace / 'capture.cpg'}", "--param",
                                     f"sourceMethod={source_method}", "--param",
                                     f"sourceParameter={source_parameter}", "--param",
                                     f"sinkMethod={sink_method}", "--param", f"sinkArgument={sink_argument}",
                                     "--param", f"maxPaths={MAX_PATHS + 1}", "--param",
                                     f"capturedFiles={encoded_files}"],
                                    cwd=workspace, timeout_seconds=phase_timeout, output_limit=MAX_OUTPUT_BYTES,
                                    env=_joern_environment(workspace),
                                )
                            except subprocess.TimeoutExpired:
                                status, result = "UNKNOWN", {"reason": "Joern data-flow query exceeded the time limit"}
                            except ValueError as exc:
                                status, result = "UNKNOWN", {"reason": str(exc)}
                            else:
                                if query_code != 0:
                                    status, result = "UNKNOWN", {"reason": "Joern data-flow query failed",
                                                                 "query_exit_code": query_code,
                                                                 "query_diagnostics": query_stderr[-MAX_OUTPUT_LINE:]}
                                else:
                                    try:
                                        status, result = _parse_query(
                                            query_stdout, source_method=source_method,
                                            source_parameter=source_parameter, sink_method=sink_method,
                                            sink_argument=sink_argument,
                                        )
                                    except (ValueError, UnicodeError) as exc:
                                        status, result = "UNKNOWN", {"reason": str(exc)}
                    if status in {"PATH_FOUND", "NO_PATH_IN_MODEL"} and (parse_stderr or parse_stdout):
                        result["frontend_diagnostics"] = (parse_stdout + parse_stderr)[-MAX_OUTPUT_LINE:]
            except _Unsupported as exc:
                status, result = "UNSUPPORTED", {"reason": str(exc)}
            except TimeoutError:
                status, result = "UNKNOWN", {"reason": "source capture exceeded its time limit"}
            except (OSError, ValueError, RuntimeError) as exc:
                status, result = "UNKNOWN", {"reason": f"Joern analysis could not produce complete evidence: {exc}"}
    response = {
        "operation": "find_data_flows", "path": relative_path,
        "source_method": source_method, "source_parameter": source_parameter,
        "sink_method": sink_method, "sink_argument": sink_argument,
        "source_sha256": digest, "source_file_count": file_count, "source_bytes": byte_count,
        "backend": BACKEND, "backend_version": BACKEND_VERSION,
        "backend_archive_sha256": installed[1] if 'installed' in locals() and installed else None,
        "timeout_ms": timeout_ms, "model_scope": _MODEL_SCOPE, "status": status, **result,
    }
    try:
        encode_json(response, max_bytes=MAX_RESULT_BYTES)
    except ValueError:
        response = {
            "operation": "find_data_flows", "path": relative_path,
            "source_method": source_method, "source_parameter": source_parameter,
            "sink_method": sink_method, "sink_argument": sink_argument,
            "source_sha256": digest, "source_file_count": file_count, "source_bytes": byte_count,
            "backend": BACKEND, "backend_version": BACKEND_VERSION,
            "backend_archive_sha256": installed[1] if 'installed' in locals() and installed else None,
            "timeout_ms": timeout_ms, "model_scope": _MODEL_SCOPE, "status": "UNKNOWN",
            "reason": "complete Joern evidence exceeded the 256 KiB response limit",
        }
    return response
