from __future__ import annotations

import subprocess

import pytest

from vkit import container
from vkit.store import Store, write_json


@pytest.mark.parametrize("case,expected,deleted", [
    ("owned", "removed", ["a" * 64]), ("absent", "removed", []),
    ("foreign", "blocked", []), ("engine_error", "blocked", []),
])
def test_orphan_recovery_is_exact_and_idempotent(tmp_path, monkeypatch, case, expected, deleted):
    store = Store(tmp_path / "state")
    run_id, cid = "attempt-123", "a" * 64
    label = run_id + "-nonce"
    run_dir = store.run_dir(run_id)
    store.save({"run_id": run_id, "state": "running"})
    write_json(container.control_path(run_dir), {
        "schema_version": 1, "run_id": label, "cid_file": "container.cid",
        "container_id": cid, "status": "created"})
    (container.control_path(run_dir).parent / "container.cid").write_text(cid, encoding="ascii")
    actual = {} if case == "absent" else {cid: "somebody-else" if case == "foreign" else label}
    removed, commands = [], []

    def docker(argv, timeout=15):
        commands.append(argv)
        if case == "engine_error":
            return subprocess.CompletedProcess(argv, 1, "", "daemon unavailable")
        if argv[0] == "ps":
            assert argv[-1] == f"label=vkit.formal.run_id={label}"
            return subprocess.CompletedProcess(argv, 0, "\n".join(key for key, value in actual.items() if value == label), "")
        if argv[0] == "inspect":
            return (subprocess.CompletedProcess(argv, 0, actual[cid], "") if cid in actual else
                    subprocess.CompletedProcess(argv, 1, "", "No such object"))
        assert argv == ["rm", "--force", cid]
        removed.append(cid)
        actual.pop(cid)
        return subprocess.CompletedProcess(argv, 0, cid, "")

    monkeypatch.setattr(container, "_docker", docker)
    record = store.record(run_id)
    assert record["state"] == "interrupted"
    assert record["container_cleanup"]["status"] == expected
    assert removed == deleted
    assert store.record(run_id)["container_cleanup"]["status"] == expected
    assert removed == deleted
    assert record.get("outcome") is None


def test_recovery_rejects_a_descriptor_for_another_attempt(tmp_path, monkeypatch):
    run = tmp_path / "mine"
    write_json(container.control_path(run), {"schema_version": 1, "run_id": "foreign-label",
                                            "cid_file": "container.cid", "container_id": "a" * 64})
    def forbidden(*args, **kwargs):
        pytest.fail("foreign control data must not reach Docker")
    monkeypatch.setattr(container, "_docker", forbidden)
    result = container.recover_attempt(run)
    assert result["status"] == "blocked"
    assert "does not belong" in result["detail"]
