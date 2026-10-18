from __future__ import annotations

import shutil

import pytest

from vkit.verifiers.node_adapter import _major_version


def test_node_version_probe_ignores_ambient_node_options(monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the live version probe")
    expected = _major_version(node)
    assert expected > 0
    monkeypatch.setenv("NODE_OPTIONS", "--vkit-invalid-node-option")
    assert _major_version(node) == expected
