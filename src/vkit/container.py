"""Run one labeled Docker container under the shared process lifecycle."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .nowindow import hidden_window

_RUN_LABEL = "vkit.formal.run_id"
_CID = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ContainerRun:
    exit: proc.Exit
    container_id: str | None
    removed: bool
    cleanup_error: str | None = None


def image_id(reference: str) -> str | None:
    if shutil.which("docker") is None:
        return None
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            **hidden_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    actual = result.stdout.strip()
    return actual if result.returncode == 0 and actual == reference else None


def _inspect_label(container_id: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{_RUN_LABEL}" }}}}',
                container_id,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            **hidden_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _owned_ids(run_id: str) -> list[str]:
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                f"label={_RUN_LABEL}={run_id}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            **hidden_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return (
        [item for item in result.stdout.splitlines() if _CID.fullmatch(item)]
        if result.returncode == 0
        else []
    )


def _remove_owned(run_id: str, container_id: str | None) -> tuple[bool, str | None]:
    ids = set(_owned_ids(run_id))
    ownership_error = None
    if container_id is not None:
        if _CID.fullmatch(container_id) is None:
            ownership_error = "container ID did not match"
        elif _inspect_label(container_id) != run_id:
            ownership_error = "container ID run label did not match"
        else:
            ids.add(container_id)
    for owned_id in ids:
        if _inspect_label(owned_id) != run_id:
            return False, "refusing to remove a container with a different run label"
        try:
            result = subprocess.run(
                ["docker", "rm", "--force", owned_id],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                **hidden_window(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if result.returncode != 0:
            return False, result.stderr.strip() or "docker rm failed"
    remaining = _owned_ids(run_id)
    if remaining:
        return False, "labeled container remains"
    return (False, ownership_error) if ownership_error else (True, None)


def run_container(
    *,
    image: str,
    run_id: str,
    options: Sequence[str],
    command: Sequence[str],
    run_dir: Path,
    timeout_seconds: float,
    should_cancel: Callable[[], bool],
) -> ContainerRun:
    """Create a container before execution and remove only its verified run label."""
    if shutil.which("docker") is None:
        return ContainerRun(
            proc.Exit(None, launch_error="docker is not installed"),
            None,
            False,
            "docker is not installed",
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    cid_file = run_dir / "container.cid"
    if cid_file.exists():
        return ContainerRun(
            proc.Exit(None, launch_error="container CID file already exists"),
            None,
            False,
            "container CID file already exists",
        )
    create = proc.run(
        [
            "docker",
            "create",
            "--cidfile",
            str(cid_file),
            "--label",
            f"{_RUN_LABEL}={run_id}",
            *options,
            image,
            *command,
        ],
        cwd=run_dir,
        stdout_path=run_dir / "container-create.stdout",
        stderr_path=run_dir / "container-create.stderr",
        timeout_seconds=30,
        should_cancel=should_cancel,
        max_output_bytes=1024 * 1024,
    )
    try:
        container_id = (
            cid_file.read_text(encoding="ascii").strip() if cid_file.is_file() else None
        )
    except (OSError, UnicodeError):
        container_id = None
    try:
        if create.cancelled or create.timed_out:
            started = create
        elif create.code != 0 or container_id is None:
            detail = (run_dir / "container-create.stderr").read_text(
                encoding="utf-8", errors="replace"
            )
            raise RuntimeError(
                detail.strip() or "docker create did not return a container ID"
            )
        else:
            if (
                _CID.fullmatch(container_id) is None
                or _inspect_label(container_id) != run_id
            ):
                raise RuntimeError(
                    "docker create returned an invalid or unowned container ID"
                )
            started = proc.run(
                ["docker", "start", "--attach", container_id],
                cwd=run_dir,
                stdout_path=run_dir / "container.stdout",
                stderr_path=run_dir / "container.stderr",
                timeout_seconds=timeout_seconds,
                should_cancel=should_cancel,
                max_output_bytes=1024 * 1024,
            )
    except (OSError, RuntimeError) as exc:
        started = proc.Exit(None, launch_error=str(exc))
    removed, cleanup_error = _remove_owned(run_id, container_id)
    return ContainerRun(started, container_id, removed, cleanup_error)
