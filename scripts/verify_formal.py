"""Run real Lean and TLC checks through VKit's public CLI.

Every case gets a disposable Git project and an accepted native check. Missing checkers are errors;
the script never turns an unavailable checker into a skip.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "formal-ci"
LEAN_TOOLCHAIN = ROOT / "examples" / "lean-proof" / "lean-toolchain"
TLC_VERSION = "2.19"
RESULT_EXIT = {"PASS": 0, "FAIL": 1, "BLOCKED": 3}


def _required_program(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"required formal checker is missing from PATH: {name}")
    return executable


def _lean_version() -> str:
    match = re.fullmatch(
        r"leanprover/lean4:v([0-9]+\.[0-9]+\.[0-9]+)(?:-[A-Za-z0-9.-]+)?",
        LEAN_TOOLCHAIN.read_text(encoding="utf-8").strip(),
    )
    if match is None:
        raise RuntimeError(f"cannot read a pinned Lean version from {LEAN_TOOLCHAIN}")
    return match.group(1)


def _tlc_jar() -> tuple[Path, str]:
    raw_path = os.environ.get("VKIT_TLA2TOOLS_JAR", "").strip()
    if not raw_path:
        raise RuntimeError("VKIT_TLA2TOOLS_JAR must name the pinned tla2tools.jar")
    jar = Path(raw_path)
    if not jar.is_file():
        raise RuntimeError(f"the pinned TLC jar does not exist: {jar}")
    expected = (FIXTURES / "tla2tools.sha256").read_text(encoding="ascii").strip()
    observed = hashlib.sha256(jar.read_bytes()).hexdigest()
    if observed != expected:
        raise RuntimeError(f"TLC jar digest mismatch: expected {expected}, got {observed}")
    return jar.resolve(), expected


def _base_check(check_id: str, input_paths: list[str]) -> dict:
    return {
        "id": check_id,
        "cwd": ".",
        "timeout_seconds": 120,
        "artifact": "checker-report.json",
        "inputs": input_paths,
        "subject": {"paths": input_paths, "digest": None},
        "claim_id": check_id,
        "description": "CI executes a real formal checker and checks the recorded result.",
    }


def _lean_entry(check_id: str, module: str, source: str, theorem: str, version: str) -> dict:
    entry = _base_check(check_id, [source])
    entry.update({
        "kind": "lean",
        "challenge": {"module": module, "path": source},
        "theorems": [theorem],
        "profile": "reviewed_proof_sources",
        "permitted_axioms": [],
        "toolchain": {"tool": "lean", "version": version},
        "prerequisites": [{"name": "Lean", "executable": "lean", "args": ["--version"]}],
    })
    return entry


def _tlc_entry(check_id: str, config: str, property_name: str, jar_sha256: str) -> dict:
    model = "FiniteWorkers.tla"
    entry = _base_check(check_id, [model, config])
    entry.update({
        "kind": "tlc",
        "model": {"module": "FiniteWorkers", "path": model},
        "config": config,
        "properties": [property_name],
        "bounds": {"Workers": 2},
        "fingerprint": {
            "constants_from_config": True,
            "checksum_states": False,
            "workers": 1,
        },
        "toolchain": {"tool": "tlc", "version": TLC_VERSION, "jar_sha256": jar_sha256},
        "prerequisites": [{"name": "Java", "executable": "java", "args": ["--version"]}],
    })
    return entry


def _cli(cli: str, project: Path, args: list[str], env: dict[str, str], expected_exit: int) -> dict:
    completed = subprocess.run(
        [cli, *args, "--project", str(project), "--json"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != expected_exit:
        raise RuntimeError(
            f"`vkit {' '.join(args)}` exited {completed.returncode}, expected {expected_exit}.\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"`vkit {' '.join(args)}` returned invalid JSON: {exc}\n{completed.stdout}\n{completed.stderr}"
        ) from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"`vkit {' '.join(args)}` returned a non-object JSON result")
    return document


def _worker_alive(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes

        synchronize = 0x00100000
        signaled = 0
        timeout = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            status = kernel32.WaitForSingleObject(handle, 0)
            if status == timeout:
                return True
            if status == signaled:
                return False
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_worker(record: dict) -> None:
    pid = record.get("worker_pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return
    deadline = time.monotonic() + 10
    while _worker_alive(pid):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"VKit worker {pid} recorded a result but did not exit")
        time.sleep(0.05)


def _run_case(
    cli: str,
    name: str,
    files: list[str],
    check: dict,
    expected: str,
    env: dict[str, str],
    export_replay: bool = False,
) -> None:
    with tempfile.TemporaryDirectory(prefix="vkit-formal-") as temporary:
        project = Path(temporary)
        for name_in_project in files:
            source = FIXTURES / name_in_project
            destination = project / name_in_project
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        verification = project / "verification"
        verification.mkdir()
        manifest = {"schema_version": 3, "description": name, "checks": [check]}
        (verification / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        subprocess.run(["git", "init", "--quiet", str(project)], check=True, capture_output=True)
        accepted = _cli(cli, project, ["accept", "--yes"], env, 0)
        if accepted.get("accepted") != [check["id"]]:
            raise RuntimeError(f"VKit did not accept the {check['id']} check: {accepted!r}")

        code = RESULT_EXIT[expected]
        result = _cli(cli, project, ["check", "run", "--check", check["id"]], env, code)
        runs = result.get("runs")
        if not isinstance(runs, list) or len(runs) != 1:
            raise RuntimeError(f"VKit did not return exactly one run for {check['id']}: {result!r}")
        record = runs[0]
        observed = (record.get("outcome") or {}).get("result")
        if record.get("check_id") != check["id"] or not record.get("run_id"):
            raise RuntimeError(f"VKit returned an incomplete run record for {check['id']}: {record!r}")
        if observed != expected:
            raise RuntimeError(
                f"{check['id']} recorded {observed!r}, expected {expected!r}; run={record!r}"
            )
        receipt = record.get("receipt") or {}
        assurance = "kernel_proof" if check["kind"] == "lean" else "finite_model"
        claim_outcome = (
            "established" if expected == "PASS"
            else "unknown" if check["kind"] == "lean" or expected == "BLOCKED"
            else "refuted"
        )
        if receipt.get("assurance") != assurance or receipt.get("claim_outcome") != claim_outcome:
            raise RuntimeError(
                f"{check['id']} receipt classified this result as "
                f"assurance={receipt.get('assurance')!r}, claim_outcome={receipt.get('claim_outcome')!r}; "
                f"expected assurance={assurance!r}, claim_outcome={claim_outcome!r}"
            )
        _wait_for_worker(record)
        print(f"{name}: {observed} (claim={claim_outcome}, assurance={assurance})")
        if export_replay:
            bundle = project / "evidence-bundle"
            exported = _cli(
                cli, project,
                ["evidence", "export", "--run", record["run_id"], "--output", str(bundle)],
                env, 0,
            )
            if (exported.get("run_id") != record["run_id"]
                    or exported.get("receipt_sha256") != receipt.get("sha256")
                    or not (bundle / "record.json").is_file()):
                raise RuntimeError(f"VKit exported an incomplete evidence bundle: {exported!r}")
            replay_record = _cli(cli, project, ["evidence", "replay", "--bundle", str(bundle)], env, 0)
            replay_receipt = replay_record.get("receipt") or {}
            replay_observed = (replay_record.get("outcome") or {}).get("result")
            if (replay_record.get("check_id") != check["id"] or replay_observed != "PASS"
                    or replay_receipt.get("assurance") != "kernel_proof"
                    or replay_receipt.get("claim_outcome") != "established"):
                raise RuntimeError(f"VKit did not replay the Lean proof successfully: {replay_record!r}")
            _wait_for_worker(replay_record)
            print("Lean evidence export and replay: PASS (claim=established, assurance=kernel_proof)")


def _lean_cases(cli: str, env: dict[str, str]) -> None:
    version = _lean_version()
    cases = (
        ("lean-proof", "Lean accepted a proved theorem", "Good.lean", "reflexive_nat", "PASS"),
        ("lean-sorry", "Lean refused an unfinished theorem", "Sorry.lean", "unfinished", "BLOCKED"),
        ("lean-axiom", "Lean rejected an injected unapproved axiom", "Injected.lean", "based_on_injected", "FAIL"),
    )
    for check_id, name, source, theorem, expected in cases:
        module = Path(source).stem
        check = _lean_entry(check_id, module, source, theorem, version)
        _run_case(cli, name, [source], check, expected, env, export_replay=(check_id == "lean-proof"))


def _tlc_cases(cli: str, env: dict[str, str], jar_sha256: str) -> None:
    model = "FiniteWorkers.tla"
    cases = (
        ("tlc-exhaustive", "TLC exhaustively checked the invariant", "TypeOK.cfg", "TypeOK", "PASS"),
        ("tlc-counterexample", "TLC found the invariant counterexample", "OnlyOne.cfg", "OnlyOne", "FAIL"),
    )
    for check_id, name, config, property_name, expected in cases:
        check = _tlc_entry(check_id, config, property_name, jar_sha256)
        _run_case(cli, name, [model, config], check, expected, env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("lean", "tlc", "all"), default="all")
    args = parser.parse_args()

    try:
        cli = _required_program("vkit")
        env = dict(os.environ)
        if args.backend in ("lean", "all"):
            _required_program("lean")
            _lean_version()
            _lean_cases(cli, env)
        if args.backend in ("tlc", "all"):
            _required_program("java")
            jar, jar_sha256 = _tlc_jar()
            env["VKIT_TLA2TOOLS_JAR"] = str(jar)
            _tlc_cases(cli, env, jar_sha256)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"formal CI failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
