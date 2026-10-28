from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from helpers import McpClient, vkit
from vkit.feedback import collect, submit
from vkit.paths import open_project
from vkit.store import Store


REQUEST = {"task": "Merge two ledger implementations", "missing_capability": "Check linearizability",
           "benefit": "Find an interleaving that violates the accepted account model"}


def test_cli_creates_markdown_without_rewriting_previous_feedback(make_project):
    project = make_project({}, [], accept=False)
    args = ("feedback", "add", "--task", REQUEST["task"], "--missing-capability",
            REQUEST["missing_capability"], "--benefit", REQUEST["benefit"])
    code, entry = vkit(project, *args)
    assert code == 0 and entry["kind"] == "formal"
    path = Path(entry["path"])
    assert path.suffix == ".md"
    assert path.read_text(encoding="utf-8").endswith("## Verification benefit\n\n" + REQUEST["benefit"] + "\n")
    path.write_text("# Maintainer consolidation\n\nKeep the reviewed feedback.\n", encoding="utf-8")
    original = path.read_bytes()
    code, repeated = vkit(project, *args)
    assert code == 0 and repeated["id"] == entry["id"]
    assert path.read_bytes() == original
    code, report = vkit(project, "feedback", "list")
    assert code == 0 and report["total"] == 1
    assert report["items"][0]["preview"] == "# Maintainer consolidation\n\nKeep the reviewed feedback.\n"
    assert not list(path.parent.glob("*.json"))


def test_real_mcp_feedback_is_append_only_bounded_and_prioritizes_formal(make_project):
    project = make_project({}, [], accept=False)
    client = McpClient(project)
    try:
        reliable, error = client.call("feedback_add", **{**REQUEST, "kind": "reliable"})
        assert not error and reliable["kind"] == "reliable"
        formal, error = client.call("feedback_add", **REQUEST)
        assert not error and formal["kind"] == "formal"
        page, error = client.call("feedback_list", limit=1)
        assert not error and page["total"] == 2 and page["next_offset"] == 1
        assert page["items"][0]["id"] == formal["id"]
        page, error = client.call("feedback_list", offset=1, limit=1)
        assert not error and page["items"][0]["id"] == reliable["id"]
        assert page["next_offset"] is None
        _, error = client.call("feedback_add", **{**REQUEST, "edit": formal["id"]})
        assert error
        _, error = client.call("feedback_edit", id=formal["id"], benefit="overwrite")
        assert error
    finally:
        client.close()


def test_concurrent_feedback_deduplicates_complete_markdown(make_project):
    store = Store.of(open_project(make_project({}, [], accept=False)))
    with ThreadPoolExecutor(max_workers=4) as workers:
        entries = list(workers.map(lambda _: submit(store, REQUEST), range(12)))
    assert len({entry["id"] for entry in entries}) == 1
    assert collect(store)["total"] == 1
    assert all(entry["preview"].startswith("# Verification feedback\n\nKind: formal\n") for entry in entries)
    assert not list((store.root / "feedback").glob("*.tmp"))


def test_feedback_collision_cannot_read_a_link_or_directory(make_project, tmp_path):
    store = Store.of(open_project(make_project({}, [], accept=False)))
    entry = submit(store, REQUEST)
    path = Path(entry["path"])
    path.unlink()
    path.mkdir()
    with pytest.raises(ValueError, match="regular Markdown file"):
        submit(store, REQUEST)
    path.rmdir()
    outside = tmp_path / "outside.md"
    outside.write_text("private content", encoding="utf-8")
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(ValueError, match="regular Markdown file"):
        submit(store, REQUEST)
    assert outside.read_text(encoding="utf-8") == "private content"


@pytest.mark.parametrize("change", [{"task": " "}, {"benefit": "x" * 2001},
                                     {"kind": "test"}, {"run_id": "missing"}])
def test_feedback_rejects_incomplete_or_unbound_requests(make_project, change):
    store = Store.of(open_project(make_project({}, [], accept=False)))
    with pytest.raises(ValueError):
        submit(store, {**REQUEST, **change})
    assert collect(store)["total"] == 0
