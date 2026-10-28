"""Bind checking results to frozen inputs, checker identities, and retained artifacts."""

from __future__ import annotations

import hashlib
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from .inputs import file_digest
from .paths import ProjectError
from .schemas import SchemaValidationError
from .store import read_json, write_json

_UNSEALED = frozenset({"record.json", "receipt.json", "lock", "cancel", "worker.log", "worker.json"})


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_link(path: Path) -> bool:
    """Reject symlinks and Windows junctions on every supported Python version."""
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _retained_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or is_link(root):
        raise ValueError("unsafe evidence path")
    current = root
    for part in path.parts:
        current = current / part
        if is_link(current):
            raise ValueError("evidence contains a filesystem link")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("evidence escapes its bundle")
    return current


def retained_files(root: Path, *, exclude_replay_assets: bool = False):
    count, total = 0, 0
    if is_link(root):
        raise ValueError("evidence root is a filesystem link")
    replay_assets = root / "replay" / "assets"
    if exclude_replay_assets and is_link(replay_assets):
        raise ValueError("portable replay asset directory is a filesystem link")
    if exclude_replay_assets and replay_assets.exists() and not replay_assets.is_dir():
        raise ValueError("portable replay assets are not a directory")
    for path in sorted(root.rglob("*")):
        if exclude_replay_assets and path.is_relative_to(replay_assets):
            continue
        info = path.lstat()
        if is_link(path) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("evidence contains a link or special file")
        if stat.S_ISREG(info.st_mode):
            count += 1
            total += info.st_size
            if count > 10000 or total > 512 * 1024 * 1024:
                raise ValueError("retained evidence exceeds 10000 files or 512 MiB")
            yield path


def _artifact_names(directory: Path) -> set[str]:
    names = (p.relative_to(directory).as_posix()
             for p in retained_files(directory, exclude_replay_assets=True))
    return {name for name in names if name not in _UNSEALED and not name.startswith(("snapshot/", "replay/"))}


def policy_identity() -> dict[str, str]:
    package = Path(__file__).parent
    files = [
        package / name
        for name in (
            "runner.py",
            "jobs.py",
            "evidence.py",
            "replay_assets.py",
            "inputs.py",
            "manifest.py",
            "outcome.py",
            "state.py",
            "store.py",
            "job.py",
            "proc.py",
            "schemas.py",
            "budgets.py",
            "claimkind.py",
            "container.py",
            "ci.py",
            "cli.py",
            "paths.py",
        )
    ]
    files += [
        p for p in (package / "verifiers").glob("*") if p.suffix in (".py", ".js")
    ]
    files += list((package / "formal").rglob("*.py"))
    from .schemas import _schema_dir

    result = {p.relative_to(package).as_posix(): sha256(p) for p in sorted(files)}
    for schema in _schema_dir().iterdir():
        if schema.name.endswith(".json"):
            result["schemas/" + schema.name] = hashlib.sha256(
                schema.read_bytes()
            ).hexdigest()
    return result


def backend_identity(check) -> dict[str, Any]:
    if check.kind.value == "external":
        from .verifiers.external import identity

        return identity(check)
    from .verifiers import dispatch

    argv = dispatch.argv_for(check, Path("__vkit_run__"), None)
    candidates = [sys.executable, argv[0]]
    candidates.extend(p.executable for p in check.prerequisites)
    if check.kind.value == "lean":
        candidates += [check.toolchain.tool]
        if check.toolchain.comparator:
            candidates += [check.toolchain.comparator, "lake", "lean4export", "landrun"]
    elif check.kind.value == "tlc":
        from .verifiers.tlc_adapter import _tool_paths

        candidates += list(_tool_paths(check.toolchain.tool))
    elif hasattr(check, "runner"):
        candidates += [check.runner.executable]
    tools = {}
    for name in candidates:
        name = name.replace("{{python}}", sys.executable)
        resolved = shutil.which(name) or (name if Path(name).is_file() else None)
        tools[name] = (
            None
            if resolved is None
            else {
                "path": str(Path(resolved).resolve()),
                "sha256": sha256(Path(resolved)),
            }
        )
    from .runner import _child_env

    return {
        "kind": check.kind.value,
        "tools": tools,
        "implementation": policy_identity(),
        "environment": {
            name: _child_env().get(name)
            for name in ("LEAN_PATH", "LEAN_SYSROOT", "JAVA_HOME", "PYTHONPATH")
        },
    }


def assurance(check) -> str:
    return {"lean": "kernel_proof", "tlc": "finite_model"}.get(
        check.kind.value, getattr(check, "assurance", "empirical")
    )


def seal(record: dict[str, Any], directory: Path, check=None) -> None:
    artifacts = {name: sha256(directory / name) for name in sorted(_artifact_names(directory))}
    result = record["outcome"]["result"]
    claim_outcome = {"PASS": "established", "FAIL": "refuted", "BLOCKED": "unknown"}[
        result
    ]
    if check is not None and check.kind.value == "lean" and result == "FAIL":
        claim_outcome = "unknown"
    receipt = {
        "schema_version": 2,
        "job_id": record["job_id"],
        "subject": record["subject"],
        "source": record["source"],
        "inputs": record["inputs"],
        "assurance": assurance(check) if check else "trusted_engine",
        "claim_outcome": claim_outcome,
        "outcome": record["outcome"],
        "obligations": record["obligations"],
        "artifacts": artifacts,
        "verification": record.get("verification", {}),
    }
    if check is None:
        receipt.update(method="verus_source", category="source_verification", scope="required_api",
                       verification=record.get("verification", {}))
    for field in (
        "check_digest",
        "backend",
        "policy_sha256",
        "package_sha256",
        "checker_sha256",
        "authority",
    ):
        if field in record:
            receipt[field] = record[field]
    from .schemas import EVIDENCE_RECORD, validate

    validate("evidence receipt", EVIDENCE_RECORD, receipt)
    write_json(directory / "receipt.json", receipt)
    record["assurance"] = receipt["assurance"]
    if check is None:
        record.update({key: receipt[key] for key in ("method", "category", "scope")})
    record["claim_outcome"] = receipt["claim_outcome"]
    record["receipt"] = {
        "path": "receipt.json",
        "sha256": sha256(directory / "receipt.json"),
        "assurance": receipt["assurance"],
        "claim_outcome": receipt["claim_outcome"],
    }


def validate_record(record: dict[str, Any], directory: Path) -> bool:
    """Local state is trusted; imported success flags are never sufficient for replay."""
    try:
        artifact_names = _artifact_names(directory)
        link = record["receipt"]
        if (
            link["path"] != "receipt.json"
            or sha256(_retained_path(directory, "receipt.json")) != link["sha256"]
        ):
            return False
        receipt = read_json(directory / "receipt.json")
        if artifact_names != set(receipt["artifacts"]):
            return False
        if receipt["schema_version"] == 1:
            if any(
                receipt[name] != record[name]
                for name in (
                    "job_id",
                    "check_digest",
                    "backend",
                    "inputs",
                    "outcome",
                    "obligations",
                )
            ):
                return False
            if record["job_id"] != record["key"]:
                return False
        elif receipt["schema_version"] == 2:
            from .inputs import load_capture
            from .job import validate_job

            job = validate_job(read_json(_retained_path(directory, "job.json")))
            source = load_capture(job["source"], directory / "snapshot")
            if (
                job["job_id"] != record["job_id"]
                or job["subject"] != record["subject"]
                or job["source"] != record["source"]
                or source.digest != record["source"]["digest"]
                or record["job_id"] != record["key"]
                or job.get("authority") != record.get("authority")
                or receipt.get("authority") != record.get("authority")
            ):
                return False
            fields = (
                "job_id",
                "subject",
                "source",
                "inputs",
                "outcome",
                "obligations",
                "check_digest",
                "backend",
                "policy_sha256",
                "package_sha256",
                "checker_sha256",
                "method",
                "category",
                "scope",
                "verification",
            )
            if any(
                name in receipt and receipt[name] != record.get(name) for name in fields
            ):
                return False
            if any(
                name not in receipt
                for name in (
                    "job_id",
                    "subject",
                    "source",
                    "inputs",
                    "outcome",
                    "obligations",
                )
            ):
                return False
            from .schemas import EVIDENCE_RECORD, validate

            validate("evidence receipt", EVIDENCE_RECORD, receipt)
            if record["subject"]["kind"] == "formal_goal" and record["outcome"]["result"] == "PASS":
                from .inputs import canonical_digest

                identity = receipt["verification"]["execution_identity"]
                if identity["digest"] != record["checker_sha256"] or canonical_digest(identity["semantic"]) != record["checker_sha256"]:
                    return False
        else:
            return False
        if (
            receipt["assurance"] != link["assurance"]
            or receipt["claim_outcome"] != link["claim_outcome"]
        ):
            return False
        if (
            record["assurance"] != receipt["assurance"]
            or record["claim_outcome"] != receipt["claim_outcome"]
        ):
            return False
        for item in (
            (record.get("verification") or {})
            .get("evidence_bundle", {})
            .get("files", [])
        ):
            if receipt["artifacts"].get("evidence/" + item["path"]) != item["sha256"]:
                return False
        for relative, digest in receipt["artifacts"].items():
            path = _retained_path(directory, relative)
            if sha256(path) != digest:
                return False
        for item in record["inputs"]["files"]:
            path = _retained_path(directory, "snapshot/" + item["path"])
            digest = sha256(path) if record.get("subject", {}).get("kind") == "formal_goal" else file_digest(path)
            if digest != item["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, ProjectError, SchemaValidationError):
        return False


def export(
    store, run_id: str, destination: Path, *, include_assets: bool = False
) -> dict[str, Any]:
    record = store.record(run_id)
    if (
        record is None
        or record["state"] != "done"
        or not validate_record(record, store.run_dir(run_id))
    ):
        raise ValueError("run has no complete, intact evidence bundle")
    shutil.copytree(
        store.run_dir(run_id),
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns("lock", "cancel", "worker.log", "worker.json"),
    )
    portable_assets = None
    if include_assets:
        from .replay_assets import capture

        try:
            portable_assets = capture(record, store.run_dir(run_id), destination)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return {
        "run_id": run_id,
        "job_id": record["job_id"],
        "bundle": str(destination.resolve()),
        "receipt_sha256": record["receipt"]["sha256"],
        **({"portable_assets": portable_assets} if portable_assets is not None else {}),
    }
