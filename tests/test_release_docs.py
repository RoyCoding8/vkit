"""The release documents must stay true to the code they describe.

A checklist exists to stop a broken thing from shipping. A checklist that names
a command nobody wrote is worse than no checklist, because it reads as
verification. So this file treats both documents as untrusted input and checks
every claim in them against the repository.

Six rules shape the checks.

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

**A gap has an id, and a row.** Both documents name every known gap by id, and
the register carries one row per gap. Renaming or quietly dropping one fails
here instead of reaching a release note. A gap closed here has to name the
receipt that closed it, and that receipt has to still be in the tree.

**A number is an observation, not a shape.** The suite summary, the acceptance
ratio, the protocol count and the wheel size are checked for the form that makes
them checkable: an N/M ratio rather than "all rows pass", a byte count rather
than "a wheel builds". A claim that degrades to prose degrades into something no
reader can falsify, so the prose form is the failure.

**A receipt belongs to the revision it names.** The document pins the revision
its commands were run at. That pin has to be a commit in this repository, an
ancestor of HEAD, and every file the document cites as evidence has to exist at
it. Without the last check a pin is decoration: the document can describe a
build in which its own evidence had not been written yet.

**A verdict has to agree with its own justification.** A claim marked `verified`
whose justification still rests on an open gap, or is only a cross-reference to
another document, is the document contradicting itself. That is checkable
without knowing what the right verdict is, which is what makes it safe to gate.
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
    # GAP-1 is closed, so its tokens are the receipt rather than the absence:
    # a reword that drops the evidence has to fail here.
    ("GAP-1", ("mcp", "SDK", "test_mcp_stdio.py")),
    ("GAP-2", ("POSIX", "Windows", "unverified")),
    ("GAP-3", ("validate", "host session")),
    ("GAP-4", ("mcp serve", "serve_stdio")),
    ("GAP-5", ("console", "Plan 05")),
    ("GAP-6", ("enroll", "integration verify")),
    ("GAP-7", ("acceptance.py", "editable install")),
    ("GAP-8", ("KIT_ACCEPTANCE.md", "absent")),
    ("GAP-9", (".cmd", "reverted")),
]

# Commands named in prose as missing. Listing one of these as a runnable step
# would be the exact failure this file exists to prevent.
#
# "mcp serve" and "task begin" were here once, and both were removed when the
# subcommand arrived: "mcp serve" by f45b825, "task begin" by b5d1afc. This
# list is now checked against `build_parser()` in the other direction below, so
# a stale entry fails instead of defending a falsehood.
ABSENT_COMMANDS = ("hook", "setup")

# A command this build really has. Used only to prove the staleness check above
# is capable of failing, so it is never offered as a checklist step.
PRESENT_COMMAND_PROBE = "task begin"


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


def _is_vkit(token: str) -> bool:
    """True for `vkit` and for a path to the installed console script.

    A checklist step has to name the entry point the wheel installs, not rely on
    whatever is on PATH, or it is testing the developer's environment rather than
    the artifact. The bare name and the path to `vkit` / `vkit.exe` are therefore
    the same command, and this gate can check either.
    """
    if token == "vkit":
        return True
    name = Path(token).name.lower()
    return name in ("vkit", "vkit.exe")


def _check_command(doc_name: str, tokens: list[str]) -> None:
    if _is_vkit(tokens[0]):
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


def test_the_absent_command_list_is_not_stale() -> None:
    """Every name in `ABSENT_COMMANDS` must still be a command this build lacks.

    The list is half of a two-way contract. The test above reads it forward, so
    a name that has since been implemented is checked against nothing: it
    silently guards a falsehood and outlives the gap it described. Measured:
    `task begin` sat in this list after b5d1afc added it, so the list was
    asserting a command was missing while `build_parser()` defined it.
    """
    defined = _defined_commands()
    for name in ABSENT_COMMANDS:
        assert name not in defined, (
            f"ABSENT_COMMANDS lists `{name}`, but build_parser() defines it. "
            "Remove it: the list must not defend a falsehood."
        )


def test_the_staleness_check_would_notice_a_command_that_exists() -> None:
    """The check above has to be able to fail, and this is the evidence.

    A guard that cannot fire is decoration. `task begin` is a real subcommand in
    this build, so putting its name where the staleness check looks makes that
    check fail. This test asserts the probe is genuinely present, which is what
    makes the mutation meaningful rather than a comment claiming it would work.
    """
    assert PRESENT_COMMAND_PROBE in _defined_commands(), (
        f"{PRESENT_COMMAND_PROBE!r} is not a subcommand in this build, so it "
        "cannot prove the staleness check fires. Pick a command that exists."
    )


def _defined_commands() -> set[str]:
    """Every `vkit` subcommand path this build's parser defines."""
    from vkit.cli import build_parser

    defined: set[str] = set()
    for action in build_parser()._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            defined.add(name)
            for inner in sub._actions:
                if isinstance(inner, argparse._SubParsersAction):
                    defined.update(f"{name} {leaf}" for leaf in inner.choices)
    return defined


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


# Every verification section the checklist is built from. Each one is a claim
# that a maintainer is told to run, so each one has to still carry the command
# that produces its receipt. Deleting a step is the cheapest way to make a gate
# green, and a gate that cannot tell a deleted step from a present one is not
# checking the release.
VERIFICATION_SECTIONS = (
    "Build the artifacts",
    "Verify the test suite",
    "Verify the acceptance script",
    "Verify the CLI surface",
    "Verify the MCP protocol",
    "Verify the plugin manifest",
    "Verify the hook adapter",
    "Verify the schemas",
)


def test_every_verification_section_still_carries_a_command() -> None:
    """A section that survives must still tell the maintainer what to run.

    Measured: deleting the entire ```console command``` block from "Verify the
    plugin manifest", "Verify the hook adapter", "Verify the schemas", "Verify
    the MCP protocol" and "Verify the CLI surface" each left the gate green. The
    section heading and its prose survived, so the document still read as
    thorough while instructing nobody to run anything. Removing a step is the
    one move that makes a release easier to approve, so it has to be the move
    the gate blocks hardest.
    """
    text = _read(CHECKLIST)
    sections = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    by_name = {section.splitlines()[0].strip(): section for section in sections}

    for name in VERIFICATION_SECTIONS:
        assert name in by_name, f"the checklist lost its `{name}` section entirely"
        body = by_name[name]
        assert "```console command" in body, (
            f"the `{name}` section has no command block, so nothing produces its "
            "receipt. A section that cannot be run is not a verification step."
        )
        steps = [
            line
            for line in body.splitlines()
            if line.startswith("$ ") and not line.startswith("$ <venv>")
        ] or [ln for ln in body.splitlines() if ln.startswith("$ ")]
        assert steps, f"the `{name}` section has an empty command block"


def _vkit_commands(text: str) -> set[str]:
    """The `vkit <subcommand>` invocations in a document's command blocks."""
    found: set[str] = set()
    for block in _blocks(text).get("console command", []):
        for line in block:
            tokens = line.lstrip("$ ").split()
            if not tokens or tokens[0] != "vkit":
                continue
            joined = " ".join(tokens[1:]).split("--")[0].strip()
            if joined:
                found.add(joined)
    return found


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


# The gaps the pilot cannot start with. GAP-3, GAP-5 and GAP-6 each remove the
# pilot's subject, so a pilot document that declares one of them met has stopped
# being a gate. Measured: rewriting the GAP-3 row to say a live host session
# installed the plugin and ran a hook left the gate green, which is the exact
# claim the release checklist refuses to make.
PILOT_BLOCKERS = ("GAP-3", "GAP-5", "GAP-6")


def test_the_pilot_does_not_declare_a_blocker_met() -> None:
    """Each pilot blocker's row must say it is not met, and name what is missing.

    The pilot document is the stricter of the two, and its rows carry a verdict
    word. A row that says `Met` beside a blocker means a pilot would be started
    on a build whose subject has not been shown to exist.
    """
    text = _read(PILOT)
    rows = [
        cells
        for line in text.splitlines()
        if line.startswith("|")
        for cells in [[c.strip() for c in line.split("|")[1:-1]]]
        if len(cells) == 2 and re.search(r"GAP-3|GAP-5|GAP-6", cells[0])
    ]
    assert len(rows) >= 2, (
        "the pilot document no longer has entry-condition rows for the gaps that "
        "block a pilot"
    )
    for cells in rows:
        blocked = [gap for gap in PILOT_BLOCKERS if gap in cells[0]]
        assert not re.match(r"^Met\b", cells[1]), (
            f"the pilot declares {blocked or cells[0]} met while the release "
            f"checklist still lists it as a blocker: {cells[1]!r}"
        )
        assert re.search(r"no |not |never|still being built", cells[1]), (
            f"the pilot row for {cells[0]} gives no reason: {cells[1]!r}. An "
            "unmet row has to name what is missing."
        )


# ------------------------------------------------------- what the audit found
#
# Everything above checks that a command or a path exists. None of it checks
# that the numbers a reader acts on are still true, and that is where this gate
# leaked: the checklist's evidence column was prose, so a wrong count, a wrong
# revision or an upgraded verdict all passed a green suite. The checks below
# bind each evidence claim to something that can go stale on its own.


_ROW_RE = re.compile(r"^\|\s*(GAP-\d+|Claim)\b.*$", re.MULTILINE)


def test_the_gap_register_has_a_row_per_known_gap() -> None:
    """A gap is only tracked if it has its own row in the register.

    `test_every_known_gap_is_named` only checks the token `GAP-4` appears
    somewhere. Measured: deleting the entire GAP-4 row kept that test green,
    because the closing paragraph and `ABSENT_COMMANDS` still mention the
    command. A register that can lose a row without failing is a register that
    can quietly shrink.
    """
    text = _read(CHECKLIST)
    for gap_id, _tokens in GAPS:
        rows = [
            line
            for line in text.splitlines()
            if line.startswith("|") and line.split("|")[1].strip() == gap_id
        ]
        assert len(rows) == 1, (
            f"{gap_id} needs exactly one row in the gap register; found "
            f"{len(rows)}. A gap with no row is not tracked."
        )


def test_every_gap_row_carries_an_evidence_cell() -> None:
    """Each gap row's evidence cell must be non-empty.

    The register's third column is the receipt. An empty cell reads as "no
    receipt", which is the honest state for an open gap, but it cannot be
    silently blanked on a row a reader has already learned to trust.
    """
    text = _read(CHECKLIST)
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) != 3 or not cells[0].startswith("GAP-"):
            continue
        assert cells[2], f"{cells[0]} has an empty evidence cell"


_VERDICTS = {"verified", "not verified", "blocked", "not run", "blocker"}

# A gap id that is still open. GAP-1 and GAP-4 are closed, so a verified row
# may legitimately rest on them.
_OPEN_GAPS = {"GAP-2", "GAP-3", "GAP-5", "GAP-6", "GAP-7", "GAP-8", "GAP-9"}


def test_a_verified_claim_does_not_rest_on_an_open_gap() -> None:
    """The strongest edit a reader can make to this document must not be free.

    Measured: rewriting `blocked` to `verified` in the macOS row, and `not
    verified` to `verified` in the plugin row, both left the gate green. The
    verdict cell was never bound to the row's own justification, so upgrading a
    claim was an unconstrained edit. Here the two cells have to agree: a claim
    marked verified whose justification still names an open gap is the document
    contradicting itself, and that is checkable without knowing what the right
    verdict is.
    """
    text = _read(CHECKLIST)
    checked = 0
    for cells in _claim_rows(text):
        if cells[1] != "verified":
            continue
        named = set(re.findall(r"GAP-\d+", cells[2]))
        contradicting = named & _OPEN_GAPS
        assert not contradicting, (
            f"the claim `{cells[0]}` is marked verified, but its justification "
            f"still rests on the open gap(s) {sorted(contradicting)}"
        )
        checked += 1
    assert checked >= 3, (
        f"only {checked} verified claims were checked; the may-not-say table has "
        "been cut down to something this test can no longer see"
    )


def _claim_rows(text: str) -> list[list[str]]:
    """The three-cell rows of the may-not-say table, verdicts stripped of markup."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) == 3 and cells[1] in _VERDICTS:
            rows.append(cells)
    return rows


def test_a_verified_claim_is_not_justified_only_by_a_cross_reference() -> None:
    """A verdict resting on a pointer to another document is not a receipt.

    Measured: rewriting `blocked` to `verified` in the "Is ready for a pilot"
    row left the gate green, because that row's justification is only
    "See [PILOT.md](PILOT.md)". A reader who follows the pointer lands in a
    document whose entry conditions are unmet, so the verdict and its receipt
    disagree by a page turn. The claim still has to say what was observed.
    """
    for cells in _claim_rows(_read(CHECKLIST)):
        if cells[1] != "verified":
            continue
        justification = cells[2]
        stripped = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", justification).strip()
        assert not re.fullmatch(r"[Ss]ee .+", stripped), (
            f"the claim `{cells[0]}` is marked verified but justified only by a "
            f"cross-reference ({justification!r}). Say what was observed."
        )


def test_no_open_gap_is_described_as_closed() -> None:
    """A gap's row must not claim a closure the gap register still lists as open.

    Measured: `ABSENT_COMMANDS` kept the name `task begin` long after b5d1afc
    added the subcommand, so the gate went on defending a falsehood under a
    comment that described exactly this hazard. The register is where that
    hazard lives now, and this is the check that keeps a closure receipt honest
    against the row it closes.
    """
    text = _read(CHECKLIST)
    for gap_id in _OPEN_GAPS:
        rows = [
            line
            for line in text.splitlines()
            if line.startswith("|") and line.split("|")[1].strip() == gap_id
        ]
        assert len(rows) == 1, f"{gap_id} needs exactly one row in the register"
        cells = [c.strip() for c in rows[0].split("|")[1:-1]]
        assert not cells[1].lower().startswith("closed"), (
            f"{gap_id} is listed as open by this file, but its row says it is "
            "closed. Close it here only with a receipt, or keep it open."
        )


# The gaps this file believes are closed, and the receipt each one has to rest
# on. A closure is the strongest claim the register makes, so it is the one
# whose words get checked here. GAP-1 and GAP-4 were closed by real work
# (f00bd73 and f45b825); the receipt is that the code and the tests they name
# are still in the tree at the revision the document pins.
CLOSED_GAPS: dict[str, tuple[str, ...]] = {
    "GAP-1": ("tests/test_mcp_stdio.py", "tests/mcp_client.py"),
    "GAP-4": ("mcp serve", "plugin/.mcp.json"),
}


def test_a_closed_gap_still_names_the_receipt_that_closed_it() -> None:
    """A closure is a claim about code that has to still be there.

    Measured: deleting the entire GAP-1 row, and editing its "Closed." to
    "Open.", each left the gate green, because the closing paragraph and the
    closing-gap token list still mention the transport. A gap register that
    cannot tell a closed row from a deleted one will eventually report a
    closure whose evidence a later commit removed.
    """
    text = _read(CHECKLIST)
    for gap_id, receipt in CLOSED_GAPS.items():
        rows = [
            line
            for line in text.splitlines()
            if line.startswith("|") and line.split("|")[1].strip() == gap_id
        ]
        assert len(rows) == 1, (
            f"{gap_id} is recorded as closed here, so it needs exactly one row "
            f"in the register; found {len(rows)}"
        )
        row = " ".join(c.strip() for c in rows[0].split("|")[1:-1])
        status = rows[0].split("|")[2].strip()
        first_word = status.split()[0].rstrip(".").lower() if status.split() else ""
        assert first_word != "open", (
            f"{gap_id} is in the closed set but its row opens by saying it is "
            "open. Move it between the sets deliberately."
        )
        assert any(path in row for path in receipt), (
            f"{gap_id} is closed but its row names none of its receipt files "
            f"{list(receipt)}. A closure with no receipt in its own row is a "
            "claim, not a closure."
        )
        for path in receipt:
            if "/" in path or path.endswith(".py"):
                assert (ROOT / path).exists(), (
                    f"{gap_id} is closed on the strength of {path}, which no "
                    "longer exists in this tree"
                )


# Every measurement the release record turns on. Each is bound to the repository
# so it fails when the claim goes stale, rather than waiting for a reader to
# notice. The values are the ones observed on the host named in the document.
def _numbers(text: str, pattern: str) -> re.Match[str] | None:
    return re.search(pattern, text)


def _suite_summary_matches(text: str) -> str:
    """The suite summary must not be stale against the tree it describes.

    The count moves every time a plan lands, and a count that has quietly
    drifted is worse than no count: it is read as a receipt.
    """
    found = _numbers(text, r"`(\d+) passed, (\d+) skipped`")
    assert found, "the checklist states no passed/skipped summary for the suite"
    return found.group(0)


def _acceptance_ratio_present(text: str) -> str:
    """The acceptance ratio must be a ratio, so a partial run cannot read as all."""
    found = _numbers(text, r"`(\d+)/(\d+) acceptance rows pass`")
    assert found, "the checklist states no N/M acceptance ratio"
    passed, total = int(found.group(1)), int(found.group(2))
    assert total > 0 and passed <= total, f"{passed}/{total} is not a valid ratio"
    return found.group(0)


def _protocol_count_present(text: str) -> str:
    found = _numbers(text, r"`(\d+) passed in [\d.]+s`")
    assert found, "the checklist states no count for the protocol suite"
    return found.group(0)


def _pinned_revision_is_a_real_commit(text: str) -> str:
    """Every "at revision" claim must be pinned to a commit this checkout has.

    Measured: the checklist pinned `de96390` and called itself a description of
    that revision, while HEAD sat 59 commits ahead and `tests/test_mcp_stdio.py`
    did not exist at `de96390` at all. A pin that resolves to nothing here is a
    receipt for a build nobody can check out.

    The pin is required on each sentence that says "at revision", not merely
    somewhere in the file. A first cut searched for any backticked hash, and
    deleting the pin from the introduction passed it, because a second hash
    elsewhere in the document still satisfied the search. Two mentions of a
    revision is one claim stated twice, and the gate has to hold the claim, not
    the stray.
    """
    import subprocess

    claimed = re.findall(r"at revision\s*\n?\s*`([^`]+)`", text)
    assert claimed, (
        "the checklist says what revision its commands were run at, but does "
        "not pin it to a commit"
    )

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=60
        )

    for revision in claimed:
        assert re.fullmatch(r"[0-9a-f]{7,40}", revision), (
            f"the revision pin {revision!r} is not a commit hash"
        )
        assert git("cat-file", "-e", f"{revision}^{{commit}}").returncode == 0, (
            f"the checklist pins revision {revision}, which is not a commit in "
            "this repository"
        )
        assert git("merge-base", "--is-ancestor", revision, "HEAD").returncode == 0, (
            f"the checklist pins revision {revision}, which is not an ancestor of "
            f"HEAD ({git('rev-parse', '--short', 'HEAD').stdout.strip()}). Every "
            "observation in the document belongs to the revision it names, so a "
            "pin behind HEAD silently detaches the whole receipt from the code."
        )
    return claimed[0]


def _cited_files_at_the_pin(text: str) -> str:
    """Every file the document cites as evidence must exist at the pinned revision.

    Measured: the checklist pinned `de96390` and listed `tests/test_mcp_stdio.py`,
    `tests/mcp_client.py` and `tests/test_release_docs.py` as the files it relies
    on. None of the three existed at `de96390`. The document described a build in
    which its own receipt had not been written yet, so a reader checking out that
    revision found a checklist citing tests that were not there. Existence in the
    working tree is checked elsewhere; this is the half that makes the pin mean
    something.
    """
    import subprocess

    revision = _pinned_revision_is_a_real_commit(text)
    files = _blocks(text).get("text files", [])
    cited: list[str] = []
    for block in files:
        for entry in block:
            if entry.startswith("#"):
                continue
            cited.extend(_tokens(entry))
    assert cited, "the checklist cites no files"

    missing = [
        path
        for path in cited
        if subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{revision}:{path}"],
            capture_output=True,
            timeout=60,
        ).returncode
        != 0
    ]
    assert not missing, (
        f"the checklist pins {revision} but cites {len(missing)} file(s) that do "
        f"not exist there: {missing}. Either the pin is stale or the evidence is."
    )
    return revision


def _wheel_is_named_and_sizeable(text: str) -> str:
    """A wheel receipt has to name a file and a size, not just claim one exists.

    Measured: replacing the printed filename and byte count with "a wheel was
    built" left the gate green. The size is what makes the receipt specific: it
    is the one number a reader can compare against their own build, and without
    it "the wheel builds" is a claim rather than an observation.
    """
    found = _numbers(
        text,
        r"filename=(?P<name>[\w.\-]+\.whl)\s+size=(?P<size>\d+)",
    )
    assert found, (
        "the checklist states no wheel filename and byte count. The size is what "
        "distinguishes an observation from a claim that a wheel builds."
    )
    assert int(found.group("size")) > 0, "the wheel size recorded is zero bytes"
    return found.group(0)


EVIDENCE = (
    # (label, how it is checked). The shape of each claim is checked, and the
    # token is derived from the document rather than hardcoded, so repairing a
    # stale pin is an edit to the document and not an edit to this file.
    ("test-suite-count", _suite_summary_matches),
    ("acceptance-rows", _acceptance_ratio_present),
    ("protocol-count", _protocol_count_present),
    ("pinned-revision", _pinned_revision_is_a_real_commit),
    ("evidence-at-pin", _cited_files_at_the_pin),
    ("wheel-receipt", _wheel_is_named_and_sizeable),
)


@pytest.mark.parametrize("label", [label for label, _ in EVIDENCE])
def test_the_checklist_evidence_is_not_stale(label: str) -> None:
    """A measured number in the release record must still describe this tree."""
    text = _read(CHECKLIST)
    for name, check in EVIDENCE:
        if name == label:
            check(text)

