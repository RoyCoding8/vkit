"""The console must work from the installed wheel, away from this checkout.

Plan 12 asks for two things at once: ship the console's static assets in the
wheel, and exercise the command outside the source checkout. Only the second
was unproven, and it is the half that fails in the field. A wheel built by
hatchling from `src/vkit` sweeps in `console/static/*` as a side effect of
`packages = ["src/vkit"]`, so a reader has no reason to believe that on any
later build. The assertions below read the built archive rather than trusting
the build configuration, because the configuration is not the artifact.

**The isolation is the test.** `tests/conftest.py` puts this checkout's `src`
on `sys.path`, and the editable install in the shared `.venv` puts the *main*
checkout's `src` there too. A test that imports `vkit` in the pytest process and
asserts the assets are reachable proves only that the source tree has them,
which was never in doubt. So nothing in this file imports `vkit` in the pytest
process at all. A subprocess is started, that interpreter's `sys.path` is
stripped of every entry that could hand out a real `vkit` package, the wheel's
install directory goes in at index 0, and the console is driven from there. The
subprocess reports the path it actually resolved and the assertions check that
it is inside the install and outside this repository's `src`.

`--target` rather than a fresh virtualenv, and the reason is a dependency the
console genuinely needs. `vkit.console.operations` imports the manifest parser,
which imports `jsonschema` at module scope. A `--no-deps` virtualenv has no
`jsonschema`, so the console cannot start there and this gate would have to be
rewritten to stop testing it. Installing into a target directory and driving it
with the interpreter running this suite keeps every declared dependency
available while `vkit` itself comes only from the install.

Every request here is a GET. The server drains an unread body before closing a
refused connection, and on Windows closing a socket with inbound data still
queued discards the response that was already written. A test that depended on a
refused POST would hit that race about one request in three and would look flaky
for a reason that has nothing to do with packaging.

A wheel build takes about two seconds and this module runs in well under a
minute, so nothing is marked slow and nothing is skipped for time. `hatchling`
must be importable from the environment running the suite, for the reason
`pyproject.toml` records: the build runs without isolation so this gate needs no
package index.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import subproc

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

#: The three files `server.py` serves. Named individually because a count would
#: pass on a wheel shipping only some of them, and a console missing its
#: stylesheet is a different defect from a console missing its page.
STATIC_ASSETS = ("app.js", "index.html", "style.css")

#: Where they have to sit in the archive. A literal the packaging configuration
#: has to keep producing, written out rather than derived from the tree, so a
#: change of layout fails here instead of quietly agreeing with itself.
STATIC_IN_WHEEL = "vkit/console/static/"

#: `server.py` substitutes this at serve time. A build that inlined a real token
#: instead would publish one credential to everyone who installs the package, and
#: the page would still render while every mutation from it was refused.
TOKEN_PLACEHOLDER = b"__VKIT_TOKEN__"

#: The id the example manifest registers, and the scenarios it requires. Both
#: written out here rather than read back from the manifest, because an assertion
#: whose expected value comes from the code under test cannot fail for a defect.
CHECK_ID = "totals-behavior"
REQUIRED_SCENARIOS = [
    "empty-cart", "single-positive", "several-positives",
    "mixed-sign", "negatives-only", "cancels-to-zero",
]

#: Runs in a fresh interpreter that has imported nothing from this checkout, and
#: reports what it resolved and what the console served. Every assertion in this
#: file reads a different fact out of the report, so a defect still names itself.
DRIVER = '''
import base64, hashlib, json, shutil, subprocess, sys, urllib.error, urllib.request
from pathlib import Path


def hidden_window():
    """`subprocess` keywords that keep a launched child off the operator's screen.

    Written out rather than imported, because this driver runs with the install
    on `sys.path` and no `tests/` directory, and the whole point of the gate is
    that it runs where nothing from this checkout is importable. Same shape as
    `tests/subproc.py`.
    """
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


target, seed, scratch = sys.argv[1], sys.argv[2], sys.argv[3]

# Drop every sys.path entry that could hand out a real `vkit` package, then put
# the install at index 0. A regular package has an `__init__.py`; the partial
# `vkit/` directory an editable install leaves behind in site-packages has none,
# so this rule spares that entry rather than discarding one for no reason. Once
# the target's `vkit` is imported, `__path__` is fixed to that one directory and
# the other entries are never consulted for it again.
sys.path[:] = [
    entry for entry in sys.path
    if not (Path(entry or ".").resolve() / "vkit" / "__init__.py").is_file()
]
sys.path.insert(0, target)

import vkit
from vkit.console import operations, server

# The same arithmetic `server.py` does to find its own assets, recomputed from
# the module this install actually loaded rather than trusted from a constant.
static = Path(server.__file__).resolve().parent / "static"

repo = Path(scratch) / "space repo"
shutil.copytree(seed, repo)
subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **hidden_window())
subprocess.run(["git", "add", "-A"], cwd=repo, check=True, **hidden_window())
subprocess.run(
    ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "fixture"],
    cwd=repo, check=True, **hidden_window(),
)

def get(port, path):
    # An HTTP error is reported as a fact rather than raised. A console whose
    # static directory is missing answers 404, and that is exactly the defect
    # this gate exists to catch; letting urlopen raise would bury it under a
    # traceback from a line that is only the messenger.
    try:
        answer = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=60)
    except urllib.error.HTTPError as refused:
        return {"status": refused.code, "body": refused.read(), "type": None}
    with answer:
        return {"status": answer.status, "body": answer.read(),
                "type": answer.headers.get("Content-Type")}

# Two consoles on one project. Comparing what they serve is what turns "the page
# carries a fresh token" into a measurement: the same packaged bytes, two
# different substitutions.
first, _ = server.start_in_thread(operations.open_context(repo), port=0)
second, _ = server.start_in_thread(operations.open_context(repo), port=0)
try:
    page = get(first.server_address[1], "/")
    other = get(second.server_address[1], "/")
    checks = get(first.server_address[1], "/api/checks")
    project = get(first.server_address[1], "/api/project")
finally:
    first.shutdown(); first.server_close()
    second.shutdown(); second.server_close()

print(json.dumps({
    "vkit_file": str(Path(vkit.__file__).resolve()),
    "vkit_paths": [str(Path(entry).resolve()) for entry in vkit.__path__],
    "static_dir": str(static.resolve()),
    "static_present": static.is_dir(),
    "static_sha": {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(static.iterdir()) if path.is_file()
    } if static.is_dir() else {},
    "page_status": page["status"],
    "page_type": page["type"],
    "page_base64": base64.b64encode(page["body"]).decode("ascii"),
    "served_token": first.session.token,
    "other_token": second.session.token,
    "other_page_sha": hashlib.sha256(other["body"]).hexdigest(),
    "checks": json.loads(checks["body"].decode("utf-8")),
    "project": json.loads(project["body"].decode("utf-8")),
}))
'''


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subproc.run(
        args, cwd=cwd, capture_output=True, encoding="utf-8",
        errors="replace", timeout=600, check=False,
    )


@pytest.fixture(scope="module")
def install_dir(wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The wheel unpacked into a directory outside the checkout, dependencies aside.

    A `--target` install rather than a virtualenv because the console imports the
    manifest parser, which imports `jsonschema` at module scope. Installing with
    dependencies would make this gate need an index, and installing into a
    virtualenv without them would leave the console unable to start, which is the
    thing under test.
    """
    target = tmp_path_factory.mktemp("console-install")
    done = _run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-cache-dir",
         "--disable-pip-version-check", "--target", str(target), str(wheel)],
        cwd=target,
    )
    assert done.returncode == 0, f"pip install failed:\n{done.stdout}\n{done.stderr}"
    return target.resolve()


@pytest.fixture(scope="module")
def console(install_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Drive a console built from the installed package, and report what it did.

    One run per module keeps the installed HTTP integration shared across
    its assertions without repeating setup.

    The working directory is a temporary directory outside the repository. That
    denies `import vkit` a path back through the ambient `sys.path` and denies
    the plugin resolver a source-tree guess, so the `installed.source` asserted
    below can only be the packaged copy.

    A console that answers a 404 is reported, not raised. Whether a 404 on `GET /`
    is a packaging defect or a server defect is a question the assertions below
    answer with the status in hand, and this fixture's job is to get them there.
    """
    scratch = tmp_path_factory.mktemp("console-scratch")
    done = _run([sys.executable, "-c", DRIVER, str(install_dir), str(EXAMPLE), str(scratch)],
                cwd=scratch)
    assert done.returncode == 0, f"the installed console did not run:\n{done.stdout}\n{done.stderr}"
    return json.loads(done.stdout)




def test_the_wheel_carries_every_console_static_asset(wheel: Path) -> None:
    """Packaging, asserted on the archive before anything is installed.

    The cheapest point to fail, and the one that tells a wheel missing the bytes
    apart from a console that cannot serve them. Reading the archive also catches
    an asset force-included to the wrong place, which an install-then-serve test
    would report as the same failure with nothing to go on.
    """
    names = set(zipfile.ZipFile(wheel).namelist())

    missing = [name for name in STATIC_ASSETS if f"{STATIC_IN_WHEEL}{name}" not in names]
    assert not missing, (
        f"the wheel ships none of {missing} under {STATIC_IN_WHEEL}. A console "
        f"installed from this wheel answers the browser a 404 for its own page. "
        f"The wheel target in pyproject.toml has to name the static directory, "
        f"the way it already names `schemas`."
    )
    assert "vkit/console/server.py" in names, "the server that reads them must ship too"


def test_the_packaged_page_carries_the_placeholder_and_no_token(wheel: Path) -> None:
    """The page in the archive holds a placeholder, never a token.

    `server.py` fills `__VKIT_TOKEN__` at serve time, once per console. A build
    that substituted a value at build time would publish one token to every
    operator who installs the package, and the page would render correctly while
    every mutation it offers was refused with "needs this console page's session
    token". The failure is invisible from the browser and total for the feature,
    so the archive is read directly rather than through the server.

    A page that is not in the wheel at all is the neighbouring assertion's defect
    rather than this one's, and it is named as such instead of surfacing as a
    KeyError from inside `zipfile`, so a reader who trips both can tell which one
    they are looking at.
    """
    archive = zipfile.ZipFile(wheel)
    entry = f"{STATIC_IN_WHEEL}index.html"
    assert entry in archive.namelist(), (
        f"{entry} is not in the wheel, so there is no packaged page to inspect; "
        f"that is the defect the wheel-contents assertion above reports"
    )
    page = archive.read(entry)

    assert TOKEN_PLACEHOLDER in page, (
        "index.html in the wheel contains no __VKIT_TOKEN__ placeholder, so the "
        "page either ships a session token or has lost the substitution. Either "
        "way the console cannot mint a token per instance, and anything written "
        "in its place is published to everyone who installs this package."
    )




def test_the_installed_console_resolved_itself_outside_the_checkout(
    console: dict, install_dir: Path,
) -> None:
    """`import vkit` in the driver resolved to the install, not to `src`.

    The assertion every other test here leans on. The driver reports the resolved
    path and the failure message quotes it, so a reader who suspects this gate is
    running against the source tree can see which directory it actually loaded
    from without re-running anything.
    """
    resolved = Path(console["vkit_file"])
    assert resolved.is_relative_to(install_dir), (
        f"import vkit resolved to {resolved}, which is not inside the install "
        f"{install_dir}. This gate proves nothing about the wheel while it can "
        f"reach a checkout."
    )
    assert not resolved.is_relative_to(REPO_ROOT / "src"), (
        f"import vkit resolved to {resolved}, which is this checkout's source "
        f"tree. The installed package was not the one under test."
    )

    paths = [Path(entry) for entry in console["vkit_paths"]]
    assert paths == [install_dir / "vkit"], (
        f"vkit.__path__ is {paths}, so `vkit` is a namespace package spanning more "
        f"than one directory. The installed copy and some other tree are sharing "
        f"the name, and which one answers is left to sys.path order."
    )


def test_the_install_directory_is_outside_this_repository(install_dir: Path) -> None:
    """The console under test was unpacked outside the repository.

    Stated on its own because it is the precondition the rest of the module rests
    on. A `--target` install landing inside the checkout would let every assertion
    above pass while reading source files, and the path assertion meant to catch
    it would then be comparing against the wrong directory.
    """
    assert not install_dir.is_relative_to(REPO_ROOT), (
        f"the wheel was installed to {install_dir}, which is inside this "
        f"repository at {REPO_ROOT}, so a reader could not tell an installed "
        f"console from the checkout"
    )


def test_every_static_asset_is_readable_at_the_installed_path(
    console: dict, install_dir: Path, wheel: Path,
) -> None:
    """The bytes the installed server reads are the bytes in the wheel.

    Hashing both ends closes the loop the archive test opens: the wheel carries
    the assets, and the console running from the install reads those same files.
    `server.py` finds them with `Path(__file__).resolve().parent / "static"`, so a
    build that placed them anywhere else leaves this directory empty and the
    console answering 404 to the browser.
    """
    static = Path(console["static_dir"])
    assert static == install_dir / "vkit" / "console" / "static", (
        f"the installed server resolves its assets to {static}, which is not "
        f"under the install {install_dir}"
    )
    assert console["static_present"], (
        f"the installed server resolved its assets to {static}, and there is no "
        f"such directory, so every request for the page, the script and the "
        f"stylesheet answers 404"
    )

    archive = zipfile.ZipFile(wheel)
    served = console["static_sha"]
    for name in STATIC_ASSETS:
        assert (static / name).is_file(), f"{static / name} is not readable at the installed path"
        expected = hashlib.sha256(archive.read(f"{STATIC_IN_WHEEL}{name}")).hexdigest()
        assert served.get(name) == expected, (
            f"{name} at {static} does not match the copy in the wheel; the "
            f"installed tree and the archive disagree about this file"
        )




def test_the_installed_console_serves_the_packaged_page_with_its_own_token(
    console: dict, wheel: Path,
) -> None:
    """GET / returns the packaged HTML with this console's own token filled in.

    Three properties have to hold at once and each is separately load-bearing.
    The page is the packaged file rather than something the install grew at
    runtime. The placeholder is gone, because it is substituted before the bytes
    go out. What replaced it is this console's token and not a constant, which
    two consoles on one project settle between them: the same packaged bytes, two
    different pages.
    """
    assert console["page_status"] == 200, (
        f"GET / answered {console['page_status']}, not 200. An installed console "
        f"whose static directory is missing serves the browser its own error "
        f"page rather than its own console."
    )

    served = base64.b64decode(console["page_base64"])
    assert TOKEN_PLACEHOLDER not in served, (
        "the served page still contains __VKIT_TOKEN__; the browser received the "
        "unsubstituted file, so nothing on the page could carry a session token"
    )
    assert console["page_type"] == "text/html; charset=utf-8", (
        f"GET / answered {console['page_type']}, so a browser would not render it as a page"
    )

    archive = zipfile.ZipFile(wheel)
    entry = f"{STATIC_IN_WHEEL}index.html"
    assert entry in archive.namelist(), (
        f"{entry} is not in the wheel, so the console answered GET / with something "
        f"the package never shipped; the wheel-contents assertion reports that"
    )
    packaged = archive.read(entry)
    token = console["served_token"].encode("ascii")

    assert served.startswith(b"<!DOCTYPE html>"), (
        f"GET / answered with bytes that are not an HTML document: {served[:64]!r}"
    )
    assert token in served, (
        "the served page does not contain this console's token, so the page and "
        "the session that served it disagree and every mutation is refused"
    )
    assert console["served_token"] not in packaged.decode("utf-8"), (
        "the served token is already present in the packaged index.html, which "
        "means it was baked in at build time and is not this console's own"
    )
    assert served == packaged.replace(TOKEN_PLACEHOLDER, token), (
        "the served page is not the packaged index.html with the placeholder "
        "substituted, so something other than the installed static directory is "
        "answering GET /"
    )

    assert console["other_token"] != console["served_token"], (
        "two consoles on one project minted the same token"
    )
    assert console["other_page_sha"] != hashlib.sha256(served).hexdigest(), (
        "two consoles served byte-identical pages, so the substitution is not per "
        "console and the token is either fixed or never substituted"
    )


def test_the_installed_console_answers_a_read_with_the_real_project(
    console: dict, install_dir: Path,
) -> None:
    """A real API read returns this project's own data, from the install.

    This catches the console that starts, serves a page, and answers every API
    call with an error or an empty body: a browser that looks alive and shows
    nothing. The ids and scenarios below are written out rather than read back
    from the manifest. The plugin source is asserted inside the install because a
    resolver that fell back to a checkout would report the source tree's `plugin/`
    while the console ran from somewhere else entirely.
    """
    checks = console["checks"]
    assert [check["id"] for check in checks["checks"]] == [CHECK_ID], (
        f"the installed console reported checks {[c['id'] for c in checks['checks']]} "
        f"for a project whose manifest registers {CHECK_ID!r}"
    )
    assert checks["checks"][0]["required_scenarios"] == REQUIRED_SCENARIOS, (
        "the scenarios this console would require for a check are not the ones the "
        "project's manifest declares"
    )
    assert checks["checks"][0]["command"][-1].endswith("result.json")

    source = checks["installed"]["source"]
    assert source is not None, (
        "the installed console found no plugin package. A wheel that cannot find "
        "its own resources cannot install them, and the check page would offer an "
        "install that is guaranteed to be refused."
    )
    assert Path(source).is_relative_to(install_dir), (
        f"the plugin resolved to {source}, which is outside the install "
        f"{install_dir}; the console fell back to a checkout"
    )

    project = console["project"]
    assert project["manifest_error"] is None, (
        "the installed console could not parse the project's manifest: "
        f"{project['manifest_error']}"
    )
    assert Path(project["manifest_path"]).is_file(), (
        f"the console reported a manifest at {project['manifest_path']} that is not a file"
    )
    assert project["source"] is not None, (
        "the installed console could not compute a source identity: "
        f"{project.get('source_error')}"
    )