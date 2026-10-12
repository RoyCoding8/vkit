"""Terminal launcher for the existing CLI and web console."""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from contextlib import ExitStack

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .console import bind
from .paths import Project, ProjectError, open_project


_COMMANDS = {
    "4": ("Project status and gate", ("status",)),
    "5": ("Check runtime availability", ("doctor",)),
    "6": ("Inspect build and test commands", ("project", "inspect")),
    "7": ("Run stale or missing checks", ("check", "run", "--needed")),
    "8": ("Review and accept check definitions", ("accept",)),
    "9": ("Find enabled tools", ("tools",)),
}


def launch(project: Project, *, port: int = 0) -> int:
    from .cli import main

    console = Console(soft_wrap=True)
    server = None
    with ExitStack() as cleanup:
        while True:
            url = f"http://127.0.0.1:{server.server_address[1]}/" if server else None
            console.print("\nvkit", style="bold cyan")
            console.print(str(project.root), markup=False)
            console.print(f"Web console: {url or 'stopped'}", markup=False)
            menu = Table(box=None, show_header=False, padding=(0, 2))
            for key, label in (
                ("1", "Open web console / settings"),
                ("2", "Start web server"),
                ("3", "Stop web server"),
                *((key, item[0]) for key, item in _COMMANDS.items()),
                ("m", "Show agent MCP configuration"),
                ("p", "Change project"),
                ("q", "Quit and stop web server"),
            ):
                menu.add_row(key, label)
            console.print(menu)
            try:
                choice = Prompt.ask("Choose", console=console, default="q").strip().lower()
                if choice == "q":
                    return 0
                if choice in ("1", "2"):
                    if server is None:
                        server = bind(project, port)
                        thread = threading.Thread(target=server.serve_forever, daemon=True)
                        thread.start()
                        cleanup.callback(thread.join)
                        cleanup.callback(server.server_close)
                        cleanup.callback(server.shutdown)
                    url = f"http://127.0.0.1:{server.server_address[1]}/"
                    console.print(url, markup=False)
                    if choice == "1" and not webbrowser.open(url):
                        console.print("Open the URL above in your browser.")
                elif choice == "3":
                    cleanup.close()
                    server = None
                elif choice == "p":
                    selected = open_project(Prompt.ask("Repository path", console=console))
                    cleanup.close()
                    server = None
                    project = selected
                elif choice == "m":
                    configuration = {"mcpServers": {"vkit": {
                        "command": sys.executable,
                        "args": ["-m", "vkit.cli", "mcp", "serve", "--project", str(project.root)],
                    }}}
                    console.print(json.dumps(configuration, indent=2), markup=False, highlight=False)
                    console.print("Add this to your harness's MCP settings. It launches the stdio server.")
                    console.print("Ask the agent to call vkit's help tool, optionally with a query.")
                elif choice in _COMMANDS:
                    command = list(_COMMANDS[choice][1])
                    if choice == "9":
                        command += ["--query", Prompt.ask("Search tools", console=console, default="")]
                    code = main([*command, "--project", str(project.root)])
                    console.print(f"Command exit code: {code}", style="dim")
                else:
                    console.print("Choose a number, m, p, or q.", style="yellow")
            except (EOFError, KeyboardInterrupt):
                console.print("\nLauncher closed.")
                return 0
            except (OSError, ProjectError) as exc:
                console.print(str(exc), style="red", markup=False)
