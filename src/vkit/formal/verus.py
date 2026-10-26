"""Resolve and run the accepted Verus source profile in sealed Docker."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from .. import container
from ..inputs import canonical_digest, is_secret
from ..outcome import Blocked, BlockedReason, Outcome, Passed, ScenarioResult
from .policy import FormalComponent

PROFILE = "verus-linux-x86_64-v1"
RUST_VERSION = "1.99.1"
Z3_VERSION = "4.16.0"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERIFICATION = re.compile(r"verification results:: [1-9][0-9]* verified, 0 errors")
_STRICT = ("--no-cheating", "--no-external-by-default", "--num-threads", "2")
_DIAGNOSTICS = ("-V", "axiom-usage-info", "--log", "call-graph")


class ExecutionSetupError(ValueError):
    def __init__(self, reason: BlockedReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"tool asset is not a regular file: {path.name}")
    return {"sha256": _sha256(path), "mode": stat.S_IMODE(info.st_mode)}


def _tree_identity(root: Path) -> list[dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"tool asset directory is missing or linked: {root}")
    root = root.resolve(strict=True)
    files = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            info = child.lstat()
            relative = child.relative_to(root).as_posix()
            if is_secret(relative):
                raise ValueError(
                    f"tool asset contains a credential-like path: {relative}"
                )
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
            elif stat.S_ISREG(info.st_mode):
                files.append({"path": relative, **_file_identity(child)})
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(child)
                resolved = child.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    raise ValueError(f"tool asset link escapes its root: {relative}")
                files.append(
                    {
                        "path": relative,
                        "symlink": target,
                        "sha256": hashlib.sha256(target.encode()).hexdigest(),
                        "mode": stat.S_IMODE(info.st_mode),
                    }
                )
            else:
                raise ValueError(f"unsupported tool asset: {relative}")
    return sorted(files, key=lambda item: item["path"])


def _archive_identity(
    archive: Path, tool_root: Path, pins: dict[str, Any]
) -> dict[str, Any]:
    expected = pins.get("archives", {}).get(archive.name)
    actual = _sha256(archive)
    if expected is None or actual != expected:
        raise ValueError("Verus archive does not match the package pin")
    entries = {}
    roots = set()
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            if item.is_dir():
                continue
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
                raise ValueError("Verus archive contains an unsafe path")
            if item.external_attr >> 16 & 0o170000 not in {0, stat.S_IFREG}:
                raise ValueError("Verus archive contains a non-regular tool")
            roots.add(path.parts[0])
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative in entries:
                raise ValueError("Verus archive contains duplicate tool paths")
            entries[relative] = hashlib.sha256(bundle.read(item)).hexdigest()
    if len(roots) != 1:
        raise ValueError("Verus archive has an unexpected root layout")
    extracted = _tree_identity(tool_root)
    if {item["path"]: item["sha256"] for item in extracted} != entries:
        raise ValueError("extracted Verus tools differ from the pinned archive")
    version_path = tool_root / "version.json"
    version_json = json.loads(version_path.read_text(encoding="utf-8"))
    version = version_json["verus"]
    if version.get("version") != pins.get("version") or version.get(
        "commit"
    ) != pins.get("revision"):
        raise ValueError("Verus version metadata does not match the package pin")
    toolchain = str(version.get("toolchain", "")).split()[0]
    if not toolchain.startswith(RUST_VERSION + "-"):
        raise ValueError("Verus release does not pin Rust 1.99.1")
    if not (tool_root / "verus").is_file() or not (tool_root / "z3").is_file():
        raise ValueError("pinned Verus archive is missing an executable")
    return {"archive_sha256": actual, "version": version, "files": extracted}


def _package_identity(package: Any) -> dict[str, Any]:
    root = Path(package.root).resolve(strict=True)
    names = ("ledger.rs", "expected.rs.in", "upstream.json")
    files = {}
    for name in names:
        path = root / name
        files[name] = _file_identity(path)
    return {
        "id": package.identity.id,
        "version": package.identity.version,
        "sha256": package.identity.sha256,
        "files": files,
    }


def resolve_execution(component: FormalComponent) -> dict[str, Any]:
    """Resolve pinned host assets; only semantic bytes enter the checker identity."""
    if platform.system() != "Linux" or platform.machine().lower() not in {
        "x86_64",
        "amd64",
    }:
        raise ExecutionSetupError(
            BlockedReason.PREREQUISITE_MISSING,
            "the Verus source profile requires Linux x86-64 and Docker",
        )
    if component.profile != PROFILE:
        raise ExecutionSetupError(
            BlockedReason.ARTIFACT_MALFORMED,
            f"unsupported formal profile: {component.profile}",
        )
    image = os.environ.get("VKIT_VERUS_IMAGE", "")
    if _SHA256.fullmatch(image) is None or container.image_id(image) is None:
        raise ExecutionSetupError(
            BlockedReason.TOOL_MISSING,
            "VKIT_VERUS_IMAGE must name an installed full sha256 Docker image ID",
        )
    names = {
        "archive": "VKIT_VERUS_ARCHIVE",
        "verus_root": "VKIT_VERUS_ROOT",
        "rustup_home": "VKIT_RUSTUP_HOME",
        "cargo_home": "VKIT_CARGO_HOME",
    }
    paths = {
        key: Path(os.environ.get(name, "")).expanduser().resolve()
        for key, name in names.items()
    }
    if any(not os.environ.get(name) for name in names.values()):
        raise ExecutionSetupError(
            BlockedReason.TOOL_MISSING,
            "Verus archive, tools, Rustup home, and Cargo home must be configured",
        )
    try:
        from . import ledger

        package = ledger.verify_package(component)
        pins = package.pins
        archive = paths["archive"]
        verus_root = paths["verus_root"]
        rustup_home = paths["rustup_home"]
        cargo_home = paths["cargo_home"]
        archive_identity = _archive_identity(archive, verus_root, pins)
        toolchain = str(archive_identity["version"]["toolchain"]).split()[0]
        rustup_identity = _tree_identity(rustup_home)
        if not (rustup_home / "toolchains" / toolchain / "bin" / "rustc").is_file():
            raise ValueError(f"Rust toolchain {toolchain} is not installed")
        cargo_identity = _tree_identity(cargo_home)
        if not (cargo_home / "bin" / "rustup").is_file():
            raise ValueError("configured Cargo home is missing the Rustup executable")
        package_identity = _package_identity(package)
        from ..evidence import policy_identity

        semantic = {
            "protocol": 1,
            "profile": PROFILE,
            "target": "x86_64-unknown-linux-gnu",
            "image_id": image,
            "package": package_identity,
            "verus": archive_identity,
            "rustup_home": rustup_identity,
            "cargo_home": cargo_identity,
            "runtime_versions": {"rustc": RUST_VERSION, "z3": Z3_VERSION},
            "strict_flags": list(_STRICT),
            "orchestrator": {"implementation": policy_identity(), "python": _file_identity(Path(sys.executable).resolve())},
            "execution": {
                "network": "none",
                "read_only_root": True,
                "uid": 65532,
                "cap_drop": "ALL",
                "no_new_privileges": True,
                "memory_bytes": 4 * 1024**3,
                "cpus": 2,
                "pids": 256,
                "tmp_bytes": 128 * 1024**2,
                "output_file_size_limit_bytes": 2 * 1024**3,
            },
        }
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise ExecutionSetupError(BlockedReason.ARTIFACT_MALFORMED, str(exc)) from exc
    return {
        "semantic": semantic,
        "paths": {key: str(path) for key, path in paths.items()},
    }


_CONTAINER_SCRIPT = """import hashlib, os, shutil, signal, subprocess, sys, json, re
plan = json.loads(sys.argv[1])
shutil.copyfile(plan['source'], '/output/candidate_api.rs')
digestor = hashlib.sha256()
with open('/output/candidate_api.rs', 'rb') as candidate:
    for chunk in iter(lambda: candidate.read(1048576), b''):
        digestor.update(chunk)
digest = digestor.hexdigest()
if digest != plan['source_sha256']:
    print('source_mismatch', file=sys.stderr)
    sys.exit(13)
for phase in plan['phases']:
    try:
        with open(phase['log'], 'wb') as stream:
            process = subprocess.Popen(phase['argv'], cwd='/output', stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                print('phase_timeout:' + phase['id'], file=sys.stderr)
                sys.exit(124)
    except OSError as error:
        print('tool_launch_error:' + phase['id'] + ':' + str(error), file=sys.stderr)
        sys.exit(127)
    if code != 0:
        print('phase_exit:' + phase['id'] + ':' + str(code), file=sys.stderr)
        sys.exit(10)
    text = open(phase['log'], encoding='utf-8', errors='replace').read()
    if phase.get('marker') and phase['marker'] not in text:
        print('runtime_mismatch:' + phase['id'], file=sys.stderr)
        sys.exit(11)
    if phase.get('verified') and not re.search(r'verification results:: [1-9][0-9]* verified, 0 errors', text):
        print('formal_obligation:' + phase['id'], file=sys.stderr)
        sys.exit(12)
sys.exit(0)
"""


def _verus_phases(
    package_root: str,
    source: str,
    expected: str,
    toolchain: str,
) -> list[dict[str, Any]]:
    verifier = "/assets/verus/verus"
    rustup = "/assets/cargo/bin/rustup"
    output = "/output"
    common = ["--crate-type=lib", *_STRICT, *_DIAGNOSTICS]

    def phase(
        name: str, argv: list[str], marker: str | None = None, verified: bool = False
    ) -> dict[str, Any]:
        return {
            "id": name,
            "argv": argv,
            "log": f"{output}/{name}.log",
            "marker": marker,
            "verified": verified,
        }

    phases = [
        phase("verus_version", [verifier, "--version"]),
        phase(
            "z3_version", ["/assets/verus/z3", "--version"], f"Z3 version {Z3_VERSION}"
        ),
        phase(
            "rust_version",
            [rustup, "run", toolchain, "rustc", "--version"],
            f"rustc {RUST_VERSION}",
        ),
    ]
    library = [
        verifier,
        *common,
        f"{package_root}/ledger.rs",
        "--export",
        f"{output}/ledger.vir",
        "--compile",
        "--log-dir",
        f"{output}/call-graph-ledger",
    ]
    candidate = [
        verifier,
        *common,
        source,
        "--import",
        f"ledger={output}/ledger.vir",
        "--extern",
        f"ledger={output}/libledger.rlib",
        "-L",
        output,
        "--export",
        f"{output}/candidate_api.vir",
        "--compile",
        "--log-dir",
        f"{output}/call-graph-candidate",
    ]
    expected_claim = [
        verifier,
        *common,
        expected,
        "--import",
        f"ledger={output}/ledger.vir",
        "--extern",
        f"ledger={output}/libledger.rlib",
        "-L",
        output,
        "--import",
        f"candidate_api={output}/candidate_api.vir",
        "--extern",
        f"candidate_api={output}/libcandidate_api.rlib",
        "-L",
        output,
        "--log-dir",
        f"{output}/call-graph-expected",
    ]
    phases.extend(
        (
            phase("verified_library", library, verified=True),
            phase("verified_candidate", candidate, verified=True),
            phase("verified_expected", expected_claim, verified=True),
        )
    )
    return phases


def _expected_logs(
    output: Path, phases: list[dict[str, Any]]
) -> tuple[bool, str | None]:
    for phase in phases:
        log_path = output / f"{phase['id']}.log"
        if not log_path.is_file():
            return False, f"missing verifier log: {phase['id']}"
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if phase.get("marker") and phase["marker"] not in text:
            return False, f"runtime identity mismatch: {phase['id']}"
        if phase.get("verified") and _VERIFICATION.search(text) is None:
            return False, f"formal obligation failed: {phase['id']}"
    return True, None


def execute_formal(
    component: FormalComponent,
    snapshot_root: Path,
    run_dir: Path,
    execution_identity: dict[str, Any],
    should_cancel: Callable[[], bool],
) -> tuple[Outcome, dict[str, Any]]:
    """Recheck accepted tools, then prove the captured candidate against the fixed package."""
    if should_cancel():
        return Blocked(BlockedReason.CANCELLED, "formal verification was cancelled"), {}
    try:
        current = resolve_execution(component)
        if canonical_digest(current["semantic"]) != canonical_digest(
            execution_identity["semantic"]
        ):
            return Blocked(
                BlockedReason.NOT_APPROVED,
                "Verus execution identity changed after the job was accepted",
            ), {}
        from . import ledger

        package = ledger.verify_package(component)
        source_root = snapshot_root.resolve(strict=True)
        source_path = source_root.joinpath(*PurePosixPath(component.entry).parts)
        if (
            source_path.is_symlink()
            or not source_path.is_file()
            or not source_path.resolve(strict=True).is_relative_to(source_root)
        ):
            return Blocked(
                BlockedReason.INPUT_MISSING,
                f"captured formal source is missing: {component.entry}",
            ), {}
        source_sha256 = _sha256(source_path)
        obligations = tuple(ledger.obligation_ids(component))
        run_dir.mkdir(parents=True, exist_ok=True)
        output = run_dir / "formal-output"
        if output.exists():
            return Blocked(
                BlockedReason.ARTIFACT_MALFORMED,
                "formal output already exists; refusing to reuse verifier artifacts",
            ), {}
        output.mkdir()
        output.chmod(0o777)
        trusted = run_dir / "formal-trusted"
        trusted.mkdir()
        expected_path = trusted / "expected.rs"
        expected_text = ledger.render_expected(component)
        expected_path.write_text(expected_text, encoding="utf-8", newline="\n")
        expected_path.chmod(0o444)
        expected_sha256 = _sha256(expected_path)
        package_root = Path(package.root).resolve(strict=True)
        toolchain = str(current["semantic"]["verus"]["version"]["toolchain"]).split()[0]
        phases = _verus_phases(
            "/trusted/package",
            "/output/candidate_api.rs",
            "/trusted/rendered/expected.rs",
            toolchain,
        )
        runtime_commands = [
            phase
            for phase in phases
            if phase["id"] in {"verus_version", "z3_version", "rust_version"}
        ]
        phases[0]["marker"] = str(current["semantic"]["verus"]["version"]["version"])
        plan = {
            "source": f"/source/{component.entry}",
            "source_sha256": source_sha256,
            "phases": [
                {
                    "id": phase["id"],
                    "argv": phase["argv"],
                    "log": phase["log"],
                    "marker": phase["marker"],
                    "verified": phase["verified"],
                }
                for phase in phases
            ],
        }
        options = [
            *container.sealed_options(),
            "--workdir", "/work",
            "--env",
            "RUSTUP_HOME=/assets/rustup",
            "--env",
            "CARGO_HOME=/assets/cargo",
            "--env",
            "PATH=/assets/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            *container.mount(source_root, "/source", True),
            *container.mount(package_root, "/trusted/package", True),
            *container.mount(trusted, "/trusted/rendered", True),
            *container.mount(Path(current["paths"]["verus_root"]), "/assets/verus", True),
            *container.mount(Path(current["paths"]["rustup_home"]), "/assets/rustup", True),
            *container.mount(Path(current["paths"]["cargo_home"]), "/assets/cargo", True),
            *container.mount(output, "/output", False),
        ]
        run_id = f"{run_dir.name}-{uuid.uuid4().hex}"
        result = container.run_container(
            image=current["semantic"]["image_id"],
            run_id=run_id,
            options=options,
            command=[
                "python3",
                "-c",
                _CONTAINER_SCRIPT,
                json.dumps(plan, separators=(",", ":")),
            ],
            run_dir=run_dir,
            timeout_seconds=900,
            should_cancel=should_cancel,
        )
        if result.exit.cancelled:
            return Blocked(
                BlockedReason.CANCELLED, "formal verification was cancelled"
            ), {}
        if result.exit.timed_out:
            return Blocked(
                BlockedReason.TIMEOUT, "formal verification exceeded 900 seconds"
            ), {}
        if result.exit.launch_error:
            return Blocked(BlockedReason.TOOL_MISSING, result.exit.launch_error), {}
        if not result.removed:
            return Blocked(
                BlockedReason.INTERNAL_ERROR,
                result.cleanup_error or "the owned verifier container was not removed",
            ), {}
        if result.exit.code != 0:
            container_error = (run_dir / "container.stderr").read_text(
                encoding="utf-8", errors="replace"
            )
            if "source_mismatch" in container_error:
                return Blocked(
                    BlockedReason.SOURCE_CHANGED,
                    "captured formal source changed while entering the container",
                ), {}
            if "tool_launch_error:" in container_error:
                return Blocked(BlockedReason.TOOL_MISSING, container_error.strip()), {}
            if "phase_timeout:" in container_error:
                return Blocked(BlockedReason.TIMEOUT, container_error.strip()), {}
            if "runtime_mismatch:" in container_error:
                return Blocked(BlockedReason.TOOL_MISSING, container_error.strip()), {}
            if any(
                f"phase_exit:{phase['id']}:" in container_error
                for phase in runtime_commands
            ):
                return Blocked(BlockedReason.TOOL_MISSING, container_error.strip()), {}
            return Blocked(
                BlockedReason.FORMAL_OBLIGATION,
                container_error.strip()
                or "Verus rejected a required formal obligation",
            ), {}
        logs_passed, detail = _expected_logs(output, phases)
        if not logs_passed:
            reason = (
                BlockedReason.TOOL_MISSING
                if detail and detail.startswith("runtime identity")
                else BlockedReason.FORMAL_OBLIGATION
            )
            return Blocked(reason, detail or "verifier evidence is incomplete"), {}
        metadata = {
            "method": "verus_source",
            "assurance": "trusted_engine",
            "category": "source_verification",
            "scope": "required_api",
            "obligations": list(obligations),
            "execution_identity": {
                "semantic": current["semantic"],
                "digest": canonical_digest(current["semantic"]),
            },
            "source": {"path": component.entry, "sha256": source_sha256},
            "expected": {
                "path": "formal-trusted/expected.rs",
                "sha256": expected_sha256,
            },
            "evidence": [
                {
                    "phase": phase["id"],
                    "path": f"formal-output/{phase['id']}.log",
                    "sha256": _sha256(output / f"{phase['id']}.log"),
                }
                for phase in phases
            ],
            "container": {
                "image_id": current["semantic"]["image_id"],
                "container_id": result.container_id,
                "removed": result.removed,
            },
        }
        return Passed(
            tuple(ScenarioResult(item, True, "verified") for item in obligations)
        ), metadata
    except ExecutionSetupError as exc:
        return Blocked(exc.reason, str(exc)), {}
    except (
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as exc:
        return Blocked(BlockedReason.ARTIFACT_MALFORMED, str(exc)), {}
