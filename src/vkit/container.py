"""Run and recover one private, run-labeled Docker container."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import proc
from .nowindow import hidden_window
from .store import write_json

RUN_LABEL = "vkit.formal.run_id"
CONTROL_DIR = "container-control"
CONTROL_FILE = "descriptor.json"
_CID = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def mount(source: Path, target: str, readonly: bool) -> list[str]:
    if "," in str(source) or "," in target:
        raise ValueError("Docker bind paths cannot contain commas")
    return ["--mount", f"type=bind,source={source},target={target}" + (",readonly" if readonly else "")]


def sealed_options() -> list[str]:
    return ["--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=4g",
            "--cpus=2", "--ulimit", "nofile=1024:1024", "--ulimit",
            "fsize=2147483648:2147483648", "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=128m", "--user", "65532:65532",
            "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp"]


@dataclass(frozen=True)
class ContainerRun:
    exit: proc.Exit
    container_id: str | None
    removed: bool
    cleanup_error: str | None = None


def _docker(argv: Sequence[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=timeout,
        check=False, **hidden_window(),
    )


def image_id(reference: str) -> str | None:
    if shutil.which("docker") is None:
        return None
    try:
        result = _docker(["image", "inspect", "--format", "{{.Id}}", reference])
    except (OSError, subprocess.SubprocessError):
        return None
    actual = result.stdout.strip()
    return actual if result.returncode == 0 and actual == reference else None


def control_path(run_dir: Path) -> Path:
    return run_dir / CONTROL_DIR / CONTROL_FILE


def _write_descriptor(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    write_json(path, value)
    os.chmod(path, 0o600)


def _read_descriptor(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("run_id"), str)
            or not _RUN_ID.fullmatch(value["run_id"])
            or value.get("cid_file") != "container.cid"):
        raise ValueError("invalid Docker container control descriptor")
    container_id = value.get("container_id")
    if container_id is not None and (not isinstance(container_id, str)
                                     or not _CID.fullmatch(container_id)):
        raise ValueError("invalid container ID in control descriptor")
    return value


def _inspect(container_id: str) -> tuple[str, str | None, str | None]:
    try:
        result = _docker([
            "inspect", "--format", f'{{{{ index .Config.Labels "{RUN_LABEL}" }}}}',
            container_id,
        ])
    except (OSError, subprocess.SubprocessError) as exc:
        return "error", None, str(exc)
    if result.returncode == 0:
        return "present", result.stdout.strip(), None
    detail = result.stderr.strip() or "docker inspect failed"
    if "no such object" in detail.lower():
        return "absent", None, None
    return "error", None, detail


def _owned_ids(run_id: str) -> tuple[list[str], str | None]:
    try:
        result = _docker([
            "ps", "-aq", "--no-trunc", "--filter", f"label={RUN_LABEL}={run_id}"
        ])
    except (OSError, subprocess.SubprocessError) as exc:
        return [], str(exc)
    if result.returncode != 0:
        return [], result.stderr.strip() or "docker ps failed"
    ids = [item for item in result.stdout.splitlines() if item]
    if any(not _CID.fullmatch(item) for item in ids):
        return [], "docker returned an invalid labeled container ID"
    return ids, None


def _remove_owned(run_id: str, container_id: str | None) -> tuple[bool, str | None]:
    ids, error = _owned_ids(run_id)
    if error:
        return False, error
    if len(ids) > 1:
        return False, "multiple containers have this attempt label"
    if container_id is not None and not _CID.fullmatch(container_id):
        return False, "container ID did not match"
    if container_id is None:
        container_id = ids[0] if ids else None
    elif ids and ids[0] != container_id:
        return False, "attempt label points to a different container ID"
    if container_id is None:
        return True, None

    state, label, error = _inspect(container_id)
    if state == "error":
        return False, error
    if state == "absent":
        remaining, error = _owned_ids(run_id)
        if error:
            return False, error
        return (False, "a labeled container remains") if remaining else (True, None)
    if label != run_id:
        return False, "refusing to remove a container with a different run label"

    try:
        result = _docker(["rm", "--force", container_id], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode != 0:
        state, label, error = _inspect(container_id)
        if state != "absent":
            return False, error or result.stderr.strip() or "docker rm failed"
    remaining, error = _owned_ids(run_id)
    if error:
        return False, error
    if remaining:
        return False, "labeled container remains"
    return True, None


def recover_attempt(run_dir: Path) -> dict[str, Any]:
    """Remove only the container named by an interrupted attempt's private descriptor."""
    path = control_path(run_dir)
    if not path.is_file():
        return {"status": "absent", "container_id": None}
    try:
        descriptor = _read_descriptor(path)
        if not descriptor["run_id"].startswith(run_dir.name + "-"):
            raise ValueError("container label does not belong to this execution attempt")
        control_dir = path.parent
        cid_path = control_dir / descriptor["cid_file"]
        cid_file_id = cid_path.read_text(encoding="ascii").strip() if cid_path.is_file() else None
        container_id = descriptor["container_id"]
        if container_id and cid_file_id and container_id != cid_file_id:
            raise ValueError("container control descriptor and CID file disagree")
        container_id = container_id or cid_file_id
        removed, error = _remove_owned(descriptor["run_id"], container_id)
        status = ("removed" if container_id else "absent") if removed else "blocked"
        descriptor.update(status=status, container_id=container_id)
        if error:
            descriptor["cleanup_error"] = error
        else:
            descriptor.pop("cleanup_error", None)
        _write_descriptor(path, descriptor)
        return {"status": status, "container_id": container_id, "detail": error}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "blocked", "container_id": None, "detail": str(exc)}


def run_container(
    *, image: str, run_id: str, options: Sequence[str], command: Sequence[str],
    run_dir: Path, timeout_seconds: float, should_cancel: Callable[[], bool],
) -> ContainerRun:
    """Create, execute, and remove one container with durable attempt control data."""
    if shutil.which("docker") is None:
        return ContainerRun(proc.Exit(None, launch_error="docker is not installed"), None,
                            False, "docker is not installed")
    if not _RUN_ID.fullmatch(run_id):
        return ContainerRun(proc.Exit(None, launch_error="invalid Docker run label"), None,
                            False, "invalid Docker run label")
    run_dir.mkdir(parents=True, exist_ok=True)
    descriptor_path = control_path(run_dir)
    if descriptor_path.exists():
        return ContainerRun(proc.Exit(None, launch_error="container descriptor already exists"),
                            None, False, "container descriptor already exists")
    control_dir = descriptor_path.parent
    cid_file = control_dir / "container.cid"
    descriptor = {"schema_version": 1, "run_id": run_id, "cid_file": cid_file.name,
                  "container_id": None, "status": "creating"}
    _write_descriptor(descriptor_path, descriptor)
    create = proc.run(
        ["docker", "create", "--cidfile", str(cid_file), "--label", f"{RUN_LABEL}={run_id}",
         *options, image, *command],
        cwd=run_dir, stdout_path=run_dir / "container-create.stdout",
        stderr_path=run_dir / "container-create.stderr", timeout_seconds=30,
        should_cancel=should_cancel, max_output_bytes=1024 * 1024,
    )
    try:
        container_id = cid_file.read_text(encoding="ascii").strip() if cid_file.is_file() else None
    except (OSError, UnicodeError):
        container_id = None
    if container_id and _CID.fullmatch(container_id):
        descriptor.update(container_id=container_id, status="created")
        _write_descriptor(descriptor_path, descriptor)
    try:
        if create.cancelled or create.timed_out:
            started = create
        elif create.code != 0 or container_id is None:
            detail = (run_dir / "container-create.stderr").read_text(
                encoding="utf-8", errors="replace")
            raise RuntimeError(detail.strip() or "docker create did not return a container ID")
        else:
            state, label, error = _inspect(container_id)
            if state != "present" or label != run_id:
                raise RuntimeError(error or "docker create returned an unowned container ID")
            started = proc.run(
                ["docker", "start", "--attach", container_id], cwd=run_dir,
                stdout_path=run_dir / "container.stdout", stderr_path=run_dir / "container.stderr",
                timeout_seconds=timeout_seconds, should_cancel=should_cancel,
                max_output_bytes=1024 * 1024,
            )
    except (OSError, RuntimeError) as exc:
        started = proc.Exit(None, launch_error=str(exc))
    removed, cleanup_error = _remove_owned(run_id, container_id)
    descriptor.update(container_id=container_id, status="removed" if removed else "blocked")
    if cleanup_error:
        descriptor["cleanup_error"] = cleanup_error
    _write_descriptor(descriptor_path, descriptor)
    return ContainerRun(started, container_id, removed, cleanup_error)
