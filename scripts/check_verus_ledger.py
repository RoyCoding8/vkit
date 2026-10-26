"""Compare the pinned Verus ledger proof with deliberately incorrect bodies."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/vkit/formal/packages/resource_ledger"
PIN_PATH = PACKAGE / "upstream.json"
SOURCE = PACKAGE / "ledger.rs"
SMOKE = ROOT / "examples/verus-qualification/smoke.rs"
MUTATIONS = {
    "lost_accounting": ("next.used = next.used + amount;", "next.used = amount;"),
    "duplicate_id": ("if entry.id == id {", "if false {"),
    "wrong_owner": ("if entry.owner != owner {", "if false {"),
    "capacity_bypass": (
        "if amount > ledger.capacity - ledger.used {\n        return",
        "if false {\n        return",
    ),
    "rejection_changes_state": (
        "return (ledger, Err(LedgerError::ZeroAmount));",
        "return (new(ledger.slots, ledger.capacity), Err(LedgerError::ZeroAmount));",
    ),
    "wrong_release_amount": ("return (next, Ok(entry.amount));", "return (next, Ok(0));"),
    "partial_batch_commit": (
        "ledger, BatchResponse::RequestFailed { index: 1, error },",
        "current, BatchResponse::RequestFailed { index: 1, error },",
    ),
    "wrong_batch_index": (
        "BatchResponse::RequestFailed { index: 1, error },",
        "BatchResponse::RequestFailed { index: 0, error },",
    ),
    "batch_bound_bypass": (
        "if count > 3 {\n        return (ledger, BatchResponse::TooManyRequests);",
        "if false {\n        return (ledger, BatchResponse::TooManyRequests);",
    ),
    "wrong_trace_response": (
        "responses[i] = response;",
        "responses[i] = Response::Reserved(Ok(()));",
    ),
    "assumed_proof": (
        "    let ledger = Ledger { slots, used: 0, capacity };",
        "    assume(false);\n    let ledger = Ledger { slots, used: 0, capacity };",
    ),
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(command: list[str], output: Path, cwd: Path) -> dict:
    start = time.monotonic()
    env = os.environ.copy()
    env.pop("VERUS_Z3_PATH", None)
    with output.open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                              stderr=subprocess.STDOUT, start_new_session=os.name != "nt") as process:
            try:
                code = process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=log, stderr=subprocess.STDOUT, check=False)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                code = None
    return {"command": command, "returncode": code,
            "seconds": round(time.monotonic() - start, 3), "log": output.name}


def tool_identity(archive: Path, tool_root: Path, pin: dict) -> dict:
    expected = pin["archives"].get(archive.name)
    if expected is None or digest(archive.read_bytes()) != expected:
        raise ValueError("archive does not match the pinned official release")
    version = json.loads((tool_root / "version.json").read_text())["verus"]
    if version["commit"] != pin["revision"] or version["version"] != pin["version"]:
        raise ValueError("tool version does not match the pinned release")
    hashes = {}
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            parts = Path(item.filename).parts
            if len(parts) != 2 or item.is_dir():
                continue
            path = tool_root / parts[1]
            if path.suffix not in {".exe", ".dll", ".so", ".rlib", ".vir", ".json", ""}:
                continue
            expected_hash = digest(bundle.read(item))
            if not path.is_file() or digest(path.read_bytes()) != expected_hash:
                raise ValueError(f"tool file differs from the pinned archive: {path.name}")
            hashes[path.name] = expected_hash
    return {"archive_sha256": expected, "version": version, "files": hashes}


def check_binding(exe: Path, captured: Path, out: Path, report: dict) -> bool:
    root = out / "binding"
    root.mkdir()
    fixtures = ROOT / "examples/verus-qualification"
    candidate = (fixtures / "candidate_api.rs").read_text(encoding="utf-8")
    template = (PACKAGE / "expected.rs.in").read_text(encoding="utf-8")
    expected = template.replace("__VKIT_RECORDS__", "3")
    for operation in ("new", "reserve", "release", "reserve_batch", "apply_command", "run_commands"):
        expected = expected.replace(f"__VKIT_BIND_{operation.upper()}__", operation)
    report["binding_template_sha256"] = digest(template.encode())
    report["binding_scope"] = "Six required_api bindings over the fixed checked ledger library; not arbitrary source or representation correspondence"
    report["binding_expected_sha256"] = digest(expected.encode())
    flags = ["--no-cheating", "--no-external-by-default", "--num-threads", "2"]

    def verify(name: str, args: list[str], cwd: Path, *, missing_symbol: str | None = None) -> dict:
        log_path = out / f"{name}.log"
        result = run([str(exe), "--crate-type=lib", *args, *flags], log_path, cwd)
        log = log_path.read_text(encoding="utf-8", errors="replace")
        proof_rejection = result["returncode"] not in {None, 0} and bool(re.search(
            r"verification results:: \d+ verified, [1-9]\d* errors", log))
        symbol_rejection = (missing_symbol is not None and result["returncode"] not in {None, 0}
                            and not proof_rejection and "candidate_api" in log
                            and missing_symbol.lower() in log.lower())
        verified = result["returncode"] == 0 and bool(re.search(
            r"verification results:: [1-9]\d* verified, 0 errors", log))
        return {"result": result, "log": log, "verified": verified,
                "proof_rejection": proof_rejection, "symbol_rejection": symbol_rejection}

    def record(name: str, outcome: dict) -> None:
        report["checks"].append({"name": name, **outcome["result"],
                                 "verified": outcome["verified"]})

    def function_bounds(text: str, api: str) -> tuple[int, int]:
        start = text.index(f"    pub fn {api}<")
        next_function = text.find("\n    pub fn ", start + 1)
        end = next_function if next_function >= 0 else text.rfind("\n}")
        return start, end

    def changed_candidate(api: str, mutation: str) -> str:
        text = candidate
        start, end = function_bounds(text, api)
        block = text[start:end]
        signature_end = block.index("\n    {")
        contract = block[:signature_end]
        if mutation == "weak_contract":
            contract, count = re.subn(
                r"        ensures\n.*", "        ensures true,",
                contract, count=1, flags=re.DOTALL,
            )
        elif mutation == "stronger_precondition":
            contract, count = re.subn(
                r"        requires [^\n]+,", "        requires false,", contract, count=1,
            )
        elif mutation == "missing_symbol":
            return text[:start] + text[end:]
        else:
            raise ValueError(f"unknown binding mutation: {mutation}")
        if count != 1:
            raise ValueError(f"could not mutate {api} for {mutation}")
        return text[:start] + contract + block[signature_end:end] + text[end:]

    def export_and_claim(name: str, source: str, *, expected_rejection: str | None = None) -> bool:
        directory = root / name
        directory.mkdir()
        candidate_path = directory / "candidate_api.rs"
        candidate_path.write_text(source, encoding="utf-8", newline="\n")
        expected_path = directory / "expected.rs"
        expected_path.write_text(expected, encoding="utf-8", newline="\n")
        report[f"{name}_candidate_sha256"] = digest(candidate_path.read_bytes())
        export = verify(f"{name}_export", [str(candidate_path), *imports, "--export",
                                             str(directory / "candidate_api.vir"), "--compile"], directory)
        record(f"{name}_export", export)
        if not export["verified"]:
            return False
        client = [str(expected_path), *imports,
                  "--import", f"candidate_api={directory / 'candidate_api.vir'}",
                  "--extern", f"candidate_api={directory / 'libcandidate_api.rlib'}", "-L", str(directory)]
        claim = verify(f"{name}_claim", client, directory,
                       missing_symbol=name[len("binding_"):-len("_missing_symbol")]
                       if expected_rejection == "compile_missing_symbol" else None)
        if expected_rejection is None:
            record(f"{name}_claim", claim)
            return claim["verified"]
        observed = ("proof" if claim["proof_rejection"] else
                    "compile_missing_symbol" if claim["symbol_rejection"] else "unexpected")
        accepted = observed == expected_rejection
        report["checks"].append({
            "name": f"{name}_claim",
            "expected_rejection": expected_rejection,
            "observed_rejection": observed,
            "rejected": accepted,
            **claim["result"],
        })
        return accepted

    ledger = verify("binding_ledger_export", [str(captured), "--export", str(root / "ledger.vir"),
                                              "--compile"], root)
    record("binding_ledger_export", ledger)
    if not ledger["verified"]:
        return False
    imports = ["--import", f"ledger={root / 'ledger.vir'}",
               "--extern", f"ledger={root / 'libledger.rlib'}", "-L", str(root)]
    success = export_and_claim("binding_positive", candidate)
    for api in ("new", "reserve", "release", "reserve_batch", "apply_command", "run_commands"):
        for mutation, rejection in (("weak_contract", "proof"),
                                    ("stronger_precondition", "proof"),
                                    ("missing_symbol", "compile_missing_symbol")):
            name = f"binding_{api}_{mutation}"
            success = export_and_claim(name, changed_candidate(api, mutation),
                                       expected_rejection=rejection) and success
    return success


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verus-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    pin = json.loads(PIN_PATH.read_text())
    identity = tool_identity(args.archive.resolve(), args.verus_root.resolve(), pin)
    source = SOURCE.read_text(encoding="utf-8")
    forbidden = r"\b(?:assume|admit|unsafe|exec_spec_unverified)\b|verifier\s*::\s*(?:external\w*|exec_allows_no_decreases_clause)|include!"
    if re.search(forbidden, source):
        raise ValueError("ledger source contains a forbidden trust escape")
    exe = args.verus_root.resolve() / ("verus.exe" if os.name == "nt" else "verus")
    captured = out / "ledger.rs"
    captured.write_text(source, encoding="utf-8", newline="\n")
    smoke = out / "smoke.rs"
    smoke.write_text(SMOKE.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    report = {
        "scope": "Verus comparison experiment, not product qualification",
        "assurance": "Verus/Rust/vstd/SMT trusted computing base; no independent kernel certificate",
        "product_policy_accepted": False,
        "operational_resource_bound": "not established",
        "source_sha256": digest(captured.read_bytes()),
        "unverified_runtime_test_sha256": digest(smoke.read_bytes()),
        "driver_sha256": digest(Path(__file__).read_bytes()),
        "pin_sha256": digest(PIN_PATH.read_bytes()),
        "toolchain": identity,
        "checks": [],
    }
    for name, command in {
        "verus_version": [str(exe), "--version"],
        "solver_version": [str(exe.parent / ("z3.exe" if os.name == "nt" else "z3")), "--version"],
        "rust_version": ["rustup", "run", identity["version"]["toolchain"].split()[0], "rustc", "--version"],
    }.items():
        result = run(command, out / f"{name}.log", out)
        report["checks"].append({"name": name, **result})
        if result["returncode"] != 0:
            raise RuntimeError(f"could not capture {name}")
    binary = out / ("ledger.exe" if exe.suffix else "ledger")
    check = run([str(exe), str(captured), "--no-cheating", "--no-external-by-default", "--num-threads", "2",
                 "-V", "axiom-usage-info", "--log", "call-graph", "--log-dir", str(out / "call-graph")],
                out / "verify.log", out)
    report["checks"].append({"name": "verified_body", **check})
    log = (out / "verify.log").read_text(encoding="utf-8", errors="replace")
    success = check["returncode"] == 0 and bool(re.search(r"verification results:: [1-9]\d* verified, 0 errors", log))
    if success:
        for name, (before, after) in MUTATIONS.items():
            if before not in source:
                raise ValueError(f"mutation no longer matches the checked implementation: {name}")
            mutated = out / f"{name}.rs"
            mutated.write_text(source.replace(before, after, 1), encoding="utf-8", newline="\n")
            check = run([str(exe), str(mutated), "--no-cheating", "--no-external-by-default", "--num-threads", "2"],
                        out / f"{name}.log", out)
            log = (out / f"{name}.log").read_text(encoding="utf-8", errors="replace")
            rejected = check["returncode"] not in {None, 0} and (
                "assume/admit not allowed with --no-cheating" in log if name == "assumed_proof"
                else bool(re.search(r"verification results:: \d+ verified, [1-9]\d* errors", log))
            )
            report["checks"].append({"name": name, "rejected": rejected,
                                     "source_sha256": digest(mutated.read_bytes()), **check})
            success = success and rejected
        report["binding_passed"] = check_binding(exe, captured, out, report)
        success = success and report["binding_passed"]
        compiled = run([str(exe), str(smoke), "--no-cheating", "--num-threads", "2",
                        "--compile", "-o", str(binary)], out / "compile.log", out)
        report["checks"].append({"name": "compiled_verified_source", **compiled})
        success = success and compiled["returncode"] == 0
        if compiled["returncode"] == 0:
            executed = run([str(binary)], out / "execution.log", out)
            report["checks"].append({"name": "compiled_execution", **executed})
            success = success and executed["returncode"] == 0 and (out / "execution.log").read_text().strip() == "VKIT_LEDGER_SMOKE_OK"
    report["comparison_passed"] = success
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"comparison_passed": success, "checks": report["checks"]}, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
