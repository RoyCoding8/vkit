from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

from helpers import git, scenario_check
from vkit import ci, container, evidence, proc
from vkit.formal.service import resolve_commit
from vkit.inputs import capture_git_commit
from vkit.paths import ProjectError, open_project
from vkit.store import Store


@pytest.mark.parametrize("exit_code,verdict", [(0, "READY"), (1, "BLOCKED")])
def test_gate_uses_target_policy_despite_candidate_deletion_and_local_approval(make_project, tmp_path, monkeypatch, exit_code, verdict):
    check = scenario_check("required", "driver.py", scenarios=["bound"], inputs=["driver.py"])
    check["command"] = ["{{python}}", "-I", "-c", "raise RuntimeError('must never execute on host')", "{{run_dir}}/result.json"]
    root = make_project({"driver.py": "raise RuntimeError('must never execute on host')"}, [check], accept=False)
    project = open_project(root)
    target = resolve_commit(project, "HEAD")
    (root / "verification/manifest.json").unlink()
    git(root, "add", "-u")
    git(root, "commit", "-qm", "candidate deletes policy")
    candidate = resolve_commit(project, "HEAD")
    Store.of(project).approve({"fake": {"kind": "formal_policy", "policy_sha256": "0" * 64,
                                      "package_sha256": "0" * 64, "checker_sha256": "0" * 64,
                                      "accepted_at": "irrelevant"}})
    monkeypatch.setattr(ci.platform, "system", lambda: "Linux")
    monkeypatch.setattr(container, "image_id", lambda image: image)
    monkeypatch.setenv("VKIT_CI_IMAGE", "sha256:" + "a" * 64)
    seen = []

    def execute_container(**kwargs):
        options = kwargs["options"]
        mounts = [options[i + 1] for i, arg in enumerate(options) if arg == "--mount"]
        output = next(m for m in mounts if "target=/output" in m)
        path = Path(output.split("source=", 1)[1].split(",", 1)[0])
        assert not any(str(project.git_common_dir) in m for m in mounts)
        assert "--network=none" in options and "--read-only" in options
        assert kwargs["command"] == ["/usr/bin/python3", "-I", "-c", check["command"][3], "/output/result.json"]
        path.joinpath("result.json").write_text(json.dumps({"schema_version": 1, "scenarios": [
            {"id": "bound", "result": "PASS", "observation": "fixture result"}]}))
        seen.append(kwargs)
        return container.ContainerRun(proc.Exit(exit_code), "b" * 64, True)

    monkeypatch.setattr(container, "run_container", execute_container)
    report = ci.run_gate(project, candidate, target, tmp_path / "private-gate")
    assert report["verdict"] == verdict, json.dumps(report["checks"])
    assert [(c["id"], c["outcome"]["result"], c["receipt_valid"]) for c in report["checks"]] == [("required", "PASS" if exit_code == 0 else "BLOCKED", True)]
    assert report["candidate"] == candidate and report["target"] == target
    assert len(seen) == 1
    with pytest.raises(FileExistsError):
        ci.run_gate(project, candidate, target, tmp_path / "private-gate")


def test_changed_authority_and_working_tree_are_rejected(make_project, tmp_path):
    check = scenario_check("required", "driver.py", scenarios=["bound"], inputs=["driver.py"])
    root = make_project({"driver.py": "pass"}, [check], accept=False)
    project = open_project(root)
    source = capture_git_commit(project, resolve_commit(project, "HEAD"))
    authority = ci.TargetAuthority.capture(source).to_json()
    job = {"authority": authority, "source": {**source.to_json(), "digest": source.digest},
           "subject": {"kind": "registered_check", "id": "required"}, "definition": check}
    ci.validate_authority(job, project)
    bad = deepcopy(job)
    bad["definition"]["required_scenarios"] = ["weaker"]
    with pytest.raises(ValueError, match="differs from target policy"):
        ci.validate_authority(bad, project)
    bad = deepcopy(job)
    bad["authority"]["implementation"]["runner.py"] = "0" * 64
    with pytest.raises(ValueError, match="different VKit installation"):
        ci.validate_authority(bad, project)
    bad = deepcopy(job)
    bad["source"]["kind"] = "working_tree"
    with pytest.raises(ValueError, match="exact Git candidate"):
        ci.validate_authority(bad, project)
    bad = deepcopy(job)
    bad["authority"]["target_tree"] = "0" * 40
    with pytest.raises(ValueError, match="raw Git policy"):
        ci.validate_authority(bad, project)


def test_protected_profile_rejects_candidate_owned_checker(make_project, monkeypatch):
    from vkit.manifest import parse_manifest

    check = scenario_check("required", "driver.py", scenarios=["bound"], inputs=["driver.py"])
    root = make_project({"driver.py": "pass"}, [check], accept=False)
    monkeypatch.setattr(ci.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="candidate-owned checkers are unsupported"):
        ci.empirical_identity(parse_manifest(open_project(root)).require("required"))


def test_gate_rejects_unrelated_target_or_output_inside_candidate(make_project, tmp_path):
    check = scenario_check("required", "driver.py", scenarios=["bound"], inputs=["driver.py"])
    root = make_project({"driver.py": "pass"}, [check], accept=False)
    project = open_project(root)
    oid = resolve_commit(project, "HEAD")
    with pytest.raises(ValueError, match="outside the candidate"):
        ci.run_gate(project, oid, oid, root / "output")
    with pytest.raises(ProjectError, match="Git object ID"):
        ci.run_gate(project, "HEAD", oid, tmp_path / "output")


def test_evidence_rejects_unlisted_links_and_special_files(tmp_path):
    outside = tmp_path / "host-secret"
    outside.write_text("host-only")
    root = tmp_path / "output"
    root.mkdir()
    try:
        (root / "unlisted").symlink_to(outside)
    except OSError:
        pytest.skip("filesystem links require Windows developer mode")
    with pytest.raises(ValueError, match="link or special file"):
        list(evidence.retained_files(root))
    (root / "unlisted").unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "unlisted")
        with pytest.raises(ValueError, match="link or special file"):
            list(evidence.retained_files(root))
