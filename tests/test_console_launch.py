"""Behavior of `vkit console`, driven as an operator drives it.

Every test here runs the real command through `cli.main` and asserts a literal
an operator could read off the terminal: the URL printed, the exit code, the
words in a refusal. Nothing compares the output to what the code computes, so a
launcher that returned undefined would fail rather than pass.

Two properties are why this file exists.

**The URL is the one that really serves.** A port asked for as 0 belongs to the
operating system, so the address a console answers on cannot be known from the
arguments. Each run asks the printed URL for the page and keeps the answer, so
"it printed the port it bound" is an observation rather than a restatement.

**The token never reaches the command's output.** The token is read out of the
page that console actually served, so this compares the value the server minted
against the output the command printed.

Output is captured with `capfd` rather than by replacing `sys.stdout`. pytest
resumes its own capturing at the start of every call phase, so a `monkeypatch`
of `sys.stdout` made in a setup fixture is undone before the test body runs. A
file-descriptor capture is not undone by that, which is why this file uses it.
"""
from __future__ import annotations

import json
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vkit import cli
from vkit.console import launcher

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

URL_PATTERN = re.compile(r"http://127\.0\.0\.1:(\d+)/")
TOKEN_PATTERN = re.compile(r'name="vkit-token" content="([^"]+)"')
TOKEN_PLACEHOLDER = "__VKIT_TOKEN__"

#: A run that never starts serving fails here rather than hanging the suite.
STARTUP_TIMEOUT_SECONDS = 30.0


@pytest.fixture()
def example_repo(tmp_path: Path) -> Path:
    """A throwaway Git repository holding a real copy of the example."""
    target = tmp_path / "console repo"
    shutil.copytree(EXAMPLE, target)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=target, check=True,
    )
    return target


@pytest.fixture()
def held_port() -> int:
    """A port this process holds open for as long as the fixture is alive.

    Holding it, rather than asking the operating system for a free number and
    releasing it, is what makes the collision deterministic. A released port can
    be handed to an outgoing connection before the console reaches it, and then
    the bind succeeds and the test asserts nothing about a refusal that never
    happened.
    """
    occupier = socket.socket()
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    try:
        yield occupier.getsockname()[1]
    finally:
        occupier.close()


class Output:
    """Everything a run wrote, drained from `capfd` as it arrives.

    A thread and the test body cannot both hold one `capfd` handle, because each
    `readouterr` drains what came since the last one. So the reading thread keeps
    what it has seen and the body is given that, plus whatever arrived after the
    run ended.
    """

    def __init__(self) -> None:
        self.out = ""
        self.err = ""
        self.page = ""

    def drain(self, capture: pytest.CaptureFixture[str]) -> None:
        read = capture.readouterr()
        self.out += read.out
        self.err += read.err

    @property
    def stderr_lines(self) -> list[str]:
        return [line for line in self.err.splitlines() if line.strip()]


def _serve_once_then_interrupt(capture: pytest.CaptureFixture[str], seen: Output,
                              stop: threading.Event) -> None:
    """Read the page from the printed URL, then raise a real SIGINT at the command.

    Nothing about the interrupt is faked. This is the signal a terminal sends on
    Ctrl-C, raised from another thread because the command owns this one. Waiting
    for the page first is what keeps the signal from landing before the serving
    loop has begun.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not stop.is_set() and time.monotonic() < deadline:
        seen.drain(capture)
        found = URL_PATTERN.search(seen.out)
        if found is not None:
            try:
                seen.page = urllib.request.urlopen(found.group(0), timeout=5).read().decode("utf-8")
            except urllib.error.HTTPError:
                # A refusal is still an answer from a listening socket.
                seen.page = ""
            except OSError:
                stop.wait(0.05)
                continue
            signal.raise_signal(signal.SIGINT)
            return
        stop.wait(0.05)


def run_console(
    repo: Path, argv: list[str], capture: pytest.CaptureFixture[str],
) -> tuple[int, Output]:
    """Run `vkit console` on this thread and interrupt it once it has served."""
    seen = Output()
    stop = threading.Event()
    thread = threading.Thread(
        target=_serve_once_then_interrupt, args=(capture, seen, stop), name="test-interrupt",
    )
    thread.start()
    try:
        code = cli.main(["console", "--project", str(repo), "--no-browser", *argv])
    finally:
        stop.set()
        thread.join(20)
        seen.drain(capture)
    assert seen.page, "the printed URL never answered, so nothing was proved about it"
    return code, seen


def printed_url(seen: Output) -> str:
    found = URL_PATTERN.search(seen.out)
    assert found is not None, f"no console URL in {seen.out!r}"
    return found.group(0)


def test_prints_the_url_of_the_port_it_actually_bound(
    example_repo: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    """`--port 0` asks the OS for a port, and the printed URL names that one.

    The proof is the request that came back from that address, so the printed
    port cannot be the requested 0.
    """
    _, seen = run_console(example_repo, ["--port", "0"], capfd)

    url = printed_url(seen)
    assert not url.endswith(":0/")
    assert seen.out.strip() == url
    assert TOKEN_PLACEHOLDER not in seen.out


def test_ctrl_c_stops_the_console_and_exits_zero_without_a_traceback(
    example_repo: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    """An interrupt is the ordinary way to stop the console, and it is not an error."""
    code, seen = run_console(example_repo, ["--port", "0"], capfd)

    assert code == 0
    assert "Traceback" not in seen.err
    assert seen.stderr_lines[-1] == "console stopped"


def test_the_session_token_reaches_the_page_and_never_the_output(
    example_repo: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    """The minted token is in the served page and in neither stdout nor stderr.

    All of it comes from one run, so this compares the value the server minted
    against the output the command printed.
    """
    _, seen = run_console(example_repo, ["--port", "0"], capfd)

    minted = TOKEN_PATTERN.search(seen.page)
    assert minted is not None, "the served page carried no session token"
    token = minted.group(1)
    assert TOKEN_PLACEHOLDER not in seen.page

    assert token not in seen.out
    assert token not in seen.err
    assert token not in printed_url(seen)


def test_no_browser_never_asks_to_open_one(
    example_repo: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--no-browser` is what suppresses the open, not the absence of a call.

    The spy records a call rather than absorbing it, so this fails if the open
    happens.
    """
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(launcher, "serve_until_stopped", lambda server, stop: False)

    code = cli.main(["console", "--project", str(example_repo), "--no-browser", "--port", "0"])

    assert code == 0
    assert opened == []


def test_a_successful_bind_opens_the_url_it_printed(
    example_repo: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the flag the printed URL is handed to the browser, once bound."""
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(launcher, "serve_until_stopped", lambda server, stop: False)

    cli.main(["console", "--project", str(example_repo), "--port", "0"])
    seen = Output()
    seen.drain(capfd)

    assert opened == [printed_url(seen)]


def test_a_browser_that_cannot_open_leaves_the_console_serving(
    example_repo: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken browser costs the operator one convenience, not the console."""
    def refuse(url: str) -> bool:
        raise RuntimeError("no browser is configured")

    monkeypatch.setattr(launcher.webbrowser, "open", refuse)
    monkeypatch.setattr(launcher, "serve_until_stopped", lambda server, stop: False)

    code = cli.main(["console", "--project", str(example_repo), "--port", "0"])
    seen = Output()
    seen.drain(capfd)

    url = printed_url(seen)
    assert code == 0
    assert url in seen.err, "the operator was not told where the console is"


def test_an_occupied_port_is_refused_and_nothing_is_served(
    example_repo: Path, capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, held_port: int,
) -> None:
    """A busy port is a refusal naming the port, and no console starts behind it."""
    started: list[object] = []
    monkeypatch.setattr(
        launcher, "serve_until_stopped",
        lambda server, stop: started.append(server) or False,
    )
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)

    code = cli.main(["console", "--project", str(example_repo), "--port", str(held_port)])
    seen = Output()
    seen.drain(capfd)

    assert code == 2
    assert str(held_port) in seen.err, "the refusal did not name the port"
    assert "--port" in seen.err, "the refusal did not say how to proceed"
    assert seen.out.strip() == ""
    assert started == [], "a console started on a port that was already taken"
    assert opened == [], "a browser was asked for a console that never bound"


@pytest.mark.parametrize("as_json", [False, True])
def test_a_busy_port_refusal_is_readable_in_both_modes(
    example_repo: Path, capfd: pytest.CaptureFixture[str], held_port: int, as_json: bool,
) -> None:
    """A refusal is readable either way, which is what `_fail` is for."""
    argv = ["console", "--project", str(example_repo), "--port", str(held_port)]
    if as_json:
        argv.append("--json")

    code = cli.main(argv)
    seen = Output()
    seen.drain(capfd)

    assert code == 2
    text = seen.out if as_json else seen.err
    assert str(held_port) in text
    assert "--port" in text


def test_json_output_is_one_object_naming_the_bound_url(
    example_repo: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--json` says the same thing the plain line says, as parseable JSON."""
    monkeypatch.setattr(launcher, "serve_until_stopped", lambda server, stop: False)

    code = cli.main([
        "console", "--project", str(example_repo), "--no-browser", "--port", "0", "--json",
    ])
    seen = Output()
    seen.drain(capfd)

    document = json.loads(seen.out)
    assert code == 0
    assert document["command"] == "console"
    assert URL_PATTERN.fullmatch(document["url"])
    assert seen.out.strip() == json.dumps(document, indent=2)


def test_the_default_port_is_8765() -> None:
    """The documented default, read off the parser an operator's arguments reach."""
    args = cli.build_parser().parse_args(["console", "--project", "here"])

    assert args.port == 8765
    assert args.no_browser is False


def test_a_root_that_is_not_a_project_is_refused(
    tmp_path: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    """A path with no repository is refused before anything binds."""
    code = cli.main(["console", "--project", str(tmp_path), "--port", "0"])
    seen = Output()
    seen.drain(capfd)

    assert code == 2
    assert seen.out.strip() == ""
    assert seen.err.strip() != ""