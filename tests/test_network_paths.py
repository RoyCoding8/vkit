from __future__ import annotations

import json
import importlib.metadata
import os
from pathlib import Path

import pytest

from vkit.operations.network_paths import (
    BATFISH_IMAGE_DIGEST,
    PYBATFISH_VERSION,
    _capture_snapshot,
    _parse_selector,
    _witnesses,
    compare_network_paths,
)


def _selector() -> dict[str, object]:
    return {
        "start_node": "edge",
        "start_interface": "GigabitEthernet0/1",
        "end_node": "host-www",
        "destination_ip": "203.0.113.10",
        "protocol": "TCP",
        "destination_port": 443,
    }


def _snapshot(root: Path, name: str, *, route: bool) -> Path:
    snapshot = root / name
    configs = snapshot / "configs"
    hosts = snapshot / "hosts"
    configs.mkdir(parents=True)
    hosts.mkdir()
    route_line = " ip route 203.0.113.0 255.255.255.0 10.0.0.2\n" if route else ""
    (configs / "edge.cfg").write_text(
        "hostname edge\n"
        "interface GigabitEthernet0/0\n ip address 10.0.0.1 255.255.255.252\n no shutdown\n!\n"
        "interface GigabitEthernet0/1\n ip address 198.51.100.1 255.255.255.0\n no shutdown\n!\n"
        f"{route_line}end\n",
        encoding="utf-8",
    )
    (configs / "dist.cfg").write_text(
        "hostname dist\n"
        "interface GigabitEthernet0/0\n ip address 10.0.0.2 255.255.255.252\n no shutdown\n!\n"
        "interface GigabitEthernet0/1\n ip address 203.0.113.2 255.255.255.0\n no shutdown\n!\n"
        "end\n",
        encoding="utf-8",
    )
    (hosts / "host-www.json").write_text(json.dumps({
        "hostname": "host-www",
        "hostInterfaces": {
            "eth0": {"name": "eth0", "prefix": "203.0.113.10/24", "gateway": "203.0.113.2"},
        },
    }), encoding="utf-8")
    return snapshot


def test_selector_is_concrete_and_rejects_engine_query_language() -> None:
    parsed = _parse_selector(_selector())
    assert parsed == {
        "start_node": "edge",
        "start_interface": "GigabitEthernet0/1",
        "end_node": "host-www",
        "destination_ip": "203.0.113.10/32",
        "protocol": "TCP",
        "destination_port": 443,
    }
    invalid = {**_selector(), "start_node": "edge|.*"}
    with pytest.raises(ValueError, match="concrete ASCII"):
        _parse_selector(invalid)
    invalid = {**_selector(), "query": "SELECT * FROM answers"}
    with pytest.raises(ValueError, match="selector must contain"):
        _parse_selector(invalid)


def test_snapshot_capture_is_exact_bounded_and_manifest_stable(tmp_path: Path) -> None:
    base = _snapshot(tmp_path, "base", route=False)
    first = _capture_snapshot(tmp_path, "base")
    second = _capture_snapshot(tmp_path, "base")
    assert first.sha256 == second.sha256
    assert {item.relative for item in first.files} == {
        "configs/dist.cfg", "configs/edge.cfg", "hosts/host-www.json",
    }
    (base / "unexpected.txt").write_text("not captured", encoding="utf-8")
    with pytest.raises(ValueError, match="configs/ and may contain hosts/ only"):
        _capture_snapshot(tmp_path, "base")


def test_snapshot_rejects_parent_traversal_and_duplicate_host_json_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported path component"):
        _capture_snapshot(tmp_path, "../outside")
    snapshot = _snapshot(tmp_path, "bad", route=False)
    (snapshot / "hosts" / "host-www.json").write_text('{"hostname":"a","hostname":"b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate keys"):
        _capture_snapshot(tmp_path, "bad")


def test_missing_engine_returns_unavailable_after_input_validation(tmp_path: Path, monkeypatch) -> None:
    base = _snapshot(tmp_path, "base", route=False)
    candidate = _snapshot(tmp_path, "candidate", route=True)
    monkeypatch.delenv("VKIT_BATFISH_HOST", raising=False)
    def missing_package(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr("vkit.operations.network_paths.importlib.metadata.version", missing_package)
    result = compare_network_paths(tmp_path, base.name, candidate.name, _selector())
    assert result["operation"] == "compare_network_paths"
    assert result["status"] == "UNAVAILABLE"
    assert result["engine"]["expected_image_digest"] == BATFISH_IMAGE_DIGEST
    assert result["engine"]["pybatfish_version"] == PYBATFISH_VERSION


def test_differential_trace_columns_are_normalized_to_snapshot_names() -> None:
    class Answer:
        def frame(self):
            class Frame:
                def to_dict(self, *, orient: str):
                    assert orient == "records"
                    return [{
                        "Flow": "dst=203.0.113.10",
                        # Snapshot_* follows answer(snapshot=...).
                        "Snapshot_Traces": ["snapshot argument trace"],
                        "Snapshot_TraceCount": 1,
                        # Reference_* follows answer(reference_snapshot=...).
                        "Reference_Traces": ["reference argument trace"],
                        "Reference_TraceCount": 1,
                    }]
            return Frame()

    witnesses = _witnesses(Answer(), snapshot="candidate", reference="base")
    assert witnesses == [{
        "flow": "dst=203.0.113.10",
        "traces_by_snapshot": {
            "candidate": ["snapshot argument trace"],
            "base": ["reference argument trace"],
        },
        "trace_counts_by_snapshot": {"candidate": 1, "base": 1},
    }]


_ENGINE_REQUIRED = os.environ.get("VKIT_BATFISH_REQUIRED") == "1"


@pytest.mark.skipif(not _ENGINE_REQUIRED, reason="set VKIT_BATFISH_REQUIRED=1 to require the pinned Batfish service")
def test_real_batfish_finds_the_added_route_flow(tmp_path: Path) -> None:
    _snapshot(tmp_path, "base", route=False)
    _snapshot(tmp_path, "candidate", route=True)

    result = compare_network_paths(tmp_path, "base", "candidate", _selector(), timeout_seconds=60)

    assert result["status"] == "DIFFERENT", result
    assert result["engine"]["actual_batfish_version"].startswith("2026.08.27")
    assert result["diagnostics"]["base"]["complete"] is True
    assert result["diagnostics"]["candidate"]["complete"] is True
    assert any(
        "203.0.113.10" in witness["flow"]
        for direction in result["directional_results"]
        for witness in direction["witnesses"]
    ), result["directional_results"]
    assert result["cleanup_status"] == "DELETED"


@pytest.mark.skipif(not _ENGINE_REQUIRED, reason="set VKIT_BATFISH_REQUIRED=1 to require the pinned Batfish service")
def test_real_batfish_reports_no_difference_for_identical_captured_snapshots(tmp_path: Path) -> None:
    _snapshot(tmp_path, "base", route=True)
    _snapshot(tmp_path, "candidate", route=True)

    result = compare_network_paths(tmp_path, "base", "candidate", _selector(), timeout_seconds=60)

    assert result["status"] == "NO_DIFFERENCE", result
    assert result["diagnostics"]["base"]["complete"] is True
    assert result["diagnostics"]["candidate"]["complete"] is True
    assert [item["witnesses"] for item in result["directional_results"]] == [[], []]
    assert result["cleanup_status"] == "DELETED"
