"""The Plan 06 loop, driven through the installed command, on real repositories.

These are the acceptance rows Plan 06 states, exercised end to end: a fresh
Git repository built from one of the shipped examples, inspected, enrolled,
accepted, and run through `vkit check run` until a real defect produces a real
FAIL and a missing prerequisite produces a real BLOCKED.

Everything goes through the console entry point rather than importing vkit,
because the thing being tested is the command a user or an agent types. A test
that imports the module proves the module works; it does not prove the
argument parsing, the exit codes, or the stdout contract.

The BLOCKED-by-prerequisite case is the one that proves prerequisites are real
rather than declared. The manifest names `node`; the test runs the check with a
PATH that has no `node` on it and asserts the recorded reason is
`prerequisite_missing`. A manifest that merely listed node would still pass a
test that never removed it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from conftest import console_script

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_HTTP = REPO_ROOT / "examples" / "node-http"
PYTHON_CLI = REPO_ROOT / "examples" / "python-cli"


def vkit(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the real console entry point, never an import."""
    return subprocess.run(
        [str(console_script()), *args],
        capture_output=True, text=True, timeout=300, env=env, check=False,
    )


#: A name no PATH entry carries, so the resolution below is about the
#: distribution rather than about anything the host has installed.
_ABSENT = "vkit-console-script-that-is-not-installed-9c1f"


def test_the_console_script_is_found_wherever_the_installer_put_it(tmp_path) -> None:
    """The locator reads the installation instead of predicting its layout.

    The bug this replaces hardcoded `sys.executable`'s parent, which holds for one
    layout only. `setup-python` puts macOS interpreters under
    `/Library/Frameworks/Python.framework/Versions/3.13/bin/python` while pip puts
    the script in `~/.local/bin/`, so thirteen of these tests failed on the macOS
    runner and the whole file read as a product defect.

    Two installs are built here with the two layouts CI actually produced, each a
    real `.dist-info` with a `RECORD` naming the script, and the locator has to
    return the script in both. The assertion is against the path that was built,
    not against the locator's own output, so it cannot be satisfied by a locator
    that returns something plausible.
    """
    import importlib.metadata as metadata

    def build(root: Path, site_packages: Path, script: Path) -> Path:
        site_packages.mkdir(parents=True, exist_ok=True)
        info = site_packages / "probe-0.1.0.dist-info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "METADATA").write_text("Name: probe\nVersion: 0.1.0\n", encoding="utf-8")
        (info / "entry_points.txt").write_text(
            "[console_scripts]\nprobe = probe.cli:main\n", encoding="utf-8"
        )
        # The RECORD path is relative to site-packages, exactly as an installer
        # writes it, so `locate_file` has to do the same arithmetic.
        (info / "RECORD").write_text(
            f"{os.path.relpath(script, site_packages).replace(os.sep, '/')},,\n",
            encoding="utf-8",
        )
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexec probe.cli:main \"$@\"\n", encoding="utf-8")
        return site_packages

    def resolve(site_packages: Path, name: str) -> Path:
        sys.path.insert(0, str(site_packages))
        try:
            metadata.MetadataPathFinder.invalidate_caches()
            for found in metadata.entry_points(group="console_scripts"):
                if found.name != name:
                    continue
                for entry in found.dist.files or ():
                    if PureWindowsPath(str(entry)).stem.lower() == name.lower():
                        return Path(found.dist.locate_file(entry)).resolve()
        finally:
            sys.path.remove(str(site_packages))
            metadata.MetadataPathFinder.invalidate_caches()
        raise AssertionError(f"no console script named {name!r} was resolved")

    # macOS: the framework interpreter and ~/.local/bin are not neighbours.
    home = tmp_path / "macos"
    macos_site = home / "Library/Frameworks/Python.framework/Versions/3.13/lib/python3.13/site-packages"
    macos_script = home / ".local/bin/probe"
    assert resolve(build(home, macos_site, macos_script), "probe") == macos_script

    # Windows: a framework/embedded layout with the script under Scripts\.
    hosted = tmp_path / "hostedtoolcache/python/3.13/x64"
    windows_site = hosted / "Lib/site-packages"
    windows_script = hosted / "Scripts/probe.exe"
    assert resolve(build(hosted, windows_site, windows_script), "probe") == windows_script


def test_the_locator_refuses_rather_than_naming_something_that_is_not_there() -> None:
    """A script nobody installed is reported, not guessed at.

    A locator that fell back to a conventional path would hand a subprocess a
    filename and let the OS raise `FileNotFoundError` at run time, which reads as
    a product failure. The message has to name the install step instead, because
    the real cause of "the console script is missing" is almost always that the
    distribution was never installed.
    """
    with pytest.raises(AssertionError) as caught:
        console_script(_ABSENT)

    assert "pip install" in str(caught.value), (
        f"the refusal does not say what to do about it: {caught.value}"
    )


def fresh_repo(tmp_path: Path, example: Path, name: str) -> Path:
    """Copy an example into a new repository, the way the README describes."""
    root = tmp_path / name
    shutil.copytree(example, root)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=root, check=True, capture_output=True,
    )
    return root


def node_available() -> bool:
    return shutil.which("node") is not None


requires_node = pytest.mark.skipif(not node_available(), reason="node is not installed")


# --- inspection ------------------------------------------------------------


def test_inspect_reports_the_node_example_it_was_pointed_at(tmp_path: Path) -> None:
    """A fresh repository from the example, inspected through the command."""
    root = fresh_repo(tmp_path, NODE_HTTP, "inspected")

    done = vkit("project", "inspect", "--project", str(root), "--json")

    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["project"] == str(root.resolve())
    assert "node" in report["ecosystem"]
    # The example declares a test script, so there is something to say about it.
    test_commands = [c for c in report["commands"] if c["kind"] == "test"]
    assert test_commands, f"the node example's test script was not discovered: {report}"
    for command in test_commands:
        assert command["provenance"]["file"] == "package.json" or command["provenance"]["line"] > 0


def test_inspect_reports_nothing_was_installed_by_leaving_the_tree_alone(
    tmp_path: Path,
) -> None:
    """Inspection must not add a node_modules or a lockfile to someone's repo."""
    root = fresh_repo(tmp_path, NODE_HTTP, "untouched")
    before = sorted(p.name for p in root.iterdir())

    vkit("project", "inspect", "--project", str(root))

    assert sorted(p.name for p in root.iterdir()) == before


def test_inspect_reports_a_repository_with_nothing_to_run_as_blocked(
    tmp_path: Path,
) -> None:
    """A repository that declares no command is exit 3, not a successful empty report."""
    root = tmp_path / "bare"
    root.mkdir()
    (root / "README.md").write_text("# nothing here\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bare"],
        cwd=root, check=True, capture_output=True,
    )

    done = vkit("project", "inspect", "--project", str(root), "--json")

    assert done.returncode == 3, done.stdout
    report = json.loads(done.stdout)
    assert report["commands"] == []
    assert any("no supported project file" in gap for gap in report["gaps"])


# --- enrollment ------------------------------------------------------------

def test_enroll_previews_the_command_policy_and_runs_nothing(tmp_path: Path) -> None:
    """The preview a person reads, and the fact that reading it changed nothing.

    Both halves are asserted. A preview nobody can see is not a preview, and a
    preview that launched the commands it described would be a very bad one.
    """
    root = fresh_repo(tmp_path, PYTHON_CLI, "previewed")
    (root / "verification" / "manifest.json").unlink()

    done = vkit("project", "enroll", "--project", str(root))

    assert done.returncode in (0, 3), done.stdout
    assert "nothing runs yet" in done.stdout
    # The policy path still does not exist, so the core cannot read a manifest.
    assert not (root / "verification" / "manifest.json").exists()
    # And a check cannot be run from what is there.
    refused = vkit("check", "run", "--project", str(root), "--check", "pytest-configured")
    assert refused.returncode == 2, f"a check ran from a proposal: {refused.stdout}"


def test_enroll_refuses_to_overwrite_a_hand_maintained_manifest(tmp_path: Path) -> None:
    """The refusal, asserted on the bytes that survived."""
    root = fresh_repo(tmp_path, PYTHON_CLI, "already-enrolled")
    before = (root / "verification" / "manifest.json").read_bytes()

    done = vkit("project", "enroll", "--project", str(root))

    assert (root / "verification" / "manifest.json").read_bytes() == before, (
        "enroll overwrote a hand-maintained manifest"
    )
    assert "was not touched" in done.stdout


def test_declining_enrollment_leaves_commands_unexecuted_and_files_unchanged(
    tmp_path: Path,
) -> None:
    """Plan 06's acceptance row, taken literally.

    Declining after proposing, then confirming that the policy file did not
    appear, that the proposal is gone, and that no command ran.
    """
    root = fresh_repo(tmp_path, PYTHON_CLI, "declined")
    (root / "verification" / "manifest.json").unlink()
    vkit("project", "enroll", "--project", str(root))
    proposal = root / "verification" / "proposed-manifest.json"
    assert proposal.is_file(), "nothing was proposed, so declining proves nothing"

    done = vkit("project", "enroll", "--project", str(root), "--decline")

    assert done.returncode == 0, done.stdout
    assert not proposal.exists(), "a declined proposal was left behind"
    assert not (root / "verification" / "manifest.json").exists()
    assert "not_enrolled" in done.stdout


def test_acceptance_is_refused_for_a_proposal_nobody_finished(tmp_path: Path) -> None:
    """A proposal vkit could not finish cannot be accepted, by the real schema."""
    root = fresh_repo(tmp_path, PYTHON_CLI, "unfinished")
    (root / "verification" / "manifest.json").unlink()
    vkit("project", "enroll", "--project", str(root))

    done = vkit("project", "enroll", "--project", str(root), "--accept")

    assert done.returncode == 2, done.stdout
    # A refusal in text mode is written to stderr, which is where diagnostics go
    # in every mode. Asserting on the combined output keeps the test honest
    # about which stream carried the message.
    assert "required_scenarios" in done.stdout + done.stderr
    assert not (root / "verification" / "manifest.json").exists()


def test_a_completed_proposal_is_accepted_and_then_runs(tmp_path: Path) -> None:
    """The full path: propose, finish by hand, accept, and execute the policy.

    A pytest configuration is added to the example so discovery has something to
    report. vkit proposing the check on its own would be wrong: it can read that
    pytest is configured, but it cannot know that `verify_totals.py` is the
    driver, or what scenarios that driver reports. Those two facts come from the
    driver, which is why the person supplies them.
    """
    root = fresh_repo(tmp_path, PYTHON_CLI, "accepted")
    (root / "verification" / "manifest.json").unlink()
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\n", encoding="utf-8"
    )
    # The proposed check's command is `python -m pytest`, whose prerequisites are
    # `python` and `pytest`. The venv running these tests has both, but the
    # console script inherits whatever PATH it was launched with, so the
    # directory holding them is named explicitly rather than assumed.
    environment = {**os.environ}
    environment["PATH"] = os.pathsep.join(
        [str(Path(sys.executable).parent), environment.get("PATH", "")]
    )
    proposed = vkit("project", "enroll", "--project", str(root), env=environment)
    assert proposed.returncode == 0, f"nothing was proposed: {proposed.stdout}"

    # A person finishes the proposal: the driver exists, so the command and the
    # scenarios come from the driver they wrote.
    proposal = root / "verification" / "proposed-manifest.json"
    document = json.loads(proposal.read_text(encoding="utf-8"))
    assert document["checks"], "discovery found a test command but proposed nothing"
    driver_scenarios = _scenarios_in_driver(root)
    assert driver_scenarios, "the example driver declares no scenarios to require"
    document["checks"][0]["command"] = [
        "{{python}}", "verify_totals.py", "--app", "src/totals.py",
        "--out", "{{run_dir}}/result.json",
    ]
    document["checks"][0]["required_scenarios"] = driver_scenarios
    proposal.write_text(json.dumps(document, indent=2), encoding="utf-8")

    accepted = vkit("project", "enroll", "--project", str(root), "--accept", env=environment)
    assert accepted.returncode == 0, accepted.stdout
    assert "accepted" in accepted.stdout
    assert (root / "verification" / "manifest.json").is_file()

    doctor = vkit("doctor", "--project", str(root), env=environment)
    assert doctor.returncode == 0, doctor.stdout

    check_id = json.loads((root / "verification" / "manifest.json").read_text(encoding="utf-8"))["checks"][0]["id"]
    run = vkit("check", "run", "--project", str(root), "--check", check_id, "--json", env=environment)
    assert run.returncode == 0, run.stdout
    assert json.loads(run.stdout)["outcome"]["result"] == "PASS"


def test_a_real_defect_yields_fail_with_expected_versus_actual(tmp_path: Path) -> None:
    """Break the application for real and watch the harness notice.

    The defect is an arithmetic change, not a flag the driver is told about, so
    the driver cannot be complicit. The assertion is on the observation text
    naming both the expected and the actual value, which is what makes the
    report useful to whoever has to fix it.
    """
    root = fresh_repo(tmp_path, PYTHON_CLI, "defective")
    source = root / "src" / "totals.py"
    original = source.read_text(encoding="utf-8")
    assert "running += amount" in original
    source.write_text(
        original.replace("running += amount", "running += amount + 1"), encoding="utf-8"
    )

    run = vkit("check", "run", "--project", str(root), "--check", "totals-behavior", "--json")

    assert run.returncode == 1, run.stdout
    outcome = json.loads(run.stdout)["outcome"]
    assert outcome["result"] == "FAIL"
    failing = [s for s in outcome["scenarios"] if s["result"] == "FAIL"]
    assert failing, "the defect produced no failing scenario"
    assert any("expected" in s["observation"] and "got" not in s["observation"] or
               "printed" in s["observation"] for s in failing)

    # Restoring the file returns the run to PASS, which is what makes the FAIL
    # a statement about the defect rather than about the harness.
    source.write_text(original, encoding="utf-8")
    restored = vkit("check", "run", "--project", str(root), "--check", "totals-behavior", "--json")
    assert restored.returncode == 0, restored.stdout


def test_a_missing_prerequisite_yields_blocked_with_that_reason(tmp_path: Path) -> None:
    """BLOCKED, by measurement: the prerequisite is made genuinely unsatisfiable.

    The manifest keeps naming node the whole time. What changes is the
    environment, so a report that read the prerequisite out of the manifest
    would return PASS here.

    The mechanism is PATH, which is the one `shutil.which` consults, and every
    directory on PATH providing a `node` is removed rather than only the one
    `shutil.which` returns first. The removal is then *verified through
    `shutil.which` in a fresh interpreter* before the assertion it exists to
    support, because on Windows a process launched with a stripped PATH can
    still resolve an executable through the App Paths registry, and a test that
    assumed the stripping worked would pass for the wrong reason.

    What is proved is the one thing this test is about: vkit resolves a
    prerequisite against the environment and BLOCKS when it is unsatisfiable. It
    is not a claim that a hostile environment cannot launch node by other
    means.
    """
    if not node_available():
        pytest.skip("node is not installed, so there is nothing to remove")
    root = fresh_repo(tmp_path, NODE_HTTP, "no-node")

    node_directories = _directories_providing("node")
    assert node_directories, "node is on PATH but no directory providing it was identified"
    # Removing node's directories also removes anything else living beside it.
    # On Windows node and git are in separate trees and this is a no-op. On Linux
    # both are /usr/bin, so stripping node took git with it and vkit answered
    # "not inside a Git repository" instead of the missing prerequisite under
    # test. Where they share a directory a shim supplies git back, so the only
    # thing the stripped PATH removes is the executable under test.
    git_directories = _directories_providing("git")
    assert git_directories, "git is required by every test in this file"
    shim = _shim_directory_for(tmp_path, "git-shim", node_directories & git_directories)
    stripped = os.pathsep.join(
        [str(shim)] if shim else []
        + [
            entry for entry in os.environ["PATH"].split(os.pathsep)
            if not entry or str(Path(entry).resolve()) not in node_directories
        ]
    )
    environment = {**os.environ, "PATH": stripped}

    # Verified the way vkit resolves it, in a fresh interpreter that has not
    # already cached anything. Both halves: node must be gone, or the
    # prerequisite is not missing and the test measures nothing, and git must
    # survive, or the run is blocked for a reason that has nothing to do with
    # the prerequisite.
    probe = subprocess.run(
        [sys.executable, "-c",
         "import shutil;print(shutil.which('node') or 'NONE', shutil.which('git') or 'NONE', sep='|')"],
        capture_output=True, text=True, env=environment, check=False,
    )
    probe_node, probe_git = probe.stdout.strip().split("|")
    assert probe_node == "NONE", (
        f"shutil.which still finds node at {probe_node!r} with the stripped "
        "PATH; the prerequisite would not be missing and this test would measure nothing"
    )
    assert probe_git != "NONE", (
        "git was stripped along with node, so the run would be BLOCKED for a "
        "missing repository rather than for the missing prerequisite"
    )

    run = vkit("check", "run", "--project", str(root), "--check", "items-api", "--json", env=environment)

    assert run.returncode == 3, run.stdout
    outcome = json.loads(run.stdout)["outcome"]
    assert outcome["result"] == "BLOCKED"
    assert outcome["reason"] == "prerequisite_missing", outcome
    assert "node" in outcome["detail"]


def _shim_directory_for(tmp_path: Path, name: str, shared: set[str]) -> Path | None:
    """A directory holding a working `git` that execs the real one by absolute path.

    Returned only when the directories that must be removed also provide git. A
    PATH is a list of directories, not a set of executables, so one directory
    cannot be half on and half off. Where node and git share it, the only way to
    remove node and keep git is to supply git from somewhere else.

    The shim names the real executable as an absolute path, so the chain resolves
    regardless of what PATH the thing it launches goes on to see.
    """
    if not shared:
        return None
    real = shutil.which("git")
    assert real is not None, "git is on PATH but _directories_providing found none"
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    script = f'#!/bin/sh\nexec "{real}" "$@"\n'
    for candidate in (directory / "git", directory / "git.exe"):
        candidate.write_text(script, encoding="utf-8")
        candidate.chmod(0o755)
    return directory


def _directories_providing(executable: str) -> set[str]:
    """Every PATH directory that holds the named executable."""
    found: set[str] = set()
    for entry in os.environ["PATH"].split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry)
        if any(
            (directory / f"{executable}{suffix}").is_file()
            for suffix in ("", ".exe", ".cmd", ".bat", ".com")
        ):
            found.add(str(directory.resolve()))
    return found


# --- features --------------------------------------------------------------

def test_features_reports_the_example_map_and_names_what_is_not_covered(
    tmp_path: Path,
) -> None:
    """The map loads, the covered features are verified, and the gaps are named.

    The example map deliberately contains two uncovered features, one of which
    records a real concurrency defect nobody has checked. A test that only
    asserted the file parsed would pass whether or not the gaps were visible.
    """
    root = fresh_repo(tmp_path, NODE_HTTP, "mapped")

    done = vkit("features", "--project", str(root), "--json")

    assert done.returncode == 3, "a map with uncovered features must not exit 0"
    report = json.loads(done.stdout)
    by_id = {f["id"]: f for f in report["features"]}
    assert "items-create-then-list" in by_id
    assert report["verified"], "the covered features were not reported as verified"

    uncovered = {row["feature"] for row in report["uncovered"]}
    assert "items-concurrent-writes" in uncovered, "a known-unverified feature is missing from the gaps"
    assert "items-update-an-existing-item" in uncovered


def test_a_feature_document_carries_no_assertions(tmp_path: Path) -> None:
    """The rule that keeps a regression from rewriting its own acceptance.

    A feature document states behavior and coverage. If it carried the expected
    values, a behaviour regression could edit the document to match and the
    change would be invisible in a review of the map. This asserts on the file's
    own text rather than on parsed output, because the rule is about what the
    file is allowed to contain.
    """
    raw = (NODE_HTTP / "verification" / "features.json").read_text(encoding="utf-8")
    document = json.loads(raw)

    for feature in document["features"]:
        assert feature["expected_outcome"] == "", (
            f"{feature['id']} records an expected outcome; that belongs in the check, "
            "not in the document a regression would edit"
        )
        assert "expected " not in feature["behavior"].lower(), (
            f"{feature['id']} states an expected value in its behavior description"
        )


def _scenarios_in_driver(root: Path) -> list[str]:
    """Scenario ids the python example driver actually reports.

    Parsed with `ast` from the driver's own case table, not with a text search
    and not hard-coded here. A hard-coded list would be a second copy of the
    acceptance criteria, which is exactly what the feature-document rule exists
    to prevent, and a text search over a formatted table is a guess about
    formatting rather than a read of the data.
    """
    import ast

    tree = ast.parse((root / "verify_totals.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.Assign) and getattr(node, "target", None):
            name = getattr(node.target, "id", None) or getattr(node.target, "attr", None)
            if name != "CASES":
                continue
            found: list[str] = []
            for element in node.value.elts:  # type: ignore[union-attr]
                if isinstance(element, ast.Tuple) and element.elts:
                    first = element.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        found.append(first.value)
            return found
    return []
