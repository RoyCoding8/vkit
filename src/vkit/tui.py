"""Rich launcher for selecting a repository and opening its web console."""
from __future__ import annotations

import threading
import webbrowser
from contextlib import ExitStack
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .console import bind
from .paths import Project, ProjectError, open_project


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


class Launcher:
    def __init__(self, project: Project, *, port: int = 0, console: Console | None = None) -> None:
        self.project = project
        self.port = port
        self.console = console or Console()
        self.message = "Ready. Enter a command below."
        self.server = None
        self._server_cleanup = ExitStack()

    @property
    def url(self) -> str | None:
        return f"http://127.0.0.1:{self.server.server_address[1]}/" if self.server else None

    def _stop_server(self) -> None:
        self._server_cleanup.close()
        self.server = None

    def _render(self) -> None:
        commands = Table.grid(padding=(0, 2))
        commands.add_column(style="bold cyan", no_wrap=True)
        commands.add_column()
        for command, label in (("open", "Start server and open web console"),
                               ("start / stop", "Start or stop the local server"),
                               ("project", "Type a repository path"),
                               ("browse", "Select a repository with a folder dialog"),
                               ("q", "Stop the server and quit")):
            commands.add_row(command, label)
        body = Group(Text(str(self.project.root), style="bold"), Text(""),
                     Text(f"Web console: {self.url or 'stopped'}"),
                     Text(f"Port: {self.port or 'automatic'}"), Text(""), commands, Text(""),
                     Text("Manage checks, settings, and agent connections in the web console.", style="dim"))
        self.console.clear()
        self.console.print(Panel(body, title="vkit / Home", border_style="cyan"))
        self.console.print(Text(self.message, style="yellow"))

    def _handle(self, line: str) -> bool:
        name, _, value = line.strip().partition(" ")
        name, value = name.lower(), value.strip()
        if name in {"q", "quit"}:
            return False
        if not name:
            return True
        if name in {"project", "browse"}:
            if name == "browse":
                path = _pick_directory(self.project.root)
                if path is None:
                    self.message = "Selection cancelled. Repository unchanged."
                    return True
            else:
                path = (value or self.console.input("Repository path: ")).strip()
                if len(path) >= 2 and path[0] == path[-1] and path[0] in {"'", '"'}:
                    path = path[1:-1]
            selected = open_project(path)
            self._stop_server()
            self.project = selected
            self.message = "Repository changed. The previous web server was stopped."
        elif name == "stop":
            self._stop_server()
            self.message = "Web console stopped."
        elif name in {"open", "start"}:
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
        else:
            raise ValueError("Unknown command. Choose a command shown above; enter open for the web console.")
        return True

    def run(self) -> int:
        try:
            with self.console.screen(hide_cursor=False):
                while True:
                    self._render()
                    try:
                        if not self._handle(self.console.input("[bold cyan]home > [/bold cyan]")):
                            break
                    except (OSError, ProjectError, ValueError) as exc:
                        self.message = str(exc)
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self._server_cleanup.close()
        return 0


def launch(project: Project, *, port: int = 0) -> int:
    return Launcher(project, port=port).run()
