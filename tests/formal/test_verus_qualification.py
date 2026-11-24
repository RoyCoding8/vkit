from __future__ import annotations

import pytest

from vkit.formal.qualification import (
    classify_mutation,
    compare_package_snapshots,
    mutation_control,
    normalize_package_snapshot,
    package_snapshot,
)


def test_mutation_is_detected_only_with_an_accepted_rejection_diagnostic():
    assert classify_mutation(returncode=1, timed_out=False, diagnostic="proof_rejection",
                             evidence_present=True) == {
        "status": "detected_rejection", "reason": "proof_rejection",
    }
    assert classify_mutation(returncode=0, timed_out=False, diagnostic="verified",
                             evidence_present=True) == {
        "status": "survived", "reason": "verifier_accepted_mutation",
    }


def test_timeout_missing_output_and_unknown_failure_are_inconclusive():
    assert classify_mutation(returncode=None, timed_out=True, diagnostic=None,
                             evidence_present=True) == {
        "status": "inconclusive", "reason": "timeout",
    }
    assert classify_mutation(returncode=None, timed_out=False, diagnostic=None,
                             evidence_present=False, infrastructure_error=True) == {
        "status": "inconclusive", "reason": "infrastructure_failure",
    }
    assert classify_mutation(returncode=2, timed_out=False, diagnostic=None,
                             evidence_present=False) == {
        "status": "inconclusive", "reason": "missing_output",
    }
    assert classify_mutation(returncode=2, timed_out=False, diagnostic=None,
                             evidence_present=True) == {
        "status": "inconclusive", "reason": "unclassified_tool_failure",
    }
    assert classify_mutation(returncode=0, timed_out=False, diagnostic=None,
                             evidence_present=True) == {
        "status": "inconclusive", "reason": "success_without_verification_evidence",
    }


def test_mutation_control_binds_digest_and_tool_and_package_identities():
    assert mutation_control(
        name="wrong_owner", mutation_sha256="mutation-digest",
        package_identity_sha256="package-digest", tool_identity_sha256="tool-digest",
        returncode=1, timed_out=False, diagnostic="proof_rejection", evidence_present=True,
    ) == {
        "name": "wrong_owner", "mutation_sha256": "mutation-digest",
        "package_identity_sha256": "package-digest", "tool_identity_sha256": "tool-digest",
        "status": "detected_rejection", "reason": "proof_rejection",
    }


def test_package_change_review_compares_assets_obligations_and_trust_identity():
    before = package_snapshot(
        identity={"id": "vkit.resource-ledger", "version": "0.1.0"},
        assets={"ledger.rs": "a", "expected.rs.in": "b"},
        obligations=("reserve", "release"), trust_identity={"verus": "tool-a"},
    )
    after = package_snapshot(
        identity={"id": "vkit.resource-ledger", "version": "0.2.0"},
        assets={"ledger.rs": "a2", "new.rs": "c"},
        obligations=("reserve", "apply"), trust_identity={"verus": "tool-b"},
    )

    assert compare_package_snapshots(before, after) == {
        "assets_added": ["new.rs"], "assets_removed": ["expected.rs.in"],
        "assets_changed": ["ledger.rs"], "obligations_added": ["apply"],
        "obligations_removed": ["release"], "trust_identity_changed": True,
        "package_identity_changed": True, "semantic_relation": "unknown",
    }


def test_package_snapshot_input_accepts_prior_report_and_rejects_incomplete_shape():
    snapshot = {
        "identity": {"id": "vkit.resource-ledger", "version": "0.1.0"},
        "assets": {"ledger.rs": "sha256-a"},
        "obligations": ["reserve"],
        "trust_identity": {"tool": "sha256-tool"},
    }

    assert normalize_package_snapshot({"package_identity": snapshot}) == snapshot
    with pytest.raises(ValueError, match="package snapshot requires identity, assets, obligations, and trust_identity"):
        normalize_package_snapshot({"assets": {}})
