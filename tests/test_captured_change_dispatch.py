from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from helpers import git
from test_paired import _git_output, _repository

from vkit import intent as intent_module
from vkit import rust_relational as rust_relational_module
from vkit.change_analysis import _run_prepared_change_analysis
from vkit.inputs import capture_git_commit
from vkit.intent import _bind_captured_change_request
from vkit.paths import open_project
from vkit.rust_relational import (
    _prepare_captured_rust_relational,
    prepare_rust_relational,
)

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_rust")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def convert(value):\n    return value\n", "observed_conformance"),
        (
            "def convert(value):\n    return 2 if value == 0 else value\n",
            "observed_violation",
        ),
    ],
)
def test_prepared_analysis_uses_captured_pair_after_checkout_edit(
    tmp_path, monkeypatch, source, expected
):
    root, target, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
        candidate_source=source,
    )
    project = open_project(root)
    base = capture_git_commit(project, target)
    candidate = capture_git_commit(project, request["candidate_oid"])

    def no_recapture(*_args, **_kwargs):
        pytest.fail("prepared analysis tried to capture a mutable Git ref")

    monkeypatch.setattr(intent_module, "capture_git_commit", no_recapture)
    bound = _bind_captured_change_request(
        project, request, base, candidate, trusted_target_oid=target
    )
    captured_source = next(
        file.content for file in candidate.files if file.path == "candidate.py"
    )
    (root / "candidate.py").write_text(
        'raise RuntimeError("live checkout must not be executed")\n', encoding="utf-8"
    )

    result = _run_prepared_change_analysis(
        bound, base, candidate, output_dir=tmp_path / "analysis", repetitions=1
    )

    assert result.report["status"] == expected
    assert result.bound.candidate_capture_sha256 == candidate.digest
    assert result.report["source"]["candidate_capture_sha256"] == candidate.digest
    assert (
        result.paired_execution.report["source"]["candidate_capture_sha256"]
        == candidate.digest
    )
    assert captured_source != (root / "candidate.py").read_bytes()


def test_captured_request_rejects_wrong_request_target_and_commit_tree(tmp_path):
    root, target, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    project = open_project(root)
    base = capture_git_commit(project, target)
    candidate = capture_git_commit(project, request["candidate_oid"])

    with pytest.raises(ValueError, match="request base must equal"):
        _bind_captured_change_request(
            project, request, base, candidate, trusted_target_oid=candidate.commit
        )
    with pytest.raises(ValueError, match="captured source identities"):
        _bind_captured_change_request(
            project,
            {**request, "candidate_oid": base.commit},
            base,
            candidate,
            trusted_target_oid=target,
        )
    with pytest.raises(ValueError, match="commit metadata"):
        _bind_captured_change_request(
            project,
            request,
            base,
            replace(candidate, tree=base.tree),
            trusted_target_oid=target,
        )


def test_captured_request_rejects_candidate_semantic_artifact_drift(tmp_path):
    root, target, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    path = root / "verification/semantics/codec.json"
    git(root, "checkout", "--detach", request["candidate_oid"])
    path.write_bytes(b'{"schema_version":1,"kind":"changed"}\n')
    git(root, "add", "verification/semantics/codec.json")
    git(root, "commit", "-qm", "candidate changes accepted codec")
    request["candidate_oid"] = _git_output(root, "rev-parse", "HEAD")

    project = open_project(root)
    base = capture_git_commit(project, target)
    candidate = capture_git_commit(project, request["candidate_oid"])
    with pytest.raises(ValueError, match="candidate codec artifact differs"):
        _bind_captured_change_request(
            project, request, base, candidate, trusted_target_oid=target
        )


def test_captured_rust_preparation_preserves_program_bytes_after_checkout_edit(
    tmp_path, monkeypatch
):
    root, target, request = _repository(
        tmp_path,
        language="rust",
        candidate_delta=1,
        outputs={"0": ["0", "1", "2"], "1": ["1", "2"]},
    )
    project = open_project(root)
    original = prepare_rust_relational(project, request, trusted_target_oid=target)
    expected_source = next(
        file.content for file in original.candidate.files if file.path == "candidate.rs"
    )
    (root / "candidate.rs").write_text(
        'compile_error!("live checkout must not be read");\n', encoding="utf-8"
    )

    def no_recapture(*_args, **_kwargs):
        pytest.fail("captured Rust preparation tried to capture a mutable Git ref")

    monkeypatch.setattr(intent_module, "capture_git_commit", no_recapture)
    monkeypatch.setattr(rust_relational_module, "_prepare_change_request", no_recapture)
    prepared = _prepare_captured_rust_relational(
        original.bound, original.base, original.candidate
    )

    assert prepared.harness == original.harness
    assert prepared.to_json() == original.to_json()
    assert (
        prepared.candidate_profile.source_sha256
        == hashlib.sha256(expected_source).hexdigest()
    )
    assert (
        next(
            file.content
            for file in prepared.candidate.files
            if file.path == "candidate.rs"
        )
        == expected_source
    )


def test_captured_rust_preparation_rejects_mismatched_candidate_capture(tmp_path):
    root, target, request = _repository(
        tmp_path,
        language="rust",
        candidate_delta=1,
        outputs={"0": ["0", "1", "2"], "1": ["1", "2"]},
    )
    original = prepare_rust_relational(
        open_project(root), request, trusted_target_oid=target
    )

    with pytest.raises(ValueError, match="captured source identities differ"):
        _prepare_captured_rust_relational(original.bound, original.base, original.base)
