"""The adapters ship, and an installed wheel runs a verifier with no checkout.

Two claims, and only the second one has ever been tested. `pyproject.toml`
force-includes `schemas` and the plugin tree, and `packages = ["src/vkit"]` sweeps
in everything else under the package directory, `src/vkit/verifiers/` included.
That is the build configuration, not the artifact: a reader has no reason to
believe it survives a later build, and the whole reason this product distrusts a
configuration is that it is not the thing that runs. So the archive is read
directly, by name, for each file the receipt path needs.

**The isolation is the test.** `tests/conftest.py` puts this checkout's `src` on
`sys.path`, and the editable install in the shared `.venv` puts the *main*
checkout's `src` there too. A test that imports `vkit` in the pytest process and
asserts the adapters are reachable proves only that the source tree has them,
which was never in doubt. Nothing in this file imports `vkit` in the pytest
process at all. A subprocess is started, that interpreter's `sys.path` is
stripped of every entry that could hand out a real `vkit` package, the wheel's
install directory goes in at index 0, the working directory is a temporary
directory outside the repository, and the driver reports the path it actually
resolved. The assertions then check that path is inside the install and outside
this checkout.

`--target` rather than a fresh virtualenv, for the reason
`test_console_packaging.py` records: `vkit.verifiers` imports the manifest
parser, which imports `jsonschema` at module scope, so a `--no-deps` virtualenv
could not start it and the gate would have to be rewritten to stop testing it.
Installing into a target directory and driving it with the interpreter running
this suite keeps every declared dependency available while `vkit` itself comes
only from the install.

**What the driver runs.** A real `pytest` check, declared by a manifest the
driver writes into the scratch repository, executed by the installed package's
own `execution.run_check`, with the installed package's own `verifiers.dispatch`
as the interpreter. That is the whole third execution path the checkpoint asks
about: foreground, detached and integration verification all reach
`execution.run_check`, so a verifier that works here works in all three.
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

import subproc

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The adapter modules a receipt path cannot work without, named individually
#: rather than counted. A count would pass on an archive shipping a subset, and
#: `dispatch` missing while `spec` shipped is a different defect from `spec`
#: missing while `dispatch` shipped.
VERIFIER_MODULES = (
    "__init__.py", "dispatch.py", "obligation.py", "spec.py",
    "pytest_adapter.py", "node_adapter.py", "property_adapter.py",
)

#: The schemas the receipt and manifest boundary documents are validated against.
#: `receipt.v2.json` is named because this file's whole claim is about the
#: receipt path, and `manifest.v2.json` because the driver declares a v2 check.
SCHEMAS = (
    "receipt.v2.json", "manifest.v2.json",
    "check-artifact.v1.json", "run-report.v1.json", "manifest.v1.json",
)

APP_SOURCE = '''\
"""The code under test, in a repository that exists only for this gate."""


def quote(amounts):
    """The sum of the amounts."""
    return sum(amounts)
'''

TEST_SOURCE = '''\
"""One real test, with its expectation written here as a literal."""
from pricing.quote import quote


def test_totals_an_empty_cart():
    assert quote([]) == 0
'''

MANIFEST = {
    "schema_version": 2,
    "description": "A totals function, verified by running its test.",
    "checks": [{
        "id": "quote-regression",
        "kind": "pytest",
        "description": "Runs the approved totals test.",
        "cwd": ".",
        "timeout_seconds": 300,
        "artifact": "pytest-report.json",
        "inputs": ["pricing/quote.py", "tests/test_behaviour.py"],
        "expectations": [],
        "subject": {"paths": ["pricing/quote.py"], "digest": None},
        "claim_id": "quote-sums-the-amounts",
        "required_tests": ["tests/test_behaviour.py::test_totals_an_empty_cart"],
        "runner": {"executable": "{{python}}", "base_argv": ["-m", "pytest", "-q"]},
        "report_format": "pytest_json_report",
        "expect_report_version": 1,
    }],
}

#: The driver. Written out rather than imported from `tests/`, for the same
#: reason `test_console_packaging.py` writes its own: this process runs with the
#: install on `sys.path` and no `tests/` directory, and the point of the gate is
#: that it runs where nothing from this checkout is importable.
DRIVER = '''
import importlib.resources, json, subprocess, sys
from pathlib import Path


def hidden_window():
    """`subprocess` keywords that keep a launched child off the operator's screen."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


target, scratch, app_source, test_source, manifest_json = sys.argv[1:6]

sys.path[:] = [
    entry for entry in sys.path
    if not (Path(entry or ".").resolve() / "vkit" / "__init__.py").is_file()
]
sys.path.insert(0, target)

import vkit
from vkit.execution import run_check
from vkit.identity import compute_source_identity
from vkit.manifest import parse_manifest
from vkit.paths import open_project
from vkit.storage import Store

repo = Path(scratch) / "shop"
(repo / "pricing").mkdir(parents=True)
(repo / "pricing" / "__init__.py").write_text("", encoding="utf-8")
(repo / "pricing" / "quote.py").write_text(app_source, encoding="utf-8")
(repo / "tests").mkdir()
(repo / "tests" / "test_behaviour.py").write_text(test_source, encoding="utf-8")
(repo / ".gitignore").write_text("__pycache__/\\n*.pyc\\n", encoding="utf-8")
(repo / "verification").mkdir()
(repo / "verification" / "manifest.json").write_text(manifest_json, encoding="utf-8")
for argv in (
    ["git", "init", "-q"],
    ["git", "add", "-A"],
    ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t",
     "commit", "-qm", "the approved baseline"],
):
    done = subprocess.run(argv, cwd=repo, capture_output=True, check=False, **hidden_window())
    if done.returncode != 0:
        raise SystemExit("git failed in the scratch repository: " + done.stderr.decode())

project = open_project(repo)
store = Store(project.db_path)
outcome = run_check(
    parse_manifest(project, project.runs_root), "quote-regression", store=store,
    source=compute_source_identity(project),
)

receipt_path = store.run_dir(outcome.report["run_id"]) / "receipt.v2.json"
receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
row = store.list_runs(limit=1)[0]

print(json.dumps({
    "vkit_file": str(Path(vkit.__file__).resolve()),
    "vkit_paths": [str(Path(entry).resolve()) for entry in vkit.__path__],
    "dispatch_file": str(Path(sys.modules["vkit.verifiers.dispatch"].__file__).resolve()),
    # Through the same public route `schemas._schema_dir` itself takes, and through
    # `_paths[0]` because the installed package here still resolves as
    # multi-location: an `--target` install puts `vkit` in the target directory
    # while the ambient `site-packages` also answers to the name, so
    # `resources.files` returns a MultiplexedPath whose `str()` is its repr. The
    # first entry is the one `import vkit` actually loaded, which is the install,
    # and the assertions below confirm that rather than trusting the index.
    "schema_dir": str(
        Path(str(importlib.resources.files("vkit._schemas")._paths[0])).resolve()
    ),
    "result": outcome.report["outcome"]["result"],
    "receipt_status": receipt["status"],
    "evidence_kind": receipt["evidence_kind"],
    "satisfied": [s["obligation"]["obligation"] for s in receipt["satisfied"]],
    "row_evidence_kind": row["evidence_kind"],
    "verifier": receipt["verifier"]["module"],
}))
'''


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subproc.run(
        args, cwd=cwd, capture_output=True, encoding="utf-8",
        errors="replace", timeout=900, check=False,
    )


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One wheel for the module, built without reaching a package index.

    `--no-build-isolation` makes pip import the declared backend (`hatchling`)
    from the environment running the suite, which the `test` extra installs.
    Under pip's own isolation the backend lands in an overlay environment that
    is then discarded, so the build would need an index to reach. That hole is
    recorded at `pyproject.toml`; this is the same door entered from the other
    side.
    """
    out_dir = tmp_path_factory.mktemp("verifier-dist")
    done = _run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
         "--wheel-dir", str(out_dir), str(REPO_ROOT)],
        cwd=out_dir,
    )
    assert done.returncode == 0, (
        f"pip wheel failed:\n{done.stdout}\n{done.stderr}\n"
        "The build backend comes from this environment, so a failure here on a "
        "host with no index means the `test` extra is not installed."
    )
    wheels = list(out_dir.glob("vkit-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found {[w.name for w in wheels]}"
    return wheels[0]


@pytest.fixture(scope="module")
def install_dir(wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The wheel unpacked into a directory outside the checkout."""
    target = tmp_path_factory.mktemp("verifier-install")
    done = _run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-cache-dir",
         "--disable-pip-version-check", "--target", str(target), str(wheel)],
        cwd=target,
    )
    assert done.returncode == 0, f"pip install failed:\n{done.stdout}\n{done.stderr}"
    return target.resolve()


@pytest.fixture(scope="module")
def installed(install_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Run a project-specific verifier from the install, and report what it did.

    One run for the module, because the expensive halves are the wheel build and
    the install rather than the check.

    The working directory is a temporary directory outside the repository. That
    denies `import vkit` a path back through the ambient `sys.path`, so the
    resolved path asserted below can only be the packaged copy.
    """
    scratch = tmp_path_factory.mktemp("verifier-scratch")
    done = _run(
        [sys.executable, "-c", DRIVER, str(install_dir), str(scratch),
         APP_SOURCE, TEST_SOURCE, json.dumps(MANIFEST)],
        cwd=scratch,
    )
    assert done.returncode == 0, (
        f"the installed package did not run a verifier:\n{done.stdout}\n{done.stderr}\n"
        "A traceback naming a checkout path means the scrub let a source tree in, "
        "and a refusal means the wheel is missing something the receipt path needs."
    )
    return json.loads(done.stdout)


# ------------------------------------------------------- what the wheel carries


@pytest.mark.parametrize("module", VERIFIER_MODULES)
def test_every_adapter_module_ships_in_the_wheel(wheel: Path, module: str) -> None:
    """Named one at a time, because a count would pass on a subset."""
    names = zipfile.ZipFile(wheel).namelist()
    assert f"vkit/verifiers/{module}" in names, (
        f"{module} is not in the wheel, so an installed vkit cannot dispatch a "
        f"verifier of that kind. The wheel carries: "
        f"{sorted(n for n in names if n.startswith('vkit/verifiers/'))}"
    )


@pytest.mark.parametrize("schema", SCHEMAS)
def test_every_schema_ships_in_the_wheel(wheel: Path, schema: str) -> None:
    """The boundary contracts are validated against, read from the install.

    `pyproject.toml` force-includes them as `vkit/_schemas`, which is a
    relocation: `schemas.py._schema_dir` reads the packaged form first and the
    checkout second, so an installed copy with no `schemas/` beside it would fall
    back to a directory that does not exist and raise rather than validate.
    """
    names = zipfile.ZipFile(wheel).namelist()
    assert f"vkit/_schemas/{schema}" in names, (
        f"{schema} is not in the wheel under vkit/_schemas, so an installed vkit "
        f"cannot validate that boundary document"
    )


# ------------------------------------------------ what the install actually ran


def test_the_install_resolved_itself_outside_the_checkout(
    installed: dict, install_dir: Path,
) -> None:
    """`import vkit` in the driver resolved to the install, not to `src`.

    The assertion every other test here leans on. The driver reports the resolved
    path and this message quotes it, so a reader who suspects the gate can reach
    a checkout can see which directory it actually loaded from without
    re-running anything.
    """
    resolved = Path(installed["vkit_file"])
    assert resolved.is_relative_to(install_dir), (
        f"import vkit resolved to {resolved}, which is not inside the install "
        f"{install_dir}. This gate proves nothing about the wheel while it can "
        f"reach a checkout."
    )
    assert not resolved.is_relative_to(REPO_ROOT / "src"), (
        f"import vkit resolved to {resolved}, which is this checkout's source "
        f"tree. The installed package was not the one under test."
    )

    paths = [Path(entry) for entry in installed["vkit_paths"]]
    assert paths == [install_dir / "vkit"], (
        f"vkit.__path__ is {paths}, so `vkit` is a namespace package spanning "
        f"more than one directory. The installed copy and some other tree are "
        f"sharing the name, and which one answers is left to sys.path order."
    )


def test_the_adapter_dispatch_also_came_from_the_install(
    installed: dict, install_dir: Path,
) -> None:
    """`vkit.verifiers.dispatch` specifically, not merely the package around it.

    Named apart from the check above because it is the module under test. A
    `vkit` that resolved to the install while its `verifiers` came from a
    checkout would pass a package-level assertion and fail every real user.
    """
    resolved = Path(installed["dispatch_file"])
    assert resolved == install_dir / "vkit" / "verifiers" / "dispatch.py", (
        f"vkit.verifiers.dispatch resolved to {resolved}, which is not the "
        f"installed adapter. The verifier under test is not the packaged one."
    )


def test_the_install_directory_is_outside_this_repository(install_dir: Path) -> None:
    """The precondition the rest of the module rests on.

    A `--target` install landing inside the checkout would let every assertion
    above pass while reading source files.
    """
    assert not install_dir.is_relative_to(REPO_ROOT), (
        f"the wheel was installed to {install_dir}, which is inside this "
        f"repository at {REPO_ROOT}, so a reader could not tell an installed "
        f"package from the checkout"
    )


def test_the_schemas_the_install_read_are_the_packaged_ones(
    installed: dict, install_dir: Path,
) -> None:
    """The schema directory the running package resolved, from the install.

    Checked by location rather than by asking the package, because a resolver
    that fell back to the checkout would report a path this assertion catches.
    """
    resolved = Path(installed["schema_dir"]).resolve()
    assert resolved == install_dir / "vkit" / "_schemas", (
        f"the installed package resolved its schemas at {resolved}, which is not "
        f"the packaged copy under {install_dir}. It found a checkout instead"
    )


# ----------------------------------------------- what the verifier actually did


def test_the_installed_verifier_ran_and_passed(installed: dict) -> None:
    """A project-specific check, executed by the installed package, end to end.

    The manifest is one the driver wrote, the tests are real, and the runner is
    the interpreter running this suite. Nothing here is mocked, so a wheel
    missing `dispatch` or its adapters would fail rather than fall back to a
    default.
    """
    assert installed["result"] == "PASS", installed
    assert installed["verifier"] == "vkit.verifiers.dispatch", (
        f"the receipt names verifier {installed['verifier']!r}, so the reading "
        f"was not made by the adapter this gate is about"
    )


def test_the_receipt_the_install_wrote_names_its_category_and_its_obligations(
    installed: dict,
) -> None:
    """What the receipt carries is what the checkpoint's contract promises.

    Both facts, asserted together, because one without the other is a partial
    receipt: the category says what a green result licenses, and the satisfied
    entries say which obligations paid for it. This is the same projection
    acceptance reads, produced with nothing from this checkout in reach.
    """
    assert installed["evidence_kind"] == "scenario", (
        f"the receipt recorded evidence_kind {installed['evidence_kind']!r} for a "
        f"pytest check with no generator block, which is not what "
        f"evidence_kind(spec) derives"
    )
    assert installed["satisfied"] == ["tests/test_behaviour.py::test_totals_an_empty_cart"], (
        f"the receipt's satisfied obligations are {installed['satisfied']}, which "
        f"do not name the case the check actually watched"
    )


def test_the_run_row_carries_what_the_receipt_recorded(installed: dict) -> None:
    """The projection `Store.publish` writes, measured at the installed package.

    The row is what acceptance reads, so a receipt that named a category the row
    did not carry would be a second, divergent record of the same fact. Written
    here rather than imported because this process must not import `vkit`.
    """
    assert installed["row_evidence_kind"] == installed["evidence_kind"], (
        f"the run row recorded evidence_kind "
        f"{installed['row_evidence_kind']!r} while its receipt recorded "
        f"{installed['evidence_kind']!r}. Acceptance compares the row, so this is "
        f"the divergence that would matter."
    )
