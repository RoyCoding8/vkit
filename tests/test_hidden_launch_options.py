import subprocess
import sys

import pytest

import subproc


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console behavior")
def test_wrapper_preserves_launch_options_and_hides_the_console(monkeypatch):
    captured = {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESTDHANDLES
    startup.wShowWindow = 5

    def capture(args, **kwargs):
        captured.update(kwargs)
        return "done"

    monkeypatch.setattr(subprocess, "run", capture)
    assert subproc.run(["unused"], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                       startupinfo=startup, timeout=3) == "done"
    assert captured["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    assert captured["startupinfo"].dwFlags & subprocess.STARTF_USESTDHANDLES
    assert captured["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert captured["startupinfo"].wShowWindow == subprocess.SW_HIDE
    assert captured["timeout"] == 3


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console behavior")
def test_hidden_child_has_no_console_handle():
    done = subproc.run([sys.executable, "-c",
                       "import ctypes; print(bool(ctypes.windll.kernel32.GetConsoleWindow()))"],
                      capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False"
