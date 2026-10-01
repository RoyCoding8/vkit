"""Guards on the boundaries that decide whether a decision is trustworthy.

Every test here calls the function a caller calls and asserts the literal it
gets back. Nothing asserts that a function was called, or that a value was
passed to itself.

The boundaries covered are the ones whose refusals no test exercised:
`scripts_of` naming nothing to pin, `repoint_approved` leaving a check the shape
could not explain untouched, and `fixture_identity` declining to measure an
input that lives outside the repository.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vkit.execution import ExecutionError, RunEnvironment, run_check  # noqa: E402
from vkit.identity import compute_source_identity  # noqa: E402
from vkit.integration.oracle import repoint_approved, scripts_of  # noqa: E402
from vkit.manifest import (  # noqa: E402
    CheckSpec,
    Manifest,
    ManifestError,
    parse_manifest_bytes,
)
from vkit.outcome import Blocked, BlockedReason  # noqa: E402
from vkit.storage import Store, StoreError  # noqa: E402

DRIVER_ARGV = ("python", "verify_price.py")
#: A name no PATH entry on a test machine will carry, so the prerequisite check
#: reports the tool missing without the run depending on the host's contents.
ABSENT_EXECUTABLE = "vkit-prerequisite-that-is-not-installed-9c1f"


def a_check(check_id: str, argv: tuple[str, ...], inputs: tuple[str, ...] = ()) -> CheckSpec:
    """A CheckSpec carrying only what these boundaries read.

    `scripts_of`, `repoint_approved` and `fixture_identity` look at `argv` and
    `inputs` and nothing else, so the remaining fields are the shape the parser
    produces and no more.
    """
    return CheckSpec(
        id=check_id,
        description="",
        argv=argv,
        cwd=Path("."),
        timeout_seconds=60.0,
        required_scenarios=(),
        artifact_name="result.json",
        prerequisites=(),
        inputs=inputs,
    )


def a_manifest(project, *checks: CheckSpec) -> Manifest:
    return Manifest(project=project, description="", checks={c.id: c for c in checks})


def a_repository(builder, **files: str):
    """A committed repository. `init_repo` commits whatever was written, so a
    repository with no file in it has nothing to commit."""
    return builder(files or {"README": "a repository\n"})


# --------------------------------------------------------------- scripts_of


def test_a_module_named_check_pins_no_script(repo) -> None:
    """`-m pkg` names a module, not a file, so there is nothing to pin.

    A reviewer told the trusted bytes are the candidate's needs that recorded as
    an empty pin, not a guess at which file `pkg` resolves to.
    """
    manifest = a_manifest(a_repository(repo), a_check("module", ("python", "-m", "verify_price")))

    assert scripts_of(manifest) == {}


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("C:/elsewhere/verify_price.py", id="absolute"),
        pytest.param("../outside/verify_price.py", id="parent-relative"),
    ],
)
def test_a_path_outside_the_tree_pins_no_script(repo, script: str) -> None:
    """An absolute path or a `..` escape names a file the approved revision
    cannot supply, so it is reported as pinning nothing."""
    manifest = a_manifest(a_repository(repo), a_check("outside", ("python", script)))

    assert scripts_of(manifest) == {}


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("C:/elsewhere/verify_price.py", id="windows-absolute"),
        pytest.param("C:\\elsewhere\\verify_price.py", id="windows-absolute-backslash"),
        pytest.param("/etc/verify_price.py", id="posix-absolute"),
    ],
)
def test_an_absolute_path_in_either_spelling_pins_no_script_on_any_host(
    repo, script: str
) -> None:
    """The refusal holds whichever way the host reads a path.

    A manifest is portable data, so the spelling of a path is not the running
    host's to decide. `pathlib.Path` parses with the platform's rules, which made
    this refusal host-shaped: on the CI POSIX runners
    `PurePosixPath("C:/elsewhere/verify_price.py")` is relative, so that check was
    pinned as a repository-relative script, the `checker_not_identifiable`
    refusal in `verify.py` never fired, and `repoint_approved` joined the value
    onto `approved_root` and ran a path outside the approved tree.

    Each spelling here is absolute to exactly one flavour, which is what makes it
    the shape of that bug: a host reading it with the other rules treats it as
    repository-relative. Asserting both readings is what pins host-independence
    rather than "this one string is refused".
    """
    from vkit.integration.oracle import _pins_a_file

    manifest = a_manifest(a_repository(repo), a_check("outside", ("python", script)))

    assert scripts_of(manifest) == {}, f"{script!r} was pinned on this host"
    assert not _pins_a_file(script), (
        f"{script!r} pins a file when read with either path flavour, so the "
        "refusal depends on which host is running"
    )
    absolute = (
        PureWindowsPath(script).is_absolute(), PurePosixPath(script).is_absolute()
    )
    assert any(absolute) and not all(absolute), (
        f"{script!r} is absolute under {absolute}, so it cannot be the shape of "
        "the defect: it needs to be absolute to one flavour and relative to "
        "another for the refusal to depend on the host"
    )


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("../outside/verify_price.py", id="parent-relative"),
        pytest.param("sub/../../outside/verify_price.py", id="escape-after-a-segment"),
        pytest.param("..\\outside\\verify_price.py", id="parent-relative-backslash"),
    ],
)
def test_a_parent_escape_in_either_spelling_pins_no_script(repo, script: str) -> None:
    """A `..` escape is refused on every host, in either separator's spelling.

    The complement of the absolute cases: these are relative to every flavour, so
    they never depended on the host, and they are asserted here so the guarantee
    is not mistaken for "absolute paths are the only thing being checked". A
    check that only ever tested one spelling would pass on the host its spelling
    happened to suit.
    """
    from vkit.integration.oracle import _pins_a_file

    manifest = a_manifest(a_repository(repo), a_check("outside", ("python", script)))

    assert scripts_of(manifest) == {}, f"{script!r} was pinned on this host"
    assert not _pins_a_file(script)
    assert ".." in PurePosixPath(script).parts or ".." in PureWindowsPath(script).parts, (
        f"{script!r} names no parent segment, so this case is not the escape it "
        "claims to be"
    )


def test_only_the_pinnable_check_of_two_is_pinned(repo) -> None:
    """The complement of the two refusals above.

    Without this, a `scripts_of` that returned `{}` for every manifest would
    satisfy the tests above and the refusals would be measuring nothing.
    """
    manifest = a_manifest(
        a_repository(repo),
        a_check("pinnable", DRIVER_ARGV),
        a_check("module", ("python", "-m", "verify_price")),
    )

    assert scripts_of(manifest) == {"pinnable": "verify_price.py"}


# ---------------------------------------------------------- repoint_approved


def test_repoint_approved_leaves_an_unpinned_check_exactly_as_declared(repo, tmp_path) -> None:
    """A check the shape cannot explain keeps the approved manifest's argv.

    Repointing it would substitute the candidate's path for approved bytes,
    which is the substitution this boundary exists to stop.
    """
    manifest = a_manifest(
        a_repository(repo),
        a_check("pinnable", ("python", "verify_price.py", "--out", "{{run_dir}}/r.json")),
        a_check("module", ("python", "-m", "verify_price")),
    )
    approved_root = tmp_path / "approved"

    repointed = repoint_approved(manifest, approved_root, scripts_of(manifest))

    assert repointed.require("module").argv == ("python", "-m", "verify_price")
    assert repointed.require("pinnable").argv == (
        "python", str(approved_root / "verify_price.py"), "--out", "{{run_dir}}/r.json",
    )
    # The manifest passed in is untouched; the copy is the only thing that moved.
    assert manifest.require("pinnable").argv[1] == "verify_price.py"


def test_repoint_approved_repoints_every_check_it_can_identify(repo, tmp_path) -> None:
    """Two pinnable checks are both moved, so the guard above cannot be met by a
    `repoint_approved` that repoints nothing at all."""
    manifest = a_manifest(
        a_repository(repo),
        a_check("first", DRIVER_ARGV),
        a_check("second", ("python", "verify_price.py", "--strict")),
    )
    approved_root = tmp_path / "approved"

    repointed = repoint_approved(manifest, approved_root, scripts_of(manifest))

    approved_script = str(approved_root / "verify_price.py")
    assert repointed.require("first").argv == ("python", approved_script)
    assert repointed.require("second").argv == ("python", approved_script, "--strict")


# -------------------------------------------------------- fixture_identity


def a_manifest_declaring(project, *inputs: str) -> Manifest:
    return a_manifest(project, a_check("c", DRIVER_ARGV, inputs=inputs))


def a_manifest_document(
    cwd: str, inputs: tuple[str, ...] = (), prerequisites: list[dict] | None = None
) -> bytes:
    import json

    return json.dumps({
        "schema_version": 1,
        "description": "A check.",
        "checks": [{
            "id": "c",
            "description": "",
            "command": list(DRIVER_ARGV),
            "cwd": cwd,
            "timeout_seconds": 60,
            "required_scenarios": ["one"],
            "artifact": "result.json",
            "inputs": list(inputs),
            "prerequisites": prerequisites or [],
        }],
    }).encode("utf-8")


def test_a_declared_input_inside_the_repository_is_measured(repo) -> None:
    """The positive case, so the refusals below are not satisfied by a
    `fixture_identity` that declines everything."""
    project = a_repository(repo, **{"present.txt": "contents\n"})

    measured = a_manifest_declaring(project, "present.txt").fixture_identity()

    assert measured is not None
    assert [entry["input"] for entry in measured.inputs] == ["present.txt"]


def test_an_absolute_input_naming_a_file_inside_the_repository_is_refused(repo) -> None:
    """Absolute is refused on its own, not because the file is out of bounds.

    This is the only case the absolute-path refusal decides on its own: the file
    is real and inside the repository, so the containment check would let it
    through, and a manifest that spells a path out absolutely would be measuring
    the same file by a route that does not survive the repository moving.
    """
    project = a_repository(repo, **{"present.txt": "contents\n"})

    assert a_manifest_declaring(
        project, str(project.root / "present.txt")
    ).fixture_identity() is None


@pytest.mark.parametrize("as_absolute", [False, True], ids=["parent-relative", "absolute"])
def test_a_real_file_outside_the_repository_is_not_measured(
    repo, tmp_path, as_absolute: bool
) -> None:
    """`fixture_identity` returns None rather than a digest over a file the
    repository's own identity cannot describe.

    None is the refusal, not an empty identity: the caller blocks on it rather
    than proceeding without a measurement.

    The file really exists, beside the repository and not inside it. That is the
    whole point of the case: a declared input that is missing is refused by a
    third, unrelated check, so a test built on a missing file would pass even
    with the containment and absolute-path refusals deleted, and would be
    measuring that check instead of these two.
    """
    project = a_repository(repo, **{"present.txt": "contents\n"})
    outside = tmp_path / "beside-the-repo.txt"
    outside.write_text("not the repository's business\n", encoding="utf-8")
    raw = str(outside) if as_absolute else f"../{outside.name}"

    measured = a_manifest_declaring(project, raw).fixture_identity()

    assert measured is None


def test_a_declared_input_that_does_not_exist_is_not_measured(repo) -> None:
    """The third refusal, for the same reason: a gap is not success."""
    project = a_repository(repo, **{"present.txt": "contents\n"})

    assert a_manifest_declaring(project, "absent.txt").fixture_identity() is None


# --------------------------------------------------------------------- cwd


def test_a_cwd_inside_the_repository_parses(repo) -> None:
    """The positive case for the cwd refusals below."""
    project = a_repository(repo)
    (project.root / "pricing").mkdir()

    manifest = parse_manifest_bytes(
        a_manifest_document("pricing"), project=project,
        run_dir=project.runs_root, origin="test",
    )

    assert manifest.require("c").cwd == (project.root / "pricing").resolve()


@pytest.mark.parametrize(
    "cwd",
    [
        pytest.param("../", id="parent-relative"),
        pytest.param("..", id="parent-itself"),
    ],
)
def test_a_cwd_outside_the_repository_is_refused(repo, cwd: str) -> None:
    """A working directory is repository-relative and must stay inside the root.

    The check is made after resolution, so a string prefix test on `../` would
    pass it. Only a `..` form can show that here: an absolute cwd resolves to the
    same place the containment check refuses, so deleting the containment line
    leaves the absolute case decided by the same test.
    """
    project = a_repository(repo)

    with pytest.raises(ManifestError, match="escapes the repository root"):
        parse_manifest_bytes(
            a_manifest_document(cwd), project=project,
            run_dir=project.runs_root, origin="test",
        )


def test_an_absolute_cwd_inside_the_repository_is_refused_by_containment(repo) -> None:
    """The absolute cwd escape, refused.

    Kept because it is the case the containment line decides, and a test that
    only used `..` would still pass with that line deleted on a machine whose
    drive layout differs.
    """
    project = a_repository(repo)
    outside = project.root.parent / "beside-the-repo"
    outside.mkdir(exist_ok=True)

    with pytest.raises(ManifestError, match="escapes the repository root"):
        parse_manifest_bytes(
            a_manifest_document(str(outside)), project=project,
            run_dir=project.runs_root, origin="test",
        )


# ------------------------------------------------------- ExecutionError


def test_a_run_whose_report_cannot_be_published_raises_rather_than_reporting(
    repo,
) -> None:
    """The report failing to publish is an `ExecutionError`, not a BLOCKED outcome.

    This is the distinction the class docstring claims: a BLOCKED is a recorded
    answer about the check and comes back as a value carrying a reason, while a
    report that cannot be recorded leaves the caller with no answer at all. A
    `run_check` that returned an outcome here would let an unrecoverable storage
    failure read as a decided refusal, and `vkit check run` would print BLOCKED
    and exit 3 rather than the internal error it is.

    The check declares a prerequisite that is not installed, so the run blocks
    before it launches anything and the publish is the only thing left to fail.
    The store refuses that publish the way the real store does when a run is
    already terminal.
    """
    project = a_repository(repo)
    manifest = parse_manifest_bytes(
        a_manifest_document(".", prerequisites=[{
            "name": "a tool that is not installed", "executable": ABSENT_EXECUTABLE,
        }]),
        project=project, run_dir=project.runs_root, origin="test",
    )

    class PublishRefused(Store):
        """A store whose publish always fails as a real one can."""

        def publish(self, run_id: str, report: dict) -> None:
            raise StoreError(f"cannot publish: {run_id} is unknown or already terminal")

    with pytest.raises(ExecutionError, match="could not publish the report"):
        run_check(
            manifest, "c", store=PublishRefused(project.db_path),
            source=compute_source_identity(project),
            run_id="publish-refused", env=RunEnvironment(),
        )


def test_the_same_unpublishable_run_would_otherwise_have_been_blocked(repo) -> None:
    """The control for the test above: with a working store, that run BLOCKS.

    Without this, an `ExecutionError` raised from anywhere in `run_check` would
    satisfy the test above, including one raised because the run blocked for an
    unrelated reason. Here the identical manifest returns the BLOCKED value.
    """
    project = a_repository(repo)
    manifest = parse_manifest_bytes(
        a_manifest_document(".", prerequisites=[{
            "name": "a tool that is not installed", "executable": ABSENT_EXECUTABLE,
        }]),
        project=project, run_dir=project.runs_root, origin="test",
    )

    result = run_check(
        manifest, "c", store=Store(project.db_path),
        source=compute_source_identity(project),
        run_id="publishes-fine", env=RunEnvironment(),
    )

    assert isinstance(result.outcome, Blocked)
    assert result.outcome.reason is BlockedReason.PREREQUISITE_MISSING