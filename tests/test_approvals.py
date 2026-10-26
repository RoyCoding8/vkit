from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from helpers import NO_WINDOW

from vkit.store import Store, write_json


def test_legacy_approval_migrates_without_becoming_a_formal_approval(tmp_path):
    store = Store(tmp_path)
    old = {"check_id": "old", "backend": {"engine": "old"}, "accepted_at": "then"}
    write_json(tmp_path / "approved.json", {"old-identity": old})
    assert store.approved() == {"old-identity": {**old, "kind": "check_definition"}}
    new = {"kind": "formal_policy", "policy_sha256": "a" * 64, "package_sha256": "b" * 64,
           "checker_sha256": "c" * 64, "accepted_at": "now"}
    store.approve({"new-identity": new})
    actual = json.loads((tmp_path / "approved.json").read_text())
    assert actual == {"schema_version": 2, "records": {
        "old-identity": {**old, "kind": "check_definition"}, "new-identity": new}}


def test_approval_requires_an_explicit_complete_record_kind(tmp_path):
    with pytest.raises(ValueError, match="complete"):
        Store(tmp_path).approve({"identity": {"kind": "formal_policy", "accepted_at": "now"}})
    assert not (tmp_path / "approved.json").exists()


def test_concurrent_process_acceptance_preserves_both_writers(tmp_path):
    source = """
import sys, time
from pathlib import Path
import vkit.store as module
original = module.read_json
def slow_read(path):
    value = original(path)
    time.sleep(0.03)
    return value
module.read_json = slow_read
store = module.Store(Path(sys.argv[1]))
for index in range(10):
    key = sys.argv[2] + str(index)
    store.approve({key: {'kind': 'check_definition', 'check_id': key,
                        'backend': {}, 'accepted_at': 'now'}})
"""
    children = [subprocess.Popen([sys.executable, "-c", source, str(tmp_path), prefix],
                                 env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 **NO_WINDOW) for prefix in ("left-", "right-")]
    try:
        for child in children:
            _, error = child.communicate(timeout=20)
            assert child.returncode == 0, error.decode()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    assert set(Store(tmp_path).approved()) == {prefix + str(index)
                                             for prefix in ("left-", "right-") for index in range(10)}
