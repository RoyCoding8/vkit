from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from helpers import git, scenario_check
from vkit import ci, container, proc
from vkit.formal.service import resolve_commit
from vkit.inputs import capture_git_commit
from vkit.paths import ProjectError, open_project
from vkit.store import Store


def test_gate_uses_target_policy_despite_candidate_deletion_and_local_approval(make_project, tmp_path, monkeypatch):
    check = scenario_check("required", "driver.py", scenarios=["bound"], inputs=["driver.py"])
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
        assert kwargs["command"] == ["/usr/bin/python3", "driver.py", "/output/result.json"]
        path.joinpath("result.json").write_text(json.dumps({"schema_version": 1, "scenarios": [
            {"id": "bound", "result": "PASS", "observation": "fixture result"}]}))
        seen.append(kwargs)
        return container.ContainerRun(proc.Exit(0), "b" * 64, True)

    monkeypatch.setattr(container, "run_container", execute_container)
    report = ci.run_gate(project, candidate, target, tmp_path / "private-gate")
    assert report["verdict"] == "READY", json.dumps(report["checks"])
    assert [(c["id"], c["outcome"]["result"], c["receipt_valid"]) for c in report["checks"]] == [("required", "PASS", True)]
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
    job = {"authority": authority, "source": source.to_json(),
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


def test_gate_rejects_unrelated_target_or_output_inside_candidate(make_project, tmp_path):
    check = scenario_check("required", "driver.py", scenarios=["bound"], inputs=["driver.py"])
    root = make_project({"driver.py": "pass"}, [check], accept=False)
    project = open_project(root)
    oid = resolve_commit(project, "HEAD")
    with pytest.raises(ValueError, match="outside the candidate"):
        ci.run_gate(project, oid, oid, root / "output")
    with pytest.raises(ProjectError, match="Git object ID"):
        ci.run_gate(project, "HEAD", oid, tmp_path / "output")
