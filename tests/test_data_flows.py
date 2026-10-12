from __future__ import annotations

import base64
import os
import platform
import subprocess
import sys
import stat
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

from vkit.operations.data_flows import (
    MAX_PATHS,
    _run_bounded,
    _parse_query,
    _snapshot,
    find_data_flows,
)
from scripts.install_dataflow_engine import _safe_members


def _flow_marker(table: str) -> str:
    return "VKIT_FLOW_B64|" + base64.b64encode(table.encode()).decode()


def test_parse_query_preserves_live_flow_path_table() -> None:
    table = "nodeType tracked lineNumber method file\nCall exec_sink(user_input) 4 relay flow.c"
    status, result = _parse_query(
        "VKIT_COUNTS|1|1|1|0\n" + _flow_marker(table),
        source_method="entry", source_parameter=1,
        sink_method="exec_sink", sink_argument=1,
    )
    assert status == "PATH_FOUND"
    assert result["flows"] == [{"table": table}]
    assert result["source"] == {"method": "entry", "parameter_index": 1}
    assert result["sink"] == {"method": "exec_sink", "argument_index": 1}


def test_parse_query_reports_only_model_local_absence() -> None:
    status, result = _parse_query(
        "VKIT_COUNTS|1|1|0|0", source_method="entry", source_parameter=1,
        sink_method="exec_sink", sink_argument=1,
    )
    assert status == "NO_PATH_IN_MODEL"
    assert result["endpoint_counts"] == {"sources": 1, "sinks": 1, "flows": 0}


def test_parse_query_refuses_ambiguous_or_truncated_evidence() -> None:
    status, result = _parse_query(
        "VKIT_COUNTS|2|1|0|0", source_method="entry", source_parameter=1,
        sink_method="exec_sink", sink_argument=1,
    )
    assert status == "UNSUPPORTED"
    assert result["endpoint_counts"]["sources"] == 2

    status, result = _parse_query(
        f"VKIT_COUNTS|1|1|{MAX_PATHS + 1}|0", source_method="entry", source_parameter=1,
        sink_method="exec_sink", sink_argument=1,
    )
    assert status == "UNKNOWN"
    assert "truncated" in result["reason"]


def test_snapshot_captures_only_c_and_header_bytes(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "flow.c").write_bytes(b"int main(void) { return 0; }\n")
    (source / "notes.txt").write_text("ignored", encoding="utf-8")
    (source / "vendor").mkdir()
    (source / "vendor" / "ignored.c").write_text("ignored", encoding="utf-8")
    target = tmp_path / "captured"
    target.mkdir()

    digest, paths, size = _snapshot(source, target, deadline=10**12)

    assert len(digest) == 64
    assert paths == ("flow.c",)
    assert size == len(b"int main(void) { return 0; }\n")
    assert (target / "flow.c").read_bytes() == (source / "flow.c").read_bytes()
    assert not (target / "notes.txt").exists()


def test_public_operation_uses_unavailable_without_verified_joern(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "flow.c").write_bytes(b"int main(void) { return 0; }\n")
    monkeypatch.delenv("VKIT_JOERN_HOME", raising=False)

    result = find_data_flows(tmp_path, ".", "main", 1, "exec_sink", 1)

    assert result["operation"] == "find_data_flows"
    assert result["backend_version"] == "4.0.653"
    assert result["status"] == "UNAVAILABLE"
    assert result["source_sha256"] is None


@pytest.mark.parametrize("index", [0, -1, True, 129])
def test_argument_indices_are_bounded_positive_positions(tmp_path: Path, index: object) -> None:
    (tmp_path / "flow.c").write_bytes(b"int main(void) { return 0; }\n")
    with pytest.raises(ValueError, match="integer from 1"):
        find_data_flows(tmp_path, ".", "main", index, "exec_sink", 1)  # type: ignore[arg-type]


def test_installer_accepts_regular_archive_paths_and_rejects_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "paths.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("joern/joern", "launch")
        archive.writestr("../outside", "unsafe")
    with ZipFile(archive_path) as archive:
        with pytest.raises(ValueError, match="unsafe path"):
            _safe_members(archive)


def test_installer_rejects_archive_symlinks(tmp_path: Path) -> None:
    archive_path = tmp_path / "link.zip"
    link = ZipInfo("joern/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive_path, "w") as archive:
        archive.writestr(link, "../../outside")
    with ZipFile(archive_path) as archive:
        with pytest.raises(ValueError, match="unsafe path or symlink"):
            _safe_members(archive)


def test_shared_engine_runner_bounds_time_and_file_output(tmp_path: Path) -> None:
    code, stdout, stderr = _run_bounded(
        [sys.executable, "-c", "import sys; print('done'); print('diag', file=sys.stderr)"],
        cwd=tmp_path, timeout_seconds=2, output_limit=1024,
    )
    assert code == 0
    assert stdout.splitlines() == ["done"]
    assert stderr.splitlines() == ["diag"]

    with pytest.raises(subprocess.TimeoutExpired):
        _run_bounded([sys.executable, "-c", "import time; time.sleep(10)"],
                     cwd=tmp_path, timeout_seconds=0.05, output_limit=1024)
    with pytest.raises(ValueError, match="output exceeded"):
        _run_bounded([sys.executable, "-c", "print('x' * 4096)"],
                     cwd=tmp_path, timeout_seconds=2, output_limit=128)


@pytest.mark.skipif(
    not (os.environ.get("VKIT_JOERN_HOME") and sys.platform == "linux"
         and platform.machine().lower() in {"x86_64", "amd64"}),
    reason="requires installed pinned Joern on Linux x86_64",
)
def test_pinned_joern_live_interprocedural_positive_and_negative(tmp_path: Path) -> None:
    positive = tmp_path / "positive"
    positive.mkdir()
    (positive / "flow.c").write_text(
        "void exec_sink(const char *value);\n"
        "void relay(const char *value) { exec_sink(value); }\n"
        "void entry(const char *user_input) { relay(user_input); }\n", encoding="utf-8")
    negative = tmp_path / "negative"
    negative.mkdir()
    (negative / "flow.c").write_text(
        "void exec_sink(const char *value);\n"
        "void relay(const char *value) { exec_sink(value); }\n"
        "void entry(const char *user_input) { relay(\"constant\"); }\n", encoding="utf-8")

    found = find_data_flows(tmp_path, "positive", "entry", 1, "exec_sink", 1, timeout_ms=60_000)
    absent = find_data_flows(tmp_path, "negative", "entry", 1, "exec_sink", 1, timeout_ms=60_000)

    assert found["status"] == "PATH_FOUND"
    assert found["endpoint_counts"] == {"sources": 1, "sinks": 1, "flows": 1}
    assert "user_input" in found["flows"][0]["table"]
    assert "value" in found["flows"][0]["table"]
    assert absent["status"] == "NO_PATH_IN_MODEL"
    assert absent["endpoint_counts"] == {"sources": 1, "sinks": 1, "flows": 0}
    assert len(json.dumps(found).encode()) <= 256 * 1024
