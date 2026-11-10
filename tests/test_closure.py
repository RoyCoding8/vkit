from __future__ import annotations
import json
import textwrap
from pathlib import Path

from helpers import DRIVER, scenario_check, vkit
from vkit.evidence import deps_receipt, evaluator_closure, pin_evaluators
from vkit.manifest import parse_check_definition
from vkit.paths import Project

OUTSIDE = DRIVER + 'report([("ok", open(r"{root}/outside/secret.py").read().startswith("V"), "ok")])\n'
ENUMERATE = DRIVER + 'report([("ok", bool(__import__("os").listdir(r"{root}/outside")), "ok")])\n'
ENV_READ = DRIVER + 'report([("ok", __import__("os").environ.get("VKIT_ESCAPE_MARKER") is not None, "ok")])\n'
SUBPROCESS_PLAIN = DRIVER + textwrap.dedent('''
    import subprocess, sys
    done = subprocess.run([sys.executable, "-c", "open(r'{root}/outside/secret.py').read()"], capture_output=True)
    report([("ok", done.returncode == 0, "ok")])
''')
SUBPROCESS_ISOLATED = DRIVER + textwrap.dedent('''
    import subprocess, sys
    done = subprocess.run([sys.executable, "-I", "-c", "open(r'{root}/outside/secret.py').read()"], capture_output=True)
    report([("ok", done.returncode == 0, "ok")])
''')
SWAP_SELF = DRIVER + textwrap.dedent('''
    with open(__file__, "a", encoding="utf-8") as handle:
        handle.write("\\n# drifted\\n")
    report([("ok", True, "ok")])
''')
ABSENT_PROBE = DRIVER + textwrap.dedent('''
    try:
        open(r"{root}/outside/missing.json").read()
    except OSError:
        pass
    report([("ok", True, "ok")])
''')
ISOLATED_WORLD = DRIVER + textwrap.dedent('''
    import os, subprocess, sys
    if os.environ.get("VKIT_ISOLATED_CHILD"):
        report([("ok", open(r"{root}/outside/secret.py").read().startswith("V"), "ok")])
    else:
        done = subprocess.run([sys.executable, "-I", __file__, sys.argv[1]],
                              env={**os.environ, "VKIT_ISOLATED_CHILD": "1"}, capture_output=True)
        report([("ok", done.returncode == 0, "ok")])
''')
IDLE = DRIVER + 'report([("ok", True, "ok")])\n'


def escape_project(tmp_path: Path, make_project, driver: str, *, inputs: tuple[str, ...] = ("esc.py",)):
    root = (tmp_path / "project").as_posix().replace("\\", "/")
    checks = [scenario_check("esc", "esc.py", scenarios=["ok"], inputs=list(inputs))]
    project = make_project({"esc.py": driver.replace("{root}", root), "outside/secret.py": "V = 1\n"}, checks)
    code, body = vkit(project, "check", "run", "--check", "esc")
    run = body["runs"][0] if "runs" in body else body
    run_dir = next((project / ".git" / "vkit" / "runs").glob("*/deps.json")).parent
    receipt = json.loads((run_dir / "deps.json").read_text(encoding="utf-8"))
    return code, run, receipt, tmp_path / "project"


def test_local_world_records_in_band_closure_and_binds_every_opened_file(tmp_path, make_project):
    code, run, receipt, project = escape_project(tmp_path, make_project, OUTSIDE)
    assert (code, run["outcome"]["result"]) == (0, "PASS")
    assert receipt["closure_provenance"] == "in-band recorder"
    assert receipt["established"] is True
    binding = receipt["bindings"].get((project / "outside" / "secret.py").resolve().as_posix())
    assert binding and binding["op"] == "read" and len(binding.get("sha256", "")) == 64


def test_directory_enumeration_is_presence_bound(tmp_path, make_project):
    code, run, receipt, project = escape_project(tmp_path, make_project, ENUMERATE)
    binding = receipt["bindings"].get((project / "outside").resolve().as_posix())
    assert binding and binding["op"] == "enumerate"


def test_isolated_world_names_none_provenance_and_does_not_establish(tmp_path, make_project):
    code, run, receipt, project = escape_project(tmp_path, make_project, ISOLATED_WORLD)
    assert run["outcome"]["result"] == "PASS"
    assert receipt["closure_provenance"] == "none"
    assert receipt["established"] is False
    assert any("isolated (-I) interpreter" in item for item in receipt["does_not_establish"])
    assert (project / "outside" / "secret.py").resolve().as_posix() not in receipt["bindings"]


def test_host_traced_provenance_is_honored(tmp_path):
    host = tmp_path / "host-deps.json"
    host.write_text(json.dumps({"bindings": {"/seen.py": {"op": "read", "sha256": "a" * 64}}}), encoding="utf-8")
    receipt = deps_receipt(host_log=json.loads(host.read_text(encoding="utf-8")))
    assert receipt["closure_provenance"] == "host-traced"
    assert receipt["established"] is True
    assert "/seen.py" in receipt["bindings"]


def test_env_reads_are_gap_named_and_never_bound(tmp_path, make_project, monkeypatch):
    monkeypatch.setenv("VKIT_ESCAPE_MARKER", "1")
    code, run, receipt, project = escape_project(tmp_path, make_project, ENV_READ)
    assert run["outcome"]["result"] == "PASS"
    assert receipt["unsupported"]["env_reads"]
    assert any("env reads" in item for item in receipt["does_not_establish"])
    assert not any("VKIT_ESCAPE_MARKER" in key for key in receipt["bindings"])


def test_plain_subprocess_descendants_stay_bound(tmp_path, make_project):
    code, run, receipt, project = escape_project(tmp_path, make_project, SUBPROCESS_PLAIN)
    secret = (project / "outside" / "secret.py").resolve().as_posix()
    assert receipt["bindings"][secret]["op"] == "read"
    assert receipt["subprocesses"]


def test_isolated_subprocess_descendants_are_gap_named(tmp_path, make_project):
    code, run, receipt, project = escape_project(tmp_path, make_project, SUBPROCESS_ISOLATED)
    secret = (project / "outside" / "secret.py").resolve().as_posix()
    assert secret not in receipt["bindings"]
    assert receipt["subprocesses"]
    assert any("isolated (-I) interpreter" in item for item in receipt["does_not_establish"])


def test_declared_but_unused_inputs_are_negative_bindings(tmp_path, make_project):
    project = make_project({"esc.py": IDLE, "unused.txt": "u\n"},
                           [scenario_check("esc", "esc.py", scenarios=["ok"], inputs=["esc.py", "unused.txt"])])
    code, body = vkit(project, "check", "run", "--check", "esc")
    run_dir = next((project / ".git" / "vkit" / "runs").glob("*/deps.json")).parent
    receipt = json.loads((run_dir / "deps.json").read_text(encoding="utf-8"))
    assert "unused.txt" in receipt["negative_bindings"]["unused_declared"]
    assert receipt["negative_bindings"]["absent_probes"] == []


def test_absent_probes_bind_negatively(tmp_path, make_project):
    code, run, receipt, project = escape_project(tmp_path, make_project, ABSENT_PROBE)
    assert any("missing.json" in path for path in receipt["negative_bindings"]["absent_probes"])


def test_evaluator_import_closure_pins_conftest_and_local_imports(tmp_path):
    (tmp_path / "conftest.py").write_text("MARK = 1\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("from helpers_local import extra\nX = extra\n", encoding="utf-8")
    (tmp_path / "helpers_local.py").write_text("extra = 2\n", encoding="utf-8")
    (tmp_path / "driver.py").write_text("import helper\nimport json, sys\nprint(json)\n", encoding="utf-8")
    entry = scenario_check("c1", "driver.py", scenarios=["s"], inputs=["driver.py"])
    closure = evaluator_closure(parse_check_definition(entry, Project(tmp_path, tmp_path / ".git")))
    assert set(closure) == {"driver.py", "conftest.py", "helper.py", "helpers_local.py"}


def test_open_time_digests_pin_the_resolved_path(tmp_path):
    (tmp_path / "driver.py").write_text("print(1)\n", encoding="utf-8")
    entry = scenario_check("c1", "driver.py", scenarios=["s"], inputs=["driver.py"])
    pinned = pin_evaluators(parse_check_definition(entry, Project(tmp_path, tmp_path / ".git")))
    assert pinned["driver.py"]["path"] == str((tmp_path / "driver.py").resolve())
    assert len(pinned["driver.py"]["sha256"]) == 64
    assert pinned["driver.py"]["inode"] == list(pinned["driver.py"]["inode"])


def test_evaluator_swap_mid_run_is_detected(tmp_path, make_project):
    code, run, receipt, project = escape_project(tmp_path, make_project, SWAP_SELF)
    assert (run["outcome"]["result"], run["outcome"]["detail"]) == (
        "BLOCKED", run["outcome"]["detail"])
    assert "evaluator bytes changed during execution" in run["outcome"]["detail"]
    assert receipt["evaluator_drift"] == ["esc.py"]


def test_one_shot_staleness_is_documented_in_the_receipt(tmp_path, make_project):
    code, run, receipt, project = escape_project(tmp_path, make_project, IDLE)
    assert receipt["staleness"]["semantics"] == "one-shot"
    assert receipt["open_time_digests"]["esc.py"]
    assert "residual_window" in receipt
