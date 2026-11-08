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
