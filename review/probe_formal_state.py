"""Report every formal-tools claim as VERIFIED or BLOCKED, with the evidence.

`formal/RESULTS.md` makes claims about four artifacts: a finite model check, a
Python mutation run, a Lean theorem check, and a TLA+ mutation run. A claim with
no receipt beside it reads as a result, and the receipts that do exist were being
read against a hash computed a different way on a different host.

This probe is the receipt of the receipts. For each artifact it recomputes the
digest the receipt recorded, decides whether the digest reproduces, and names the
toolchain state that decides whether a fresh run is possible. It runs no model
checker and downloads nothing. Exit 0 when every artifact is either VERIFIED
against a reproducing digest or BLOCKED with a stated reason, and 1 when an
artifact claims a result its receipt cannot support.

It also checks the one property every receipt shares. Three of the four state
the digest convention they were computed under, in `digest_normalization`. A
receipt that omits the field while its siblings carry it is drift, and the probe
fails. One receipt is excused by name, and the excuse is recomputed rather than
trusted, so it cannot become a blank cheque.

    python review/probe_formal_state.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "formal"))

from digest import canonical_sha256  # noqa: E402

RESULTS = ROOT / "formal" / "results"

#: Receipts that do not state the digest convention they were computed under.
#: Each name carries the reason, so an absent field is a recorded decision
#: rather than a gap. `formal/RESULTS.md` says the same in prose.
#:
#: This is not an amnesty. `_convention_state` recomputes every digest of an
#: excused receipt and fails the probe if one does not reproduce, so an excuse
#: asserts that the receipt's digests are sound under the canonical form and is
#: checked on every run rather than believed once.
UNDECLARED_CONVENTION = {
    "OwnershipAcceptance-receipt.json": (
        "written 2026-09-30, before formal/digest.py and the digest_normalization "
        "field existed; both arrived in 6c92860. Its model_sha256 and "
        "config_sha256 match the canonical form and the committed git blob, so "
        "the receipt is sound. It is not regenerated and not hand-edited, "
        "because the TLC run it records cannot be repeated on a host with no "
        "JRE. See the digest section of formal/RESULTS.md."
    ),
}

#: (artifact, receipt file, {receipt key: file it digests}). One row per claim
#: RESULTS.md makes, so a new claim has to be added here to be checked at all.
CLAIMS = (
    ("TLC finite model check", "OwnershipAcceptance-receipt.json", {
        "model_sha256": "formal/tla/OwnershipAcceptance.tla",
        "config_sha256": "formal/tla/OwnershipAcceptance.cfg",
    }),
    ("Lean theorem check", "Acceptance-lean-receipt.json", {
        "module_sha256": "formal/lean/Acceptance.lean",
    }),
    ("Python core mutation run", "python-core-mutants-receipt.json", {
        "target_sha256": "src/vkit/tasks.py",
    }),
)


def _digest_checks(receipt: dict, files: dict[str, str]) -> dict[str, dict]:
    """Does each digest in the receipt still describe the file it names."""
    checks = {}
    for key, relative in files.items():
        recorded = receipt.get(key)
        path = ROOT / relative
        actual = canonical_sha256(path) if path.is_file() else None
        checks[relative] = {
            "recorded": recorded,
            "recomputed": actual,
            "reproduces": recorded is not None and recorded == actual,
        }
    return checks


def _convention_state() -> tuple[dict, list[str]]:
    """Does every receipt state the digest convention it was computed under.

    A receipt is a claim about bytes, and a digest is only checkable once you
    know which bytes were hashed. `digest_normalization` says so. A receipt that
    omits it leaves that unstated, so a reader on a CRLF checkout cannot tell
    whether a non-reproducing digest means the artifact changed or only the line
    endings did.

    This walks `formal/results/*receipt.json` rather than a hand-written list,
    so a receipt written by a new harness is checked because it exists. A field
    missing while siblings carry it is the drift worth catching: it means a
    harness changed its hashing without the receipt saying so.

    An excused receipt is not skipped, it is re-measured against the files
    `CLAIMS` names for it. The excuse claims the digests are sound under the
    canonical form, and that claim is only worth something if it is recomputed
    every run. An excuse for a receipt whose digests no longer reproduce would
    be stale, and is reported as a failure rather than honoured.
    """
    files_by_receipt = {receipt_name: files for _, receipt_name, files in CLAIMS}
    states: dict[str, dict] = {}
    failures: list[str] = []
    for path in sorted(RESULTS.glob("*receipt*.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        declared = receipt.get("digest_normalization")
        record: dict = {
            "declared": bool(declared),
            "digests": sorted(k for k in receipt if k.endswith("_sha256")),
        }
        if declared:
            record["state"] = "DECLARED"
            states[path.name] = record
            continue

        record["state"] = "FAIL"
        reason = UNDECLARED_CONVENTION.get(path.name)
        if reason is None:
            record["why"] = (
                "this receipt records no digest_normalization, so nothing says "
                "which form of the file its digests were taken over. Add the "
                "field to the harness in formal/ that writes it and rerun the "
                "harness, or name the receipt in UNDECLARED_CONVENTION with "
                "the reason it is excused."
            )
            failures.append(path.name)
            states[path.name] = record
            continue

        checks = _digest_checks(receipt, files_by_receipt.get(path.name, {}))
        if not checks:
            record["why"] = (
                f"{reason} No digest in it is named by CLAIMS, so the excuse "
                "cannot be re-measured and is refused rather than trusted."
            )
            failures.append(path.name)
            states[path.name] = record
            continue
        if not all(c["reproduces"] for c in checks.values()):
            stale = [name for name, c in checks.items() if not c["reproduces"]]
            record["why"] = (
                f"{reason} The excuse no longer holds: {', '.join(sorted(stale))} "
                "no longer digests to what the receipt recorded. The artifact "
                "changed after the run, so the receipt is stale and the excuse "
                "cannot be carried on its authority."
            )
            failures.append(path.name)
            states[path.name] = record
            continue
        record["state"] = "UNDECLARED_EXCUSED"
        record["why"] = reason
        record["remeasured"] = {
            name: c["recomputed"] for name, c in sorted(checks.items())
        }
        states[path.name] = record
    return states, failures


def _toolchain() -> dict:
    """What would a fresh run need, measured rather than assumed."""
    tools = ROOT / "tmp" / "formal-tools"
    jar = tools / "tla2tools.jar"
    return {
        "java": shutil.which("java"),
        "tla2tools_jar": str(jar) if jar.is_file() else None,
        "lean": shutil.which("lean"),
    }


def main() -> int:
    chain = _toolchain()
    conventions, convention_failures = _convention_state()
    report: dict[str, dict] = {
        "toolchain": chain,
        "digest_convention": conventions,
        "artifacts": {},
    }
    broken: list[str] = []

    for name, receipt_name, files in CLAIMS:
        path = RESULTS / receipt_name
        if not path.is_file():
            report["artifacts"][name] = {
                "state": "BLOCKED",
                "reason": f"no receipt at formal/results/{receipt_name}",
                "unblocked_by": "running the harness that writes that receipt",
            }
            continue

        receipt = json.loads(path.read_text(encoding="utf-8"))
        checks = _digest_checks(receipt, files)
        holds = all(c["reproduces"] for c in checks.values())
        record = {
            "receipt": f"formal/results/{receipt_name}",
            "receipt_status": receipt.get("status"),
            "tool": receipt.get("tool"),
            "tool_version": receipt.get("tool_version"),
            "finished_at": receipt.get("finished_at"),
            "digests": checks,
        }

        if receipt.get("status") == "PASS" and not holds:
            record["state"] = "BLOCKED"
            record["reason"] = (
                "the receipt claims PASS, but a digest it records does not "
                "reproduce against the file it names"
            )
            record["unblocked_by"] = (
                "rerunning the harness, or finding the revision the receipt was "
                "measured at"
            )
            broken.append(name)
        elif receipt.get("status") == "PASS":
            record["state"] = "VERIFIED"
            record["reason"] = "every digest in the receipt reproduces"
            record["scope"] = receipt.get("scope")
        else:
            record["state"] = "BLOCKED"
            record["reason"] = receipt.get("reason")
        report["artifacts"][name] = record

    # The TLA+ mutant run has no receipt of its own: run_mutants.py prints its
    # verdicts and writes nothing, so there was nothing here to check. It is
    # listed with the state that is honest rather than left out.
    report["artifacts"]["TLA+ mutation run"] = {
        "state": "BLOCKED",
        "receipt": None,
        "reason": (
            "formal/run_mutants.py writes no receipt, so its four results have "
            "nothing to verify against. It also cannot run here: "
            f"java={'present' if chain['java'] else 'absent'}, "
            f"tla2tools.jar={'present' if chain['tla2tools_jar'] else 'absent'}"
        ),
        "unblocked_by": (
            "a JRE and tla2tools.jar under tmp/formal-tools, and a receipt "
            "written by run_mutants.py"
        ),
    }

    broken.extend(
        f"{name} states no digest convention" for name in convention_failures
    )

    out = ROOT / "review" / "formal-state.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print("formal claim state")
    print("=" * 72)
    for name, record in report["artifacts"].items():
        print(f"{record['state']:>8}  {name}")
        if record.get("tool_version"):
            print(f"          tool: {record['tool_version']}")
        if record.get("finished_at"):
            print(f"          run:  {record['finished_at']}")
        print(f"          {record['reason']}")
        if record.get("unblocked_by"):
            print(f"          needs: {record['unblocked_by']}")

    print("=" * 72)
    print("digest convention declared per receipt")
    for name, record in sorted(conventions.items()):
        print(f"{record['state']:>21}  {name}")
        if record["state"] != "DECLARED":
            print(f"                        {record['why']}")

    print("=" * 72)
    print(f"written: {out.relative_to(ROOT).as_posix()}")
    if broken:
        print(f"\nFAILED: {len(broken)} receipt(s) claim a result they cannot support.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
