from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from helpers import scenario_check, vkit
from vkit.manifest import parse_manifest
from vkit.operations.reduction import ReductionRefused, reduce_failure
from vkit.paths import open_project
from vkit.runner import run_check
from vkit.store import Store


def failed_run(make_project, *, accept: bool = True, nested_source: bool = False) -> tuple[Path, str]:
    source_path = "pkg/subject.py" if nested_source else "subject.py"
    source_module = "pkg.subject" if nested_source else "subject"
    project = make_project({
        source_path: '"""A source file with removable material."""\nVALUE = 0\n\ndef unused():\n    return 42\n',
        "driver.py": (
            f"import json, sys\nfrom {source_module} import VALUE\n"
            "result = 'FAIL' if VALUE == 0 else 'PASS'\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as report:\n"
            "    json.dump({'schema_version': 1, 'scenarios': [{"
            "'id': 'regression', 'result': result, 'observation': f'value={VALUE}'"
            "}]}, report)\n"
        ),
        "helper.py": "VALUE = 0\n",
    }, [scenario_check("failed", "driver.py", scenarios=["regression"],
                       inputs=[source_path, "driver.py", "helper.py"])], accept=False)
    manifest = project / "verification" / "manifest.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["checks"][0]["subject"]["paths"] = [source_path]
    manifest.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    if accept:
        code, body = vkit(project, "accept", "--yes")
        assert code == 0, body
    opened = open_project(project)
    record = run_check(opened, parse_manifest(opened), "failed")
    return project, record["run_id"]


def test_reduction_refuses_inputs_that_changed(make_project):
    project, run_id = failed_run(make_project)
    (project / "subject.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(ReductionRefused, match="inputs changed"):
        reduce_failure(open_project(project), run_id, "subject.py")


def test_reduction_refuses_changed_check_definition(make_project):
    project, run_id = failed_run(make_project)
    manifest = project / "verification" / "manifest.json"
    raw = manifest.read_text(encoding="utf-8")
    manifest.write_text(raw.replace('"driver.py"', '"different.py"', 1), encoding="utf-8")

    with pytest.raises(ReductionRefused, match="definition changed"):
        reduce_failure(open_project(project), run_id, "subject.py")


def test_reduction_refuses_unapproved_check(make_project):
    project, run_id = failed_run(make_project, accept=False)

    with pytest.raises(ReductionRefused, match="approved"):
        reduce_failure(open_project(project), run_id, "subject.py")


@pytest.mark.parametrize("path", ["../outside.py", "/outside.py", "subject.py\\..\\outside.py"])
def test_reduction_refuses_path_escape(make_project, path):
    project, run_id = failed_run(make_project)

    with pytest.raises(ReductionRefused, match="repository-relative|inside the repository"):
        reduce_failure(open_project(project), run_id, path)


def test_reduction_refuses_run_id_path_escape(make_project):
    project, run_id = failed_run(make_project)

    with pytest.raises(ReductionRefused, match="run ID has an invalid format"):
        reduce_failure(open_project(project), "../" + run_id, "subject.py")


def test_reduction_refuses_source_outside_input_snapshot(make_project):
    project, run_id = failed_run(make_project)
    (project / "outside.py").write_text("VALUE = 0\n", encoding="utf-8")

    with pytest.raises(ReductionRefused, match="outside the recorded check inputs"):
        reduce_failure(open_project(project), run_id, "outside.py")


def test_reduction_refuses_the_approved_check_driver(make_project):
    project, run_id = failed_run(make_project)

    with pytest.raises(ReductionRefused, match="driver or required test"):
        reduce_failure(open_project(project), run_id, "driver.py")


def test_reduction_refuses_input_outside_check_subject(make_project):
    project, run_id = failed_run(make_project)

    with pytest.raises(ReductionRefused, match="outside the approved check subject"):
        reduce_failure(open_project(project), run_id, "helper.py")


def test_reduction_refuses_a_failure_not_present_in_the_recorded_artifact(make_project):
    project, run_id = failed_run(make_project)
    store = Store.of(open_project(project))
    record = store.record(run_id)
    record["outcome"]["scenarios"][0]["observation"] = "different failure"
    store.save(record)

    with pytest.raises(ReductionRefused, match="cannot be replayed"):
        reduce_failure(open_project(project), run_id, "subject.py")


def test_reduction_requires_finite_positive_timeout(make_project):
    project, run_id = failed_run(make_project)

    with pytest.raises(ValueError, match="finite and positive"):
        reduce_failure(open_project(project), run_id, "subject.py", timeout_seconds=float("inf"))


def test_reduction_reports_unavailable_without_pinned_engine(make_project, monkeypatch):
    project, run_id = failed_run(make_project)
    monkeypatch.delenv("VKIT_PERSES_JAR", raising=False)
    monkeypatch.delenv("VKIT_PERSES_SHA256", raising=False)
    before = (project / "subject.py").read_bytes()

    result = reduce_failure(open_project(project), run_id, "subject.py")

    assert result.status == "UNAVAILABLE"
    assert result.source_sha256 == hashlib.sha256(before).hexdigest()
    assert result.reduced_source is None
    assert (project / "subject.py").read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="Perses integration requires a POSIX runtime")
def test_real_perses_reduces_source_and_preserves_recorded_failure(make_project, tmp_path):
    if not os.environ.get("VKIT_PERSES_JAR") or not os.environ.get("VKIT_PERSES_SHA256"):
        pytest.skip("pinned Perses 2.7 runtime is not configured")
    project, run_id = failed_run(make_project, nested_source=True)
    source = project / "pkg" / "subject.py"
    original = source.read_bytes()

    result = reduce_failure(open_project(project), run_id, "pkg/subject.py", timeout_seconds=120)

    assert result.status == "REDUCED"
    assert result.perses_version and "2.7" in result.perses_version
    assert result.perses_sha256 == os.environ["VKIT_PERSES_SHA256"].lower()
    assert result.reduced_source is not None
    assert result.reduced_sha256 == hashlib.sha256(result.reduced_source.encode()).hexdigest()
    assert result.reduced_source == "VALUE = 0\n"
    assert len(result.reduced_source) < len(original)
    assert source.read_bytes() == original

    replay = tmp_path / "independent-replay"
    replay.mkdir()
    (replay / "pkg").mkdir()
    (replay / "pkg" / "subject.py").write_text(result.reduced_source, encoding="utf-8")
    (replay / "driver.py").write_bytes((project / "driver.py").read_bytes())
    artifact = tmp_path / "independent-report.json"
    subprocess.run([sys.executable, str(replay / "driver.py"), str(artifact)], cwd=replay, check=True, timeout=30)
    scenario = json.loads(artifact.read_text(encoding="utf-8"))["scenarios"][0]
    assert scenario == {"id": "regression", "result": "FAIL", "observation": "value=0"}
