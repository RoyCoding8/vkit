"""Run package-level P2 transfer and concurrent contract controls."""
from __future__ import annotations

import json
import re
from pathlib import Path


def qualify(
    verus: Path,
    archive: Path,
    tool_root: Path,
    output: Path,
    *,
    tool_identity: dict,
) -> dict:
    from check_verus_ledger import PACKAGE, ROOT, digest, run
    from vkit.formal import ledger

    version = tool_identity["version"]
    platform = version["platform"]
    if platform not in ({"os": "windows", "arch": "x86_64"}, {"os": "linux", "arch": "x86_64"}):
        raise ValueError("verified tool identity has an unsupported platform")
    executable = "verus.exe" if platform == {"os": "windows", "arch": "x86_64"} else "verus"
    if not tool_identity["archive_sha256"]:
        raise ValueError("verified tool identity has no archive digest")
    if verus.resolve() != (tool_root / executable).resolve():
        raise ValueError("Verus executable path differs from the verified tool root")

    fixtures = ROOT / "examples/verus-qualification"
    model = (PACKAGE / "ledger.rs").read_text(encoding="utf-8")
    transfer = (fixtures / "p2-transfer.rs").read_text(encoding="utf-8")
    concurrent = (fixtures / "p2-concurrent.rs").read_text(encoding="utf-8")
    positive = "\n".join((model, transfer, concurrent))
    transfer_good = """Err(error) => (
                    source,
                    destination,
                    TransferResult::DestinationFailed(error),
                ),"""
    transfer_bad = transfer_good.replace("DestinationFailed", "SourceFailed")
    reserve_good = "let (updated, result) = reserve(current, id, owner, amount);"
    reserve_bad = "let (updated, result) = (current, Ok(()));"
    if positive.count(transfer_good) != 1 or positive.count(reserve_good) != 1:
        raise ValueError("P2 mutation anchors must each occur exactly once")

    output.mkdir(parents=True, exist_ok=False)
    cases = []
    mutations = (
        ("positive", positive, None),
        ("broken_transfer_error_origin", positive.replace(transfer_good, transfer_bad, 1),
         "out.2 == transfer_model(source.view(), destination.view(), id, owner).1"),
        ("broken_concurrent_reserve_guarantee", positive.replace(reserve_good, reserve_bad, 1),
         "out.1@.1.slots == slots_after_reserve("),
    )
    for name, source, marker in mutations:
        source_path = output / f"{name}.rs"
        log_path = output / f"{name}.log"
        source_path.write_text(source, encoding="utf-8", newline="\n")
        result = run(
            [str(verus), str(source_path), "--no-cheating", "--no-external-by-default",
             "--num-threads", "2", "--triggers-mode", "silent"],
            log_path,
            ROOT,
        )
        log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        verified = result["returncode"] == 0 and bool(re.search(
            r"verification results:: [1-9]\d* verified, 0 errors", log,
        ))
        rejected = (
            result["returncode"] not in {None, 0}
            and "postcondition not satisfied" in log
            and marker is not None
            and marker in log
            and bool(re.search(r"verification results:: \d+ verified, [1-9]\d* errors", log))
        )
        cases.append({
            "name": name,
            "expected": "verified" if marker is None else "postcondition_rejection",
            "expected_rejection_marker": marker,
            "matched": verified if marker is None else rejected,
            **result,
            "source_sha256": digest(source_path.read_bytes()),
            "log_sha256": digest(log_path.read_bytes()) if log_path.exists() else None,
        })

    package = ledger.load_package()
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
            "ledger_rs_sha256": digest((PACKAGE / "ledger.rs").read_bytes()),
            "transfer_fixture_sha256": digest((fixtures / "p2-transfer.rs").read_bytes()),
            "concurrent_fixture_sha256": digest((fixtures / "p2-concurrent.rs").read_bytes()),
        },
        "verus": {
            "executable_sha256": digest(verus.read_bytes()),
            "archive": archive.name,
            "archive_sha256": tool_identity["archive_sha256"],
            "version": version,
        },
        "cases": cases,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
