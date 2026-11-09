from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from vkit.cli import Refused, _confirm


def _tty(monkeypatch, interactive: bool) -> None:
    monkeypatch.setattr("vkit.cli.sys.stdin", SimpleNamespace(isatty=lambda: interactive))


def test_confirm_names_which_authority_granted_the_acceptance(monkeypatch):
    args = argparse.Namespace(yes=True, json=False)
    monkeypatch.setenv("VKIT_ALLOW_YES", "1")
    monkeypatch.setattr("vkit.cli._parent_commands", lambda: ["bash"])
    _tty(monkeypatch, True)
    assert _confirm(args, "approve the definition", "accept it?") == "operator_env"
    _tty(monkeypatch, False)
    assert _confirm(args, "approve the definition", "accept it?") == "shell_env"


def test_confirm_refuses_an_acceptance_spawned_from_the_agent_hook_chain(monkeypatch):
    args = argparse.Namespace(yes=True, json=False)
    monkeypatch.setenv("VKIT_ALLOW_YES", "1")
    _tty(monkeypatch, False)
    monkeypatch.setattr("vkit.cli._parent_commands", lambda: ["bash", "python vkit_hook.py Stop"])
    with pytest.raises(Refused):
        _confirm(args, "approve the definition", "accept it?")


def test_interactive_confirm_records_interactive_authority(monkeypatch):
    args = argparse.Namespace(yes=False, json=False)
    _tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert _confirm(args, "approve the definition", "accept it?") == "interactive"
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert _confirm(args, "approve the definition", "accept it?") is None


def test_yes_requires_operator_authorization(monkeypatch):
    args = argparse.Namespace(yes=True, json=False)
    _tty(monkeypatch, True)
    monkeypatch.setattr("vkit.cli._parent_commands", lambda: [])
    monkeypatch.delenv("VKIT_ALLOW_YES", raising=False)
    with pytest.raises(Refused) as exc:
        _confirm(args, "approve the definition", "accept it?")
    assert "--yes" not in str(exc.value)
    monkeypatch.setenv("VKIT_ALLOW_YES", "1")
    assert _confirm(args, "approve the definition", "accept it?") == "operator_env"
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


def test_a_strengthening_label_is_demonstrated_or_reads_unproven():
    from vkit.cli import _demonstrated_label

    assert _demonstrated_label("strengthening", None) == "strengthening (unproven: no rejection corpus)"
    shown = _demonstrated_label("strengthening", {"added": 2})
    assert "2 added obligation(s)" in shown and "unproven" not in shown
    assert _demonstrated_label("weakening", None) == "weakening"


def _card(project, approved, checks):
    from vkit.cli import _accept_card
    from vkit.evidence import backend_identity
    from vkit.inputs import Snapshot
    from vkit.manifest import parse_manifest
    from vkit.paths import open_project
    from vkit.store import Store

    project_obj = open_project(project)
    manifest = parse_manifest(project_obj)
    identities = {c: backend_identity(manifest.require(c)) for c in checks}
    return "\n".join(_accept_card(manifest, identities, list(checks), {}, approved,
                                  Snapshot(project_obj), Store.of(project_obj)))


def test_accept_card_surfaces_gaps_and_prior_diff(make_project):
    from helpers import DRIVER, scenario_check

    entry = scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])
    project = make_project({"ok.py": DRIVER + 'report([("ok", True, "ok")])\n', "README.md": "hi"}, [entry],
                           accept=False)
    card = _card(project, {}, ["ok"])
    assert "incomparable vs last acceptance" in card
    assert "null-subject sentinel:" in card
    assert "uncovered paths: README.md" in card


def test_accept_card_compares_against_the_last_and_the_first_acceptance(make_project):
    from helpers import DRIVER, scenario_check

    base = scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])
    project = make_project({"ok.py": DRIVER + 'report([("ok", True, "ok")])\n'}, [base], accept=False)
    approved = {
        "0" * 64: {"kind": "check_definition", "check_id": "ok", "accepted_at": "2024-01-01T00:00:00.000+00:00",
                   "definition": base},
        "f" * 64: {"kind": "check_definition", "check_id": "ok", "accepted_at": "2024-06-01T00:00:00.000+00:00",
                   "confirmed_via": "shell_env", "definition": {**base, "required_scenarios": ["ok", "extra"]}},
    }
    card = _card(project, approved, ["ok"])
    assert "weakening vs last acceptance (2024-06-01T00:00:00.000+00:00, shell_env)" in card
    assert "unchanged vs first acceptance (2024-01-01" in card


def test_accept_card_renders_formal_obligation_removal_and_demotion():
    from vkit.cli import _inventory_diff

    previous = {"checks": {"ok": "a"}, "formal_required": {"alloc": "d"}, "dropped": {}}
    current = {"checks": {"ok": "a"}, "formal_required": {}, "dropped": {}}
    lines = "\n".join(_inventory_diff(previous, current, {}))
    assert "formal obligations" in lines and "alloc" in lines and "no longer required" in lines
    lines = "\n".join(_inventory_diff(previous, {**current, "formal_required": {"alloc": "d"}}, {"alloc": False}))
    assert "formal obligation demoted: alloc" in lines


def test_the_strengthening_label_is_demonstrated_over_the_declared_obligations(make_project):
    from helpers import DRIVER, scenario_check

    base = scenario_check("ok", "ok.py", scenarios=["ok"], inputs=["ok.py"])
    grown = scenario_check("ok", "ok.py", scenarios=["ok", "more"], inputs=["ok.py"])
    project = make_project({"ok.py": DRIVER + 'report([("ok", True, "ok"), ("more", True, "ok")])\n'},
                           [grown], accept=False)
    approved = {"f" * 64: {"kind": "check_definition", "check_id": "ok",
                           "accepted_at": "2024-01-01T00:00:00.000+00:00", "definition": base}}
    card = _card(project, approved, ["ok"])
    assert "strengthening (demonstrated over the declared corpus: 1 added obligation(s))" in card
