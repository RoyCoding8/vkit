from __future__ import annotations

import argparse

import pytest

from vkit.cli import Refused, _confirm


def test_yes_requires_operator_authorization(monkeypatch):
    args = argparse.Namespace(yes=True, json=False)
    monkeypatch.delenv("VKIT_ALLOW_YES", raising=False)
    with pytest.raises(Refused) as exc:
        _confirm(args, "approve the definition", "accept it?")
    assert "--yes" not in str(exc.value)
    monkeypatch.setenv("VKIT_ALLOW_YES", "1")
    assert _confirm(args, "approve the definition", "accept it?") is True
    monkeypatch.setenv("VKIT_ALLOW_YES", "0")
    with pytest.raises(Refused):
        _confirm(args, "approve the definition", "accept it?")


def test_definition_changes_are_labeled():
    from vkit.cli import _def_change

    base = {"required_scenarios": ["a"], "inputs": ["x.py"], "timeout_seconds": 30}
    label, lines = _def_change(base, {**base, "required_scenarios": ["a", "b"]})
    assert label == "strengthening" and any("required_scenarios" in ln for ln in lines)
    assert _def_change(base, {"inputs": ["x.py"], "timeout_seconds": 30})[0] == "weakening"
    assert _def_change(base, {**base, "timeout_seconds": 60, "required_scenarios": ["a", "b"]})[0] == "incomparable"
    assert _def_change(None, base)[0] == "incomparable"
    assert _def_change(base, dict(base))[0] == "unchanged"


def test_accept_card_surfaces_gaps_and_prior_diff(make_project):
    from helpers import DRIVER, scenario_check
    from vkit.cli import _accept_card
    from vkit.evidence import backend_identity
    from vkit.inputs import Snapshot
    from vkit.manifest import parse_manifest
    from vkit.paths import open_project

    entry = scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])
    project = make_project({"ok.py": DRIVER + 'report([("ok", True, "ok")])\n', "README.md": "hi"}, [entry])
    project_obj = open_project(project)
    manifest = parse_manifest(project_obj)
    card = "\n".join(_accept_card(manifest, {"ok": backend_identity(manifest.require("ok"))}, ["ok"], {},
                                  {}, Snapshot(project_obj)))
    assert "definition change vs prior acceptance: incomparable" in card
    assert "null-subject sentinel:" in card and "pending 1.0" in card
    assert "uncovered paths: README.md" in card
