"""Run records and evidence on disk, shared by every worktree of one repository.

Layout under `<git common dir>/vkit/`:
  runs/<run_id>/record.json   the run; `state` is "running" until it is "done"
  runs/<run_id>/lock          held by the runner for as long as it lives
  runs/<run_id>/cancel        present when someone asked the run to stop
  evidence/<check_id>/<key>.json  the latest finished run for one evidence key
  latest/<check_id>.json      the latest finished run of that check, any key
  approved.json               check digests a human accepted
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .nowindow import is_windows
from .paths import Project

STATE_DIR = "vkit"
_RUN_ID = re.compile(r"[A-Za-z0-9_-]+")
_SUBJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _subject_dir(subject: dict[str, Any]) -> Path:
    if (
        not isinstance(subject, dict)
        or set(subject) != {"kind", "id"}
        or subject["kind"] not in {"registered_check", "formal_goal"}
        or not isinstance(subject["id"], str)
        or not _SUBJECT_ID.fullmatch(subject["id"])
    ):
        raise ValueError("invalid evidence subject")
    return Path(subject["kind"]) / subject["id"]


def _process_alive(pid: int) -> bool:
    if is_windows:
        import pywintypes
        import win32api
        import win32con
        import win32process

        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION, False, pid
            )
            try:
                return win32process.GetExitCodeProcess(handle) == 259
            finally:
                handle.Close()
        except pywintypes.error as error:
            return error.winerror == 5
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.02)


def read_json(path: Path) -> Any | None:
    for attempt in range(50):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (PermissionError, json.JSONDecodeError):
            if attempt == 49:
                raise
            time.sleep(0.02)
    return None


def _try_lock(handle: Any) -> bool:
    if is_windows:
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: Any) -> None:
    if is_windows:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class Store:
    root: Path

    @classmethod
    def of(cls, project: Project) -> Store:
        return cls(project.git_common_dir / STATE_DIR)

    def run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError(
                "run_id must contain only letters, digits, underscores, or hyphens"
            )
        return self.root / "runs" / run_id

    def record(self, run_id: str) -> dict[str, Any] | None:
        record = read_json(self.run_dir(run_id) / "record.json")
        if (
            record is not None
            and record["state"] == "running"
            and not self.is_alive(run_id)
        ):
            record = {**record, "state": "interrupted"}
        if record is not None and record["state"] == "queued":
            worker = read_json(self.run_dir(run_id) / "worker.json")
            if worker is not None and not _process_alive(worker["pid"]):
                record = {**record, "state": "interrupted"}
        if (record is not None and record["state"] == "interrupted"
                and record.get("container_cleanup", {}).get("status") not in {"removed", "absent"}):
            from .container import control_path, recover_attempt

            if control_path(self.run_dir(run_id)).is_file():
                try:
                    with self.hold(run_id):
                        current = read_json(self.run_dir(run_id) / "record.json")
                        if current["state"] in {"queued", "running", "interrupted"}:
                            record = {**current, "state": "interrupted", "ended_at": now(),
                                      "container_cleanup": recover_attempt(self.run_dir(run_id))}
                            self.save(record)
                        else:
                            record = current
                except RuntimeError:
                    record = read_json(self.run_dir(run_id) / "record.json")
        return record

    def save(self, record: dict[str, Any]) -> None:
        write_json(self.run_dir(record["run_id"]) / "record.json", record)

    def publish(self, record: dict[str, Any]) -> None:
        """Save a finished record and publish its typed evidence and latest pointers."""
        self.save(record)
        from .inputs import canonical_digest

        subject = record["subject"]
        subject_dir = _subject_dir(subject)
        pointer = {
            "run_id": record["run_id"],
            "result": record["outcome"]["result"],
            "record_sha256": canonical_digest(record),
            "ended_at": record["ended_at"],
            "key": record["key"],
            "job_id": record["job_id"],
            "subject": subject,
        }
        write_json(
            self.root / "evidence" / subject_dir / f"{record['job_id']}.json", pointer
        )
        write_json(self.root / "latest" / subject_dir / "latest.json", pointer)

    def evidence_for(
        self, kind: str, subject_id: str, job_id: str
    ) -> dict[str, Any] | None:
        from .evidence import validate_record
        from .inputs import canonical_digest

        subject = {"kind": kind, "id": subject_id}
        try:
            if not re.fullmatch(r"[0-9a-f]{64}", job_id):
                return None
            directory = _subject_dir(subject)
            pointer = read_json(self.root / "evidence" / directory / f"{job_id}.json")
            if pointer is None:
                return None
            record = self.record(pointer["run_id"])
            if (
                record is None
                or record["state"] != "done"
                or record["job_id"] != job_id
                or record["subject"] != subject
                or record["outcome"]["result"] != pointer["result"]
                or record["key"] != pointer["key"]
                or canonical_digest(record) != pointer["record_sha256"]
                or not validate_record(record, self.run_dir(pointer["run_id"]))
            ):
                return None
        except (ValueError, KeyError, TypeError, OSError):
            return None
        return pointer

    def latest_for(self, kind: str, subject_id: str) -> dict[str, Any] | None:
        try:
            directory = _subject_dir({"kind": kind, "id": subject_id})
        except ValueError:
            return None
        return read_json(self.root / "latest" / directory / "latest.json")

    def evidence(self, check_id: str, key: str) -> dict[str, Any] | None:
        return self.evidence_for("registered_check", check_id, key)

    def latest(self, check_id: str) -> dict[str, Any] | None:
        return self.latest_for("registered_check", check_id) or read_json(
            self.root / "latest" / f"{check_id}.json"
        )

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        base = self.root / "runs"
        if not base.is_dir():
            return []
        out = []
        for run_dir in sorted(base.iterdir(), key=lambda p: p.name, reverse=True)[
            :limit
        ]:
            record = self.record(run_dir.name)
            if record is not None:
                out.append(record)
        return out

    @contextmanager
    def hold(self, run_id: str) -> Iterator[None]:
        """Hold the run's liveness lock; the OS drops it if this process dies."""
        path = self.run_dir(run_id) / "lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+b") as handle:
            if not _try_lock(handle):
                raise RuntimeError(f"run {run_id} is already held")
            yield

    def is_alive(self, run_id: str) -> bool:
        path = self.run_dir(run_id) / "lock"
        if not path.exists():
            return False
        with open(path, "a+b") as handle:
            if not _try_lock(handle):
                return True
            _unlock(handle)
            return False

    def request_cancel(self, run_id: str) -> None:
        (self.run_dir(run_id) / "cancel").touch()

    def cancel_requested(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "cancel").exists()

    def baselines(self) -> dict[str, Any]:
        return read_json(self.root / "baselines.json") or {}

    def pin_baseline(self, check_id: str, baseline: dict[str, Any]) -> None:
        write_json(
            self.root / "baselines.json", {**self.baselines(), check_id: baseline}
        )

    def approved(self) -> dict[str, Any]:
        document = read_json(self.root / "approved.json") or {}
        if not isinstance(document, dict):
            raise ValueError("approved.json must contain an object")
        if "schema_version" not in document:
            if any(not isinstance(record, dict) for record in document.values()):
                raise ValueError("legacy check approvals must be objects")
            records = {
                key: {**record, "kind": "check_definition"}
                for key, record in document.items()
            }
        else:
            if document.get("schema_version") != 2 or set(document) != {
                "schema_version",
                "records",
            }:
                raise ValueError("unsupported approval document")
            records = document["records"]
        if not isinstance(records, dict):
            raise ValueError("approval records must be an object")
        for record in records.values():
            self._validate_approval(record)
        return records

    @staticmethod
    def _validate_approval(record: Any) -> None:
        if not isinstance(record, dict):
            raise ValueError("approval record must be an object")
        required = {
            "check_definition": {"check_id", "backend", "accepted_at"},
            "formal_policy": {
                "policy_sha256",
                "package_sha256",
                "checker_sha256",
                "accepted_at",
            },
        }
        fields = required.get(record.get("kind"))
        if fields is None or not fields <= record.keys():
            raise ValueError(
                "approval must be a complete check_definition or formal_policy record"
            )

    @contextmanager
    def _approval_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "approval.lock").open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + 10
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out waiting for the shared approval lock")
                time.sleep(0.02)
            try:
                yield
            finally:
                _unlock(handle)

    def approve(self, entries: dict[str, dict[str, Any]]) -> None:
        for record in entries.values():
            self._validate_approval(record)
        with self._approval_lock():
            records = {**self.approved(), **entries}
            write_json(
                self.root / "approved.json", {"schema_version": 2, "records": records}
            )
