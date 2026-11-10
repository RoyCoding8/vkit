from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from helpers import DRIVER, git, scenario_check, vkit
from vkit.inputs import canonical_digest, capture_working_tree
from vkit.paths import open_project
from vkit.state import evaluator_digests
from vkit.store import Store

PASSES = DRIVER + 'report([("ok", True, "printed ok")])\n'
BAD_SUBJECT = DRIVER + 'import pathlib\nreport([("ok", pathlib.Path("subject.py").read_text() != "bad", "read")])\n'
WEAK = DRIVER + 'report([("ok", True, "printed ok")])\n'


def _identity(project: Path) -> str:
    return canonical_digest(evaluator_digests(_check(project)))


def _check(project: Path):
    from vkit.manifest import parse_manifest
    return parse_manifest(open_project(project)).require("ok")


def test_accept_retains_the_evaluator_bytes(make_project):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    stored = Store.of(open_project(project)).evaluator_bytes("ok", _identity(project))
    assert stored == {"ok.py": (project / "ok.py").read_bytes()}


def test_the_accept_card_shows_the_oracle_byte_diff(make_project, monkeypatch):
    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    (project / "ok.py").write_text(PASSES + 'print("extra")\n', encoding="utf-8")
    monkeypatch.delenv("VKIT_ALLOW_YES")
    code, body = vkit(project, "accept", "--check", "ok")
    assert code == 2
    assert "oracle byte diff vs last acceptance" in body["error"]
    assert "(candidate)" in body["error"] and "+print(\"extra\")" in body["error"]


def _weaken_flow(make_project):
    project = make_project({"driver.py": BAD_SUBJECT, "subject.py": "bad"},
                           [scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["driver.py", "subject.py"])])
    assert vkit(project, "check", "run", "--check", "ok")[0] == 1
    return project


def test_a_weakened_evaluator_refuses_acceptance(make_project, monkeypatch):
    project = _weaken_flow(make_project)
    (project / "driver.py").write_text(WEAK, encoding="utf-8")
    from vkit import cli

    monkeypatch.setenv("VKIT_ALLOW_YES", "1")
    args = argparse.Namespace(project=str(project), check=["ok"], component=[], drop=[],
                              proposal=None, yes=True, json=True)
    with pytest.raises(cli.Refused) as refused:
        cli.cmd_accept(args)
    message = str(refused.value)
    assert "weakened_check" in message
    assert "accepted evaluator vs current candidate" in message
    assert "retained FAIL" in message and '"result": "PASS"' in message


def test_a_labeled_false_positive_fix_is_fixed_evaluator(make_project):
    project = _weaken_flow(make_project)
    Store.of(open_project(project)).label_fail("ok", _identity(project), "bad")
    (project / "driver.py").write_text(WEAK, encoding="utf-8")
    code, body = vkit(project, "accept", "--check", "ok", "--yes")
    assert code == 0 and body["accepted"] == ["ok"]


def test_an_unrunnable_accepted_evaluator_is_incomparable(make_project, monkeypatch):
    project = make_project({"driver.py": 'import module\n' + PASSES, "module.py": "x = 1\n"},
                           [scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["driver.py", "module.py"])])
    (project / "module.py").unlink()
    git(project, "add", "-u")
    git(project, "commit", "-qm", "move module")
    (project / "driver.py").write_text(PASSES + "\n", encoding="utf-8")
    from vkit import cli

    monkeypatch.setenv("VKIT_ALLOW_YES", "1")
    args = argparse.Namespace(project=str(project), check=["ok"], component=[], drop=[],
                              proposal=None, yes=True, json=True)
    with pytest.raises(cli.Refused) as refused:
        cli.cmd_accept(args)
    message = str(refused.value)
    assert "incomparable" in message and "re-accept the reference" in message


def test_restoring_the_reference_clears_an_incomparable_comparison(make_project):
    project = make_project({"driver.py": 'import module\n' + PASSES, "module.py": "x = 1\n"},
                           [scenario_check("ok", "driver.py", scenarios=["ok"], inputs=["driver.py", "module.py"])])
    (project / "module.py").unlink()
    git(project, "add", "-u")
    git(project, "commit", "-qm", "move module")
    (project / "driver.py").write_text(PASSES + "\n", encoding="utf-8")
    assert vkit(project, "accept", "--check", "ok")[0] == 3
    (project / "module.py").write_text("x = 1\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-qm", "restore module")
    assert vkit(project, "accept", "--check", "ok", "--yes")[0] == 0


def test_fail_snapshots_keep_the_most_recent_per_evaluator(make_project):
    from vkit.store import _SNAPSHOT_LIMIT
    project = make_project({"ok.py": PASSES}, [], accept=False)
    store = Store.of(open_project(project))
    tree = {item.path: item.content for item in capture_working_tree(project).files}
    for round in range(6):
        identity = f"i{round}"
        store.retain_fail("ok", identity, tree)
        store.label_fail("ok", identity, "good")
    assert len([p for p in (store.root / "fails" / "ok").iterdir() if p.is_dir()]) == _SNAPSHOT_LIMIT
    assert store.fail_snapshot("ok", "i5") is not None and store.fail_snapshot("ok", "i0") is None


def test_a_stale_check_blocks_the_gate(make_project, monkeypatch):
    from vkit import query
    from vkit.evidence import backend_identity

    project = make_project({"ok.py": PASSES}, [scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])])
    vkit(project, "check", "run", "--check", "ok")
    real = backend_identity
    monkeypatch.setattr("vkit.evidence.backend_identity",
                        lambda check: {**real(check), "implementation": {"changed.py": "f" * 64}})
    gate = query.status(query.open_context(project))["gate"]
    assert gate["verdict"] == "BLOCKED" and "stale:" in gate["reason"]
