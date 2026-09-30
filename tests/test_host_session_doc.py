"""The live-host record must stay true to the host it describes.

`docs/HOST-SESSION.md` is the evidence for GAP-3. An evidence document is worth
nothing if it drifts from the repository: a transcript quoting a tool name the
host no longer sends, or a commit that does not exist, is worse than no
document, because it reads as a run someone did.

So the checks here are narrow on purpose. The run itself is not re-verified;
`tests/test_plugin_host.py` re-runs the host and that is the claim. What is
checked here is that the document and the code still agree, so a reader who
finds the document knows which of the two moved.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "HOST-SESSION.md"
HOOK = ROOT / "plugin" / "scripts" / "vkit_hook.py"
HOOKS_JSON = ROOT / "plugin" / "hooks" / "hooks.json"
MCP_JSON = ROOT / "plugin" / ".mcp.json"

TOOL_NAMES = (
    "check_start", "project_inspect", "run_cancel",
    "run_get", "task_begin", "task_finalize",
)


@pytest.fixture(scope="module")
def doc() -> str:
    assert DOC.is_file(), f"{DOC} is missing; the GAP-3 record is the deliverable"
    return DOC.read_text(encoding="utf-8")


def test_the_record_is_about_a_live_host_and_says_what_it_is_not(doc: str) -> None:
    """The document distinguishes a host run from the surfaces it is not.

    The failure this prevents is a later reader taking a paragraph about
    `plugin validate` or the protocol suite as a live-session claim. Both are
    named, and both are named as not being one.
    """
    assert "validate --strict" in doc, "the record does not mention what it is not"
    assert "test_mcp_stdio" in doc, "the record does not name the protocol suite"
    for phrase in ("It is not", "This is about a different program"):
        assert phrase in doc, f"the record no longer says {phrase!r}"


def test_every_tool_the_record_quotes_is_one_the_server_publishes(doc: str) -> None:
    """No quoted tool name is invented.

    The six names are read from the server's own table, so a tool added or
    renamed in `_tools.py` and not here fails instead of leaving the document
    quietly wrong.
    """
    sys_path = ROOT / "src"
    import sys

    if str(sys_path) not in sys.path:
        sys.path.insert(0, str(sys_path))
    from vkit.mcp import TOOL_NAMES as published

    assert set(published) == set(TOOL_NAMES), (
        f"the server publishes {sorted(published)}; this file pins {sorted(TOOL_NAMES)}"
    )
    for name in TOOL_NAMES:
        assert f"__plugin_vkit_vkit__{name}" in doc or f"__{name}" in doc, (
            f"the record never mentions {name}"
        )


def test_every_hook_event_the_record_quotes_is_one_the_manifest_declares(
    doc: str,
) -> None:
    """The three recorded deliveries are declared events, in the host's names.

    The host appends a qualifier to some events, as in
    `SessionStart:startup`, so the comparison is on the event name rather than
    the full label. A record quoting an event the plugin does not register would
    be quoting a delivery that cannot happen.
    """
    declared = set(json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"])
    for event in ("SessionStart", "PreToolUse", "Stop"):
        assert event in doc, f"the record never mentions {event}"
        assert event in declared, (
            f"the record quotes {event}, which hooks.json does not declare"
        )


def test_the_record_quotes_the_tool_name_the_hook_accepts(doc: str) -> None:
    """The bug the record documents is still fixed in the code it describes.

    The record's central claim is that `PreToolUse` used to reject every tool a
    host sent. If the code went back to rejecting it, the record would be
    describing a bug that no longer exists and the test that pins the fix would
    be the only thing left saying so. So the record and the code are checked
    against each other here.
    """
    source = HOOK.read_text(encoding="utf-8")
    assert "_is_vkit_server" in source, (
        "the hook no longer has the plugin-scoped server check the record describes"
    )
    assert "plugin_vkit_vkit" in doc, (
        "the record no longer quotes the tool name that exposed the bug"
    )
    # The record claims `{}` was the old response. If the hook no longer has a
    # path that returns nothing for a vkit tool, the record is stale.
    assert "_mcp_tool(payload) is None" in source, (
        "the hook no longer has the unmatched-tool path the record describes"
    )


def test_the_record_cites_commits_that_exist(doc: str) -> None:
    """Every sha the record cites resolves in this repository.

    A record that cites a commit nobody pushed is the exact failure this file
    exists to catch, and it is invisible to a reader who does not go looking.
    The shas are read from a fenced citation rather than by scanning for hex,
    because the record quotes session and tool-use UUIDs that are not shas and
    a scan for hex would demand they be commits.
    """
    cited = re.findall(r"`\b([0-9a-f]{7,40})\b`", doc)
    assert cited, "the record cites no commit, so it names no fix to check"
    for sha in sorted(set(cited)):
        done = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, text=True,
        )
        assert done.returncode == 0, (
            f"the record cites {sha}, which is not a commit in this repository"
        )


def test_the_record_names_the_test_that_repeats_it(doc: str) -> None:
    """The record points at the file a reader can re-run.

    A transcript with no reproduction command is a story. The command is here,
    and the file it names has to exist.
    """
    assert "tests/test_plugin_host.py" in doc, "the record names no host suite"
    assert (ROOT / "tests" / "test_plugin_host.py").is_file(), (
        "the record names a host suite that does not exist"
    )
    assert "pytest tests/test_plugin_host.py" in doc, (
        "the record gives no command to repeat the run"
    )


def test_the_record_keeps_its_open_claims_open(doc: str) -> None:
    """The unproven claims are still named as unproven.

    The record closes GAP-3 for a specific set of surfaces. A subagent's tool
    access and a BLOCKED run blocking a real turn were not tested, and if that
    sentence is edited away the document would read as a claim about a session
    that never ran.

    The comparison is over whitespace-normalised text because the record wraps
    its own prose, and a phrase broken across a line break is still there.
    """
    flat = " ".join(doc.split())
    for open_claim in ("A subagent inheriting tool access",
                       "A BLOCKED run blocking a",
                       "Not claimed"):
        assert " ".join(open_claim.split()) in flat, (
            f"the record no longer flags: {open_claim}"
        )
