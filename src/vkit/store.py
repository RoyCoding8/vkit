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
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .nowindow import is_windows
from .paths import Project

STATE_DIR = "vkit"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


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
    def of(cls, project: Project) -> "Store":
        return cls(project.git_common_dir / STATE_DIR)

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def record(self, run_id: str) -> dict[str, Any] | None:
        record = read_json(self.run_dir(run_id) / "record.json")
        if record is not None and record["state"] == "running" and not self.is_alive(run_id):
            record = {**record, "state": "interrupted"}
        return record

    def save(self, record: dict[str, Any]) -> None:
        write_json(self.run_dir(record["run_id"]) / "record.json", record)

    def publish(self, record: dict[str, Any]) -> None:
        """Save a finished record and point its evidence key and check at it."""
        self.save(record)
        pointer = {"run_id": record["run_id"], "result": record["outcome"]["result"],
                   "ended_at": record["ended_at"], "key": record["key"]}
        write_json(self.root / "evidence" / record["check_id"] / f"{record['key']}.json", pointer)
        write_json(self.root / "latest" / f"{record['check_id']}.json", pointer)

    def evidence(self, check_id: str, key: str) -> dict[str, Any] | None:
        return read_json(self.root / "evidence" / check_id / f"{key}.json")

    def latest(self, check_id: str) -> dict[str, Any] | None:
        return read_json(self.root / "latest" / f"{check_id}.json")

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        base = self.root / "runs"
        if not base.is_dir():
            return []
        out = []
        for run_dir in sorted(base.iterdir(), key=lambda p: p.name, reverse=True)[:limit]:
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
        write_json(self.root / "baselines.json", {**self.baselines(), check_id: baseline})

    def approved(self) -> dict[str, Any]:
        return read_json(self.root / "approved.json") or {}

    def approve(self, entries: dict[str, dict[str, Any]]) -> None:
        write_json(self.root / "approved.json", {**self.approved(), **entries})
