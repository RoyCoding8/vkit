"""Pure qualification results and scoped semantic/package-change reports."""
from __future__ import annotations

from typing import Any

_REJECTION_DIAGNOSTICS = frozenset({"proof_rejection", "trust_escape_rejection", "missing_symbol"})
_SEMANTIC_RESULTS = frozenset({"verified_equivalence", "checked_counterexample", "inconclusive"})

HISTORICAL_POSITIVE_LIMIT_FIX = {
    "commit": "48ea7b7260d6ac7fe77474ae2fdac72fb9fb4cc5",
    "source_path": "formal/packages/resource_ledger/Model.v",
    "source_sha256": "4ef80555b3834bed3e9a5bbd8b581fdb0d2315233d77d53586523ba2703c5070",
    "change": "max_records and batch_bound changed from nat to positive",
    "reconstruction": "Verus shared-vocabulary zero-bound control; not a replay of the Rocq proof",
}


def classify_mutation(*, returncode: int | None, timed_out: bool, diagnostic: str | None,
                      evidence_present: bool, infrastructure_error: bool = False) -> dict[str, str]:
    if timed_out:
        return {"status": "inconclusive", "reason": "timeout"}
    if infrastructure_error or returncode is None:
        return {"status": "inconclusive", "reason": "infrastructure_failure"}
    if not evidence_present:
        return {"status": "inconclusive", "reason": "missing_output"}
    if returncode == 0:
        if diagnostic == "verified":
            return {"status": "survived", "reason": "verifier_accepted_mutation"}
        return {"status": "inconclusive", "reason": "success_without_verification_evidence"}
    if diagnostic in _REJECTION_DIAGNOSTICS:
        return {"status": "detected_rejection", "reason": diagnostic}
    return {"status": "inconclusive", "reason": "unclassified_tool_failure"}


def mutation_control(*, name: str, mutation_sha256: str, package_identity_sha256: str,
                     tool_identity_sha256: str, returncode: int | None, timed_out: bool,
                     diagnostic: str | None, evidence_present: bool,
                     infrastructure_error: bool = False) -> dict[str, Any]:
    return {"name": name, "mutation_sha256": mutation_sha256,
            "package_identity_sha256": package_identity_sha256,
            "tool_identity_sha256": tool_identity_sha256,
            **classify_mutation(returncode=returncode, timed_out=timed_out,
                                diagnostic=diagnostic, evidence_present=evidence_present,
                                infrastructure_error=infrastructure_error)}


def package_snapshot(*, identity: dict[str, str], assets: dict[str, str],
                     obligations: list[str] | tuple[str, ...],
                     trust_identity: dict[str, Any]) -> dict[str, Any]:
    return {"identity": identity, "assets": dict(sorted(assets.items())),
            "obligations": sorted(set(obligations)),
            "trust_identity": trust_identity}


def normalize_package_snapshot(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("package snapshot must be a JSON object")
    snapshot = document.get("package_identity", document)
    if not isinstance(snapshot, dict) or not all(key in snapshot for key in
                                                  ("identity", "assets", "obligations", "trust_identity")):
        raise ValueError("package snapshot requires identity, assets, obligations, and trust_identity")
    if (not isinstance(snapshot["identity"], dict) or not isinstance(snapshot["assets"], dict)
            or not all(isinstance(name, str) and isinstance(value, str)
                       for name, value in snapshot["assets"].items())
            or not isinstance(snapshot["obligations"], list)
            or not all(isinstance(item, str) for item in snapshot["obligations"])
            or not isinstance(snapshot["trust_identity"], dict)):
        raise ValueError("package snapshot fields have invalid types")
    return package_snapshot(identity=snapshot["identity"], assets=snapshot["assets"],
                            obligations=snapshot["obligations"],
                            trust_identity=snapshot["trust_identity"])


def compare_package_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old_assets, new_assets = before["assets"], after["assets"]
    old_obligations, new_obligations = set(before["obligations"]), set(after["obligations"])
    return {
        "assets_added": sorted(new_assets.keys() - old_assets.keys()),
        "assets_removed": sorted(old_assets.keys() - new_assets.keys()),
        "assets_changed": sorted(name for name in old_assets.keys() & new_assets.keys()
                                  if old_assets[name] != new_assets[name]),
        "obligations_added": sorted(new_obligations - old_obligations),
        "obligations_removed": sorted(old_obligations - new_obligations),
        "package_identity_changed": before["identity"] != after["identity"],
        "trust_identity_changed": before["trust_identity"] != after["trust_identity"],
        "semantic_relation": "unknown",
    }


def semantic_check_record(*, name: str, status: str, predicates: dict[str, str], scope: str,
                          proof_source_sha256: str, package_identity_sha256: str,
                          tool_identity_sha256: str, witness: dict[str, Any] | None = None,
                          provenance: dict[str, str] | None = None) -> dict[str, Any]:
    if status not in _SEMANTIC_RESULTS:
        raise ValueError("unsupported semantic check result")
    if not predicates or not all(isinstance(key, str) and isinstance(value, str)
                                 for key, value in predicates.items()):
        raise ValueError("semantic check predicates must name exact predicate text")
    if status == "checked_counterexample" and witness is None:
        raise ValueError("a checked counterexample requires its concrete witness")
    result = {
        "name": name,
        "status": status,
        "predicates": dict(predicates),
        "scope": scope,
        "proof_source_sha256": proof_source_sha256,
        "package_identity_sha256": package_identity_sha256,
        "tool_identity_sha256": tool_identity_sha256,
    }
    if witness is not None:
        result["witness"] = witness
    if provenance is not None:
        result["provenance"] = dict(provenance)
    return result
