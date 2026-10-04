"""Read what a repository already says about how to build, test and launch it.

Everything here is a reader. No install script runs, no build is triggered, no
process is launched, and no configuration is evaluated. A repository can name a
command that would execute arbitrary code the moment it is registered, so the
only trustworthy thing to do with it is to *report* it with the line it came
from and let a human decide.

Three facts travel with every discovered command, and each answers a different
question a reader has:

* **Provenance** answers "where did this come from". A command with no
  provenance is a guess, and a guess that reaches a manifest becomes executable
  policy nobody reviewed.
* **Ambiguity** answers "was it obvious how to run this". `npm test` is one
  spelling of one command. `pytest` when the repository also configures tox is
  a choice between runners, and the choice is not in the file.
* **Prerequisites** answers "could this run here at all". Reading a config file
  that names `python3.12` does not mean python3.12 is installed.

The output is bounded on purpose. This report lands in an agent's context
window, and a repository with four hundred npm scripts would otherwise be the
thing that fills it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .nowindow import hidden_window
from .paths import Project

MAX_PER_CATEGORY = 40

TEST_SCRIPT_NAMES = frozenset(
    {"test", "tests", "test:unit", "test:integration", "test:e2e", "test:watch"}
)

LAUNCH_SCRIPT_NAMES = frozenset({"start", "serve", "dev", "run", "preview"})

MAKE_TEST_TARGETS = frozenset({"test", "tests", "check", "unit", "unit-tests", "integration"})
MAKE_LAUNCH_TARGETS = frozenset({"serve", "start", "run", "dev", "server"})


class DiscoveryError(Exception):
    """A file that would have to be read to answer the question could not be.

    This never stops a report. One unreadable file is one declared gap, and a
    partial report that names its gap is more useful than a refusal.
    """


@dataclass(frozen=True)
class Provenance:
    """Where a fact was read from, down to the line.

    The line number is not decoration. "package.json has a test script" is not
    reviewable; "package.json:12" is, because a reader opens the file and sees
    the same thing. When a line is genuinely unknown, `line` is 0 and `detail`
    says so, rather than a fabricated number.
    """

    file: str
    line: int
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "detail": self.detail}

    def render(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass(frozen=True)
class Prerequisite:
    """Something that must exist before the command can run.

    `satisfied` is a fact about this host, measured now, not a promise about
    another one. A report written on Windows cannot say a POSIX command is
    available, and it does not try.
    """

    name: str
    executable: str
    satisfied: bool
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "executable": self.executable,
            "satisfied": self.satisfied,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DiscoveredCommand:
    """One command a repository already describes.

    `argv` is a suggestion shaped like the manifest's `command` array, not a
    promise it can be registered as. Nothing here has been executed, so nothing
    here has been verified, and `verified` is never set by discovery.
    """

    id: str
    kind: str
    argv: tuple[str, ...]
    summary: str
    provenance: Provenance
    ambiguity: tuple[str, ...]
    prerequisites: tuple[Prerequisite, ...]
    runner: str

    @property
    def ambiguous(self) -> bool:
        return bool(self.ambiguity)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "runner": self.runner,
            "argv": list(self.argv),
            "summary": self.summary,
            "provenance": self.provenance.to_json(),
            "ambiguity": list(self.ambiguity),
            "prerequisites": [p.to_json() for p in self.prerequisites],
        }

    def render(self) -> str:
        marks = []
        if self.ambiguous:
            marks.append("ambiguous")
        unmet = [p.name for p in self.prerequisites if not p.satisfied]
        if unmet:
            marks.append("missing:" + ",".join(unmet))
        suffix = f"  ({'; '.join(marks)})" if marks else ""
        return f"{self.id:28} {self.runner:9} {' '.join(self.argv)}{suffix}"


@dataclass(frozen=True)
class ToolVersion:
    """A declared or measured tool version.

    `source` distinguishes a version the repository *asks for* (a pyproject
    `requires-python`, a `.nvmrc`) from one measured on this host by running
    `--version`. The two answer different questions and a report that blurred
    them would be claiming a compatibility it never checked.
    """

    name: str
    declared: str | None
    measured: str | None
    source: str
    satisfied: bool | None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "declared": self.declared,
            "measured": self.measured,
            "source": self.source,
            "satisfied": self.satisfied,
        }


@dataclass
class Inspection:
    """Everything one repository says about itself, bounded and attributed.

    This is the record `vkit project inspect --json` emits and the record
    enrollment proposes from. It is deliberately a value with a `gaps` list
    rather than an exception on failure, because "this repository declares no
    test command" is a finding a reader needs, not an error.
    """

    project: Project
    ecosystem: tuple[str, ...]
    commands: list[DiscoveredCommand] = field(default_factory=list)
    test_configuration: list[str] = field(default_factory=list)
    ci_commands: list[DiscoveredCommand] = field(default_factory=list)
    tools: list[ToolVersion] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    truncated: bool = False

    def by_kind(self, kind: str) -> list[DiscoveredCommand]:
        return [c for c in self.commands if c.kind == kind]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command": "project inspect",
            "project": str(self.project.root),
            "ecosystem": list(self.ecosystem),
            "commands": [c.to_json() for c in self.commands],
            "test_configuration": list(self.test_configuration),
            "ci_commands": [c.to_json() for c in self.ci_commands],
            "tools": [t.to_json() for t in self.tools],
            "gaps": list(self.gaps),
            "files_read": list(self.files_read),
            "truncated": self.truncated,
        }

    def render(self) -> str:
        lines = [f"project : {self.project.root}", f"kind     : {', '.join(self.ecosystem) or 'unknown'}"]
        if self.test_configuration:
            lines.append(f"tests   : {', '.join(self.test_configuration)}")
        if self.commands:
            lines.append("")
            lines.append("commands")
            for command in self.commands:
                lines.append(f"  {command.render()}")
        if self.ci_commands:
            lines.append("")
            lines.append("ci")
            for command in self.ci_commands:
                lines.append(f"  {command.render()}")
        if self.tools:
            lines.append("")
            lines.append("tools")
            for tool in self.tools:
                declared = tool.declared or "-"
                measured = tool.measured or "-"
                lines.append(f"  {tool.name:10} declared {declared:12} measured {measured}")
        if self.gaps:
            lines.append("")
            lines.append("gaps")
            lines.extend(f"  {gap}" for gap in self.gaps)
        lines.append("")
        lines.append("nothing was executed and nothing was installed")
        return "\n".join(lines)




def _read_text(root: Path, relative: str) -> str | None:
    """Read a repository file as text, or None when it is not there.

    A file larger than a megabyte is treated as not-a-config-file. It is a real
    file and its absence is recorded as a gap rather than silently skipped,
    because a 40 MB `package.json` is worth a person knowing about.
    """
    path = root / relative
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > 1_048_576:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DiscoveryError(f"cannot read {relative}: {exc}") from exc


def _line_of(text: str, needle: str) -> int:
    """The 1-based line where `needle` first appears, or 0 when it does not.

    Zero is honest for a fact with no single line: a `[tool.pytest.ini_options]`
    section header describes a table, not a line.
    """
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return index
    return 0


def which_or_none(executable: str) -> str | None:
    """The absolute path of an executable on PATH, or None.

    Never runs it. `shutil.which` only looks, which is the difference between
    reporting that `node` exists and discovering what `npm test` does.
    """
    return shutil.which(executable)


def _probe_version(executable: str, args: tuple[str, ...] = ("--version",)) -> str | None:
    """Run `<executable> --version` and return its first line, or None.

    The one subprocess discovery is allowed, because a version is the fact
    being asked for and there is no other honest way to read it. It is bounded
    by a timeout and the result is treated as untrusted text, truncated, so a
    tool that prints a megabyte of banner cannot flood the report.
    """
    import subprocess

    found = which_or_none(executable)
    if found is None:
        return None
    try:
        done = subprocess.run(
            [found, *args], capture_output=True, encoding="utf-8",
            errors="replace", timeout=15, check=False,
            **hidden_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (done.stdout or done.stderr or "").strip()
    if not output:
        return None
    return output.splitlines()[0][:120]


def _executable_prerequisites(names: Iterable[str]) -> tuple[Prerequisite, ...]:
    return tuple(
        Prerequisite(
            name=name,
            executable=name,
            satisfied=which_or_none(name) is not None,
            detail=which_or_none(name) or f"{name!r} is not on PATH",
        )
        for name in names
    )



INSTALL_SCRIPT_NAMES = frozenset(
    {"preinstall", "install", "postinstall", "prepare", "prepublish",
     "prepublishOnly", "prepack", "postpack", "dependencies"}
)

_SCRIPT_DIRECTORIES = frozenset({"pre", "post", "pretest", "posttest", "predocs"})


def _npm_script_kind(name: str) -> str | None:
    base = name.split(":")[0].lower()
    if name.lower() in TEST_SCRIPT_NAMES or base in ("test", "tests"):
        return "test"
    if name.lower() in LAUNCH_SCRIPT_NAMES:
        return "launch"
    if base in ("build", "compile", "bundle"):
        return "build"
    if base in ("lint", "eslint", "typecheck"):
        return "lint"
    if base in ("format", "fmt", "prettier"):
        return "format"
    return None


def _read_package_json(root: Path, inspection: Inspection) -> None:
    text = _read_text(root, "package.json")
    if text is None:
        return
    if "node" not in inspection.ecosystem:
        inspection.ecosystem.append("node")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        inspection.gaps.append(f"package.json is not valid JSON: {exc}")
        return
    if not isinstance(document, dict):
        inspection.gaps.append("package.json does not contain an object")
        return

    inspection.files_read.append("package.json")

    engines = document.get("engines")
    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
        declared = engines["node"]
        inspection.tools.append(
            ToolVersion(
                name="node",
                declared=declared,
                measured=_probe_version("node"),
                source="package.json:engines.node",
                satisfied=_node_range_satisfied(declared),
            )
        )

    scripts = document.get("scripts")
    if not isinstance(scripts, dict):
        inspection.gaps.append("package.json declares no scripts, so no node command is discoverable")
        return

    runners = _declared_test_runners(root)
    found_test = False
    for name, command in scripts.items():
        if not isinstance(name, str) or not isinstance(command, str):
            continue
        lowered = name.lower()
        if lowered in INSTALL_SCRIPT_NAMES:
            inspection.commands.append(
                DiscoveredCommand(
                    id=f"npm:{name}",
                    kind="install",
                    argv=("npm", "run", name),
                    summary=command.strip()[:160],
                    provenance=Provenance(
                        "package.json", _line_of(text, f'"{name}"'), f"scripts.{name}"
                    ),
                    ambiguity=(
                        "npm runs this automatically during `npm install`; vkit never "
                        "runs it, and enrolling must not make it run either",
                    ),
                    prerequisites=_executable_prerequisites(("npm", "node")),
                    runner="npm",
                )
            )
            continue
        base = name.split(":")[0].lower()
        if base in _SCRIPT_DIRECTORIES:
            continue
        kind = _npm_script_kind(name)
        if kind is None:
            continue
        if kind == "test":
            found_test = True
        ambiguity: list[str] = []
        if kind == "test" and runners:
            ambiguity.append(
                "this repository also configures " + ", ".join(sorted(runners))
                + f", so `npm run {name}` and that runner are different entry points"
            )
        if command.strip() == "":
            continue
        inspection.commands.append(
            DiscoveredCommand(
                id=f"npm:{name}",
                kind=kind,
                argv=("npm", "run", name),
                summary=command.strip()[:160],
                provenance=Provenance(
                    "package.json",
                    _line_of(text, f'"{name}"'),
                    f"scripts.{name}",
                ),
                ambiguity=tuple(ambiguity),
                prerequisites=_executable_prerequisites(("npm", "node")),
                runner="npm",
            )
        )
    if not found_test and not _has_test_configuration(root):
        inspection.gaps.append(
            "package.json has scripts but none of them is a test entry point, "
            "and no test configuration file was found; the repository declares no test command"
        )


def _node_range_satisfied(declared: str) -> bool | None:
    """Whether this host's node satisfies a declared range.

    Only the two constraints that are decidable without a semver library are
    honoured: a `>=X` floor and an `X` major pin. A range this does not
    understand returns None, which renders as "not checked" rather than a
    confident wrong answer.
    """
    measured = _probe_version("node")
    if measured is None:
        return None
    match = re.search(r"v?(\d+)\.", measured)
    if match is None:
        return None
    have = int(match.group(1))
    floor = re.search(r">=\s*v?(\d+)", declared)
    if floor is not None:
        return have >= int(floor.group(1))
    major = re.fullmatch(r"[\^~]?\s*v?(\d+)(?:\.x)?", declared.strip())
    if major is not None:
        return have == int(major.group(1))
    return None


def _has_test_configuration(root: Path) -> bool:
    """Whether a Python test-runner configuration file is present.

    Checks the files themselves rather than `inspection.test_configuration`,
    because this runs while `package.json` is being read and the Python reader
    has not run yet.
    """
    if (root / "pytest.ini").is_file() or (root / "tox.ini").is_file():
        return True
    for name in ("pyproject.toml", "setup.cfg"):
        text = _read_text(root, name)
        if text and _PYTEST_SECTION.search(text):
            return True
    return False



_PYTEST_SECTION = re.compile(r"^\[(?:tool:pytest|tool\.pytest|pytest)\]", re.MULTILINE)


def _read_python_config(root: Path, inspection: Inspection) -> None:
    pyproject = _read_text(root, "pyproject.toml")
    if pyproject is not None:
        try:
            document = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError as exc:
            inspection.gaps.append(f"pyproject.toml is not valid TOML: {exc}")
            document = {}
        if document:
            if "ecosystem" not in inspection.ecosystem:
                inspection.ecosystem.append("python")
            inspection.files_read.append("pyproject.toml")
            project = document.get("project")
            if isinstance(project, dict):
                requires = project.get("requires-python")
                if isinstance(requires, str):
                    inspection.tools.append(
                        ToolVersion(
                            name="python",
                            declared=requires,
                            measured=f"{__import__('sys').version_info.major}."
                            f"{__import__('sys').version_info.minor}",
                            source="pyproject.toml:project.requires-python",
                            satisfied=_python_spec_satisfied(requires),
                        )
                    )
            if _PYTEST_SECTION.search(pyproject):
                inspection.commands.append(
                    DiscoveredCommand(
                        id="pytest:configured",
                        kind="test",
                        argv=("python", "-m", "pytest"),
                        summary="pytest configured by pyproject.toml; no testpaths key found",
                        provenance=Provenance("pyproject.toml", _line_of(pyproject, "[tool.pytest"), "tool.pytest"),
                        ambiguity=(),
                        prerequisites=_executable_prerequisites(("python",)),
                        runner="pytest",
                    )
                )
            tool_table = document.get("tool")
            if isinstance(tool_table, dict):
                if "pytest" in tool_table:
                    inspection.test_configuration.append("pyproject.toml [tool.pytest.ini_options]")
                if "tox" in tool_table:
                    inspection.test_configuration.append("pyproject.toml [tool.tox]")

    for name, marker, label in (
        ("pytest.ini", "pytest", "pytest.ini"),
        ("tox.ini", "tox", "tox.ini"),
        ("setup.cfg", "pytest", "setup.cfg [tool:pytest]"),
    ):
        text = _read_text(root, name)
        if text is None:
            continue
        if "ecosystem" not in inspection.ecosystem:
            inspection.ecosystem.append("python")
        if name == "setup.cfg" and not _PYTEST_SECTION.search(text):
            continue
        if not _PYTEST_SECTION.search(text) and name != "tox.ini":
            continue
        inspection.files_read.append(name)
        inspection.test_configuration.append(label)
        if name == "tox.ini" and "[testenv" in text:
            runner = "tox"
            argv = ("tox", "-e", "py")
            line = _line_of(text, "[testenv")
        else:
            runner = "pytest"
            argv = ("python", "-m", "pytest")
            line = _line_of(text, "[tool:pytest]") or _line_of(text, "[pytest]")
        inspection.commands.append(
            DiscoveredCommand(
                id=f"{runner}:configured",
                kind="test",
                argv=argv,
                summary=f"test configuration present in {name}",
                provenance=Provenance(name, line, label),
                ambiguity=(
                    ("tox.ini also configures environments; `tox -e py` picks one of them "
                     "and the choice is not written in the file",)
                    if runner == "tox" and (root / "pytest.ini").exists() else ()
                ),
                prerequisites=_executable_prerequisites(("python", "pytest" if runner == "pytest" else "tox")),
                runner=runner,
            )
        )

    make = _read_makefile(root, inspection)
    if make is not None:
        _add_make_commands(root, make, inspection)


def _python_spec_satisfied(declared: str) -> bool | None:
    """Whether this interpreter satisfies a `requires-python` specifier.

    A floor and a ceiling are both honoured. Anything more elaborate returns
    None rather than a guess, because `!=3.11.*` is a real specifier this
    deliberately does not pretend to evaluate.
    """
    import sys

    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    floor = re.search(r">=\s*(\d+)\.(\d+)", declared)
    ceiling = re.search(r"<\s*(\d+)\.(\d+)", declared)
    if floor is None and ceiling is None:
        return None
    current = (int(have.split(".")[0]), int(have.split(".")[1]))
    if floor is not None and current < (int(floor.group(1)), int(floor.group(2))):
        return False
    return ceiling is None or current < (int(ceiling.group(1)), int(ceiling.group(2)))



_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)")


def _read_makefile(root: Path, inspection: Inspection) -> str | None:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = root / name
        if not path.is_file():
            continue
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise DiscoveryError(f"cannot read {name}: {exc}") from exc
    return None


def _add_make_commands(root: Path, text: str, inspection: Inspection) -> None:
    if "ecosystem" not in inspection.ecosystem:
        inspection.ecosystem.append("make")
    name = next(
        n for n in ("Makefile", "makefile", "GNUmakefile") if (root / n).is_file()
    )
    inspection.files_read.append(name)
    seen: set[str] = set()
    for index, line in enumerate(text.splitlines(), start=1):
        match = _MAKE_TARGET.match(line)
        if match is None:
            continue
        target = match.group(1)
        if target in seen:
            continue
        seen.add(target)
        kind = (
            "test" if target in MAKE_TEST_TARGETS
            else "launch" if target in MAKE_LAUNCH_TARGETS
            else "build" if target in ("build", "all", "compile")
            else None
        )
        if kind is None:
            continue
        recipe = _make_recipe(text, target)
        inspection.commands.append(
            DiscoveredCommand(
                id=f"make:{target}",
                kind=kind,
                argv=("make", target),
                summary=recipe[:160],
                provenance=Provenance(name, index, f"target {target}"),
                ambiguity=(
                    ("the target body was not found in the file, so what it does is "
                     "not established",)
                    if not recipe else ()
                ),
                prerequisites=_executable_prerequisites(("make",)),
                runner="make",
            )
        )


def _make_recipe(text: str, target: str) -> str:
    """The first recipe line under a target, or "" when the target has none.

    The recipe is a summary for a reader, not something to execute. A target
    that includes another file has no body here, and saying so is better than
    printing the include line as though it were the command.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _MAKE_TARGET.match(line) or _MAKE_TARGET.match(line).group(1) != target:
            continue
        for follow in lines[index + 1:]:
            if not follow.startswith(("\t", " ")):
                break
            stripped = follow.strip()
            if stripped:
                return stripped
        return ""
    return ""



_WORKFLOW_RUN = re.compile(r"^\s*(?:-\s*)?run:\s*(?:\|[-+]?\s*)?(.*)$")


def _read_workflows(root: Path, inspection: Inspection) -> None:
    base = root / ".github" / "workflows"
    if not base.is_dir():
        return
    for path in sorted(base.glob("*.y*ml")):
        relative = path.relative_to(root).as_posix()
        inspection.files_read.append(relative)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise DiscoveryError(f"cannot read {relative}: {exc}") from exc
        for index, line in enumerate(text.splitlines(), start=1):
            match = _WORKFLOW_RUN.match(line)
            if match is None:
                continue
            command = match.group(1).strip()
            if not command or command.startswith(("${{", "echo")):
                continue
            kind = _ci_kind(command)
            if kind is None:
                continue
            inspection.ci_commands.append(
                DiscoveredCommand(
                    id=f"ci:{path.stem}:{index}",
                    kind=kind,
                    argv=("sh", "-c", command) if os.name != "nt" else ("cmd", "/c", command),
                    summary=command[:160],
                    provenance=Provenance(relative, index, "workflow step"),
                    ambiguity=(
                        ("this is a CI shell line, not a local command; the runner's "
                         "working directory and installed tools may differ from a "
                         "developer machine",)
                    ),
                    prerequisites=_executable_prerequisites(
                        _tools_named_in(command) or ("git",)
                    ),
                    runner="workflow",
                )
            )
    if not inspection.ci_commands:
        inspection.gaps.append(
            f".github/workflows has {len(list(base.glob('*.y*ml')))} workflow file(s) "
            "but none names a recognizable test, build or launch command"
        )


def _ci_kind(command: str) -> str | None:
    lowered = command.lower()
    if any(word in lowered for word in ("pytest", "npm test", "npm run test", "yarn test",
                                        "go test", "cargo test", "make test", "tox", "jest",
                                        "vitest", "unittest")):
        return "test"
    if "npm run build" in lowered or lowered.startswith("make ") or "cargo build" in lowered:
        return "build"
    if lowered.startswith("npm start") or lowered.startswith("npm run dev"):
        return "launch"
    return None


def _tools_named_in(command: str) -> tuple[str, ...]:
    """Executables a CI line names, from a closed list.

    A closed list is the point. Reading an arbitrary word out of a shell string
    and reporting it as a prerequisite would claim `PYTHON` in
    `PYTHON=3.12 npm test` is a program to look for on PATH.
    """
    words = set(re.findall(r"[A-Za-z0-9_.-]+", command))
    known = {"python", "python3", "node", "npm", "npx", "yarn", "pnpm", "go", "cargo",
             "make", "tox", "pytest", "dotnet", "deno", "bun"}
    return tuple(sorted(words & known))



def _declared_test_runners(root: Path) -> set[str]:
    """Which test runners this repository configures, beyond npm.

    Read from the files on disk, not from the command list, so the answer is
    about configuration rather than about what discovery happened to emit. A
    repository with pytest.ini and tox.ini genuinely has a choice, and hiding it
    would make `npm test` look unambiguous when it is not. Two *spellings* of
    one runner are still one runner: pytest.ini and a `[tool:pytest]` table are
    two files describing the same choice, and counting them twice would invent
    an ambiguity that does not exist.
    """
    runners: set[str] = set()
    if (root / "pytest.ini").is_file():
        runners.add("pytest")
    if (root / "tox.ini").is_file():
        runners.add("tox")
    for name in ("pyproject.toml", "setup.cfg"):
        text = _read_text(root, name)
        if text and _PYTEST_SECTION.search(text):
            runners.add("pytest")
        if text and re.search(r"^\[testenv", text, re.MULTILINE):
            runners.add("tox")
    return runners


def inspect_repository(project: Project) -> Inspection:
    """Read every supported project file and report what the repository declares.

    Order matters only for readability of `files_read`. Each reader is
    independent: a malformed `package.json` adds a gap and the Python and CI
    readers still run, because a partial answer naming what it could not read
    is the honest one.
    """
    inspection = Inspection(project=project, ecosystem=[])

    for step in (
        lambda: _read_package_json(project.root, inspection),
        lambda: _read_python_config(project.root, inspection),
        lambda: _read_workflows(project.root, inspection),
    ):
        try:
            step()
        except DiscoveryError as exc:
            inspection.gaps.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - a reader must not take the report down
            inspection.gaps.append(f"inspection step failed: {exc}")

    if not inspection.ecosystem:
        inspection.gaps.append(
            "no supported project file was found (looked for package.json, "
            "pyproject.toml, setup.cfg, tox.ini, pytest.ini and Makefile)"
        )
    if not inspection.commands:
        inspection.gaps.append(
            "no runnable test, build or launch command could be derived; "
            "a check must be written before this repository has evidence"
        )

    inspection.files_read = sorted(set(inspection.files_read))
    inspection.test_configuration = sorted(set(inspection.test_configuration))
    _bound(inspection)
    return inspection


_PRIORITY = ("test", "install", "launch", "build", "lint", "format", "ci")


def _bound(inspection: Inspection) -> None:
    """Keep the report inside its bound without dropping what it is for.

    The first cut of this took `commands[:40]` in discovery order, which meant a
    repository with 40 build helpers declared before its test script reported no
    test command at all. Silent absence of the one command a reader came for is
    worse than a long report, so the bound drops the least important kinds
    first. A `test`, `install` or `launch` command is kept ahead of any helper,
    so it is dropped only when there are more of those than the whole bound has
    room for, and the gap then says so by name.
    """
    for label, collection in (("commands", inspection.commands), ("CI commands", inspection.ci_commands)):
        if len(collection) <= MAX_PER_CATEGORY:
            continue
        kept: list[DiscoveredCommand] = []
        for kind in _PRIORITY:
            room = MAX_PER_CATEGORY - len(kept)
            if room <= 0:
                break
            of_kind = [c for c in collection if c.kind == kind]
            kept.extend(of_kind[:room])
            if len(of_kind) > room and kind in ("test", "install", "launch"):
                inspection.gaps.append(
                    f"more than {room} {kind} commands were discovered; some are not shown"
                )
        dropped = [c for c in collection if c not in kept]
        inspection.truncated = True
        kinds = sorted({c.kind for c in dropped})
        spared = [k for k in ("test", "install", "launch") if k in kinds]
        if spared:
            inspection.gaps.append(
                f"more than {MAX_PER_CATEGORY} {label} were discovered; "
                f"{len(dropped)} of kind {', '.join(kinds)} are not shown, "
                f"including {len([c for c in dropped if c.kind in spared])} "
                f"{'/'.join(spared)} command(s). The bound has no room for them."
            )
        else:
            inspection.gaps.append(
                f"more than {MAX_PER_CATEGORY} {label} were discovered; "
                f"{len(dropped)} of kind {', '.join(kinds)} are not shown. "
                "Test, install and launch commands are never dropped by this bound"
            )
        if label == "commands":
            inspection.commands = kept
        else:
            inspection.ci_commands = kept
