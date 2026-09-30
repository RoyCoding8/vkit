"""The plugin must be installable from the wheel, not only from a checkout.

The defect this guards was measured, not assumed. The audit built the wheel,
extracted it outside this repository and got, from the extracted tree:

    Refused: the vkit plugin package is not present; looked in
    C:\\Users\\roysh\\AppData\\Local\\Temp\\plugin,
    .../vkit/console/_plugin. Install operates on a packaged plugin, and
    this build has none to install.

Two causes, and this file fails if either returns. The wheel force-included the
schemas but not the plugin, so there was nothing to find; and the lookup probed
two paths that exist in neither install shape -- a checkout puts the plugin
beside `src/`, and a wheel puts it inside the installed package, not one level
above it.

The tests install the wheel into a virtualenv under their own temporary
directory and ask the installed package where its plugin is. That temporary
directory is outside the repository, so a resolver that answered from `PATH` or
from a source-tree guess could not pass by accident. Every plugin resource is
asserted by name, because shipping the manifest while dropping the hooks is the
same defect in smaller clothes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from vkit import pluginres

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every file the host reads when it loads the plugin. Asserted individually
# because a count would pass on a partial package, and a package shipping the
# manifest without the hooks is the same defect in smaller clothes.
EXPECTED_PLUGIN_FILES = (
    "plugin/.claude-plugin/plugin.json",
    "plugin/.mcp.json",
    "plugin/hooks/hooks.json",
    "plugin/scripts/vkit_hook.py",
    "plugin/skills/vkit-onboard/SKILL.md",
    "plugin/skills/vkit-status/SKILL.md",
    "plugin/skills/vkit-verify/SKILL.md",
    "plugin/skills/vkit-work/SKILL.md",
)

MARKETPLACE = pluginres.MARKETPLACE_MANIFEST.as_posix()

# Runs in a fresh interpreter and reports where the installed package believes
# its resources are. `import vkit` here can only find the installed
# distribution, because the subprocess inherits no path back to this checkout.
PROBE = (
    "import json\n"
    "from pathlib import Path\n"
    "from vkit import pluginres\n"
    "print(json.dumps({\n"
    "    'root': str(pluginres.resolve_plugin_root()),\n"
    "    'plugin': str(pluginres.plugin_dir()),\n"
    "    'package': str(Path(pluginres.__file__).resolve().parent),\n"
    "}))\n"
)


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, encoding="utf-8",
        errors="replace", timeout=600, check=False,
    )


def _make_venv(venv: Path) -> Path:
    """A virtualenv under the test's own temporary directory.

    Deliberately not the shared project virtualenv. Installing into it would
    change what every other worker's `import vkit` resolves to, and the point of
    these tests is that the answer comes from an installed distribution rather
    than from whatever happens to be on this machine.
    """
    done = _run([sys.executable, "-m", "venv", str(venv)], cwd=venv.parent)
    assert done.returncode == 0, f"venv creation failed:\n{done.stdout}\n{done.stderr}"
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One wheel for the module, built through pip's own build isolation.

    `python -m build` would need a build dependency installed into the ambient
    interpreter, which for a worktree worker is a shared virtualenv. Pip's
    isolation installs hatchling from the declared backend, so this is the same
    code path a real `pip install .` takes. Building it once per module rather
    than once per test keeps the gate to a few builds instead of four.
    """
    out_dir = tmp_path_factory.mktemp("dist")
    done = _run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                 "--wheel-dir", str(out_dir), str(REPO_ROOT)], cwd=out_dir)
    assert done.returncode == 0, f"pip wheel failed:\n{done.stdout}\n{done.stderr}"
    wheels = list(out_dir.glob("vkit-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found {[w.name for w in wheels]}"
    return wheels[0]


def _install(wheel_path: Path, python: Path, outside: Path) -> None:
    """Install the wheel with its dependencies left out.

    The probe imports only `vkit.pluginres`, which imports only the standard
    library, so jsonschema and pywin32 are not needed to answer the question and
    pulling them would make this gate depend on a package index. Dependencies are
    exercised where they matter, by the suite that runs against a full install.
    """
    outside.mkdir(parents=True, exist_ok=True)
    done = _run([str(python), "-m", "pip", "install", "--no-deps", "--no-cache-dir",
                 "--disable-pip-version-check", str(wheel_path)], cwd=outside)
    assert done.returncode == 0, f"pip install failed:\n{done.stdout}\n{done.stderr}"


def test_the_built_wheel_carries_every_plugin_resource(wheel: Path) -> None:
    """Packaging, asserted on the archive before anything is installed.

    The cheapest point to fail, and the one that distinguishes a wrong resolver
    from a wheel that is missing the bytes. Reading the archive also catches a
    resource that is force-included to the wrong place, which an install-then-
    resolve test would report as the same failure with less to go on.
    """
    names = set(zipfile.ZipFile(wheel).namelist())

    missing = [name for name in EXPECTED_PLUGIN_FILES
               if f"vkit/_plugin_root/{name}" not in names]
    assert not missing, f"the wheel ships none of {missing} under vkit/_plugin_root"

    assert f"vkit/_plugin_root/{MARKETPLACE}" in names, (
        "marketplace.json is what `claude plugin marketplace add` reads; without "
        "it the host has nothing to register and install is unreachable"
    )
    assert "vkit/pluginres.py" in names, "the resolver must ship with the package"


def test_the_installed_package_resolves_the_plugin_outside_the_checkout(
    wheel: Path, tmp_path: Path,
) -> None:
    """The full gate: build, install into a fresh environment, resolve there.

    Driven through a subprocess whose working directory is a temporary directory
    outside the repository. That cwd matters twice: it denies the resolver any
    source-tree guess, and it denies `import vkit` a path back to this checkout
    through the ambient `sys.path`, so the answer necessarily comes from the
    installed distribution's own resources.
    """
    python = _make_venv(tmp_path / "venv")
    outside = tmp_path / "outside"
    _install(wheel, python, outside)

    found = _run([str(python), "-c", PROBE], cwd=outside)
    assert found.returncode == 0, f"resolver failed outside the checkout:\n{found.stderr}"
    answer = json.loads(found.stdout)

    # The plugin has to live inside the installed package. This one assertion
    # rules out a checkout answer and a PATH guess together: the checkout's
    # package directory is src/vkit, which force-includes nothing, so a resolver
    # that fell back to it could not satisfy this.
    package = Path(answer["package"])
    root = Path(answer["root"])
    assert root.is_relative_to(package), (
        f"plugin resolved to {root}, which is outside the installed package {package}"
    )
    assert Path(answer["plugin"]) == root / "plugin", (
        f"plugin_dir() {answer['plugin']} is not the plugin/ beside the marketplace"
    )

    missing = [name for name in EXPECTED_PLUGIN_FILES if not (root / name).is_file()]
    assert not missing, f"the installed package cannot find {missing}"
    assert (root / MARKETPLACE).is_file()

    # The marketplace points at `./plugin`, so the sibling relationship the host
    # depends on has to survive packaging.
    document = json.loads((root / MARKETPLACE).read_text(encoding="utf-8"))
    source = Path(document["plugins"][0]["source"])
    assert not source.is_absolute() and (root / source).is_dir(), (
        f"marketplace source {source} does not resolve inside the package root"
    )


def test_uninstalling_removes_the_plugin_resources(wheel: Path, tmp_path: Path) -> None:
    """Removal has to be a round trip, or the install leaves a stale package.

    A wheel that ships the plugin can leave a host configured against files that
    are gone. Asserting the resolved directory no longer exists after uninstall
    is the difference between "installed" and "installed once". Its own virtualenv
    because it uninstalls, which would break the test above sharing one.
    """
    python = _make_venv(tmp_path / "venv")
    outside = tmp_path / "outside"
    _install(wheel, python, outside)

    found = _run([str(python), "-c", PROBE], cwd=outside)
    assert found.returncode == 0, found.stderr
    resolved = Path(json.loads(found.stdout)["root"])
    assert resolved.exists()

    done = _run([str(python), "-m", "pip", "uninstall", "-y", "vkit"], cwd=outside)
    assert done.returncode == 0, f"uninstall failed:\n{done.stdout}\n{done.stderr}"
    assert not resolved.exists(), f"uninstall left the plugin package at {resolved}"


def test_a_checkout_with_no_packaged_plugin_still_resolves() -> None:
    """The development checkout keeps working.

    `pip install -e` is how a contributor runs this, and it force-includes nothing
    into `vkit/_plugin_root`, so only the checkout arm of the resolver can serve
    it. Removing that arm would break the test suite and every developer's
    machine, which is the usual way a packaging fix lands and breaks the project.
    """
    pluginres.resolve_plugin_root.cache_clear()
    root = pluginres.resolve_plugin_root()
    assert (root / pluginres.PLUGIN_MANIFEST).is_file()
    assert root == REPO_ROOT, (
        f"a source checkout must resolve to the repository root, got {root}"
    )
