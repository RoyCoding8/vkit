"""The release documents must stay true to the code they describe.

A checklist exists to stop a broken thing from shipping. A checklist that names
a command nobody wrote is worse than no checklist, because it reads as
verification. So this file treats both documents as untrusted input and checks
every claim in them against the repository.

Three rules shape the checks.

**A path is either verified or declared absent.** The documents use two closed
worlds. A `text files` block lists a path that must exist. A `text absent` block
lists a path that must NOT exist. Any other path-shaped token in either document
is a failure, which is what stops a new claim from appearing in prose where no
gate checks it. The absent block is checked in the other direction too, so a
document cannot keep calling a file missing after the file arrives.

**A command is either real or the test fails.** Every line of a
`console command` block is resolved against the repository. A `vkit` line is
walked through `build_parser()` one subcommand at a time, and its flags are
checked against that subparser's own option strings, so an invented flag fails
as loudly as an invented subcommand. A line whose shape the test does not
understand is a failure rather than a skip, so extending the checklist means
teaching this file, which is a deliberate act.

**A gap has an id.** Both documents name every known gap by id. Renaming or
quietly dropping one fails here instead of reaching a release note.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

CHECKLIST = ROOT / "docs" / "RELEASE-CHECKLIST.md"
PILOT = ROOT / "docs" / "PILOT.md"

# Placeholders name a location the release host supplies, or a path that only
# exists inside the built wheel, so they are named in prose and never resolved
# against this repository.
PLACEHOLDERS = (
    "<venv>",
    "<repo>",
    "<enrolled-repo>",
    "<example>",
    "<dist>",
    "<git-common-dir>",
    "<run-id>",
    "<check-id>",
    "<path>",
    "vkit/_schemas",
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.<>-]+(?:/[A-Za-z0-9_.<>-]+)+")
# "22/22" is a ratio and not a path. A path claim ends in a name, so require a
# letter or a placeholder somewhere in the last segment.
_NAME_RE = re.compile(r"[A-Za-z<>]")
_FENCE_RE = re.compile(r"^```(console command|text files|text absent)\s*$")

# Every gap carried into the release, with the tokens that pin its meaning.
# A gap that loses its evidence loses the ability to be reported honestly, so
# the test fails on a rewording that drops the substance.
GAPS: list[tuple[str, tuple[str, ...]]] = [
    ("GAP-1", ("mcp", "SDK", "unexecuted")),
    ("GAP-2", ("POSIX", "Windows", "unverified")),
    ("GAP-3", ("validate", "host session")),
    ("GAP-4", ("mcp serve", "build_parser")),
    ("GAP-5", ("console", "Plan 05")),
    ("GAP-6", ("enroll", "integration verify")),
    ("GAP-7", ("acceptance.py", "editable install")),
    ("GAP-8", ("KIT_ACCEPTANCE.md", "absent")),
    ("GAP-9", (".cmd", "reverted")),
]

# Commands named in prose as missing. Listing one of these as a runnable step
# would be the exact failure this file exists to prevent.
ABSENT_COMMANDS = ("mcp serve", "hook", "task begin", "setup")


# --------------------------------------------------------------------- reading


def _read(path: Path) -> str:
    if not path.is_file():
        pytest.fail(f"{path.name} does not exist, so it cannot be reviewed")
    return path.read_text(encoding="utf-8")


def _blocks(text: str) -> dict[str, list[list[str]]]:
    """Split a document into its typed fenced blocks.

    The fence info string carries the type, so prose that shows a command for
    illustration stays out of the checked set while a step a maintainer is told
    to run goes in.
    """
    found: dict[str, list[list[str]]] = {}
    open_kind: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("```"):
            if open_kind is None:
                kind = _FENCE_RE.match(line)
                if kind:
                    open_kind, buf = kind.group(1), []
            else:
                # Markdown closes a block with a bare fence, so the closing
                # fence never repeats the info string.
                found.setdefault(open_kind, []).append(
                    [ln for ln in buf if ln.strip()]
                )
                open_kind = None
            continue
        if open_kind is not None:
            buf.append(line)
    if open_kind is not None:
        pytest.fail(f"an unclosed ```{open_kind} block")
    return found


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text) if _NAME_RE.search(t.split("/")[-1])]


def _strip_fences(text: str) -> str:
    kept, inside = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            inside = not inside
            continue
        if not inside:
            kept.append(line)
    return "\n".join(kept)


# ----------------------------------------------------------------- vkit parser


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    return {
        name: sub
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for name, sub in action.choices.items()
    }


def _resolve(tokens: list[str]) -> tuple[argparse.ArgumentParser, list[str]]:
    """Walk `tokens` down the real parser and return the leaf subparser.

    Every token before the first option is a subcommand name and must be one
    the parser actually defines, so an invented subcommand fails here. A bare
    `vkit --help` has no such token and resolves to the root, which is how a
    maintainer reads the command list.
    """
    from vkit.cli import build_parser

    node = build_parser()
    consumed = 0
    for token in tokens:
        if token.startswith("-"):
            break
        choices = _subcommands(node)
        if token not in choices:
            pytest.fail(
                f"vkit {' '.join(tokens)}: `{token}` is not a subcommand. "
                f"This build defines {sorted(_subcommands(build_parser()))}."
            )
        node = choices[token]
        consumed += 1
    return node, tokens[consumed:]


def _valid_options(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


# ------------------------------------------------------------------- the checks


@pytest.mark.parametrize("doc", [CHECKLIST, PILOT], ids=lambda p: p.name)
def test_every_path_is_verified_or_declared_absent(doc: Path) -> None:
    """A path in either document is checked, or it is not allowed to exist."""
    text = _read(doc)
    blocks = _blocks(text)

    assert "text files" in blocks, f"{doc.name} needs a ```text files block"

    present: set[str] = set()
    for block in blocks["text files"]:
        for entry in block:
            if entry.startswith("#"):
                continue
            for token in _tokens(entry):
                present.add(token)
                assert (ROOT / token).exists(), (
                    f"{doc.name} claims {token} exists, but it does not"
                )

    absent: set[str] = set()
    for block in blocks.get("text absent", []):
        for entry in block:
            if entry.startswith("#"):
                continue
            for token in _tokens(entry):
                absent.add(token)
                assert not (ROOT / token).exists(), (
                    f"{doc.name} still calls {token} absent, but it now exists"
                )

    for token in _tokens(_strip_fences(text)):
        if token.startswith(PLACEHOLDERS):
            continue
        assert token in present or token in absent, (
            f"{doc.name} mentions {token} outside a checked block, so nothing "
            "verifies it"
        )


@pytest.mark.parametrize("doc", [CHECKLIST, PILOT], ids=lambda p: p.name)
def test_every_listed_command_exists(doc: Path) -> None:
    """Every step in the checklist resolves to real code in this repository."""
    text = _read(doc)
    for block in _blocks(text).get("console command", []):
        for line in block:
            tokens = line.lstrip("$ ").split()
            assert tokens, f"{doc.name} has an empty command line"
            _check_command(doc.name, tokens)


def _check_command(doc_name: str, tokens: list[str]) -> None:
    if tokens[0] == "vkit":
        _check_vkit(tokens[1:])
    elif tokens[:2] == ["uv", "venv"]:
        # Creates an empty environment. The trailing path is the environment
        # the later steps use, and a place name is not a claim about this tree.
        return
    elif _is_interpreter(tokens[0]):
        _check_python(doc_name, tokens[1:])
    elif tokens[:3] == ["claude", "plugin", "validate"]:
        target = [a for a in tokens[3:] if not a.startswith("-")]
        assert target and (ROOT / target[0]).exists(), (
            f"{doc_name}: validate target {target} does not exist"
        )
    else:
        pytest.fail(
            f"{doc_name}: this file cannot check the command {' '.join(tokens)}. "
            "Teach it the shape before listing the command."
        )


def _is_interpreter(token: str) -> bool:
    """True for `python`, `python3`, or an absolute path to an interpreter.

    A checklist step names the interpreter it wants rather than relying on
    whatever is on PATH, so both spellings have to be understood here.
    """
    return token in ("python", "python3", "py") or _NAME_RE.search(
        Path(token).name
    ) is not None and Path(token).name.lower().startswith(("python", "py"))


def _check_python(doc_name: str, tokens: list[str]) -> None:
    if tokens[:2] == ["-m", "pip"] and tokens[2:3] == ["install"]:
        _check_pip_install(tokens[3:])
    elif tokens[:2] == ["-m", "pip"] and tokens[2:3] == ["wheel"]:
        # `pip wheel .` builds the tree it is pointed at, and `-w` names an
        # output directory rather than an input the gate can resolve.
        return
    elif tokens[:2] == ["-m", "pytest"]:
        for arg in tokens[2:]:
            if not arg.startswith("-"):
                assert (ROOT / arg).exists(), f"{doc_name}: no such test path {arg}"
    elif tokens[:1] == ["-m"]:
        pytest.fail(f"{doc_name}: cannot check the module {' '.join(tokens)}")
    else:
        assert (ROOT / tokens[0]).is_file(), f"{doc_name}: no such script {tokens[0]}"


def _check_pip_install(targets: list[str]) -> None:
    """Every named requirement resolves to a path in this repository.

    A requirement that is a local path is a claim that the thing it names is
    here. A requirement that is a bare name is a third-party requirement the
    gate deliberately does not resolve.
    """
    for target in targets:
        if target.startswith("-"):
            continue
        looks_local = any(c in target for c in (".", "/", "\\")) and "/" in target
        if looks_local:
            assert (ROOT / target).exists(), f"no such path to install: {target}"


def _check_vkit(tokens: list[str]) -> None:
    leaf, rest = _resolve(tokens)
    valid = _valid_options(leaf)
    for token in rest:
        if token.startswith("-"):
            assert token in valid, (
                f"vkit {' '.join(tokens)}: {token} is not an option of that "
                f"subcommand, which accepts {sorted(valid)}"
            )


@pytest.mark.parametrize("doc", [CHECKLIST, PILOT], ids=lambda p: p.name)
def test_no_missing_command_is_offered_as_runnable(doc: Path) -> None:
    """A subcommand that does not exist must be named as a gap, not run."""
    text = _read(doc)
    for block in _blocks(text).get("console command", []):
        for line in block:
            tokens = line.lstrip("$ ").split()
            if not tokens or tokens[0] != "vkit":
                continue
            joined = " ".join(tokens[1:]).split("--")[0].strip()
            for absent in ABSENT_COMMANDS:
                assert joined != absent, (
                    f"{doc.name} tells a maintainer to run `vkit {absent}`, "
                    "which build_parser() does not define"
                )


@pytest.mark.parametrize("doc", [CHECKLIST, PILOT], ids=lambda p: p.name)
def test_every_known_gap_is_named(doc: Path) -> None:
    """No gap is dropped quietly between this commit and the next."""
    text = _read(doc)
    for gap_id, tokens in GAPS:
        assert gap_id in text, f"{doc.name} never names {gap_id}"
        for token in tokens:
            assert token in text, (
                f"{doc.name} names {gap_id} without saying anything about {token}"
            )


def test_the_checklist_states_its_own_gate() -> None:
    """The checklist must refuse the release, not encourage it."""
    text = _read(CHECKLIST)
    assert "GAP-" in text, "the checklist carries no gap register"
    for word in ("blocker", "verified", "not verified"):
        assert word in text.lower(), f"the checklist never says {word}"


def test_the_pilot_has_entry_conditions() -> None:
    """The pilot document gates entry rather than describing a launch."""
    text = _read(PILOT)
    for word in ("before", "must", "not"):
        assert word in text.lower(), f"the pilot document never says {word}"
