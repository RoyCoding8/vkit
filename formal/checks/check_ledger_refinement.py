"""Check the ledger contract across native and split-column representations."""
from __future__ import annotations

import json
from pathlib import Path
from vkit.formal import ledger
from vkit.formal.policy import FormalComponent, LedgerBindings
from vkit.core.inputs import canonical_digest

ROOT = Path(__file__).resolve().parents[2]


def component(*, columns: bool = False) -> FormalComponent:
    package = ledger.load_package()
    prefix = "columns::" if columns else ""
    return FormalComponent(
        "refinement", package.identity, ledger.CONTRACT, "candidate_api.rs", ledger.PROFILE, 3,
        LedgerBindings(**{name: prefix + name for name in
                          ("new", "reserve", "release", "reserve_batch", "apply_command", "run_commands")}),
        True, "columns::Columns" if columns else None,
    )


def qualify_product(directory: Path) -> dict:
    from qualify_ledger_reuse import approve, project_for, runtime_fixture
    from vkit.core import evidence
    from vkit.formal import verus
    from vkit.change.requests import GitCommit, PackageVerificationRequest
    from vkit.core.runner import execute, prepare_formal
    from vkit.core.store import Store

    policy = component(columns=True)
    identity = verus.resolve_execution(policy)
    source = (ROOT / "examples/ledger-refinement/columns.rs").read_text(encoding="utf-8")
    broken = source.replace("owner: input.values[i].0", "owner: 0", 1)
    if broken == source:
        raise ValueError("refinement negative control no longer matches")
    cases = []
    for name, text, expected in (("columns", source, "PASS"), ("broken_conversion", broken, "BLOCKED")):
        project, commit = project_for(directory / name / "project", policy, text)
        store = Store.of(project)
        approve(store, policy, identity)
        record = prepare_formal(project, PackageVerificationRequest(policy.claim, GitCommit(commit)), policy,
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
