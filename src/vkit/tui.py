"""Rich panels and line commands for the local launcher."""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import proc
from .console import bind
from .paths import Project, ProjectError, open_project
from .settings import (
    launcher_settings_path,
    load_launcher_settings,
    parse_launcher_settings,
    save_launcher_settings,
)

_TABS = ("home", "checks", "settings", "agent")
_COMMANDS = {
    "status": ("Project status", ("status",)),
    "doctor": ("Runtime availability", ("doctor",)),
    "inspect": ("Build and test commands", ("project", "inspect")),
    "run": ("Run needed checks", ("check", "run", "--needed")),
    "review": ("Review check definitions", ("accept",)),
    "find": ("Find enabled tools", ("tools",)),
}


def _pick_directory(initial: Path) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise ValueError("Folder picker unavailable. Use project to type a path instead.") from exc
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        selected = filedialog.askdirectory(
            parent=root, title="Choose a Git repository", initialdir=str(initial), mustexist=True,
        )
    except tk.TclError as exc:
        raise ValueError("Folder picker unavailable. Use project to type a path instead.") from exc
    finally:
        if root is not None:
            root.destroy()
    return Path(selected) if selected else None


def _run_command(project: Project, command: tuple[str, ...], cancel: threading.Event) -> tuple[str, str]:
    with TemporaryDirectory(prefix="vkit-tui-") as directory:
        output, errors = Path(directory) / "stdout", Path(directory) / "stderr"
        result = proc.run(
            [sys.executable, "-m", "vkit.cli", *command, "--project", str(project.root)],
            cwd=project.root, stdout_path=output, stderr_path=errors,
            timeout_seconds=float("inf"), should_cancel=cancel.is_set,
            graceful_cancel=True, max_output_bytes=1_048_576,
        )
        text = output.read_text(encoding="utf-8", errors="replace") + errors.read_text(encoding="utf-8", errors="replace")
    status = result.launch_error or ("Cancelled." if result.cancelled else f"Exit code {result.code}.")
    if result.output_limited:
        status += " Output stopped at 1 MiB."
    return status, text


class Launcher:
    def __init__(self, project: Project, *, port: int = 0, console: Console | None = None) -> None:
        self.project = project
        self.port = port
        self.console = console or Console()
        self.tab = "home"
        self.message = "Ready. Enter a tab number or a command below."
        self.output = ""
        self.server = None
        self._server_cleanup = ExitStack()
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._job: Future[tuple[str, str]] | None = None
        self._cancel = threading.Event()

    @property
    def url(self) -> str | None:
        return f"http://127.0.0.1:{self.server.server_address[1]}/" if self.server else None

    def _stop_server(self) -> None:
        self._server_cleanup.close()
        self.server = None

    def _collect_result(self) -> None:
        if self._job is not None and self._job.done():
            self.message, self.output = self._job.result()
            self._job = None

    def _agent_config(self) -> str:
        return json.dumps({"mcpServers": {"vkit": {
            "command": sys.executable,
            "args": ["-m", "vkit.cli", "mcp", "serve", "--project", str(self.project.root)],
        }}}, indent=2)

    def _body(self) -> Group:
        commands = Table.grid(padding=(0, 2))
        commands.add_column(style="bold cyan", no_wrap=True)
        commands.add_column()
        if self.tab == "home":
            for command, label in (("open", "Start server and open web console"),
                                   ("start / stop", "Start or stop the local server"),
                                   ("project", "Type a repository path for this session"),
                                   ("browse", "Select a repository with a folder dialog")):
                commands.add_row(command, label)
            return Group(Text(str(self.project.root), style="bold"), Text(""),
                         Text(f"Web console: {self.url or 'stopped'}"),
                         Text(f"Port: {self.port or 'automatic'}"), Text(""), commands)
        if self.tab == "checks":
            for command, (label, _) in _COMMANDS.items():
                commands.add_row("find QUERY" if command == "find" else command, label)
            commands.add_row("cancel", "Cancel the active command and its child processes")
            commands.add_row("output", "Read the full last result in a pager")
            lines = self.output.splitlines()
            limit = max(3, self.console.height - 23)
            preview = "\n".join(lines[:limit]) or "No result yet."
            if len(lines) > limit:
                preview += f"\n… {len(lines) - limit} more lines; enter output to read them."
            return Group(commands, Text(""), Panel(Text(preview), title="Result", border_style="dim"))
        if self.tab == "settings":
            defaults = load_launcher_settings()
            commands.add_row("project", Text(defaults.project or "Current directory"))
            commands.add_row("browse", "Choose the default repository with a folder dialog")
            commands.add_row("port", str(defaults.port) + (" (automatic)" if defaults.port == 0 else ""))
            commands.add_row("browser", "on" if defaults.open_browser else "off")
            return Group(Text(str(launcher_settings_path())), Text(""), commands, Text(""),
                         Text("Enter a setting name to edit it. Each valid change saves to JSON.\n"
                              "Defaults apply to future launches; CLI options override them.", style="dim"))
        return Group(Text("Add this MCP configuration to your agent harness.\n"
                          "Ask the agent to call help to discover enabled tools.\n"
                          "Enter config for unboxed JSON in a pager.\n"),
                     Syntax(self._agent_config(), "json", word_wrap=True, background_color="default"))

    def _render(self) -> None:
        self._collect_result()
        tabs = Text()
        for index, tab in enumerate(_TABS, 1):
            tabs.append(f" {index} {tab.title()} ", style="bold black on cyan" if tab == self.tab else "dim")
        self.console.clear()
        self.console.print(Panel(tabs, title="vkit / Repository verification", border_style="cyan"))
        self.console.print(Panel(self._body(), title=self.tab.title(), border_style="cyan"))
        self.console.print(Text(self.message, style="yellow"))
        if self._job is not None:
            self.console.print("Command running · Enter refreshes · cancel stops it", style="dim")
        self.console.print("1–4 switch tabs · q quits · Enter refreshes", style="dim")

    def _handle(self, line: str) -> bool:
        self._collect_result()
        name, _, value = line.strip().partition(" ")
        name, value = name.lower(), value.strip()
        if name in {"q", "quit"}:
            return False
        if not name:
            return True
        if name in _TABS or name in {"1", "2", "3", "4"}:
            self.tab = name if name in _TABS else _TABS[int(name) - 1]
            return True
        if name == "cancel":
            self._cancel.set()
            self.message = "Cancelling…" if self._job else "No command is running."
            return True
        if self.tab == "home" and name in {"open", "start", "stop", "project", "browse"}:
            if name in {"project", "browse"}:
                if self._job is not None:
                    raise ValueError("Cancel the active command before switching repositories.")
                if name == "browse":
                    path = _pick_directory(self.project.root)
                    if path is None:
                        self.message = "Selection cancelled. Repository unchanged."
                        return True
                else:
                    path = value or self.console.input("Repository path: ")
                selected = open_project(path)
                self._stop_server()
                self.project = selected
                self.output = ""
                self.message = "Repository changed. The previous web server was stopped."
            elif name == "stop":
                self._stop_server()
                self.message = "Web console stopped."
            else:
                if self.server is None:
                    self.server = bind(self.project, self.port)
                    thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                    thread.start()
                    self._server_cleanup.callback(thread.join)
                    self._server_cleanup.callback(self.server.server_close)
                    self._server_cleanup.callback(self.server.shutdown)
                self.message = f"Web console: {self.url}"
                if name == "open":
                    webbrowser.open(self.url)
        elif self.tab == "checks" and name == "output":
            with self.console.pager(styles=True):
                self.console.print(Text(self.output or "No result yet."))
        elif self.tab == "agent" and name == "config":
            with self.console.pager():
                self.console.print(Text(self._agent_config()), soft_wrap=True)
        elif self.tab == "checks" and name in _COMMANDS:
            if self._job is not None:
                raise ValueError("A command is already running. Enter cancel to stop it.")
            label, command = _COMMANDS[name]
            if name == "find":
                command += ("--query", value)
            self._cancel.clear()
            self.output = ""
            self.message = f"Running {label.lower()}…"
            self._job = self._executor.submit(_run_command, self.project, command, self._cancel)
        elif self.tab == "settings" and name in {"project", "browse", "port", "browser"}:
            updated = load_launcher_settings().as_json()
            if name in {"project", "browse"}:
                if name == "browse":
                    path = _pick_directory(Path(updated["project"]) if updated["project"] else self.project.root)
                    if path is None:
                        self.message = "Selection cancelled. Defaults unchanged."
                        return True
                else:
                    path = value or self.console.input("Default repository (blank uses current directory): ").strip() or None
                updated["project"] = str(open_project(path).root) if path else None
            elif name == "port":
                updated[name] = int(value or self.console.input("Port (0 chooses a free port): "))
            else:
                choice = value or self.console.input("Open browser with vkit console (on/off): ").strip().lower()
                if choice not in {"on", "off"}:
                    raise ValueError("Browser must be on or off.")
                updated["open_browser"] = choice == "on"
            save_launcher_settings(parse_launcher_settings(updated))
            self.message = "Defaults saved. They apply to the next launch."
        else:
            raise ValueError("Unknown command for this tab. Choose a command shown above.")
        return True

    def run(self) -> int:
        try:
            with self.console.screen():
                while True:
                    self._render()
                    try:
                        if not self._handle(self.console.input(f"[bold cyan]{self.tab} > [/bold cyan]")):
                            break
                    except (OSError, ProjectError, ValueError) as exc:
                        self.message = str(exc)
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self._cancel.set()
            self._executor.shutdown(wait=True)
            self._server_cleanup.close()
        return 0


def launch(project: Project, *, port: int = 0) -> int:
    return Launcher(project, port=port).run()
