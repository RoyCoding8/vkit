"""Every receipt in `formal/results/` is checked against the files it names.

`formal/results/` holds the receipts for four formal claims. An earlier version
of this file also checked them through `review/probe_formal_state.py`, which is
an audit run by hand from `review/`, a directory `.gitignore` keeps out of every
clone. That made the audit the only witness for one property, so the property
could hold on the author's machine and nowhere else. Everything checked here is
now read from a committed file.

The gate below is derived from what is on disk rather than from a table. It
walks `formal/results/*receipt.json`, and for each one pairs every
`<thing>_sha256` key with the sibling `<thing>` field that names the file it
digests. A receipt therefore brings its own map: a new receipt is checked
because it exists, not because someone remembered it.

Two properties this is built to hold:

**A receipt cannot claim a run it did not do.** `status` is checked against the
evidence in the same receipt. A BLOCKED receipt with no states, no properties
and no toolchain is honest; flipping its status to PASS without a run leaves a
receipt asserting 207360 distinct states from null, which is refused. The
mirror check refuses a PASS with empty evidence, so neither direction passes.

**The digest function is pinned, not trusted.** The receipt digests are
compared against `formal/digest.py`, so neutralizing that function would
silently make every receipt agree with itself. The control tests therefore
compare it against literal sha256 values computed here, and a digest key with
no file to check against is a finding rather than a skip.

Nothing here runs Java, Lean, or a model checker, and nothing touches the
network. It reads committed files and recomputes their digests.

**What this does not cover.** A reproducing digest says the artifact has not
changed since the receipt was written. It says nothing about whether the tool
that wrote the receipt was correct, and it does not re-run anything. A PASS
receipt whose recorded tool version was never installed here still passes this
gate, by design: re-running is the harness's job and this host has neither a
JRE nor the JAR.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORMAL = ROOT / "formal"
RESULTS = FORMAL / "results"

sys.path.insert(0, str(FORMAL))

from digest import canonical_sha256  # noqa: E402

#: Where a bare filename in a receipt may live. A receipt names the model or
#: module by filename alone, so the directory is implied by the harness that
#: wrote it rather than stated in the receipt. More than one match is ambiguous
#: and is refused rather than picked from.
SEARCH_DIRS = (FORMAL / "tla", FORMAL / "lean", ROOT)

#: Digests that cannot be reproduced from the tree at all. Each names a binary
#: that git does not carry: `tool_jar_sha256` is over the exact bytes of
#: tla2tools.jar, which `formal/digest.py` deliberately does not fold and which
#: `.gitignore` keeps out of the checkout. An entry here is a stated decision,
#: and `test_every_digest_key_is_reproduced_or_explained` fails when a new
#: digest arrives that nobody explained.
UNVERIFIABLE_DIGESTS = {
    "tool_jar_sha256": "a binary; tla2tools.jar is not committed, so there are "
    "no bytes in the tree to reproduce it against",
}


def _receipts() -> dict[str, dict]:
    """Every receipt on disk, by filename. Empty is a failure at the callers."""
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(RESULTS.glob("*receipt.json"))
    }


#: `*_sha256` keys whose file is named by the receipt's `model`/`module`/
#: `target` field rather than by a sibling of the same name. TLC is the one
#: harness that digests two files and describes only one of them, so the second
#: digest has no `<thing>` field to pair with. Mapping it here keeps the pair
#: derived from the harness's own naming rather than from a hand-written table
#: that would need editing when a harness grows a digest.
SIBLINGLESS_DIGESTS = {
    "config_sha256": "{modelstem}.cfg",
}


def _digested_files(receipt: dict) -> dict[str, str]:
    """{receipt key: the path it digests}, for keys a receipt can name a file for.

    `<thing>_sha256` is paired with the sibling `<thing>` field. The two
    exceptions are named above: `tool_jar_sha256`, which has no file in the tree
    at all and is dropped here so it must be claimed by `UNVERIFIABLE_DIGESTS`,
    and `config_sha256`, which is derived from the receipt's own `model` field.

    `test_every_digest_key_is_reproduced_or_explained` is what keeps this
    honest. Anything that falls out of the pairing shows up there.
    """
    files = {
        key: receipt[key[: -len("_sha256")]]
        for key in receipt
        if key.endswith("_sha256") and key[: -len("_sha256")] in receipt
    }
    for key, pattern in SIBLINGLESS_DIGESTS.items():
        model = receipt.get("model", "")
        if key in receipt and model:
            files[key] = pattern.format(modelstem=Path(model).stem)
    return files


def _resolve(name: str) -> tuple[Path | None, str]:
    """The committed file a receipt's filename refers to, and why not if absent."""
    if "/" in name or "\\" in name:
        path = ROOT / name
        return (path, "") if path.is_file() else (None, f"{name} is not a file in the tree")

    hits = [directory / name for directory in SEARCH_DIRS if (directory / name).is_file()]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        searched = ", ".join(directory.relative_to(ROOT).as_posix() for directory in SEARCH_DIRS)
        return None, f"{name} is in none of: {searched}"
    found = ", ".join(hit.relative_to(ROOT).as_posix() for hit in hits)
    return None, f"{name} is ambiguous, it matches {found}"


def _pairs() -> list[tuple[str, str, str]]:
    """(receipt filename, digest key, file the receipt names) for every digest."""
    rows = []
    for receipt_name, receipt in _receipts().items():
        for key, name in sorted(_digested_files(receipt).items()):
            rows.append((receipt_name, key, name))
    return rows


_PAIRS = _pairs()
_PAIR_IDS = [f"{receipt}-{key}" for receipt, key, _ in _PAIRS]


def _artifact_rows() -> list[tuple[str, str, str]]:
    """(artifact, state, receipt cell) for each row of the RESULTS.md table.

    Derived from the committed file by reading the table's own column positions
    rather than by holding the four rows here, so a row edited or added in
    RESULTS.md is checked by the assertions that use this instead of being a
    second copy that can drift from the first.
    """
    rows = []
    for line in (FORMAL / "RESULTS.md").read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != 6 or cells[2] not in ("VERIFIED", "BLOCKED"):
            continue
        rows.append((cells[1], cells[2], cells[3]))
    return rows


def test_the_results_table_and_the_receipt_directory_agree() -> None:
    """Every artifact row names a receipt that exists, and every receipt has a row.

    These two files can drift apart in either direction, and both leave a result
    nobody reads. A harness writes a receipt and nobody adds a row for it, so the
    result sits in the tree pointed at by nothing. Or a row survives its receipt
    being deleted, so the table cites evidence that is not there.

    The direction that used to be unguarded is the second one. `review/`, where
    the formal-state probe recorded which artifacts existed, is gitignored, so no
    committed file said whether the table's rows still had receipts behind them.
    """
    rows = _artifact_rows()
    assert rows, (
        "the artifact table in formal/RESULTS.md could not be read, so this gate "
        "would pass over a table nobody can check"
    )

    for artifact, state, receipt_cell in rows:
        if receipt_cell == "none":
            continue
        relative = receipt_cell.strip("`")
        assert (ROOT / relative).is_file(), (
            f"{artifact} is {state} and cites {relative}, which is not in the tree. "
            "A row citing evidence that is not here is a claim with nothing behind "
            "it, which is the failure this gate exists to catch."
        )

    on_disk = _receipts()
    assert on_disk, (
        "formal/results/ holds no receipts, so every VERIFIED row above is a claim "
        "against evidence that is not here"
    )

    results = (FORMAL / "RESULTS.md").read_text(encoding="utf-8")
    unnamed = sorted(name for name in on_disk if name not in results)
    assert not unnamed, (
        f"these receipts are committed in formal/results/ but formal/RESULTS.md "
        f"never names them: {unnamed}. A result the write-up does not point at is "
        "not on the record, so either cite it or stop committing it."
    )


@pytest.mark.parametrize("receipt_name,key,name", _PAIRS, ids=_PAIR_IDS)
def test_every_digest_reproduces_against_the_file_it_names(
    receipt_name: str, key: str, name: str
) -> None:
    """The digest a receipt recorded still describes the file it names."""
    receipt = _receipts()[receipt_name]
    recorded = receipt[key]
    path, why_not = _resolve(name)
    assert path is not None, (
        f"{receipt_name} records {key} for {name}, and that file is gone: {why_not}. "
        "A receipt naming a file that is not here is a finding, not a skip."
    )

    recomputed = canonical_sha256(path)
    assert recorded == recomputed, (
        f"{receipt_name} records {key}={recorded} for "
        f"{path.relative_to(ROOT).as_posix()}, but the file in the tree now digests "
        f"to {recomputed}. The artifact changed since the receipt was written, so "
        f"{receipt_name} is stale and its status of {receipt.get('status')!r} is a "
        "claim about bytes that are no longer here."
    )


def test_every_digest_key_is_reproduced_or_explained() -> None:
    """No digest key escapes checking without a stated reason.

    The pairing in `_digested_files` drops any `*_sha256` with no sibling
    `<thing>` field, so a new key could vanish from the gate. This is where that
    shows up: every digest key in every receipt is either reproduced by a
    parametrized case above or named in `UNVERIFIABLE_DIGESTS` with a reason.
    """
    checked = {(receipt, key) for receipt, key, _ in _PAIRS}
    unaccounted = [
        f"{receipt_name}: {key}"
        for receipt_name, receipt in _receipts().items()
        for key in sorted(receipt)
        if key.endswith("_sha256")
        and (receipt_name, key) not in checked
        and key not in UNVERIFIABLE_DIGESTS
    ]
    assert not unaccounted, (
        f"these digests are verified by nobody: {unaccounted}. Each is either a "
        "file the receipt can name (give it a sibling field holding the path, so "
        "_digested_files picks it up) or a binary that is not in the tree (name it "
        "in UNVERIFIABLE_DIGESTS with the reason)."
    )


def test_the_digest_function_is_pinned_to_literal_sha256(tmp_path: Path) -> None:
    """The control the receipt checks above depend on.

    Comparing receipts against `canonical_sha256` proves the receipts agree
    with the shipped function. It cannot prove the function hashes anything: a
    `canonical_sha256` returning a constant would satisfy every case above
    while measuring nothing. So it is checked here against digests computed
    independently below, from sha256 over the canonical form, with the hex
    expected values written out longhand rather than recomputed by the same call
    the test is checking.

    `canonical_sha256` is not sha256 over the on-disk bytes. It is sha256 over
    those bytes with CRLF folded to LF, which is why the LF and CRLF cases for
    the same content share one expected value while the bare-CR case has its
    own: a lone CR is not a line ending here and must survive the fold.

    The bytes are written with explicit escapes rather than a triple-quoted
    literal, because this file is committed with `eol=lf` and must produce the
    same bytes on a CRLF checkout. A literal containing a CRLF pair would fold
    here for the wrong reason, which is the bug `formal/digest.py` exists to
    fix.

    The scratch file is written with `write_bytes` for the same reason. A text
    write would put CRLF on a Windows checkout and the LF cases would be
    measuring the writer, not the function.
    """
    cases = [
        (b"", hashlib.sha256(b"").hexdigest(), "the empty file has a digest"),
        (
            b"one\ntwo\n",
            hashlib.sha256(b"one\ntwo\n").hexdigest(),
            "the LF checkout is already canonical, and it must agree with the "
            "CRLF checkout below, because the fold removes the difference git "
            "already treats as absent",
        ),
        (
            b"one\r\ntwo\r\n",
            hashlib.sha256(b"one\ntwo\n").hexdigest(),
            "the CRLF checkout folds onto that same canonical form, which is the "
            "entire reason formal/digest.py exists",
        ),
        (
            b"a\rb\r\nc",
            hashlib.sha256(b"a\rb\nc").hexdigest(),
            "a bare CR is not a line ending, so the fold must leave it in place "
            "and must not consume the CR of the pair that follows",
        ),
    ]
    scratch = tmp_path / "digest-control"
    for raw, expected, why in cases:
        scratch.write_bytes(raw)
        got = canonical_sha256(scratch)
        assert got == expected, (
            f"canonical_sha256 disagrees with sha256 over the canonical form "
            f"for bytes {raw!r} ({why}): {got} against {expected}. Every "
            "receipt digest is measured with this function, so a wrong answer "
            "here makes every receipt check above vacuous."
        )


def test_the_blocked_receipt_still_records_that_no_run_happened() -> None:
    """A BLOCKED receipt that claims a pass is refused, and so is the reverse.

    This host has no JRE, so `formal/run_tlc.py` wrote the receipt that says so
    and the model check on record comes from an earlier host. The temptation
    this guards is editing `status` to `PASS` because a PASS reads better in
    the results table, or deleting the receipt so nothing has to be explained.

    Editing the status alone is caught here: the receipt still carries null
    counters and an empty property list, so PASS would be asserting 207360
    distinct states and six properties from nothing. The second block is the
    mirror, because a PASS receipt that has been emptied of its evidence is the
    same fraud with the key edited the other way.
    """
    blocked = _receipts()["OwnershipAcceptance-blocked-receipt.json"]
    assert blocked["status"] == "BLOCKED", (
        f"the receipt written because this host has no JRE now says "
        f"{blocked['status']!r}. Its own contents contradict that: it records "
        f"states_generated={blocked['states_generated']!r}, "
        f"properties={blocked['properties']!r} and "
        f"tool_version={blocked['tool_version']!r}. A run did not happen, so "
        "nothing here may say it did."
    )
    assert blocked["states_generated"] is None, (
        f"a BLOCKED receipt recorded states_generated={blocked['states_generated']!r}; "
        "no model checker ran, so no state count exists"
    )
    assert blocked["distinct_states"] is None and blocked["depth"] is None, (
        "a BLOCKED receipt recorded distinct_states or depth, which a run that "
        "never happened cannot measure"
    )
    assert blocked["properties"] == [], (
        f"a BLOCKED receipt claims {len(blocked['properties'])} checked "
        f"properties: {blocked['properties']}. Nothing was checked."
    )
    assert blocked["tool_version"] == "unavailable", (
        f"a BLOCKED receipt reports tool_version={blocked['tool_version']!r}; the "
        "block was that the tool was unavailable"
    )
    assert blocked["tool_jar_sha256"] == "", (
        f"a BLOCKED receipt reports a tool jar digest "
        f"{blocked['tool_jar_sha256']!r}; no jar was present to digest"
    )
    assert blocked["elapsed_s"] == 0.0, (
        f"a BLOCKED receipt reports elapsed_s={blocked['elapsed_s']!r}; the time "
        "was spent discovering the tool was missing"
    )
    assert "BLOCKED" in blocked["reason"], (
        f"the reason a run did not happen is not stated: {blocked['reason']!r}"
    )
    assert blocked["scope"] == "No run happened, so no property was established.", (
        f"a BLOCKED receipt must say what it does not establish, and this one "
        f"says {blocked['scope']!r}"
    )

    for receipt_name, receipt in sorted(_receipts().items()):
        if receipt.get("status") != "PASS":
            continue
        assert receipt["scope"], f"{receipt_name} is PASS with no scope stated"
        tool = receipt.get("tool")
        if tool == "TLC":
            states = receipt["states_generated"]
            assert states and states > 0, (
                f"{receipt_name} is a TLC PASS recording states_generated="
                f"{states!r}. A model check that explored no states established "
                "nothing, whatever the status says."
            )
            assert receipt["distinct_states"] and receipt["depth"] is not None, (
                f"{receipt_name} is a TLC PASS with no distinct-state count or no "
                "depth, so the exploration it claims cannot be checked against "
                "the state count it reports"
            )
            assert receipt["properties"], (
                f"{receipt_name} is a TLC PASS that checked no properties"
            )
        elif tool == "Lean":
            assert receipt["forbidden_constructs_found"] == [], (
                f"{receipt_name} is a Lean PASS that found "
                f"{receipt['forbidden_constructs_found']}"
            )
            assert receipt["allowed_axioms"], (
                f"{receipt_name} is a Lean PASS that names no allowed axiom set, so "
                "there is nothing to check the reported axioms against"
            )
        elif tool == "pytest":
            assert receipt["mutants"], f"{receipt_name} is PASS with no mutants recorded"
            for mutant in receipt["mutants"]:
                assert mutant["status"] == "CAUGHT", (
                    f"{receipt_name} records {mutant['name']} as "
                    f"{mutant['status']!r} while the run is PASS. A mutant that was "
                    "not caught is a broken property, and a receipt cannot be a "
                    "PASS while one is outstanding."
                )
                assert mutant["test"], (
                    f"{receipt_name} records {mutant['name']} as CAUGHT with no "
                    "test named, so nobody can check which test caught it"
                )


def test_the_harness_with_no_receipt_is_documented_as_blocked() -> None:
    """Every harness that writes no receipt is on the record as BLOCKED.

    `formal/run_mutants.py` prints four mutant verdicts and writes nothing, so
    there is no receipt for anyone to check those four results against. The
    honest handling is a stated BLOCKED, and this is what keeps it stated: a
    harness whose silence is undocumented is a claim with nothing behind it.

    The gate is deliberately NOT "every harness has a receipt", which would be
    false, and it is NOT a fixed list of harnesses, which would let a new
    silent harness through. The set is read from `formal/run_*.py`, each file is
    classified by whether it names a receipt file to write, and the
    receipt-less ones are then required to appear as BLOCKED in both places a
    reader looks.

    Both places are committed. An earlier version of this test also asserted the
    same fact against `review/formal-state.json`, which `.gitignore` keeps out
    of every clone, so that assertion could only ever pass on the machine that
    wrote it. What made it meaningful is now checked against committed files
    instead, and more strictly: the BLOCKED row's receipt cell has to be `none`,
    which ties the row to the absence of a receipt rather than to a probe's
    memory of one.
    """
    harnesses = sorted(path.name for path in FORMAL.glob("run_*.py"))
    assert harnesses, "no harness found in formal/, so this gate would pass vacuously"

    silent = [name for name in harnesses if "receipt.json" not in (FORMAL / name).read_text(encoding="utf-8")]
    assert silent == ["run_mutants.py"], (
        f"these harnesses write no receipt: {silent}. Each one needs a receipt "
        "written by its harness and a BLOCKED row naming it, or a harness that "
        "states results a reader has no way to check."
    )

    results = (FORMAL / "RESULTS.md").read_text(encoding="utf-8")
    assert "BLOCKED" in results and "run_mutants.py" in results, (
        "formal/RESULTS.md no longer records that run_mutants.py is BLOCKED, so a "
        "reader of the results table would have no way to know its four mutant "
        "results are unchecked"
    )
    assert "writes no receipt" in results, (
        "formal/RESULTS.md must say the harness writes no receipt, which is the "
        "reason its results are unverified rather than merely unrerun"
    )

    rows = _artifact_rows()
    assert rows, "the artifact table in formal/RESULTS.md could not be read"

    blocked = [row for row in rows if row[1] == "BLOCKED"]
    assert len(blocked) == len(silent), (
        f"{len(silent)} harness writes no receipt ({silent}) but the artifact table "
        f"has {len(blocked)} BLOCKED rows ({[row[0] for row in blocked]}). Each "
        "silent harness needs a row of its own, and a BLOCKED row with a receipt "
        "behind it is a status the receipt checks contradict."
    )

    for artifact, _, receipt_cell in blocked:
        assert receipt_cell == "none", (
            f"{artifact} is BLOCKED yet its receipt cell names {receipt_cell!r}. A "
            "BLOCKED row whose harness writes no receipt has to say so in the table "
            "itself, or a reader counts a receipt that does not exist."
        )
