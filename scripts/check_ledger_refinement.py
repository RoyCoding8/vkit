"""Check the ledger contract across native and split-column representations."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from vkit.formal import ledger
from vkit.formal.policy import FormalComponent, LedgerBindings
from vkit.inputs import canonical_digest
from vkit.nowindow import hidden_window

ROOT = Path(__file__).resolve().parents[1]


def component(*, columns: bool = False) -> FormalComponent:
    package = ledger.load_package()
    prefix = "columns::" if columns else ""
    return FormalComponent(
        "refinement", package.identity, ledger.CONTRACT, "candidate_api.rs", ledger.PROFILE, 3,
        LedgerBindings(**{name: prefix + name for name in
                          ("new", "reserve", "release", "reserve_batch", "apply_command", "run_commands")}),
        True, "columns::Columns" if columns else None,
    )


def combined_source(policy: FormalComponent, candidate: str) -> str:
    model = (ledger.load_package().root / "ledger.rs").read_text(encoding="utf-8")
    candidate = candidate.replace("extern crate ledger;", "").replace("ledger::ledger_api::", "crate::ledger_api::")
    expected = ledger.render_expected(policy)
    expected = expected.replace("extern crate ledger;", "").replace("extern crate candidate_api;", "use crate::candidate_api;")
    expected = expected.replace("ledger::ledger_api::", "crate::ledger_api::")
    return model + "\npub mod candidate_api {\n" + candidate + "\n}\nmod expected {\n" + expected + "\n}\n"


def preflight(verus: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    package = ledger.load_package()
    columns = (ROOT / "examples/ledger-refinement/columns.rs").read_text(encoding="utf-8")
    native = (ROOT / "examples/verus-qualification/candidate_api.rs").read_text(encoding="utf-8")
    broken = columns.replace("owner: input.values[i].0", "owner: 0", 1)
    if broken == columns:
        raise ValueError("refinement negative control no longer matches")
    cases = []
    for name, policy, source, should_pass in (
        ("native", component(), native, True),
        ("columns", component(columns=True), columns, True),
        ("broken_conversion", component(columns=True), broken, False),
    ):
        text = combined_source(policy, source)
        path = output / f"{name}.rs"
        path.write_text(text, encoding="utf-8", newline="\n")
        log_path = output / f"{name}.log"
        with log_path.open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    [str(verus), str(path), "--no-cheating", "--no-external-by-default",
                     "--num-threads", "2", "--triggers-mode", "silent"],
                    stdout=log, stderr=subprocess.STDOUT, timeout=90, **hidden_window(),
                )
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = None
        log = log_path.read_text(encoding="utf-8", errors="replace")
        matched = (code == 0 and bool(re.search(r"verification results:: [1-9]\d* verified, 0 errors", log))
                   if should_pass else code not in (None, 0) and bool(re.search(
                       r"verification results:: \d+ verified, [1-9]\d* errors", log)))
        cases.append({"name": name, "expected": "verified" if should_pass else "proof_rejection",
                      "matched": matched, "returncode": code, "log": log_path.name,
                      "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    report = {"passed": all(case["matched"] for case in cases), "method": "same_crate_preflight",
              "scope": "required_api; separate-crate product qualification is required",
              "package": vars(package.identity),
              "verus_sha256": hashlib.sha256(verus.read_bytes()).hexdigest(), "cases": cases}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def qualify_product(directory: Path) -> dict:
    from qualify_ledger_reuse import approve, project_for, runtime_fixture
    from vkit import evidence
    from vkit.formal import verus
    from vkit.formal.policy import GitCommit, VerificationRequest
    from vkit.runner import execute, prepare_formal
    from vkit.store import Store

    policy = component(columns=True)
    identity = verus.resolve_execution(policy)
    source = (ROOT / "examples/ledger-refinement/columns.rs").read_text(encoding="utf-8")
    broken = source.replace("owner: input.values[i].0", "owner: 0", 1)
    if broken == source:
        raise ValueError("refinement negative control no longer matches")
    cases = []
    for name, text, expected in (("columns", source, "PASS"), ("broken_conversion", broken, "BLOCKED")):
        project, commit = project_for(directory / name / "project", policy, text)
        store = Store(project.state_root)
        approve(store, policy, identity)
        record = prepare_formal(project, VerificationRequest(policy.id, GitCommit(commit)), policy,
                                canonical_digest(identity["semantic"]), store=store)
        record = execute(project, record["run_id"], store=store)
        valid = evidence.validate_record(record, store.run_dir(record["run_id"]))
        actual = record["outcome"]
        if (not valid or actual["result"] != expected
                or (expected == "BLOCKED" and actual.get("reason") != "formal_obligation")):
            raise RuntimeError(f"refinement qualification failed for {name}: {actual}")
        item = {"name": name, "job_id": record["job_id"], "run_id": record["run_id"],
                "outcome": actual, "receipt_valid": valid, "claim_outcome": record["claim_outcome"]}
        if name == "columns":
            fixture = json.loads((ROOT / "examples/ledger-reuse/fixtures.json").read_text())[0]
            fixture["namespace"] = "columns"
            fixture["id"] = "columns"
            item["runtime"] = runtime_fixture(fixture, policy, store.run_dir(record["run_id"]) / "formal-output",
                                               identity, directory / "runtime")
        cases.append(item)
    report = {"passed": True, "scope": "required_api", "assurance": "trusted_engine",
              "representation": policy.representation,
              "limitations": ["ordered slot abstraction retained", "verified conversions cost O(N)",
                              "resource aborts remain possible", "no concurrency guarantee"], "cases": cases}
    (directory / "refinement.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = preflight(args.verus.resolve(), args.output.resolve())
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
