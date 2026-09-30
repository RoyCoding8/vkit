"""The integration documents must stay true to the code they describe.

A checked-in CI example that nobody ran is a claim, not a feature, and a claim
that reads like a feature is worse than no file. So this test treats the
integration documentation as untrusted input and checks it against the
repository, the same way `tests/test_release_docs.py` treats the release
documents.

Three things are checked.

**Every command in the CI example exists.** Each `vkit` line is walked through
`build_parser()` one subcommand at a time, and each flag is checked against that
subparser's own option strings, so an invented flag fails as loudly as an
invented subcommand.

**Every path-shaped token in the documents exists in this repository.** A
placeholder is named in prose and never resolved. A path that is neither a real
file nor a declared placeholder is a failure, which is what stops a new claim
appearing in a document where no gate checks it.

**The honest-status claim is load bearing.** The documents say the CI example has
not been executed, and that a capacity pool cannot return an individual slot.
Both are checked against the code rather than trusted, so neither can quietly
stop being true.
"""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

WORKFLOW = ROOT / "examples" / "ci" / "integration.yml"
CI_DOC = ROOT / "docs" / "INTEGRATION-CI.md"

# Location-shaped tokens a release host supplies, or that only exist inside a
# built wheel or a CI run. Named in the documents, never resolved.
PLACEHOLDERS = (
    "<ref>", "<repo>", "<enrolled-repo>", "<example>", "<run-id>", "<check-id>",
    "<path>", "<venv>", "verification-kit",
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.<>-]+(?:/[A-Za-z0-9_.<>-]+)+")


@pytest.fixture(scope="module")
def parser() -> argparse.ArgumentParser:
    from vkit.cli import build_parser

    return build_parser()


def test_both_documents_exist() -> None:
    assert WORKFLOW.is_file(), f"the CI example is missing: {WORKFLOW}"
    assert CI_DOC.is_file(), f"the integration document is missing: {CI_DOC}"


def test_every_vkit_command_in_the_workflow_is_real(parser: argparse.ArgumentParser) -> None:
    """A `vkit` line that cannot be parsed is a line that cannot run.

    Walked through the real parser rather than pattern-matched, so a flag the
    subcommand does not define fails here exactly as it would in a CI log.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    vkit_lines = [
        line.strip() for line in text.splitlines()
        if "vkit " in line and not line.strip().startswith("#")
    ]
    assert vkit_lines, "the workflow runs no vkit command at all, which is not an example"

    from vkit.cli import build_parser

    for line in vkit_lines:
        # The workflow uses shell line continuations: a `vkit` line ends with a
        # backslash and the arguments are on the following lines. Rejoin the
        # whole run before parsing, or the parser sees a command with no
        # arguments and correctly refuses it.
        block = _command_block(text, line)
        # Tokenized the way a shell would, so a quoted `"${{ a || b }}"` is one
        # argument rather than four. `shlex` is the right tool because the
        # workflow is a shell script, and guessing at quoting is how a doc test
        # ends up testing the wrong thing.
        tokens = shlex.split(block)
        start = tokens.index("vkit")
        arguments = _filter_expression_values(tokens[start + 1:])
        parsed = build_parser().parse_args(arguments)
        subparser = _find_subparser(parser, parsed)
        assert subparser is not None, f"no subparser resolved for {arguments!r}"
        flags = {opt for action in subparser._actions for opt in action.option_strings}
        for token in arguments:
            if token.startswith("--"):
                assert token in flags, (
                    f"{token} is not an option of the subcommand; it has {sorted(flags)}"
                )


def _command_block(text: str, first_line: str) -> str:
    """The whole shell command beginning at `first_line`, continuations joined.

    A trailing backslash continues the command onto the next line; a line
    without one ends it. The first line is always part of the command, whether
    or not it is continued, because a single-line command is a command.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == first_line)
    block = [first_line.removesuffix("\\").strip()]
    for line in lines[start + 1:]:
        stripped = line.strip()
        continues = stripped.endswith("\\")
        block.append(stripped.removesuffix("\\").strip())
        if not continues:
            break
    return " ".join(part for part in block if part)


def _filter_expression_values(tokens: list[str]) -> list[str]:
    """Command tokens with every GitHub expression replaced by a placeholder.

    An expression is a value, not a flag, and its spelling varies: bare
    `${{ x }}`, or quoted with a shell fallback as `"${{ x || y }}"`. A token
    containing `${{` is therefore dropped and the flag it was the value of gets
    a placeholder in its place, so the parser sees the SHAPE of the command
    rather than the value the forge would supply.
    """
    arguments: list[str] = []
    state = "value"  # "value" | "expect_value" | "skip_redirection"
    for token in tokens:
        if state == "skip_redirection":
            state = "value"
            continue
        if token in (">", ">>"):
            state = "skip_redirection"
            continue
        if state == "expect_value":
            # A flag's value. A GitHub expression has no parseable shape here,
            # so it becomes a placeholder: the parser only needs to see that the
            # flag carries a value, not what the forge would substitute.
            arguments.append("PLACEHOLDER" if "${{" in token else token)
            state = "value"
            continue
        if "${{" in token:
            continue
        arguments.append(token)
        state = "expect_value" if token.startswith("--") else "value"
    return arguments


def _find_subparser(root: argparse.ArgumentParser, parsed: argparse.Namespace):
    """The subparser that owns the flags in `parsed`, by walking the real tree."""
    for action in root._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                if name != getattr(parsed, "command", None):
                    continue
                for inner in sub._actions:
                    if isinstance(inner, argparse._SubParsersAction):
                        for subname, subsub in inner.choices.items():
                            if subname == getattr(parsed, f"{inner.dest}", None):
                                return subsub
                return sub
    return None


def test_every_path_in_the_documents_exists_or_is_a_placeholder() -> None:
    for document in (WORKFLOW, CI_DOC):
        text = document.read_text(encoding="utf-8")
        for token in _TOKEN_RE.findall(text):
            if token in PLACEHOLDERS:
                continue
            # A token that names a command, an action name or a prose word
            # rather than a location is not a path claim.
            if not any(segment[:1].isalpha() and "." in segment
                       for segment in token.split("/")):
                continue
            assert (ROOT / token).exists(), f"{document.name} names {token}, which does not exist"


def test_the_documents_state_that_the_ci_example_has_not_run() -> None:
    """The unverified claim has to be in the document, not only in a commit message."""
    text = CI_DOC.read_text(encoding="utf-8").lower()
    assert "not executed" in text
    assert "unverified" in text
    assert "branch protection" in text
    # And the workflow itself says so, so somebody reading only the YAML is not
    # misled either.
    assert "has not been executed" in WORKFLOW.read_text(encoding="utf-8").lower()


def test_the_workflow_uses_a_policy_ref_rather_than_a_path() -> None:
    """The one flag choice the whole design turns on, checked in the file itself.

    `--policy @<ref>` confers protected context; `--policy <path>` does not. A
    workflow that passed a path would produce local evidence and a green check,
    which is the failure this product exists to prevent.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    policy_lines = [
        line for line in text.splitlines() if "--policy" in line and not line.strip().startswith("#")
    ]
    assert policy_lines, "the workflow names no --policy at all"
    for line in policy_lines:
        assert "@${{" in line, (
            f"--policy is not an approved reference: {line.strip()!r}. A path is local "
            "evidence and can never be the protected integration decision."
        )


def test_the_documented_capacity_behaviour_is_real() -> None:
    """The documents describe the capacity pool's semantics. Prove them.

    `docs/INTEGRATION-CI.md` used to carry a known limit: a capacity pool could
    not return an individual holder's slot. That was fixed, so the document no
    longer claims it. The check is here so the document and the code cannot
    drift in either direction -- if the semantics change again, this fails and
    the document is edited in the same change.
    """
    from vkit.claims import ResourceSpec, acquire, holder, release
    from vkit.storage import Store
    import tempfile

    store = Store(Path(tempfile.mkdtemp()) / "state.sqlite3")
    spec = ResourceSpec("pool", "capacity", capacity=3)
    acquire(store, "first", 1, [spec])
    acquire(store, "second", 1, [spec])
    assert holder(store, "pool").held == 2

    # Any holder can return its own slot, not only the one the row names.
    release(store, "second", 1, ["pool"])
    assert holder(store, "pool").held == 1

    # And a task holding nothing cannot free a slot it never took.
    release(store, "second", 1, ["pool"])
    assert holder(store, "pool").held == 1, (
        "a second release by a task that already gave its slot back freed a slot "
        "nobody held"
    )


def test_the_documents_do_not_claim_a_sandbox_the_product_is_not() -> None:
    """The trust boundary is stated, and stating it falsely is the risk.

    The launcher keeps the candidate from editing its own verdict and refuses
    `import vkit` inside the check. It is not a sandbox against hostile code, and
    a document that implied otherwise would be the same class of error this
    product is built to stop.
    """
    text = CI_DOC.read_text(encoding="utf-8").lower()
    assert "not a sandbox" in text
    assert "driver is repository code" in text
