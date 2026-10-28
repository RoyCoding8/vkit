"""Run scoped Verus proofs for ledger transfer and lock-protected operations."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import zipfile
from pathlib import PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from vkit.formal import ledger
from vkit.nowindow import hidden_window

PACKAGE = ROOT / "src/vkit/formal/packages/resource_ledger"
FIXTURES = ROOT / "examples/verus-qualification"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned_tool_identity(archive: Path, tool_root: Path, pin: dict) -> dict:
    expected_archive = pin["archives"].get(archive.name)
    if expected_archive is None or sha256(archive) != expected_archive:
        raise ValueError("Verus archive does not match the official package pin")
    required = (
        "verus.exe", "rust_verify.exe", "z3.exe", "version.json", "vstd.vir",
        "verus_builtin_macros.dll", "verus_state_machines_macros.dll", "vir_macros.dll",
    )
    members = {}
    with zipfile.ZipFile(archive) as bundle:
        for name in required:
            paths = [item.filename for item in bundle.infolist()
                     if not item.is_dir()
                     and PurePosixPath(item.filename).name == name
                     and len(PurePosixPath(item.filename).parts) == 2]
            if len(paths) != 1:
                raise ValueError(f"pinned Verus archive must contain one {name}")
            members[name] = hashlib.sha256(bundle.read(paths[0])).hexdigest()
    for name, expected_file in members.items():
        path = tool_root / name
        if not path.is_file() or path.is_symlink() or sha256(path) != expected_file:
            raise ValueError(f"installed Verus file differs from pinned archive: {name}")
    version = json.loads((tool_root / "version.json").read_text(encoding="utf-8"))["verus"]
    if version["commit"] != pin["revision"] or version["version"] != pin["version"]:
        raise ValueError("Verus version does not match the official package pin")
    return {
        "archive_sha256": expected_archive,
        "archive_members": members,
        "version": version,
    }


def combined_source(model: str, transfer_fixture: str, concurrent_fixture: str) -> str:
    return "\n".join((model, transfer_fixture, concurrent_fixture))


def run_case(verus: Path, output: Path, name: str, source: str, *, should_verify: bool) -> dict:
    source_path = output / f"{name}.rs"
    log_path = output / f"{name}.log"
    source_path.write_text(source, encoding="utf-8", newline="\n")
    command = [str(verus), str(source_path), "--no-cheating", "--no-external-by-default",
               "--num-threads", "2", "--triggers-mode", "silent"]
    environment = {**os.environ, "RUSTUP_TOOLCHAIN": "1.99.1-x86_64-pc-windows-msvc"}
    with log_path.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, env=environment,
                start_new_session=os.name != "nt", **hidden_window(),
            )
        except OSError as error:
            log.write(f"tool_launch_error: {type(error).__name__}: {error}\n")
            returncode = None
        else:
            with process:
                try:
                    returncode = process.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=log, stderr=subprocess.STDOUT, check=False,
                            **hidden_window(),
                        )
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    returncode = None
    output_text = log_path.read_text(encoding="utf-8", errors="replace")
    verified = returncode == 0 and bool(re.search(
        r"verification results:: [1-9]\d* verified, 0 errors", output_text,
    ))
    rejected = returncode not in (None, 0) and bool(re.search(
        r"verification results:: \d+ verified, [1-9]\d* errors", output_text,
    ))
    matched = verified if should_verify else rejected
    return {
        "name": name,
        "expected": "verified" if should_verify else "proof_rejection",
        "matched": matched,
        "returncode": returncode,
        "source_sha256": sha256(source_path),
        "log_sha256": sha256(log_path),
        "log": log_path.name,
    }


def qualify(verus: Path, archive: Path, tool_root: Path, output: Path) -> dict:
    pin = json.loads((PACKAGE / "upstream.json").read_text(encoding="utf-8"))
    tool = pinned_tool_identity(archive, tool_root, pin)
    package = ledger.load_package()
    model = (PACKAGE / "ledger.rs").read_text(encoding="utf-8")
    transfer = (FIXTURES / "p2-transfer.rs").read_text(encoding="utf-8")
    concurrent = (FIXTURES / "p2-concurrent.rs").read_text(encoding="utf-8")
    positive = combined_source(model, transfer, concurrent)

    bad_transfer = """Err(error) => (
                    source,
                    destination,
                    TransferResult::DestinationFailed(error),
                ),"""
    wrong_transfer = """Err(error) => (
                    source,
                    destination,
                    TransferResult::SourceFailed(error),
                ),"""
    if positive.count(bad_transfer) != 1:
        raise ValueError("transfer rollback mutation anchor must occur exactly once")
    broken_transfer = positive.replace(bad_transfer, wrong_transfer, 1)

    good_shared = "let (updated, result) = reserve(current, id, owner, amount);"
    bad_shared = "let (updated, result) = (current, Ok(()));"
    if positive.count(good_shared) != 1:
        raise ValueError("concurrent guarantee mutation anchor must occur exactly once")
    broken_concurrent = positive.replace(good_shared, bad_shared, 1)

    output.mkdir(parents=True, exist_ok=False)
    cases = [
        run_case(verus, output, "positive", positive, should_verify=True),
        run_case(verus, output, "broken_transfer_error_origin", broken_transfer,
                 should_verify=False),
        run_case(verus, output, "broken_concurrent_reserve_guarantee", broken_concurrent,
                 should_verify=False),
    ]
    report = {
        "passed": all(case["matched"] for case in cases),
        "method": "pinned_verus_same_crate_package_proof",
        "scope": "package-level transfer and lock-protected reserve/release contracts; completed calls only",
        "limits": [
            "not a global history or universal linearizability proof",
            "no lock liveness, deadlock freedom, or starvation bound",
            "no abort recovery guarantee while a write handle is held",
            "no source-to-binary correspondence or runtime observation claim",
            "separate from six candidate API bindings and their receipts",
        ],
        "package": {
            "identity": vars(package.identity),
            "ledger_rs_sha256": sha256(PACKAGE / "ledger.rs"),
            "transfer_fixture_sha256": sha256(FIXTURES / "p2-transfer.rs"),
            "concurrent_fixture_sha256": sha256(FIXTURES / "p2-concurrent.rs"),
        },
        "verus": {
            "executable_sha256": sha256(verus),
            "archive_sha256": tool["archive_sha256"],
            "archive_members": tool["archive_members"],
            "version": tool["version"],
        },
        "cases": cases,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verus", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--tool-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = qualify(args.verus.resolve(), args.archive.resolve(), args.tool_root.resolve(),
                     args.output.resolve())
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
