"""Compare bounded network snapshots with Batfish's fixed differential question."""
from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import json
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_TIMEOUT_SECONDS = 60
MAX_FILES_PER_SNAPSHOT = 128
MAX_FILE_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTIC_ROWS = 64
MAX_WITNESS_ROWS = 4
MAX_TRACE_CHARS = 8192
PYBATFISH_VERSION = "2026.9.17.3748"
BATFISH_VERSION = "2026.08.27"
BATFISH_IMAGE_DIGEST = "sha256:54cb0ed94fd9a3c1ca0985f73e5be479e955cee9b9f6a6799b66be3364fd8e5c"
_HOST_ENV = "VKIT_BATFISH_HOST"
_NODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_INTERFACE = re.compile(r"[A-Za-z0-9_.:/-]{1,64}\Z")
_SNAPSHOT_PART = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_ENGINE_VERSION = re.compile(r"v?2026\.08\.27(?:[-+][A-Za-z0-9_.-]+)?\Z")


@dataclass(frozen=True)
class _CapturedFile:
    relative: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class _Snapshot:
    files: tuple[_CapturedFile, ...]
    sha256: str
    byte_count: int


class _Unsupported(ValueError):
    pass


class _DeadlineExpired(TimeoutError):
    pass


def compare_network_paths(root: Path, base_path: str, candidate_path: str,
                          selector: dict[str, Any],
                          timeout_seconds: int = 45) -> dict[str, Any]:
    """Compare successful reachability for one fixed header/path selector.

    The result concerns only the captured configuration files and Batfish's model.
    It is not evidence about a live network or unmodeled configuration inputs.
    """
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        return _result("UNSUPPORTED", "timeout_seconds must be an integer in [1, 60]")
    try:
        query = _parse_selector(selector)
        base = _capture_snapshot(root, base_path)
        candidate = _capture_snapshot(root, candidate_path)
    except (OSError, TypeError, ValueError) as exc:
        return _result("UNSUPPORTED", str(exc))

    base_result: dict[str, Any] = {
        "operation": "compare_network_paths",
        "status": "UNKNOWN",
        "claim_scope": "successful reachability in the supplied captured configs/ and hosts/ files under the fixed selector",
        "snapshot_sha256": {"base": base.sha256, "candidate": candidate.sha256},
        "snapshot_bytes": {"base": base.byte_count, "candidate": candidate.byte_count},
        "selector": query,
        "engine": {
            "name": "Batfish",
            "expected_version": BATFISH_VERSION,
            "expected_image_digest": BATFISH_IMAGE_DIGEST,
            "digest_evidence": "deployment pin; the service API does not attest the running image digest",
            "pybatfish_version": PYBATFISH_VERSION,
        },
        "diagnostics": None,
        "directional_results": None,
        "detail": None,
    }
    try:
        actual_pybatfish = importlib.metadata.version("pybatfish")
    except importlib.metadata.PackageNotFoundError:
        return {**base_result, "status": "UNAVAILABLE",
                "detail": "the pinned pybatfish package is not installed"}
    if actual_pybatfish != PYBATFISH_VERSION:
        return {**base_result, "status": "UNAVAILABLE",
                "detail": "installed pybatfish does not match its version pin",
                "engine": {**base_result["engine"], "actual_pybatfish_version": actual_pybatfish}}
    host = os.environ.get(_HOST_ENV, "127.0.0.1")
    if not _valid_host(host):
        return {**base_result, "status": "UNSUPPORTED",
                "detail": f"{_HOST_ENV} must be an operator-configured hostname or IP address"}

    try:
        from pybatfish.client.consts import CoordConsts, WorkStatusCode
        from pybatfish.client.session import Session
        from pybatfish.datamodel.flow import HeaderConstraints, PathConstraints
        from requests.exceptions import RequestException
    except ImportError:
        return {**base_result, "status": "UNAVAILABLE",
                "detail": "the pinned pybatfish package could not be imported"}

    deadline = time.monotonic() + timeout_seconds
    analysis_deadline = deadline - min(1.0, timeout_seconds / 5)
    network_name = f"vkit_{uuid.uuid4().hex}"
    session = None
    network_created = False
    final: dict[str, Any] = base_result
    cleanup_error = None
    try:
        session = Session(host=host, port=9996,
                          timeout=min(5, max(0.05, analysis_deadline - time.monotonic())))
        _set_request_timeout(session, analysis_deadline)
        versions = session.get_component_versions()
        server_version = str(versions.get("Batfish", ""))
        if not _ENGINE_VERSION.fullmatch(server_version):
            final = {**base_result, "status": "UNAVAILABLE",
                     "detail": "the connected service is not the pinned Batfish release",
                     "engine": {**base_result["engine"], "actual_batfish_version": server_version}}
        else:
            base_result["engine"] = {**base_result["engine"],
                                      "actual_batfish_version": server_version}
            with tempfile.TemporaryDirectory(prefix="vkit-batfish-") as temporary:
                work = Path(temporary)
                base_dir = _write_snapshot(work / "base", base)
                candidate_dir = _write_snapshot(work / "candidate", candidate)
                _set_request_timeout(session, analysis_deadline)
                network_created = True
                session.set_network(network_name)
                _set_request_timeout(session, analysis_deadline)
                session.init_snapshot(str(base_dir), name="base", overwrite=True)
                _set_request_timeout(session, analysis_deadline)
                session.init_snapshot(str(candidate_dir), name="candidate", overwrite=True)

                diagnostics = {
                    "base": _snapshot_diagnostics(session, "base", base, analysis_deadline,
                                                   CoordConsts, WorkStatusCode),
                    "candidate": _snapshot_diagnostics(session, "candidate", candidate, analysis_deadline,
                                                        CoordConsts, WorkStatusCode),
                }
                base_result["diagnostics"] = diagnostics
                if not all(item["complete"] for item in diagnostics.values()):
                    final = {**base_result, "status": "UNKNOWN",
                             "detail": "snapshot parsing or conversion diagnostics prevent a complete reachability claim"}
                else:
                    header = HeaderConstraints(
                        dstIps=query["destination_ip"],
                        srcIps=query.get("source_ip"),
                        ipProtocols=[query["protocol"]],
                        dstPorts=str(query["destination_port"])
                        if "destination_port" in query else None,
                    )
                    path = PathConstraints(
                        startLocation=f"@enter({query['start_node']}[{query['start_interface']}])",
                        endLocation=query["end_node"],
                    )
                    directions = []
                    for name, snapshot, reference in (
                        ("candidate_vs_base", "candidate", "base"),
                        ("base_vs_candidate", "base", "candidate"),
                    ):
                        answer = _run_question(
                            session,
                            session.q.differentialReachability(
                                headers=header,
                                pathConstraints=path,
                                actions="SUCCESS",
                                ignoreFilters=False,
                                invertSearch=False,
                                maxTraces=1,
                            ),
                            snapshot,
                            analysis_deadline,
                            reference,
                            CoordConsts,
                            WorkStatusCode,
                        )
                        directions.append({
                            "snapshot": snapshot,
                            "reference_snapshot": reference,
                            "witnesses": _witnesses(answer, snapshot, reference),
                        })
                    any_witness = any(direction["witnesses"] for direction in directions)
                    final = {**base_result,
                             "status": "DIFFERENT" if any_witness else "NO_DIFFERENCE",
                             "detail": None,
                             "directional_results": directions}
    except _DeadlineExpired:
        final = {**base_result, "status": "UNKNOWN",
                 "detail": f"Batfish analysis exceeded the {timeout_seconds}-second deadline"}
    except Exception as exc:
        status = "UNAVAILABLE" if not network_created and isinstance(exc, RequestException) else "UNKNOWN"
        final = {**base_result, "status": status,
                 "detail": f"Batfish request failed ({type(exc).__name__})"}
    finally:
        if session is not None and network_created:
            try:
                _set_request_timeout(session, deadline)
                session.delete_network(network_name)
            except Exception as exc:
                cleanup_error = type(exc).__name__
    if cleanup_error is not None:
        final = {**final, "cleanup_status": "FAILED", "cleanup_error": cleanup_error}
    elif network_created:
        final = {**final, "cleanup_status": "DELETED"}
    return final


def _result(status: str, detail: str) -> dict[str, Any]:
    return {"operation": "compare_network_paths", "status": status,
            "claim_scope": "supplied network configuration model only", "detail": detail}


def _parse_selector(raw: Any) -> dict[str, Any]:
    required = {"start_node", "start_interface", "end_node", "destination_ip", "protocol"}
    optional = {"source_ip", "destination_port"}
    if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - optional:
        raise _Unsupported("selector must contain start_node, start_interface, end_node, destination_ip, protocol, and optional source_ip/destination_port")
    if any(not isinstance(raw[key], str) for key in ("start_node", "start_interface", "end_node")):
        raise _Unsupported("path node and interface names must be strings")
    if not _NODE.fullmatch(raw["start_node"]) or not _INTERFACE.fullmatch(raw["start_interface"]) or not _NODE.fullmatch(raw["end_node"]):
        raise _Unsupported("path must name concrete ASCII nodes and an interface")
    query: dict[str, Any] = {
        "start_node": raw["start_node"],
        "start_interface": raw["start_interface"],
        "end_node": raw["end_node"],
    }
    for key in ("destination_ip", "source_ip"):
        if key not in raw:
            continue
        if not isinstance(raw[key], str):
            raise _Unsupported(f"selector.{key} must be an IP address or CIDR string")
        try:
            query[key] = str(ipaddress.ip_network(raw[key], strict=False))
        except ValueError as exc:
            raise _Unsupported(f"selector.{key} must be an IP address or CIDR string") from exc
    if "destination_ip" not in query:
        raise _Unsupported("selector.destination_ip is required")
    if "source_ip" in query and ipaddress.ip_network(query["source_ip"]).version != ipaddress.ip_network(query["destination_ip"]).version:
        raise _Unsupported("source_ip and destination_ip must use the same IP version")
    protocol = raw["protocol"]
    if not isinstance(protocol, str) or protocol.upper() not in {"TCP", "UDP", "ICMP"}:
        raise _Unsupported("selector.protocol must be TCP, UDP, or ICMP")
    query["protocol"] = protocol.upper()
    if "destination_port" in raw:
        port = raw["destination_port"]
        if query["protocol"] == "ICMP" or type(port) is not int or not 0 <= port <= 65535:
            raise _Unsupported("destination_port must be an integer in [0, 65535] for TCP or UDP")
        query["destination_port"] = port
    return query


def _valid_host(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253 or any(char in value for char in "/\\:@?#"):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return all(_SNAPSHOT_PART.fullmatch(label) and not label.startswith("-") and not label.endswith("-")
                   for label in value.rstrip(".").split("."))


def _capture_snapshot(root: Path, relative: str) -> _Snapshot:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise _Unsupported("snapshot path must be a nonempty repository-relative POSIX path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} or not _SNAPSHOT_PART.fullmatch(part) for part in pure.parts):
        raise _Unsupported("snapshot path contains an unsupported path component")
    root_path = Path(root).resolve(strict=True)
    snapshot_path = root_path.joinpath(*pure.parts)
    _reject_reparse_path(root_path, snapshot_path)
    if not snapshot_path.is_dir():
        raise _Unsupported("snapshot path must name a directory")
    entries = list(os.scandir(snapshot_path))
    if any(entry.name not in {"configs", "hosts"} for entry in entries) or "configs" not in {entry.name for entry in entries}:
        raise _Unsupported("snapshot must contain configs/ and may contain hosts/ only")
    files: list[_CapturedFile] = []
    total = 0
    for folder in ("configs", "hosts"):
        folder_path = snapshot_path / folder
        if not folder_path.exists():
            continue
        _reject_reparse_path(root_path, folder_path)
        if not folder_path.is_dir():
            raise _Unsupported(f"snapshot {folder}/ must be a directory")
        for entry in sorted(os.scandir(folder_path), key=lambda item: item.name):
            if (not _SNAPSHOT_PART.fullmatch(entry.name) or entry.name.startswith(".")
                    or entry.name in {".", ".."}):
                raise _Unsupported(f"snapshot contains an unsupported filename in {folder}/")
            if folder == "hosts" and not entry.name.lower().endswith(".json"):
                raise _Unsupported("hosts/ accepts JSON host files only")
            path = folder_path / entry.name
            _reject_reparse_path(root_path, path)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise _Unsupported("snapshot files must be regular files; nested directories are unsupported")
            if info.st_size > MAX_FILE_BYTES:
                raise _Unsupported(f"snapshot file {folder}/{entry.name} exceeds {MAX_FILE_BYTES} bytes")
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(info):
                    raise _Unsupported("snapshot changed while it was being captured")
                content = stream.read(MAX_FILE_BYTES + 1)
                final_info = os.fstat(stream.fileno())
            if len(content) > MAX_FILE_BYTES or _file_identity(opened) != _file_identity(final_info):
                raise _Unsupported("snapshot changed or grew while it was being captured")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _Unsupported(f"snapshot file {folder}/{entry.name} must be UTF-8") from exc
            if "\x00" in text:
                raise _Unsupported(f"snapshot file {folder}/{entry.name} contains a NUL byte")
            if folder == "hosts":
                try:
                    host_data = json.loads(text, object_pairs_hook=_unique_object)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise _Unsupported(f"host file {entry.name} must contain a JSON object without duplicate keys") from exc
                if not isinstance(host_data, dict):
                    raise _Unsupported(f"host file {entry.name} must contain a JSON object")
            total += len(content)
            if total > MAX_SNAPSHOT_BYTES:
                raise _Unsupported(f"snapshot exceeds {MAX_SNAPSHOT_BYTES} total bytes")
            files.append(_CapturedFile(f"{folder}/{entry.name}", content,
                                       hashlib.sha256(content).hexdigest()))
            if len(files) > MAX_FILES_PER_SNAPSHOT:
                raise _Unsupported(f"snapshot exceeds {MAX_FILES_PER_SNAPSHOT} files")
    if not any(item.relative.startswith("configs/") for item in files):
        raise _Unsupported("snapshot configs/ must contain at least one file")
    manifest = [{"path": item.relative, "sha256": item.sha256, "bytes": len(item.content)}
                for item in files]
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return _Snapshot(tuple(files), digest, total)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _reject_reparse_path(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise _Unsupported("snapshot path escapes the repository root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _Unsupported("snapshot paths may not contain symlinks")
        info = current.lstat()
        if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise _Unsupported("snapshot paths may not contain reparse points")


def _write_snapshot(destination: Path, snapshot: _Snapshot) -> Path:
    destination.mkdir()
    for item in snapshot.files:
        output = destination.joinpath(*PurePosixPath(item.relative).parts)
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(item.content)
    return destination


def _set_request_timeout(session: Any, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineExpired
    session.timeout = min(5.0, remaining)


def _run_question(session: Any, question: Any, snapshot: str, deadline: float,
                  reference: str | None, consts: Any, statuses: Any) -> Any:
    _set_request_timeout(session, deadline)
    work_id = question.answer(snapshot=snapshot, reference_snapshot=reference, background=True)
    if not isinstance(work_id, str) or not work_id:
        raise ValueError("Batfish returned an invalid background work id")
    while True:
        _set_request_timeout(session, deadline)
        status_row = session.get_work_status(work_id)
        try:
            status = statuses(status_row[consts.SVC_KEY_WORKSTATUS])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Batfish returned an unknown work status") from exc
        if statuses.is_terminated(status):
            if status != statuses.TERMINATEDNORMALLY:
                raise ValueError(f"Batfish work ended as {status.value}")
            _set_request_timeout(session, deadline)
            return session.get_answer(question.get_name(), snapshot, reference)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExpired
        time.sleep(min(0.2, remaining))


def _snapshot_diagnostics(session: Any, name: str, snapshot: _Snapshot,
                          deadline: float, consts: Any, statuses: Any) -> dict[str, Any]:
    issues = _frame_records(_run_question(session, session.q.initIssues(), name, deadline,
                                          None, consts, statuses))
    parse_status = _frame_records(_run_question(session, session.q.fileParseStatus(), name,
                                                deadline, None, consts, statuses))
    warnings = _frame_records(_run_question(session, session.q.parseWarning(), name, deadline,
                                            None, consts, statuses))
    if max(len(issues), len(parse_status), len(warnings)) > MAX_DIAGNOSTIC_ROWS:
        raise ValueError("Batfish snapshot diagnostics exceed their result limit")
    expected = {item.relative for item in snapshot.files}
    observed = {str(row.get("File_Name", "")) for row in parse_status}
    complete = (not issues and not warnings and bool(parse_status)
                and observed == expected
                and all(row.get("Status") == "PASSED" for row in parse_status))
    return {
        "complete": complete,
        "init_issues": _safe_rows(issues),
        "file_parse_status": _safe_rows(parse_status),
        "parse_warnings": _safe_rows(warnings),
    }


def _frame_records(answer: Any) -> list[dict[str, Any]]:
    frame = answer.frame()
    if not hasattr(frame, "to_dict"):
        raise ValueError("Batfish returned a non-tabular answer")
    records = frame.to_dict(orient="records")
    if not isinstance(records, list):
        raise ValueError("Batfish returned an invalid table")
    return records


def _safe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if value is None:
                clean[str(key)] = None
            elif isinstance(value, (str, int, float, bool)):
                text = str(value)
                if len(text) > MAX_TRACE_CHARS:
                    raise ValueError("Batfish diagnostic output exceeds its size limit")
                clean[str(key)] = value
            else:
                text = str(value)
                if len(text) > MAX_TRACE_CHARS:
                    raise ValueError("Batfish diagnostic output exceeds its size limit")
                clean[str(key)] = text
        output.append(clean)
    return output


def _witnesses(answer: Any, snapshot: str, reference: str) -> list[dict[str, Any]]:
    rows = _frame_records(answer)
    if len(rows) > MAX_WITNESS_ROWS:
        raise ValueError("Batfish returned more reachability witnesses than allowed")
    required = {"Flow", "Snapshot_Traces", "Snapshot_TraceCount",
                "Reference_Traces", "Reference_TraceCount"}
    output = []
    for row in rows:
        if not required <= set(row):
            raise ValueError("Batfish differential answer omitted trace evidence")
        flow = str(row["Flow"])
        if not flow or len(flow) > 2048:
            raise ValueError("Batfish returned an invalid flow witness")
        traces = {}
        # The first field belongs to the snapshot= argument and the second to
        # reference_snapshot=. Preserve those API roles while naming the files.
        for key in ("Snapshot_Traces", "Reference_Traces"):
            value = row[key]
            if value is None:
                values = []
            elif isinstance(value, (str, bytes)):
                raise ValueError("Batfish returned malformed trace evidence")
            else:
                try:
                    values = list(value)
                except TypeError as exc:
                    raise ValueError("Batfish returned malformed trace evidence") from exc
            if len(values) > 4:
                raise ValueError("Batfish trace count exceeds the witness limit")
            rendered = sorted(str(item) for item in values)
            if any(len(item) > MAX_TRACE_CHARS for item in rendered):
                raise ValueError("Batfish trace exceeds the result size limit")
            traces[key] = rendered
        by_snapshot = {
            snapshot: traces["Snapshot_Traces"],
            reference: traces["Reference_Traces"],
        }
        counts = {
            snapshot: _count(row["Snapshot_TraceCount"]),
            reference: _count(row["Reference_TraceCount"]),
        }
        if not any(by_snapshot.values()):
            raise ValueError("Batfish returned a changed flow without trace evidence")
        output.append({
            "flow": flow,
            "traces_by_snapshot": by_snapshot,
            "trace_counts_by_snapshot": counts,
        })
    return output


def _count(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Batfish returned an invalid trace count")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Batfish returned an invalid trace count") from exc
    if result < 0 or result != value:
        raise ValueError("Batfish returned an invalid trace count")
    return result
