"""Plan 11 checkpoint 1: what a cleanup preview proposes, and what it refuses.

Each test writes a real file into a real repository, calls the public preview,
and asserts on the bytes the caller would receive: the proposed content, the
preservation receipt, or the refusal. Nothing here asserts which internal
function ran, and every assertion would fail if the module returned nothing.

The load-bearing properties are the ones the plan names: only comments are
touched, an ordinary trailing comment on a task-owned line is removed, every
category the plan says to preserve survives, a malformed or unsupported file is
refused and left byte-identical, the preservation check is real enough to catch
a changed `# type:` comment, both byte strings compile, an empty proposal is a
valid answer, and the same file previewed twice proposes the same thing.
"""
from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from vkit.cleanup import (
    PreservationFailed,
    Proposal,
    Refusal,
    RefusalReason,
    preview_comment_cleanup,
    verify_preservation,
)
from vkit.paths import Project, open_project


def run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run git without putting a console window over the operator's screen.

    A worker runs these fixtures dozens of times, and each unsuppressed spawn
    opens and closes a window. Windows only, because the flag does not exist on
    POSIX and this is the platform the suite is verified on.
    """
    startupinfo = None
    creationflags = 0
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        startupinfo=startupinfo, creationflags=creationflags,
    )


def write(repo_root: Path, relative: str, content: bytes) -> Path:
    target = repo_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def project_with(tmp_path: Path, relative: str, content: bytes) -> Project:
    """A real repository already holding one committed file.

    `open_project` resolves the root through git, so a plain directory is not a
    project. The commit is made after the file is written, because `open_project`
    needs a HEAD and `git add -A` has to see the file.
    """
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    write(root, relative, content)
    run_git("init", "-q", cwd=root)
    run_git("add", "-A", cwd=root)
    run_git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture", cwd=root)
    return open_project(root)


def empty_project(tmp_path: Path) -> Project:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    run_git("init", "-q", cwd=root)
    run_git(
        "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty",
        "-qm", "fixture", cwd=root,
    )
    return open_project(root)


def rules_by_line(proposal: Proposal) -> dict[int, str]:
    return {site.line: site.rule_id for site in proposal.preserved}


# --------------------------------------------------------------------------- #
# What it proposes
# --------------------------------------------------------------------------- #


def test_an_ordinary_trailing_comment_on_a_task_owned_line_is_proposed_for_removal(
    tmp_path: Path,
) -> None:
    """The plan's first acceptance row: an approved trailing comment on a
    task-owned changed line is removed, with a receipt, and the file is not
    touched."""
    original = (
        b"def bump(total):\n"
        b"    total = total + 1  # increment the counter\n"
        b"    return total\n"
    )
    project = project_with(tmp_path, "sample.py", original)

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[2])

    assert isinstance(preview, Proposal), preview
    assert preview.after_bytes == (
        b"def bump(total):\n    total = total + 1\n    return total\n"
    )
    assert [(c.line, c.text) for c in preview.removed] == [(2, "# increment the counter")]
    assert preview.receipt.protected_directives == 0
    assert preview.receipt.line_count == 3
    assert preview.receipt.python_version.count(".") == 2
    assert (project.root / "sample.py").read_bytes() == original


def test_a_comment_on_a_line_the_task_did_not_change_is_preserved(tmp_path: Path) -> None:
    """Ownership is the default automatic scope, so an unchanged line's comment
    survives even though it is ordinary."""
    project = project_with(
        tmp_path,
        "sample.py",
        b"a = 1  # increment the counter\nb = 2  # increment the counter\n",
    )

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1])

    assert isinstance(preview, Proposal), preview
    assert preview.after_bytes == b"a = 1\nb = 2  # increment the counter\n"
    assert rules_by_line(preview)[2] == "NOT_TASK_OWNED"


def test_a_trailing_comment_removed_leaves_no_trailing_whitespace(tmp_path: Path) -> None:
    """The span covers the whitespace run before the comment, so applying the
    proposal does not leave a line ending in spaces."""
    project = project_with(tmp_path, "sample.py", b"x = 1\t  \t# note\n")

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1])

    assert isinstance(preview, Proposal), preview
    assert preview.after_bytes == b"x = 1\n"


def test_a_standalone_explanation_is_preserved_even_on_a_changed_line(tmp_path: Path) -> None:
    """A comment on a line of its own explains the code, so ownership does not
    make it removable."""
    project = project_with(tmp_path, "sample.py", b"x = 1\n# increment the counter\n")

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1, 2])

    assert isinstance(preview, Proposal), preview
    assert preview.after_bytes == b"x = 1\n# increment the counter\n"
    assert rules_by_line(preview)[2] == "STANDALONE"


# --------------------------------------------------------------------------- #
# What it preserves, one case per plan-named category
# --------------------------------------------------------------------------- #

#: (description, source, changed_lines, expected_rule, fragment that must survive)
#:
#: The fragment rather than the whole file, because two cases put an ordinary
#: trailing comment next to the thing being preserved: the point is that the
#: preserved text survives while the ordinary comment does not.
PRESERVED_CASES: tuple[tuple[str, bytes, list[int], str, bytes], ...] = (
    (
        "comment-like text inside a string literal",
        b"message = '# not a comment, and it stays'  # increment the counter\n",
        [1],
        "ORDINARY_TRAILING_COMMENT",
        b"'# not a comment, and it stays'",
    ),
    (
        "a docstring",
        b'def f():\n    """Does a thing.  # not a comment."""\n    return 1  # note\n',
        [3],
        "ORDINARY_TRAILING_COMMENT",
        b'"""Does a thing.  # not a comment."""',
    ),
    (
        "a shebang",
        b"#!/usr/bin/env python3\nx = 1  # note\n",
        [2],
        "SHEBANG",
        b"#!/usr/bin/env python3",
    ),
    (
        "an encoding declaration",
        b"# -*- coding: utf-8 -*-\nx = 1  # note\n",
        [2],
        "ENCODING_DECLARATION",
        b"# -*- coding: utf-8 -*-",
    ),
    (
        "a license header",
        b"# Copyright 2026 The Authors.  All rights reserved.\nx = 1  # note\n",
        [2],
        "HEADER_BLOCK",
        b"# Copyright 2026 The Authors.  All rights reserved.",
    ),
    (
        "a # type: comment",
        b"total = []  # type: list[int]\n",
        [1],
        "TYPE_COMMENT",
        b"# type: list[int]",
    ),
    (
        "a # type: ignore directive",
        b"value = other()  # type: ignore[attr-defined]\n",
        [1],
        "TYPE_IGNORE",
        b"# type: ignore[attr-defined]",
    ),
    (
        "an unknown directive",
        b"value = other()  # pragma: no cover\n",
        [1],
        "PRAGMA_DIRECTIVE",
        b"# pragma: no cover",
    ),
    (
        "an explicit keep marker",
        b"value = other()  # vkit: keep this one\n",
        [1],
        "KEEP_MARKER",
        b"# vkit: keep this one",
    ),
    (
        "a pyre suppression",
        b"value = other()  # pyre-ignore[7]\n",
        [1],
        "PYRE_SUPPRESSION",
        b"# pyre-ignore[7]",
    ),
    (
        "a pyre fixme",
        b"value = other()  # pyre-fixme[56]: infer later\n",
        [1],
        "PYRE_SUPPRESSION",
        b"# pyre-fixme[56]: infer later",
    ),
    (
        "a pyre local mode",
        b"# pyre-strict\nvalue = other()  # note\n",
        [2],
        "PYRE_SUPPRESSION",
        b"# pyre-strict",
    ),
    (
        "a CodeQL lgtm suppression",
        b"value = other()  # lgtm[py/unused-import]\n",
        [1],
        "CODEQL_SUPPRESSION",
        b"# lgtm[py/unused-import]",
    ),
    (
        "a CodeQL codeql suppression",
        b"value = other()  # codeql[py/clear-text-logging]\n",
        [1],
        "CODEQL_SUPPRESSION",
        b"# codeql[py/clear-text-logging]",
    ),
    (
        "a pytype directive",
        b"value = other()  # pytype: disable=wrong-arg-types\n",
        [1],
        "UNKNOWN_DIRECTIVE",
        b"# pytype: disable=wrong-arg-types",
    ),
)


@pytest.mark.parametrize(
    ("description", "source", "changed", "expected_rule", "fragment"),
    PRESERVED_CASES,
    ids=[case[0] for case in PRESERVED_CASES],
)
def test_a_preserved_category_survives_and_is_reported_under_its_own_rule(
    tmp_path: Path,
    description: str,
    source: bytes,
    changed: list[int],
    expected_rule: str,
    fragment: bytes,
) -> None:
    """Every category the plan names survives, and the receipt names which rule
    preserved it, so a reader can see why a comment is still there."""
    project = project_with(tmp_path, "sample.py", source)

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=changed)

    assert isinstance(preview, Proposal), preview
    assert fragment in preview.after_bytes, f"{description} was not preserved"
    assert expected_rule in rules_by_line(preview).values(), rules_by_line(preview)


#: (source, the one comment that must be removable from it)
ORDINARY_PROSE: tuple[tuple[bytes, str], ...] = (
    (b"owner = who  # e-mail the owner\n", "# e-mail the owner"),
    (b"delta = right - left  # off-by-one case\n", "# off-by-one case"),
    (
        b"url = 'https://example.com/a-b'  # see the docs at https://example.com\n",
        "# see the docs at https://example.com",
    ),
)


@pytest.mark.parametrize(
    "source",
    [
        b"value = other()  # notalinter-ignore[7]\n",
        b"value = other()  # unknowntool-strict\n",
        b"value = other()  # somelinter[rule-name]\n",
    ],
    ids=["hyphen plus bracket", "hyphenated local mode", "hyphen plus rule name"],
)
def test_a_colonless_directive_from_an_unlisted_tool_is_still_preserved(
    tmp_path: Path, source: bytes
) -> None:
    """No rule names these tools, which is the point: an enumeration of
    recognised tools cannot say "or any tool not in the table", so the shape
    layer has to. These three are the colonless family that a colon requirement
    cannot see, and each is the shape pyre and CodeQL actually use."""
    project = project_with(tmp_path, "sample.py", source)

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1])

    assert isinstance(preview, Proposal), preview
    assert preview.removed == (), rules_by_line(preview)


@pytest.mark.parametrize(
    ("source", "expected"), ORDINARY_PROSE, ids=[case[1] for case in ORDINARY_PROSE]
)
def test_the_hyphenated_shape_does_not_swallow_ordinary_prose(
    tmp_path: Path, source: bytes, expected: str
) -> None:
    """The shape rule matches a hyphenated tool name, and a hyphenated English
    word has the same shape. This is the counter-test: without it the rule would
    quietly stop removing ordinary comments, which is as wrong as removing a
    directive."""
    project = project_with(tmp_path, "sample.py", source)

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1])

    assert isinstance(preview, Proposal), preview
    assert [c.text for c in preview.removed] == [expected]


def test_a_license_header_in_another_language_is_still_a_license_header(
    tmp_path: Path,
) -> None:
    """The header rule is defined by position, not by reading the text, so a
    licence this tool cannot parse is still recognised as one."""
    project = project_with(
        tmp_path,
        "sample.py",
        b"# Copyright (C) 2026. Licensed under the Apache License, Version 2.0\nx = 1\n",
    )

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1, 2])

    assert isinstance(preview, Proposal), preview
    assert rules_by_line(preview)[1] == "HEADER_BLOCK"
    assert preview.removed == ()


# --------------------------------------------------------------------------- #
# What it refuses
# --------------------------------------------------------------------------- #


def test_a_malformed_file_is_refused_and_left_byte_identical(tmp_path: Path) -> None:
    """A file the tokenizer cannot parse gets a refusal, and the refusal is not
    a proposal: the bytes on disk are what they were."""
    broken = b"def broken(:\n    pass\n"
    project = project_with(tmp_path, "sample.py", broken)

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1])

    assert isinstance(preview, Refusal), preview
    assert preview.reason is RefusalReason.MALFORMED_SOURCE
    assert (project.root / "sample.py").read_bytes() == broken


def test_an_unterminated_string_is_refused_rather_than_crashing(tmp_path: Path) -> None:
    """The tokenizer and the parser can disagree about an unterminated string;
    the answer names which condition applied rather than letting an unhandled
    TokenError escape."""
    project = project_with(tmp_path, "sample.py", b'x = """unterminated\n')

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1])

    assert isinstance(preview, Refusal), preview
    assert preview.reason in (
        RefusalReason.MALFORMED_SOURCE,
        RefusalReason.PRESERVATION_FAILED,
    )
    assert preview.detail


@pytest.mark.parametrize("suffix", [".js", ".ts", ".tsx", ".mjs"])
def test_javascript_and_typescript_are_refused_with_a_message_naming_the_language(
    tmp_path: Path, suffix: str
) -> None:
    """Unsupported languages get a clear refusal, not a crash and not a silent
    pass."""
    original = b"const a = 1; // increment the counter\n"
    project = project_with(tmp_path, f"sample{suffix}", original)

    preview = preview_comment_cleanup(project, f"sample{suffix}", changed_lines=[1])

    assert isinstance(preview, Refusal), preview
    assert preview.reason is RefusalReason.UNSUPPORTED_LANGUAGE
    assert "Python only" in preview.detail
    assert (project.root / f"sample{suffix}").read_bytes() == original


def test_a_path_escape_is_refused(tmp_path: Path) -> None:
    """`../outside.py` resolves outside the root and is refused."""
    project = empty_project(tmp_path)
    write(tmp_path, "outside.py", b"x = 1  # note\n")
    assert (tmp_path / "outside.py").is_file()

    preview = preview_comment_cleanup(project, "../outside.py", changed_lines=[1])

    assert isinstance(preview, Refusal), preview
    assert preview.reason is RefusalReason.PATH_ESCAPE


def test_a_protected_path_is_refused(tmp_path: Path) -> None:
    """`schemas/` and `verification/` are policy, not source, and cleanup does
    not read them."""
    project = project_with(tmp_path, "schemas/manifest.json", b"{}\n")

    preview = preview_comment_cleanup(project, "schemas/manifest.json", changed_lines=[1])

    assert isinstance(preview, Refusal), preview
    assert preview.reason is RefusalReason.PROTECTED_PATH


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    """A path that is not a file has no bytes to propose from."""
    project = empty_project(tmp_path)

    preview = preview_comment_cleanup(project, "absent.py", changed_lines=[1])

    assert isinstance(preview, Refusal), preview
    assert preview.reason is RefusalReason.NOT_A_FILE


# --------------------------------------------------------------------------- #
# The preservation check itself
# --------------------------------------------------------------------------- #


def test_the_checker_reports_a_changed_type_comment_rather_than_accepting_it() -> None:
    """The AST comparison runs with `type_comments=True`, so a proposal that
    also edits a `# type:` comment is caught by the AST comparison itself. This
    is the claim the check makes, tested directly rather than through the
    preview."""
    before = b"total = []  # type: list[int]\nx = 1\n"
    after = b"total = []\nx = 1\n"

    check = verify_preservation(before, after)

    assert isinstance(check, PreservationFailed)
    assert check.reason == "ast_changed"
    assert "type comments 1->0" in check.detail


def test_the_checker_reports_a_dropped_type_ignore() -> None:
    """A `# type: ignore` lives in `module.type_ignores`, so losing it changes
    the type_comments=True AST as well as the protected-comment set."""
    before = b"value = other()  # type: ignore[attr-defined]\n"
    after = b"value = other()\n"

    check = verify_preservation(before, after)

    assert isinstance(check, PreservationFailed)
    assert check.reason in ("ast_changed", "protected_directive_changed")
    if check.reason == "ast_changed":
        assert "type ignores 1->0" in check.detail


def test_the_checker_reports_changed_executable_content() -> None:
    """An equal line count and no dropped directive are not enough when the
    code changed. The AST comparison catches this before the line-by-line
    check, which is the stronger of the two answers."""
    before = b"x = 1  # note\n"
    after = b"x = 2\n"

    check = verify_preservation(before, after)

    assert isinstance(check, PreservationFailed)
    assert check.reason == "ast_changed"


def test_the_checker_reports_an_edited_line_that_keeps_the_same_ast() -> None:
    """Whitespace inside a line is not a comment removal either, and this case
    reaches the line-by-line check because the AST cannot see it."""
    before = b"x = 1  # note\n"
    after = b"x  =  1\n"

    check = verify_preservation(before, after)

    assert isinstance(check, PreservationFailed)
    assert check.reason == "bytes_outside_spans_changed"
    assert "line 1" in check.detail


def test_the_checker_reports_a_changed_line_count() -> None:
    """A proposal that deleted a line is not a comment removal."""
    before = b"x = 1  # note\ny = 2\n"
    after = b"x = 1\n"

    check = verify_preservation(before, after)

    assert isinstance(check, PreservationFailed)
    assert check.reason == "line_count_changed"


def test_the_checker_reports_a_changed_newline_style() -> None:
    """CRLF in and LF out re-encodes the file, which is not surgical."""
    before = b"x = 1  # note\r\ny = 2\r\n"
    after = b"x = 1\ny = 2\n"

    check = verify_preservation(before, after)

    assert isinstance(check, PreservationFailed)
    assert check.reason == "newline_changed"


def test_the_checker_accepts_a_real_comment_removal_and_records_the_facts() -> None:
    """The positive case, so the refusals above are refusals rather than a
    checker that refuses everything."""
    before = b"x = 1  # note\ny = 2\n"
    after = b"x = 1\ny = 2\n"

    check = verify_preservation(before, after)

    assert not isinstance(check, PreservationFailed), check
    assert check.before_digest == hashlib.sha256(before).hexdigest()
    assert check.after_digest == hashlib.sha256(after).hexdigest()
    assert check.newline == "lf"
    assert check.line_count == 2
    assert check.to_json()["result"] == "PASS"


def test_both_the_original_and_the_proposed_bytes_compile(tmp_path: Path) -> None:
    """The plan requires compiling both, and a receipt that exists is evidence
    the check ran. Compiling here proves the two byte strings are loadable."""
    project = project_with(
        tmp_path, "sample.py", b"def f(a):\n    return a + 1  # increment\n"
    )

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[2])

    assert isinstance(preview, Proposal), preview
    assert compile(preview.before_bytes, "<original>", "exec") is not None
    assert compile(preview.after_bytes, "<proposal>", "exec") is not None
    assert ast.dump(
        ast.parse(preview.before_bytes, type_comments=True)
    ) == ast.dump(ast.parse(preview.after_bytes, type_comments=True))


# --------------------------------------------------------------------------- #
# Encoding and newline fidelity
# --------------------------------------------------------------------------- #


def test_a_latin1_file_keeps_its_encoding_and_its_non_ascii_comment_offsets(
    tmp_path: Path,
) -> None:
    """`tokenize` reports character columns, so a non-ASCII character before a
    comment shifts the byte offset. This is the test that would catch a byte
    edit that sliced at the raw column."""
    original = "# -*- coding: latin-1 -*-\nlabel = 'café'  # note\n".encode("latin-1")
    project = project_with(tmp_path, "sample.py", original)

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[2])

    assert isinstance(preview, Proposal), preview
    assert preview.receipt.encoding.lower().replace("-", "") == "iso88591"
    assert preview.after_bytes == "# -*- coding: latin-1 -*-\nlabel = 'café'\n".encode(
        "latin-1"
    )


def test_a_crlf_file_keeps_crlf(tmp_path: Path) -> None:
    """Newline style is part of the file's bytes, so the proposal preserves it."""
    original = b"x = 1  # note\r\ny = 2\r\n"
    project = project_with(tmp_path, "sample.py", original)

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1])

    assert isinstance(preview, Proposal), preview
    assert preview.receipt.newline == "crlf"
    assert preview.after_bytes == b"x = 1\r\ny = 2\r\n"


# --------------------------------------------------------------------------- #
# Cheap answers and convergence
# --------------------------------------------------------------------------- #


def test_a_file_with_nothing_to_remove_is_a_valid_empty_proposal(tmp_path: Path) -> None:
    """An empty proposal is an answer, not an error."""
    project = project_with(tmp_path, "sample.py", b"x = 1\ny = 2\n")

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1, 2])

    assert isinstance(preview, Proposal), preview
    assert preview.removed == ()
    assert preview.after_bytes == b"x = 1\ny = 2\n"
    assert preview.preserved == ()


def test_no_ownership_evidence_proposes_nothing_rather_than_removing_everything(
    tmp_path: Path,
) -> None:
    """With no changed-line set, every comment is off-scope. The safe default is
    a comment-intact file, not a comment-free one."""
    project = project_with(tmp_path, "sample.py", b"x = 1  # note\ny = 2  # other\n")

    preview = preview_comment_cleanup(project, "sample.py")

    assert isinstance(preview, Proposal), preview
    assert preview.removed == ()
    assert preview.after_bytes == b"x = 1  # note\ny = 2  # other\n"


def test_previewing_the_same_file_twice_yields_the_same_proposal(tmp_path: Path) -> None:
    """The proposal id is derived from the bytes, so repeating a request
    converges by arithmetic rather than by remembering state."""
    project = project_with(tmp_path, "sample.py", b"x = 1  # note\ny = 2  # other\n")

    first = preview_comment_cleanup(project, "sample.py", changed_lines=[1, 2])
    second = preview_comment_cleanup(project, "sample.py", changed_lines=[1, 2])

    assert isinstance(first, Proposal), first
    assert isinstance(second, Proposal), second
    assert first.proposal_id == second.proposal_id
    assert first.after_bytes == second.after_bytes
    assert [c.to_json() for c in first.removed] == [c.to_json() for c in second.removed]


def test_a_proposal_is_serialisable_for_a_command_to_print(tmp_path: Path) -> None:
    """The CLI a later checkpoint adds has to render this, so the JSON shape is
    part of the contract rather than an afterthought."""
    project = project_with(tmp_path, "sample.py", b"x = 1  # note\n")

    preview = preview_comment_cleanup(project, "sample.py", changed_lines=[1])

    assert isinstance(preview, Proposal), preview
    document = preview.to_json()
    assert document["result"] == "PROPOSAL"
    assert document["path"] == "sample.py"
    assert len(document["proposalId"]) == 64
    assert document["removed"] == [
        {"line": 1, "text": "# note", "byteStart": 5, "byteEnd": 13}
    ]
    assert document["receipt"]["result"] == "PASS"
    assert document["receipt"]["lineCount"] == 1


def test_a_refusal_is_serialisable_with_its_reason(tmp_path: Path) -> None:
    """A refusal carries the reason a caller has to act on."""
    project = project_with(tmp_path, "sample.ts", b"const a = 1; // note\n")

    document = preview_comment_cleanup(project, "sample.ts").to_json()

    assert document["result"] == "REFUSED"
    assert document["reason"] == "unsupported_language"
