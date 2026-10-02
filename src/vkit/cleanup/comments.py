"""Plan 11 checkpoint 1: propose removing incidental trailing comments from Python.

Nothing here writes. The unit of work is a *proposal* plus a *preservation
receipt*, and the receipt is produced by a checker that never reads the
classifier's opinion of what it was allowed to remove. That separation is the
plan's requirement -- "editorial judgment and preservation are separate" -- and it
is why `verify_preservation` takes two byte strings rather than a proposal: a
checker handed a proposal checks the proposal, not the file.

Four facts were measured on CPython 3.13.14 before any of this was written, and
each one decides a part of the design below.

* `tokenize` reports a comment's position as CHARACTER columns into a line the
  reader has already decoded, so a byte offset is `len(text[:col].encode(codec))`
  and not `col`. Slicing raw bytes at the raw column silently corrupts any file
  containing a non-ASCII character before the comment.
* `tokenize` numbers physical lines from ONE. A 0-based line table addresses
  every comment one line away from the bytes an edit cuts, and the symptom is a
  corrupted proposal rather than an error, so `_split_lines` carries an empty
  placeholder at index 0 and every line number here is that same 1-based number.
* `tokenize` emits a `COMMENT` token only where a comment really is. Comment-like
  text inside a string literal, including inside an f-string's literal parts,
  arrives as `STRING`/`FSTRING_MIDDLE`, so "preserve comment-like text inside
  strings" needs no rule of its own: it falls out of using the tokenizer instead
  of a regular expression, which is what the plan forbids.
* `ast.parse(..., type_comments=True)` keeps `# type: X` in the node's
  `type_comment` and `# type: ignore` in `module.type_ignores`, so an `ast.dump`
  comparison of the two trees already detects a removed type comment or
  type-ignore. That is why the AST check needs no separate type-comment scanner.

## The directive question

The plan requires a closed, defensible enumeration of the comments that survive,
and calls an unenumerated "looks like a directive" heuristic the failure this
checkpoint exists to prevent. The enumeration is `PRESERVED_COMMENT_RULES`;
each entry names the tool that documents it.

Two layers cover the space. The enumerated rules match a named tool's directive,
anchored so they match that directive and not prose beginning with the same
word. The `tool: keyword` SHAPE is then preserved as `UNKNOWN_DIRECTIVE`,
because an unrecognised tool's directive has to survive too and "unrecognised"
is not something an enumeration of recognised tools can express. The shape is
enumerable even when the tool is not, which is the honest way to cover the open
set without guessing at individual tools.

The shape layer makes removal narrower than "any comment that says nothing to a
machine": `# TODO: ...` and `# Note: ...` are preserved. That errs toward
keeping, and it is why the list is presented as a preservation list rather than a
removal list.
"""
from __future__ import annotations

import ast
import codecs
import hashlib
import io
import platform
import re
import tokenize
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Sequence

from ..console.plan import under_protected_path
from ..paths import Project

#: The only language whose comments this checkpoint proposes to remove.
#: JavaScript and TypeScript have no `tokenize` equivalent in the standard
#: library, so there is no preservation check to pair with a rule, and the plan
#: holds them unsupported rather than shipping a rule with nothing to verify it.
SUPPORTED_SUFFIX = ".py"

#: Suffixes refused with a message naming the language rather than the suffix,
#: because "cleanup does not handle .ts" is a worse answer than "cleanup handles
#: Python, and JavaScript and TypeScript are excluded until a parser and a
#: preservation adapter exist".
ECMASCRIPT_SUFFIXES = frozenset(
    {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"}
)


class RefusalReason(str, Enum):
    """Why no proposal was offered. Every value is a reason, never a flag."""

    PATH_ESCAPE = "path_escape"
    PROTECTED_PATH = "protected_path"
    NOT_A_FILE = "not_a_file"
    UNREADABLE = "unreadable"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    MALFORMED_SOURCE = "malformed_source"
    PRESERVATION_FAILED = "preservation_failed"


# --------------------------------------------------------------------------- #
# The preserved-comment registry
# --------------------------------------------------------------------------- #
#
# One ordered tuple, one place. The order IS the precedence: a comment is
# classified by the first rule it matches, so `# type: ignore` reports as a
# type-ignore rather than as the generic `# type:` comment that would otherwise
# match it. Each entry is (rule_id, description, pattern), matched against the
# comment body with its leading `#` and surrounding whitespace removed. Every
# pattern is anchored at the start and case-insensitive unless the tool
# documents that spelling as case-sensitive.

#: PEP 263's own regular expression, verbatim. Widening it would mean guessing
#: which near-miss spellings a real file uses, and the standard's wording is the
#: one place the answer is authoritative. https://peps.python.org/pep-0263/
_PEP_263 = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)")

_PRESERVED: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "TYPE_IGNORE",
        "`# type: ignore` and its bracketed `# type: ignore[code]` form. PEP 484 "
        "gives them meaning; mypy reads them and errors on an unused one under "
        "--warn-unused-ignores, so deleting one is a behaviour change rather "
        "than a cosmetic one. https://peps.python.org/pep-0484/#type-comments",
        re.compile(r"^type\s*:\s*ignore\b", re.IGNORECASE),
    ),
    (
        "TYPE_COMMENT",
        "A PEP 484 `# type:` comment. ast.parse(type_comments=True) records it "
        "on the node, making it executable-adjacent metadata rather than prose. "
        "https://peps.python.org/pep-0484/#type-comments",
        re.compile(r"^type\s*:"),
    ),
    (
        "NOQA",
        "flake8's `# noqa` and `# noqa: <codes>`, case-insensitive and able to "
        "carry several codes. flake8 documents both spellings and that the "
        "comment may follow other text on the line. "
        "https://flake8.pycqa.org/en/latest/user/violations.html",
        re.compile(r"^noqa\b", re.IGNORECASE),
    ),
    (
        "FLAKE8_FILE_DIRECTIVE",
        "flake8's file-level `# flake8: noqa`, which suppresses the whole file "
        "when it stands alone on its line. "
        "https://flake8.pycqa.org/en/latest/user/configuration.html",
        re.compile(r"^flake8\s*:\s*noqa\b", re.IGNORECASE),
    ),
    (
        "RUFF_FILE_DIRECTIVE",
        "ruff's file-level `# ruff: noqa` and `# ruff: noqa: E501`, which "
        "suppress the whole file or a named set of rules. "
        "https://docs.astral.sh/ruff/configuration/#lint",
        re.compile(r"^ruff\s*:\s*noqa\b", re.IGNORECASE),
    ),
    (
        "PYLINT_DIRECTIVE",
        "pylint's `# pylint: disable=...`, and its `enable`, `skip-file` and "
        "`disable-next` spellings. pylint reads the message names out of the "
        "comment, so the text is the configuration. "
        "https://pylint.readthedocs.io/en/latest/user_guide/configuration/all-options.html",
        re.compile(r"^pylint\s*:", re.IGNORECASE),
    ),
    (
        "MYPY_FILE_DIRECTIVE",
        "mypy's file-level `# mypy: <options>` configuration comment. Its "
        "line-level `# type: ignore` is TYPE_IGNORE above. "
        "https://mypy.readthedocs.io/en/stable/config_file.html",
        re.compile(r"^mypy\s*:", re.IGNORECASE),
    ),
    (
        "PYRIGHT_DIRECTIVE",
        "pyright's `# pyright: <setting>`, covering the line-level "
        "`# pyright: ignore[reportGeneralTypeIssues]` and file-level rules such "
        "as `# pyright: strict`. "
        "https://microsoft.github.io/pyright/#/configuration",
        re.compile(r"^pyright\s*:", re.IGNORECASE),
    ),
    (
        "FMT_DIRECTIVE",
        "black's and ruff-format's `# fmt: off`, `# fmt: on` and `# fmt: skip`. "
        "`# fmt: skip` is attached to the line it suppresses, which is exactly "
        "the trailing position this tool would otherwise edit. "
        "https://docs.astral.sh/ruff/formatter/#comment-handling",
        re.compile(r"^fmt\s*:", re.IGNORECASE),
    ),
    (
        "ISORT_DIRECTIVE",
        "isort's `# isort: skip`, `off`, `on` and `skip_file`. isort documents "
        "the line-level and the file-level spellings. "
        "https://pycqa.github.io/isort/docs/configuration/options.html#skip",
        re.compile(r"^isort\s*:", re.IGNORECASE),
    ),
    (
        "YAPF_DIRECTIVE",
        "YAPF's `# yapf: disable` and `# yapf: enable`. "
        "https://github.com/google/yapf/blob/main/docs/usage.md",
        re.compile(r"^yapf\s*:", re.IGNORECASE),
    ),
    (
        "PRAGMA_DIRECTIVE",
        "coverage.py's `# pragma: no cover` and `# pragma: no branch`. Both are "
        "matched against the coverage configuration's own exclude regex, so a "
        "`pragma:` name a project configures is honoured rather than only the "
        "two shipped defaults. "
        "https://coverage.readthedocs.io/en/latest/excluding.html",
        re.compile(r"^pragma\s*:", re.IGNORECASE),
    ),
    (
        "NOSEC",
        "bandit's `# nosec` and `# nosec B101`, optionally with several codes. "
        "https://bandit.readthedocs.io/en/latest/core/#excluding-lines",
        re.compile(r"^nosec\b", re.IGNORECASE),
    ),
    (
        "NOSEMGREP",
        "semgrep's `# nosemgrep`, which suppresses findings on that line. "
        "https://semgrep.dev/docs/writing-rules/ignoring-findings",
        re.compile(r"^nosemgrep\b", re.IGNORECASE),
    ),
    (
        "CODEQL_SUPPRESSION",
        "CodeQL's `# security: ignore` suppression comment for Python. "
        "https://docs.github.com/en/code-security/code-scanning",
        re.compile(r"^security\s*:", re.IGNORECASE),
    ),
    (
        "SPDX",
        "SPDX tag-value license headers, `# SPDX-License-Identifier:` and "
        "`# SPDX-FileCopyrightText:`. The tag is the standard's, not this "
        "project's, so a header written for a different license still matches. "
        "https://spdx.github.io/spdx-spec/v2.3/tag-values/",
        re.compile(r"^SPDX-[A-Za-z0-9-]+\s*:"),
    ),
    (
        "UNKNOWN_DIRECTIVE",
        "The `tool: keyword` shape, which is how every tool in this table and "
        "every tool NOT in it writes its directive. This is the layer that makes "
        "an enumerated list honest: an unrecognised tool has to survive too, and "
        "a closed list of recognised tools cannot say that. A bare URL (`:` not "
        "followed by `//`) is excluded so a reference link in prose is not read "
        "as a directive. The cost is that `# TODO: ...` and `# Note: ...` are "
        "preserved, which narrows removal rather than widening it.",
        re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*\s*:(?!//)"),
    ),
)

#: Rule id, description, and pattern, in precedence order.
PRESERVED_COMMENT_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = _PRESERVED

#: The marker recognised as an explicit request to leave a comment alone.
#: Namespaced so it cannot collide with a project's own vocabulary. A project
#: with its own marker passes it to `preview_comment_cleanup`; the mechanism is
#: here and the vocabulary is policy, which is where the plan puts it.
DEFAULT_KEEP_MARKERS: tuple[str, ...] = (r"^vkit\s*:\s*keep\b",)

#: Rule ids for a comment preserved by where it is rather than by what it says.
SHEBANG_RULE = "SHEBANG"
ENCODING_RULE = "ENCODING_DECLARATION"
HEADER_RULE = "HEADER_BLOCK"
STANDALONE_RULE = "STANDALONE"
BRACKET_RULE = "BRACKET_INTERIOR"
NOT_TASK_OWNED_RULE = "NOT_TASK_OWNED"
KEEP_MARKER_RULE = "KEEP_MARKER"
TRAILING_RULE = "ORDINARY_TRAILING_COMMENT"

_CLOSING = ")]}"
_OPENING = "([{"


# --------------------------------------------------------------------------- #
# The result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CommentSite:
    """One comment the tokenizer located, with the rule that decided its fate."""

    line: int
    text: str
    rule_id: str

    def to_json(self) -> dict[str, Any]:
        return {"line": self.line, "text": self.text, "rule": self.rule_id}


@dataclass(frozen=True)
class RemovedComment:
    """One comment a proposal would delete, addressed by a byte span.

    The span covers the whitespace run before the comment as well as the
    comment, so applying it leaves no trailing whitespace behind. It is a byte
    offset into the ORIGINAL file, which is what makes the edit surgical and
    re-derivable rather than a rewrite.
    """

    line: int
    text: str
    byte_start: int
    byte_end: int

    def to_json(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "text": self.text,
            "byteStart": self.byte_start,
            "byteEnd": self.byte_end,
        }


@dataclass(frozen=True)
class PreservationPassed:
    """The receipt. It states what was compared and on which runtime."""

    before_digest: str
    after_digest: str
    python_version: str
    encoding: str
    newline: str
    line_count: int
    ast_digest: str
    type_comments: int
    type_ignores: int
    protected_directives: int
    preserved_rules: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "PASS",
            "beforeDigest": self.before_digest,
            "afterDigest": self.after_digest,
            "pythonVersion": self.python_version,
            "encoding": self.encoding,
            "newline": self.newline,
            "lineCount": self.line_count,
            "astDigest": self.ast_digest,
            "typeComments": self.type_comments,
            "typeIgnores": self.type_ignores,
            "protectedDirectives": self.protected_directives,
            "preservedRules": list(self.preserved_rules),
        }


@dataclass(frozen=True)
class PreservationFailed:
    """The checker found the two byte strings are not interchangeable."""

    reason: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"result": "FAIL", "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class Proposal:
    """An offered edit. Nothing has been written; `after_bytes` is a proposal."""

    relative_path: str
    proposal_id: str
    before_bytes: bytes
    after_bytes: bytes
    removed: tuple[RemovedComment, ...]
    preserved: tuple[CommentSite, ...]
    receipt: PreservationPassed

    @property
    def before_digest(self) -> str:
        return self.receipt.before_digest

    @property
    def after_digest(self) -> str:
        return self.receipt.after_digest

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "PROPOSAL",
            "path": self.relative_path,
            "proposalId": self.proposal_id,
            "beforeDigest": self.receipt.before_digest,
            "afterDigest": self.receipt.after_digest,
            "removed": [c.to_json() for c in self.removed],
            "preserved": [c.to_json() for c in self.preserved],
            "receipt": self.receipt.to_json(),
        }


@dataclass(frozen=True)
class Refusal:
    """No proposal. `reason` is required, which is the point of this variant."""

    relative_path: str
    reason: RefusalReason
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "result": "REFUSED",
            "path": self.relative_path,
            "reason": self.reason.value,
            "detail": self.detail,
        }


#: The sum. A preview either proposes or refuses; it is never "proposed with
#: problems", and a refusal always names which of the reasons above applied.
CleanupPreview = Proposal | Refusal

#: The checker's own sum, exposed so a caller can verify a byte pair directly
#: without going through a preview.
PreservationCheck = PreservationPassed | PreservationFailed


# --------------------------------------------------------------------------- #
# Bytes and lines
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Source:
    """A decoded file, split so a byte offset can be found without guessing.

    `lines` is indexed the way `tokenize` reports rows: a 1-based index whose
    element 0 is an empty placeholder. See the module docstring for why that
    index is load-bearing rather than cosmetic.
    """

    data: bytes
    encoding: str
    text: str
    lines: tuple[bytes, ...]

    @property
    def line_count(self) -> int:
        return len(self.lines) - 1


def _split_lines(data: bytes) -> tuple[bytes, ...]:
    """Physical lines, each keeping its own terminator, indexed as `tokenize` indexes.

    Splits on CR, LF and CRLF because that is what the tokenizer counts as a
    line, and it treats a lone CR as a terminator for the same reason. A file
    whose newline style is CRLF therefore keeps CRLF through the whole edit.
    """
    lines: list[bytes] = [b""]
    start = 0
    index = 0
    length = len(data)
    while index < length:
        byte = data[index]
        if byte == 0x0D:
            stop = index + 2 if data[index + 1 : index + 2] == b"\n" else index + 1
            lines.append(data[start:stop])
            start = index = stop
        elif byte == 0x0A:
            lines.append(data[start : index + 1])
            index += 1
            start = index
        else:
            index += 1
    if start < length:
        lines.append(data[start:])
    return tuple(lines)


def _newline_style(data: bytes) -> str:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf and not lf:
        return "crlf"
    if lf and not crlf:
        return "lf"
    if crlf and lf:
        return "mixed"
    return "none"


def _base_codec(encoding: str) -> str:
    """The codec that re-encodes a line's text to that line's bytes.

    `utf-8-sig` decodes a BOM away but re-encodes one back in, so measuring a
    prefix with it would add three bytes the file does not have there.
    """
    return codecs.lookup(encoding).name


def _column_to_byte(line: bytes, encoding: str, column: int) -> int:
    """Byte offset within one raw line of a CHARACTER column from the tokenizer."""
    return len(line.decode(encoding)[:column].encode(_base_codec(encoding)))


def _read_source(data: bytes) -> _Source:
    """Decode bytes the way the interpreter does, BOM included.

    `detect_encoding` is the same call the tokenizer makes, so the codec used to
    turn character columns back into byte offsets is the codec Python itself
    would have used to read the file.
    """
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return _Source(
        data=data, encoding=encoding, text=data.decode(encoding), lines=_split_lines(data)
    )


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Located:
    """A comment plus where it sits, before any preservation rule is applied.

    `row` is the tokenizer's own 1-based line number, and `line` is that same
    number, so a caller reports lines the way Python's tracebacks do.
    """

    row: int
    col: int
    end_col: int
    text: str
    depth: int
    standalone: bool
    header: bool

    @property
    def key(self) -> tuple[int, int]:
        return (self.row, self.col)

    @property
    def line(self) -> int:
        return self.row


def _body(text: str) -> str:
    return text[1:].strip()


def _keep_patterns(keep_markers: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(marker, re.IGNORECASE) for marker in keep_markers)


def _content_rule(body: str, keep_patterns: Sequence[re.Pattern[str]]) -> str | None:
    """The preservation rule matching a comment body, or None to remove it.

    Keep markers run first: a marker means "do not touch this" whatever the body
    also happens to say, so it must not lose to a rule that happens to be listed
    earlier.
    """
    for pattern in keep_patterns:
        if pattern.match(body):
            return KEEP_MARKER_RULE
    for rule_id, _description, pattern in PRESERVED_COMMENT_RULES:
        if pattern.match(body):
            return rule_id
    return None


def _locate(text: str, encoding: str, lines: Sequence[bytes]) -> list[_Located]:
    """Every comment the tokenizer found, with bracket depth and header membership.

    A header is the run of comment-only lines before the first line carrying
    code. It is defined by position rather than by reading it, so a licence
    header in a language this tool has never seen is still recognised as one.

    Bracket depth is read at the comment's own position, so a comment that
    follows a CLOSING bracket reports depth zero. That is the honest reading:
    `value = f(a)  # note` is a trailing comment on a complete line, and the
    plan's trailing rule is about position on the line, not about whether some
    bracket opened earlier in the file.
    """
    found: list[_Located] = []
    depth = 0
    seen_code = False
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.OP:
            if token.string in _CLOSING:
                depth = max(0, depth - 1)
            elif token.string in _OPENING:
                depth += 1
        elif token.type == tokenize.COMMENT:
            row, column = token.start
            raw = lines[row] if 0 <= row < len(lines) else b""
            prefix = _column_to_byte(raw, encoding, column)
            found.append(
                _Located(
                    row=row,
                    col=column,
                    end_col=token.end[1],
                    text=token.string,
                    depth=depth,
                    standalone=raw[:prefix].strip(b" \t\f") == b"",
                    header=not seen_code,
                )
            )
        elif token.type not in (
            tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
            tokenize.ENCODING, tokenize.ENDMARKER,
        ):
            seen_code = True
    return found


def _classify(
    site: _Located, keep_patterns: Sequence[re.Pattern[str]], changed: frozenset[int]
) -> str:
    """The one rule that decides this comment's fate, in precedence order.

    Position rules run before the ownership check so that a standalone comment
    on a changed line reports as STANDALONE. Both are refusals to remove, and the
    difference is only which reason the receipt records.
    """
    if site.row == 1 and site.text.startswith("#!"):
        return SHEBANG_RULE
    if site.row in (1, 2) and _PEP_263.match(site.text):
        return ENCODING_RULE
    by_content = _content_rule(_body(site.text), keep_patterns)
    if by_content is not None:
        return by_content
    if site.header:
        return HEADER_RULE
    if site.standalone:
        return STANDALONE_RULE
    if site.depth:
        return BRACKET_RULE
    if site.line not in changed:
        return NOT_TASK_OWNED_RULE
    return TRAILING_RULE


def _spans(
    source: _Source, located: Sequence[_Located], rules: dict[tuple[int, int], str]
) -> list[tuple[int, int, _Located]]:
    """Byte spans to delete, one per removable comment, as (start, end, site).

    A span runs from the first whitespace byte before the comment to the end of
    the comment, so deleting it leaves the code with no trailing whitespace. Its
    end is computed from the tokenizer's END column through the codec, so a
    comment containing non-ASCII text does not shift the offset.

    Line starts come from `_line_starts` over the whole split rather than from a
    running total advanced per comment, because a line carrying no comment still
    occupies bytes.
    """
    spans: list[tuple[int, int, _Located]] = []
    starts = _line_starts(source.lines)
    for site in located:
        if rules.get(site.key) != TRAILING_RULE:
            continue
        raw = source.lines[site.row]
        line_start = starts[site.row]
        comment_at = _column_to_byte(raw, source.encoding, site.col)
        comment_end = _column_to_byte(raw, source.encoding, site.end_col)
        cut = comment_at
        while cut > 0 and raw[cut - 1 : cut] in (b" ", b"\t", b"\f"):
            cut -= 1
        spans.append((line_start + cut, line_start + comment_end, site))
    return spans


def _line_starts(lines: Sequence[bytes]) -> tuple[int, ...]:
    """Byte offset at which each entry of the split begins, in its own indexing."""
    starts: list[int] = []
    running = 0
    for line in lines:
        starts.append(running)
        running += len(line)
    return tuple(starts)


def _delete(source: _Source, spans: Sequence[tuple[int, int, _Located]]) -> bytes:
    """Apply spans to the raw bytes. Every byte outside a span is copied through."""
    out = bytearray()
    cursor = 0
    for start, end, _site in spans:
        out += source.data[cursor:start]
        cursor = end
    out += source.data[cursor:]
    return bytes(out)


# --------------------------------------------------------------------------- #
# The preservation check
# --------------------------------------------------------------------------- #


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _protected(text: str, keep_patterns: Sequence[re.Pattern[str]]) -> list[tuple[int, str, str]]:
    """Every protected comment as (line, rule_id, text), sorted by position.

    `tokenize` rows are already 1-based, which is the same numbering `_Located`
    reports, so the two sets can be compared directly.
    """
    found: list[tuple[int, str, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT:
            continue
        rule = _content_rule(_body(token.string), keep_patterns)
        if rule is not None:
            found.append((token.start[0], rule, token.string))
    return sorted(found)


def _ast_facts(text: str) -> tuple[str, int, int]:
    """(dump, type-comment count, type-ignore count) for one source text.

    Attributes are excluded from the dump. Removing a trailing comment moves no
    line, and this checker separately requires the line count to be unchanged,
    so positional attributes could only add noise. What the dump DOES carry, even
    without attributes, is every `type_comment` string and the `type_ignores`
    list, which is the entire reason this parses with `type_comments=True`.
    """
    tree = ast.parse(text, type_comments=True)
    dump = ast.dump(tree, include_attributes=False)
    type_comments = sum(
        1 for node in ast.walk(tree) if getattr(node, "type_comment", None) is not None
    )
    return dump, type_comments, len(tree.type_ignores)


def _divergence(
    original: _Source, proposed: _Source, protected_lines: frozenset[int]
) -> tuple[int, bytes, bytes] | None:
    """The first line on which the two files differ in a way this forbids.

    Returns None when every line is byte-identical, and otherwise the 1-based
    line number with both raw lines, so the refusal can name them.

    A line may differ in exactly one way: the original carried a real comment
    that the proposal does not. Everything before that comment must be
    byte-identical modulo the whitespace the comment's own indent occupied, and
    the comment must not be protected.

    The comment's position comes from the TOKENIZER, never from a search for
    `#`. A line such as `message = '# not a comment'  # note` has two hashes in
    it and only the second is a comment; a text search would cut the line at the
    first one and call a string literal code.

    This check is independent of the classifier in the sense that matters: it
    never asks which comments the classifier chose to remove. For each line it
    asks whether the proposal is that line with a non-protected comment deleted.
    A classifier that also rewrote code, deleted a line, or dropped a directive
    produces a divergence here.
    """
    comment_columns: dict[int, int] = {}
    for site in _locate(original.text, original.encoding, original.lines):
        comment_columns.setdefault(site.row, site.col)

    for row in range(1, min(len(original.lines), len(proposed.lines))):
        before_line = original.lines[row]
        after_line = proposed.lines[row]
        if before_line == after_line:
            continue
        if row in comment_columns and _only_lost_a_comment(
            before_line, after_line, original.encoding, comment_columns[row], row,
            protected_lines,
        ):
            continue
        return row, before_line, after_line
    return None


def _only_lost_a_comment(
    before_line: bytes,
    after_line: bytes,
    encoding: str,
    comment_column: int,
    row: int,
    protected_lines: frozenset[int],
) -> bool:
    """Whether `after_line` is `before_line` with the comment at `comment_column` removed.

    `comment_column` is the tokenizer's CHARACTER column, converted through the
    declared encoding, so a non-ASCII character earlier on the line does not
    shift it. The tolerated difference is exactly the whitespace run the
    comment's leading indent occupied, so deleting `x = 1  # note` may yield
    `x = 1` but never `x =  1` and never `x =  2`.

    A line that keeps only whitespace where the original had code is not a
    comment removal, and a line carrying no comment cannot differ this way.
    """
    if not before_line.strip(b" \t\f\r\n"):
        return False
    comment_at = _column_to_byte(before_line, encoding, comment_column)
    code = before_line[:comment_at].decode(encoding).rstrip(" \t\f")
    terminator = before_line[len(before_line.rstrip(b"\r\n")) :].decode(encoding)
    if after_line.decode(encoding) != code + terminator:
        return False
    return row not in protected_lines


def verify_preservation(
    before: bytes, after: bytes, *, keep_markers: Sequence[str] = DEFAULT_KEEP_MARKERS
) -> PreservationCheck:
    """Establish that two byte strings are interchangeable, or say why they are not.

    Takes the bytes rather than the proposal. A checker handed a proposal is
    checking the proposal; this one reads both files for itself, so a classifier
    that removed something it had no business removing is caught instead of
    confirmed.

    Six comparisons, each answering a different question:

    * encoding and newline style are unchanged, so the edit preserved the file's
      shape rather than re-encoding it;
    * line count is unchanged;
    * the `type_comments=True` AST dumps are equal, which is the plan's "identical
      executable content" including type-comment information. This runs BEFORE
      the protected-comment comparison so that the AST check is the one that
      catches a changed `# type:` comment, which is what the plan asks it to
      catch;
    * the protected-comment sets are equal, which is the plan's "identical
      protected directives" and catches every directive the table names;
    * both texts compile, because equal ASTs of two uncompilable files certify
      nothing;
    * every line is byte-identical or differs only by having a non-protected
      comment removed, checked against the BEFORE bytes so no byte outside a
      removed comment can have moved.
    """
    keep_patterns = _keep_patterns(keep_markers)

    try:
        original = _read_source(before)
        proposed = _read_source(after)
    except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
        return PreservationFailed("undecodable_source", str(exc))

    if original.encoding != proposed.encoding:
        return PreservationFailed(
            "encoding_changed",
            f"encoding moved from {original.encoding} to {proposed.encoding}",
        )
    before_style, after_style = _newline_style(before), _newline_style(after)
    if before_style != after_style:
        return PreservationFailed(
            "newline_changed", f"newline style moved from {before_style} to {after_style}"
        )
    if original.line_count != proposed.line_count:
        return PreservationFailed(
            "line_count_changed",
            f"line count moved from {original.line_count} to {proposed.line_count}",
        )

    try:
        before_dump, before_tc, before_ti = _ast_facts(original.text)
    except (SyntaxError, ValueError) as exc:
        return PreservationFailed("original_does_not_parse", str(exc))
    try:
        after_dump, after_tc, after_ti = _ast_facts(proposed.text)
    except (SyntaxError, ValueError) as exc:
        return PreservationFailed("proposal_does_not_parse", str(exc))
    if before_dump != after_dump:
        return PreservationFailed(
            "ast_changed",
            "the type_comments=True AST differs, so executable content or a "
            f"# type: comment changed (type comments {before_tc}->{after_tc}, "
            f"type ignores {before_ti}->{after_ti})",
        )

    before_protected = _protected(original.text, keep_patterns)
    after_protected = _protected(proposed.text, keep_patterns)
    if before_protected != after_protected:
        lost = [entry for entry in before_protected if entry not in after_protected]
        return PreservationFailed(
            "protected_directive_changed",
            f"protected comments differ; {len(lost)} present in the original are "
            f"not present in the proposal: {lost[:3]!r}",
        )

    for label, text in (("original", original.text), ("proposal", proposed.text)):
        try:
            compile(text, f"<cleanup-{label}>", "exec")
        except (SyntaxError, ValueError) as exc:
            return PreservationFailed(f"{label}_does_not_compile", str(exc))

    protected_lines = frozenset(line for line, _rule, _text in before_protected)
    divergent = _divergence(original, proposed, protected_lines)
    if divergent is not None:
        row, before_line, after_line = divergent
        return PreservationFailed(
            "bytes_outside_spans_changed",
            f"line {row} differs in a way that is not a removed comment: "
            f"{before_line!r} became {after_line!r}",
        )

    return PreservationPassed(
        before_digest=_digest(before),
        after_digest=_digest(after),
        python_version=platform.python_version(),
        encoding=original.encoding,
        newline=before_style,
        line_count=original.line_count,
        ast_digest=_digest(before_dump.encode("utf-8")),
        type_comments=before_tc,
        type_ignores=before_ti,
        protected_directives=len(before_protected),
        preserved_rules=tuple(sorted({rule for _line, rule, _text in before_protected})),
    )


# --------------------------------------------------------------------------- #
# The proposal
# --------------------------------------------------------------------------- #


def preview_comment_cleanup(
    project: Project,
    relative_path: str,
    *,
    changed_lines: Iterable[int] | None = None,
    keep_markers: Sequence[str] = DEFAULT_KEEP_MARKERS,
) -> CleanupPreview:
    """Propose removing approved incidental trailing comments from one file.

    Nothing is written. The caller gets either a `Proposal` carrying the after
    bytes and the receipt that justifies them, or a `Refusal` naming which of
    the refusable conditions applied.

    `changed_lines` is the bound task's 1-based set of changed lines, and it is
    the plan's default automatic scope: a trailing comment is removable only on a
    line the task changed. `None` means no ownership evidence was supplied, so
    the set is empty and the result is a well-formed proposal that removes
    nothing. That default errs toward the safe direction: a wrong guess in this
    direction costs a missed cleanup, and a wrong guess in the other direction
    costs an edit outside the task's scope.

    `keep_markers` adds project-specific spellings to the default `# vkit: keep`.
    """
    refused = _check_path(project, relative_path)
    if refused is not None:
        return refused

    try:
        source = _read_source((project.root / relative_path).read_bytes())
    except OSError as exc:
        return Refusal(relative_path, RefusalReason.UNREADABLE, str(exc))
    except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
        return Refusal(relative_path, RefusalReason.MALFORMED_SOURCE, str(exc))

    keep_patterns = _keep_patterns(keep_markers)
    changed = frozenset(changed_lines or ())

    try:
        located = _locate(source.text, source.encoding, source.lines)
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        return Refusal(relative_path, RefusalReason.MALFORMED_SOURCE, str(exc))

    rules = {site.key: _classify(site, keep_patterns, changed) for site in located}
    spans = _spans(source, located, rules)
    after_bytes = _delete(source, spans)

    check = verify_preservation(source.data, after_bytes, keep_markers=keep_markers)
    if isinstance(check, PreservationFailed):
        return Refusal(
            relative_path,
            RefusalReason.PRESERVATION_FAILED,
            f"{check.reason}: {check.detail}",
        )

    preserved = tuple(
        CommentSite(line=site.line, text=site.text, rule_id=rules[site.key])
        for site in located
    )
    return Proposal(
        relative_path=relative_path,
        proposal_id=_proposal_id(relative_path, check, spans),
        before_bytes=source.data,
        after_bytes=after_bytes,
        removed=tuple(
            RemovedComment(line=site.line, text=site.text, byte_start=start, byte_end=end)
            for start, end, site in spans
        ),
        preserved=preserved,
        receipt=replace(
            check, preserved_rules=tuple(sorted({site.rule_id for site in preserved}))
        ),
    )


def _proposal_id(
    relative_path: str,
    check: PreservationPassed,
    removed: Sequence[tuple[int, int, _Located]],
) -> str:
    """A deterministic name for this edit.

    Derived from the path, the two digests, the runtime and the removed comments,
    with no clock and no counter. Previewing the same file twice therefore yields
    the same id, and a later apply can bind to it without carrying any state:
    repeating a request converges by arithmetic rather than by remembering what
    happened last time.
    """
    running = hashlib.sha256()
    for field in (
        relative_path,
        check.before_digest,
        check.after_digest,
        check.python_version,
        ",".join(site.text for _start, _end, site in removed),
    ):
        running.update(field.encode("utf-8"))
        running.update(b"\x00")
    return running.hexdigest()


def _check_path(project: Project, relative_path: str) -> Refusal | None:
    """Refuse a path this package must not touch, before opening anything.

    The escape check compares resolved paths, which is what catches `../`, an
    absolute path, and `a/../../b`. Comparing strings would not: `a/../../b` and
    `b` name the same file.
    """
    if under_protected_path(relative_path):
        return Refusal(
            relative_path,
            RefusalReason.PROTECTED_PATH,
            "this path is repository policy rather than source, and cleanup does "
            "not read it",
        )
    candidate = (project.root / relative_path).resolve()
    root = project.root.resolve()
    if candidate != root and root not in candidate.parents:
        return Refusal(
            relative_path,
            RefusalReason.PATH_ESCAPE,
            f"the path resolves outside the project root {root}",
        )
    suffix = candidate.suffix.lower()
    if suffix in ECMASCRIPT_SUFFIXES:
        return Refusal(
            relative_path,
            RefusalReason.UNSUPPORTED_LANGUAGE,
            f"cleanup supports Python only. {suffix} is JavaScript or TypeScript, "
            "which stay unsupported for automatic cleanup until a parser and a "
            "preservation adapter exist (Plan 11). The file is unchanged.",
        )
    if suffix != SUPPORTED_SUFFIX:
        return Refusal(
            relative_path,
            RefusalReason.UNSUPPORTED_LANGUAGE,
            f"cleanup supports {SUPPORTED_SUFFIX} only, and this file is "
            f"{suffix or 'extensionless'}",
        )
    if not candidate.is_file():
        return Refusal(
            relative_path, RefusalReason.NOT_A_FILE, f"{candidate} is not a file"
        )
    return None
