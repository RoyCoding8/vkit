from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from helpers import McpClient
from vkit.paths import Project
from vkit.query import Context, run_view
from vkit.store import Store, write_json


@pytest.mark.parametrize("run_id", ["", ".", "..", "../foreign", "nested/run", r"nested\run", "/absolute", "C:relative"])
def test_run_lookup_rejects_paths(tmp_path, run_id):
    store = Store(tmp_path / "state")

    with pytest.raises(ValueError, match="run_id"):
        store.record(run_id)


def test_run_lookup_cannot_read_a_foreign_record(tmp_path):
    store = Store(tmp_path / "state")
    foreign = tmp_path / "foreign"
    write_json(foreign / "record.json", {"run_id": "foreign", "state": "done"})

    with pytest.raises(ValueError, match="run_id"):
        store.record(str(foreign))


def test_mcp_refuses_run_paths_for_reading_and_cancellation(make_project, tmp_path):
    project = make_project({"README.md": "run lookup\n"}, [], accept=False)
    foreign = tmp_path / "foreign"
    write_json(foreign / "record.json", {"run_id": "foreign", "state": "done"})
    client = McpClient(project)
    try:
        for name in ("run_get", "run_cancel"):
            body, error = client.call(name, run_id=str(foreign))
            assert error and body == {
                "error": "run_id must contain only letters, digits, underscores, or hyphens",
            }
    finally:
        client.close()


@pytest.mark.parametrize("offset,limit,expected", [
    (2, 3, {"stream": "stdout", "offset": 2, "size": 10, "text": "234"}),
    (-3, 2, {"stream": "stdout", "offset": 7, "size": 10, "text": "78"}),
    (-20, 2, {"stream": "stdout", "offset": 0, "size": 10, "text": "01"}),
    (20, 2, {"stream": "stdout", "offset": 20, "size": 10, "text": ""}),
    (1, -1, {"stream": "stdout", "offset": 1, "size": 10, "text": ""}),
])
def test_run_log_reads_only_the_requested_window(tmp_path, monkeypatch, offset, limit, expected):
    store = Store(tmp_path / "state")
    store.save({"run_id": "example-run", "state": "done"})
    project = Project(tmp_path, tmp_path / ".git")
    context = Context(project, store, None, None)
    log = store.run_dir("example-run") / "stdout.log"
    log.write_bytes(b"0123456789")
    original_open = Path.open

    class WindowOnly(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= max(0, limit), "log reads must be bounded by the requested window"
            return super().read(size)

    def open_log(path, *args, **kwargs):
        if path == log:
            return WindowOnly(b"0123456789")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_log)

    assert run_view(context, "example-run", offset=offset, limit=limit)["log"] == expected


def test_run_without_a_log_has_an_empty_window(tmp_path):
    store = Store(tmp_path / "state")
    store.save({"run_id": "example-run", "state": "done"})
    context = Context(Project(tmp_path, tmp_path / ".git"), store, None, None)

    assert run_view(context, "example-run")["log"] == {
        "stream": "stdout", "offset": 0, "size": 0, "text": "",
    }


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_run_view_does_not_open_a_non_regular_log(tmp_path, monkeypatch, kind):
    store = Store(tmp_path / "state")
    store.save({"run_id": "example-run", "state": "done"})
    log = store.run_dir("example-run") / "stdout.log"
    if kind == "directory":
        log.mkdir()
    elif hasattr(os, "mkfifo"):
        os.mkfifo(log)
    else:
        pytest.skip("named pipes require POSIX")
    original_open = Path.open

    def open_regular(path, *args, **kwargs):
        assert path != log, "non-regular logs must not be opened"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_regular)
    context = Context(Project(tmp_path, tmp_path / ".git"), store, None, None)

    assert run_view(context, "example-run")["log"] == {
        "stream": "stdout", "offset": 0, "size": 0, "text": "",
    }
