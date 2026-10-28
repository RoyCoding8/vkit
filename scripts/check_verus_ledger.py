"""Compare the pinned Verus ledger proof with deliberately incorrect bodies."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from vkit.formal.ledger import CONTRACT, PACKAGE_ID, PROFILE, VERSION
from vkit.formal.qualification import (
    HISTORICAL_POSITIVE_LIMIT_FIX,
    compare_package_snapshots,
    mutation_control,
    normalize_package_snapshot,
    package_snapshot,
    semantic_check_record,
)
from vkit.nowindow import hidden_window

ROOT = Path(__file__).resolve().parents[1]

PACKAGE = ROOT / "src/vkit/formal/packages/resource_ledger"
PIN_PATH = PACKAGE / "upstream.json"
SOURCE = PACKAGE / "ledger.rs"
SMOKE = ROOT / "examples/verus-qualification/smoke.rs"
ADEQUACY_FIXTURES = ROOT / "examples/verus-qualification"
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


def archive_files(archive: Path, pin: dict) -> dict[str, str]:
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != pin["archives"].get(archive.name):
            raise ValueError("archive does not match its official pin")
    files, roots = {}, set()
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            if item.is_dir():
                continue
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
                raise ValueError("archive contains an unsafe path")
            if item.external_attr >> 16 & 0o170000 not in {0, stat.S_IFREG}:
                raise ValueError("archive contains a non-regular tool")
            roots.add(path.parts[0])
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative in files:
                raise ValueError("archive contains duplicate paths")
            with bundle.open(item) as stream:
                files[relative] = hashlib.file_digest(stream, "sha256").hexdigest()
    if len(roots) != 1:
        raise ValueError("archive has an unexpected root layout")
    return files


def inventory_digest(files: dict[str, str]) -> str:
    return digest(json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def run(command: list[str], output: Path, cwd: Path) -> dict:
    start = time.monotonic()
    env = os.environ.copy()
    env.pop("VERUS_Z3_PATH", None)
    with output.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                                       **hidden_window())
        except OSError as exc:
            return {"command": command, "returncode": None,
                    "seconds": round(time.monotonic() - start, 3), "log": output.name,
                    "error": f"{type(exc).__name__}: {exc}"}
        with process:
            try:
                code = process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=log, stderr=subprocess.STDOUT, check=False,
                                   **hidden_window())
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
    hashes = archive_files(archive, pin)
    if pin.get("expanded_files", {}).get(archive.name) != inventory_digest(hashes):
        raise ValueError("expanded-file pin differs from the official archive")
    for relative, expected_hash in hashes.items():
        path = tool_root / relative
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != expected_hash:
            raise ValueError(f"tool file differs from the pinned archive: {relative}")
    return {"archive_sha256": expected, "version": version, "files": hashes}


def check_binding(exe: Path, captured: Path, out: Path, report: dict,
                  package_identity_sha256: str, tool_identity_sha256: str) -> bool:
    root = out / "binding"
    root.mkdir()
    fixtures = ROOT / "examples/verus-qualification"
    candidate = (fixtures / "candidate_api.rs").read_text(encoding="utf-8")
    template = (PACKAGE / "expected.rs.in").read_text(encoding="utf-8")
    expected = (fixtures / "expected.rs").read_text(encoding="utf-8")
    report["binding_template_sha256"] = digest(template.encode())
    report["binding_scope"] = "Six required_api bindings over the fixed checked ledger library; not arbitrary source or representation correspondence"
    report["binding_expected_sha256"] = digest(expected.encode())
    flags = ["--no-cheating", "--no-external-by-default", "--num-threads", "2"]

    def control(name: str, mutation_path: Path, result: dict, diagnostic: str | None,
                evidence_present: bool) -> dict:
        row = mutation_control(
            name=name, mutation_sha256=digest(mutation_path.read_bytes()),
            package_identity_sha256=package_identity_sha256,
            tool_identity_sha256=tool_identity_sha256,
            returncode=result["returncode"], timed_out=result["returncode"] is None and
            "error" not in result, diagnostic=diagnostic, evidence_present=evidence_present,
            infrastructure_error="error" in result,
        )
        report["mutation_controls"].append(row)
        return row

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

    def export_and_claim(name: str, source: str, *, expected_diagnostic: str | None = None) -> bool:
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
            control(f"{name}_export", candidate_path, export["result"], None,
                    export["result"].get("returncode") is not None and bool(export["log"].strip()))
            return False
        client = [str(expected_path), *imports,
                  "--import", f"candidate_api={directory / 'candidate_api.vir'}",
                  "--extern", f"candidate_api={directory / 'libcandidate_api.rlib'}", "-L", str(directory)]
        claim = verify(f"{name}_claim", client, directory,
                       missing_symbol=name[len("binding_"):-len("_missing_symbol")]
                       if expected_diagnostic == "compile_missing_symbol" else None)
        if expected_diagnostic is None:
            record(f"{name}_claim", claim)
            return claim["verified"]
        observed = ("proof" if claim["proof_rejection"] else
                    "compile_missing_symbol" if claim["symbol_rejection"] else "unexpected")
        accepted = observed == expected_diagnostic
        diagnostic = ("proof_rejection" if observed == "proof" else
                      "missing_symbol" if observed == "compile_missing_symbol" else
                      "verified" if claim["verified"] else None)
        control(f"{name}_claim", candidate_path, claim["result"], diagnostic,
                claim["result"].get("returncode") is not None and bool(claim["log"].strip()))
        report["checks"].append({
            "name": f"{name}_claim",
            "expected_diagnostic": expected_diagnostic,
            "observed_diagnostic": diagnostic or "unclassified",
            "accepted_qualification_diagnostic": accepted,
            "qualification_status": report["mutation_controls"][-1]["status"],
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
                                       expected_diagnostic=rejection) and success
    return success


def check_specification_adequacy(exe: Path, out: Path, binding_root: Path, report: dict) -> bool:
    root = out / "specification-adequacy"
    root.mkdir()
    identities = {
        "package_identity_sha256": report["package_identity_sha256"],
        "tool_identity_sha256": report["tool_identity_sha256"],
    }
    flags = ["--no-cheating", "--no-external-by-default", "--num-threads", "2"]
    fixtures = (
        {
            "name": "reserve_capacity_equivalence",
            "file": "reserve-admission.rs",
            "status": "verified_equivalence",
            "predicates": {
                "current": "amount <= state.capacity - state.used",
                "rewritten": "state.used as nat + amount as nat <= state.capacity as nat",
                "reserve_result_view": "Ok iff amount > 0, id absent, slots not full, and rewritten capacity predicate",
            },
            "scope": "reserve_result_view capacity admission and Ok branch for every valid LedgerView<N>",
        },
        {
            "name": "reserve_strict_bound_control",
            "file": "reserve-strict-bound.rs",
            "status": "checked_counterexample",
            "predicates": {
                "current": "amount <= state.capacity - state.used",
                "mutated": "state.used as nat + amount as nat < state.capacity as nat",
            },
            "scope": "exact-capacity reserve request in the shared ledger_api model",
            "witness": {
                "records": 2,
                "reserve_precondition": "N > 0 && valid_view(input.view())",
                "precondition_inhabited": True,
                "existing_entry": {"id": 1, "owner": 9, "amount": 7},
                "request": {"id": 2, "owner": 11, "amount": 3},
                "capacity": 10,
                "current_result": "Ok",
                "mutated_result": "rejected by strict bound",
            },
        },
        {
            "name": "historical_positive_limit_regression",
            "file": "historical-positive-limit.rs",
            "status": "checked_counterexample",
            "predicates": {
                "historical": "max_records : nat (zero admitted)",
                "fixed": "max_records : positive (zero excluded)",
                "shared_vocabulary_witness": "valid_view::<0>(LedgerView { slots: [], used: 0, capacity: 0 })",
            },
            "scope": "reconstructed zero-bound regression mapped to the current ledger slot bound",
            "witness": {"record_limit": 0, "valid_view": True, "historical_admits": True, "fixed_rejects": True},
            "provenance": HISTORICAL_POSITIVE_LIMIT_FIX,
        },
    )
    rows = []
    all_passed = True
    for fixture in fixtures:
        source = ADEQUACY_FIXTURES / fixture["file"]
        source_sha256 = digest(source.read_bytes())
        log_path = root / f"{fixture['name']}.log"
        result = run(
            [str(exe), "--crate-type=lib", str(source),
             "--import", f"ledger={binding_root / 'ledger.vir'}",
             "--extern", f"ledger={binding_root / 'libledger.rlib'}", "-L", str(binding_root), *flags],
            log_path, root,
        )
        log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        verified = result["returncode"] == 0 and bool(re.search(
            r"verification results:: [1-9]\d* verified, 0 errors", log))
        status = fixture["status"] if verified else "inconclusive"
        row = semantic_check_record(
            name=fixture["name"], status=status, predicates=fixture["predicates"],
            scope=fixture["scope"], proof_source_sha256=source_sha256, **identities,
            witness=fixture.get("witness") if verified else None,
            provenance=fixture.get("provenance"),
        )
        row["verifier_log"] = log_path.name
        row["verifier_log_sha256"] = digest(log_path.read_bytes()) if log_path.is_file() else None
        row["verifier_returncode"] = result["returncode"]
        if "error" in result:
            row["verifier_error"] = result["error"]
        rows.append(row)
        report["checks"].append({
            "name": fixture["name"], "verified": verified,
            "source_sha256": source_sha256, **result,
        })
        all_passed = all_passed and verified
    positive_source = (ADEQUACY_FIXTURES / "reserve-admission.rs").read_text(encoding="utf-8")
    negative_source = root / "false-capacity-equivalence.rs"
    marker = "<= state.capacity as nat"
    if positive_source.count(marker) != 1:
        raise ValueError("capacity-equivalence control no longer matches its checked source")
    negative_source.write_text(positive_source.replace(marker, "< state.capacity as nat"), encoding="utf-8")
    negative_log = root / "false-capacity-equivalence.log"
    result = run(
        [str(exe), "--crate-type=lib", str(negative_source),
         "--import", f"ledger={binding_root / 'ledger.vir'}",
         "--extern", f"ledger={binding_root / 'libledger.rlib'}", "-L", str(binding_root), *flags],
        negative_log, root,
    )
    log = negative_log.read_text(encoding="utf-8", errors="replace") if negative_log.is_file() else ""
    rejected = bool(re.search(r"verification results:: \d+ verified, [1-9]\d* errors", log))
    control = mutation_control(
        name="false_capacity_equivalence", mutation_sha256=digest(negative_source.read_bytes()),
        **identities, returncode=result["returncode"],
        timed_out=result["returncode"] is None and "error" not in result,
        infrastructure_error="error" in result,
        diagnostic="proof_rejection" if rejected else "verified" if result["returncode"] == 0 else None,
        evidence_present=bool(log.strip()),
    )
    control.update(verifier_log=negative_log.name,
                   verifier_log_sha256=digest(negative_log.read_bytes()) if negative_log.is_file() else None)
    report["semantic_controls"] = [control]
    all_passed = all_passed and control["status"] == "detected_rejection"
    report["semantic_checks"] = rows
    report["semantic_checks_passed"] = all_passed
    return all_passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verus-root", type=Path)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare-package", type=Path,
                        help="compare assets, obligations, and trust identity with a prior package snapshot or report")
    parser.add_argument("--pin-extracted", action="store_true", help="derive the compact expanded-file pin from the verified official archive")
    args = parser.parse_args()
    pin = json.loads(PIN_PATH.read_text())
    if args.pin_extracted:
        pin.setdefault("expanded_files", {})[args.archive.name] = inventory_digest(archive_files(args.archive, pin))
        PIN_PATH.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
        return 0
    if args.verus_root is None or args.output is None:
        parser.error("--verus-root and --output are required for qualification")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
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
        "tool_identity_sha256": digest(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()),
        "mutation_controls": [],
        "semantic_checks": [],
        "semantic_checks_passed": False,
        "specification_adequacy_passed": False,
        "checks": [],
    }
    template = (PACKAGE / "expected.rs.in").read_text(encoding="utf-8")
    obligations = ["resource-ledger:ledger_map_accounting"]
    obligations.extend(f"resource-ledger:{name}" for name in
                       re.findall(r"pub fn expected_(\w+)", template))
    assets = {path.name: digest(path.read_bytes()) for path in
              (SOURCE, PACKAGE / "expected.rs.in", PIN_PATH)}
    assets.update({f"qualification/{path.name}": digest(path.read_bytes()) for path in (
        ADEQUACY_FIXTURES / "reserve-admission.rs",
        ADEQUACY_FIXTURES / "reserve-strict-bound.rs",
        ADEQUACY_FIXTURES / "historical-positive-limit.rs",
    )})
    package = package_snapshot(
        identity={"id": PACKAGE_ID, "version": VERSION, "contract": CONTRACT, "profile": PROFILE},
        assets=assets,
        obligations=obligations,
        trust_identity={"pin_sha256": report["pin_sha256"],
                        "tool_identity_sha256": report["tool_identity_sha256"]},
    )
    report["package_identity"] = package
    report["package_identity_sha256"] = digest(
        json.dumps(package, sort_keys=True, separators=(",", ":")).encode())
    if args.compare_package is not None:
        try:
            baseline_document = json.loads(args.compare_package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read package snapshot {args.compare_package}: {exc}") from exc
        baseline = normalize_package_snapshot(baseline_document)
        report["package_change_review"] = compare_package_snapshots(baseline, package)
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
            diagnostic = ("trust_escape_rejection" if name == "assumed_proof" and
                          "assume/admit not allowed with --no-cheating" in log else
                          "proof_rejection" if re.search(
                              r"verification results:: \d+ verified, [1-9]\d* errors", log) else
                          "verified" if check["returncode"] == 0 and re.search(
                              r"verification results:: [1-9]\d* verified, 0 errors", log) else None)
            control = mutation_control(
                name=name, mutation_sha256=digest(mutated.read_bytes()),
                package_identity_sha256=report["package_identity_sha256"],
                tool_identity_sha256=report["tool_identity_sha256"],
                returncode=check["returncode"], timed_out=check["returncode"] is None and
                "error" not in check, diagnostic=diagnostic, evidence_present=bool(log.strip()),
                infrastructure_error="error" in check,
            )
            report["mutation_controls"].append(control)
            rejected = control["status"] == "detected_rejection"
            report["checks"].append({"name": name,
                                     "sensitivity_detected": rejected,
                                     "source_sha256": digest(mutated.read_bytes()),
                                     "qualification": control, **check})
            success = success and rejected
        report["binding_passed"] = check_binding(
            exe, captured, out, report, report["package_identity_sha256"],
            report["tool_identity_sha256"])
        success = success and report["binding_passed"]
        if report["binding_passed"]:
            report["specification_adequacy_passed"] = check_specification_adequacy(
                exe, out, out / "binding", report)
        else:
            report["specification_adequacy_passed"] = False
        success = report["specification_adequacy_passed"] and success
        compiled = run([str(exe), str(smoke), "--no-cheating", "--num-threads", "2",
                        "--compile", "-o", str(binary)], out / "compile.log", out)
        report["checks"].append({"name": "compiled_verified_source", **compiled})
        success = success and compiled["returncode"] == 0
        if compiled["returncode"] == 0:
            executed = run([str(binary)], out / "execution.log", out)
            report["checks"].append({"name": "compiled_execution", **executed})
            success = success and executed["returncode"] == 0 and (out / "execution.log").read_text().strip() == "VKIT_LEDGER_SMOKE_OK"
    report["mutation_controls_passed"] = bool(report["mutation_controls"]) and all(
        control["status"] == "detected_rejection" for control in report["mutation_controls"])
    report["comparison_passed"] = (success and report["mutation_controls_passed"]
                                    and report["semantic_checks_passed"])
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"comparison_passed": report["comparison_passed"],
                      "mutation_controls_passed": report["mutation_controls_passed"],
                      "semantic_checks_passed": report["semantic_checks_passed"],
                      "semantic_checks": report["semantic_checks"],
                      "mutation_controls": report["mutation_controls"],
                      "checks": report["checks"]}, indent=2))
    return 0 if report["comparison_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
