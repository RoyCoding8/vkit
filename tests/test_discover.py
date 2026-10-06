"""Repository inspection: what a project says about itself, read without running it.

These assert three things a report exists to establish, and the third is the
one that matters most. A discovery command carries provenance, because a command
nobody can trace back to a line is a guess. It carries ambiguity, because
`npm test` in a repository that also configures tox is not the same command as
the tox suite and the file does not say which one a person meant. And it
carries prerequisites measured on this host, because a config file that names
an interpreter is not evidence that the interpreter is installed.

The refusal tests are load-bearing. A repository whose `package.json` has a
`preinstall` script that writes a sentinel file must come back with that file
absent, and it must come back having named the script as something a person has
to decide about. That is the difference between inspection and running
something.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

import pytest

from vkit.discover import inspect_repository
from vkit.paths import Project

ProjectBuilder = Callable[..., Project]


@pytest.fixture
def repo(tmp_path: Path) -> ProjectBuilder:
    from helpers import git
    from vkit.paths import open_project

    def build(files: dict[str, str]) -> Project:
        root = tmp_path / "repo"
        for relative, text in files.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text, encoding="utf-8")
        root.mkdir(exist_ok=True)
        git(root, "init", "-q")
        return open_project(root)
    return build

#: A package.json whose install hooks would be destructive if anything ran them.
#: The sentinel is a path, not a command effect, so the assertion is a fact
#: about the filesystem rather than about a side effect that might be cleaned up.
HOSTILE_SCRIPTS = {
    "name": "hostile",
    "scripts": {
        "preinstall": "node -e \"require('fs').writeFileSync('SENTINEL', 'x')\"",
        "install": "node -e \"require('fs').writeFileSync('SENTINEL2', 'x')\"",
        "postinstall": "node -e \"require('fs').writeFileSync('SENTINEL3', 'x')\"",
        "test": "node --test test/",
    },
}


def test_inspect_reports_a_npm_test_script_with_the_line_it_came_from(
    repo: ProjectBuilder,
) -> None:
    """A discovered command names the file and the line a reader opens to check it."""
    package = json.dumps(
        {"name": "x", "scripts": {"test": "node --test test/", "build": "tsc -p ."}},
        indent=2,
    )
    project = repo({"package.json": package})

    report = inspect_repository(project)
    tests = {c.id: c for c in report.by_kind("test")}

    assert list(tests) == ["npm:test"], f"expected one test entry point, got {list(tests)}"
    command = tests["npm:test"]
    assert command.argv == ("npm", "run", "test")
    assert command.runner == "npm"
    assert command.provenance.file == "package.json"
    assert command.provenance.line == 4, f"provenance line was {command.provenance.line}"
    lines = package.splitlines()
    assert lines[command.provenance.line - 1].strip() == '"test": "node --test test/",'


def test_inspect_names_the_ambiguity_when_two_test_runners_are_configured(
    repo: ProjectBuilder,
) -> None:
    """`npm test` in a repository that also configures tox is a choice, not a fact.

    Without the ambiguity flag, a reader would register the npm script as *the*
    test command and never learn the repository offers another one.
    """
    project = repo({
        "package.json": json.dumps({"scripts": {"test": "vitest run"}}),
        "tox.ini": "[testenv]\ndeps = pytest\ncommands = pytest\n",
        "pytest.ini": "[pytest]\ntestpaths = tests\n",
    })

    command = {c.id: c for c in inspect_repository(project).by_kind("test")}["npm:test"]

    assert command.ambiguous, "npm test was reported unambiguous while tox and pytest are configured"
    assert "tox" in " ".join(command.ambiguity)
    assert "vitest run" in " ".join(command.ambiguity) or command.summary == "vitest run"


def test_inspect_reports_ambiguity_is_absent_when_only_one_runner_exists(
    repo: ProjectBuilder,
) -> None:
    """The ambiguity flag has to be able to say no, or saying yes means nothing."""
    project = repo({"package.json": json.dumps({"scripts": {"test": "node --test test/"}})})

    command = {c.id: c for c in inspect_repository(project).by_kind("test")}["npm:test"]

    assert command.ambiguity == ()
    assert command.ambiguous is False


def test_inspect_measures_prerequisites_against_this_host(repo: ProjectBuilder) -> None:
    """A missing tool is a BLOCKED reason later, so inspection has to see it now.

    `make` is required because the discovered command is `make <target>`, and
    the assertion compares the report against a fresh `shutil.which` rather than
    hard-coding an answer. On a host with a POSIX toolchain the satisfied branch
    is the one under test instead, so neither outcome can pass by being lucky.
    """
    project = repo({"Makefile": "build:\n\tvkit-definitely-not-a-real-tool --strict\n"})

    report = inspect_repository(project)
    build = {c.id: c for c in report.by_kind("build")}["make:build"]

    prerequisites = {p.name: p for p in build.prerequisites}
    assert set(prerequisites) == {"make"}, f"unexpected prerequisites {sorted(prerequisites)}"
    make = prerequisites["make"]
    assert make.satisfied is (shutil.which("make") is not None)
    assert make.detail == (shutil.which("make") or "'make' is not on PATH")
    assert "vkit-definitely-not-a-real-tool" not in prerequisites


def test_inspect_does_not_execute_an_install_script(repo: ProjectBuilder) -> None:
    """The refusal. A hostile package.json is read; none of its hooks run.

    Three separate sentinels, because the three hooks are separate code paths
    and a single one would leave the other two unproven. `install` and
    `preinstall` in particular are the ones npm runs automatically.
    """
    project = repo({"package.json": json.dumps(HOSTILE_SCRIPTS)})
    before = sorted(p.name for p in project.root.iterdir())

    report = inspect_repository(project)

    after = sorted(p.name for p in project.root.iterdir())
    assert after == before, f"inspection created files: {set(after) - set(before)}"
    for sentinel in ("SENTINEL", "SENTINEL2", "SENTINEL3"):
        assert not (project.root / sentinel).exists(), f"{sentinel} exists; a hook ran"

    assert "preinstall" in json.dumps(report.to_json()), "the hostile script is not visible to a reviewer"


def test_inspect_survives_a_malformed_package_json_and_still_reads_the_rest(
    repo: ProjectBuilder,
) -> None:
    """One unreadable file is a declared gap, not the end of the report.

    A repository with a broken package.json and a working Makefile should still
    yield the Makefile commands. A reader who loses the whole report because of
    one syntax error has been told less than the tool actually knew.
    """
    project = repo({
        "package.json": "{ this is not json",
        "Makefile": "test:\n\tnode --test test/\n",
    })

    report = inspect_repository(project)

    assert any("package.json is not valid JSON" in gap for gap in report.gaps)
    assert "make:test" in {c.id for c in report.commands}
    assert "node" in report.ecosystem


def test_inspect_reports_a_gap_when_the_repository_declares_no_command(
    repo: ProjectBuilder,
) -> None:
    """"Nothing to run" is a finding, and it must be stated rather than implied
    by an empty list that a reader has to interpret."""
    project = repo({"README.md": "# a library with no scripts\n"})

    report = inspect_repository(project)

    assert report.commands == []
    assert any("no supported project file" in gap for gap in report.gaps)
    assert any("no runnable test, build or launch command" in gap for gap in report.gaps)


def test_inspect_reads_make_targets_and_ci_workflow_commands(
    repo: ProjectBuilder,
) -> None:
    """Both ecosystems' entry points, with the CI line's caveat attached.

    A CI shell line is not a local command. The working directory, the installed
    tools and the interpreter are the runner's, so a report that presented it
    beside `npm test` with no caveat would invite someone to register it as-is.
    """
    project = repo({
        "Makefile": "test:\n\tnode --test test/\n\nserve:\n\tnode src/server.js\n",
        ".github/workflows/ci.yml": "jobs:\n  a:\n    steps:\n      - run: npm ci\n      - run: npm test\n",
    })

    report = inspect_repository(project)

    kinds = {(c.id, c.kind) for c in report.commands}
    assert ("make:test", "test") in kinds
    assert ("make:serve", "launch") in kinds

    ci = {c.id: c for c in report.ci_commands}
    assert len(ci) == 1, f"expected the one recognizable CI line, got {list(ci)}"
    only = next(iter(ci.values()))
    assert only.provenance.file == ".github/workflows/ci.yml"
    assert only.provenance.line == 5
    assert only.ambiguous, "a CI shell line was reported as an unambiguous local command"
    assert "npm ci" not in json.dumps(only.to_json()), "an install line was reported as a runnable command"


def test_inspect_reports_a_declared_node_engine_separately_from_a_measured_one(
    repo: ProjectBuilder,
) -> None:
    """What the repository asks for and what this host has are two facts."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed, so a measured version cannot exist")

    project = repo({
        "package.json": json.dumps({"engines": {"node": ">=99"}, "scripts": {"test": "vitest"}}),
    })

    tools = {t.name: t for t in inspect_repository(project).tools}

    assert tools["node"].declared == ">=99"
    assert tools["node"].measured is not None and "v" in tools["node"].measured
    assert tools["node"].satisfied is False, "node >=99 is not satisfied by any real node"
    assert tools["node"].source == "package.json:engines.node"


def test_inspect_json_and_text_describe_the_same_commands(repo: ProjectBuilder) -> None:
    """Both output modes are the interface. A report that differs between them is
    a report whose `--json` cannot be trusted for the same reason its text cannot."""
    project = repo({"package.json": json.dumps({"scripts": {"test": "vitest run", "start": "node ."}})})

    report = inspect_repository(project)
    text = report.render()
    document = report.to_json()

    assert [c["id"] for c in document["commands"]] == ["npm:test", "npm:start"]
    for command in document["commands"]:
        assert command["id"] in text, f"{command['id']} is in the JSON but not the text"
    assert "nothing was executed" in text
    assert document["truncated"] is False


def test_inspect_bounds_a_report_without_dropping_the_test_command(
    repo: ProjectBuilder,
) -> None:
    """A bounded report that lost the test command is a report that lied.

    The build helpers are declared *first*, so a report that simply took the
    first 40 commands would have shown 40 build helpers and no test entry
    point. That is the failure this test exists to prevent, and asserting only
    `len(commands) <= 40` would have passed against it.
    """
    scripts: dict[str, str] = {f"build:{n}": f"echo {n}" for n in range(200)}
    scripts["test"] = "node --test test/"
    scripts["postinstall"] = "echo installing"
    project = repo({"package.json": json.dumps({"scripts": scripts})})

    report = inspect_repository(project)

    assert len(report.commands) <= 40, f"{len(report.commands)} commands were reported"
    assert report.truncated is True
    kinds = {c.kind for c in report.commands}
    assert "test" in kinds, "the bound dropped the only test command"
    assert "install" in kinds, "the bound dropped the install hook a reviewer must see"
    builds = [c for c in report.commands if c.kind == "build"]
    assert len(builds) < 198, f"{len(builds)} build helpers survived; the bound is not prioritising"
    assert any("never dropped by this bound" in gap for gap in report.gaps)
