"""A terminal workspace for the CLI, web console, and user settings."""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
    TextArea,
)
from textual.worker import get_current_worker

from . import proc
from .console import bind
from .paths import Project, ProjectError, open_project
from .settings import (
    launcher_settings_path,
    load_launcher_settings,
    parse_launcher_settings,
    save_launcher_settings,
)

_COMMANDS = {
    "status": ("Project status", ("status",)),
    "doctor": ("Runtime availability", ("doctor",)),
    "inspect": ("Build and test commands", ("project", "inspect")),
    "run-checks": ("Run needed checks", ("check", "run", "--needed")),
    "accept": ("Review check definitions", ("accept",)),
    "find-tools": ("Find enabled tools", ("tools",)),
}


class LauncherApp(App[None]):
    TITLE = "vkit"
    SUB_TITLE = "Repository verification"
    CSS_PATH = "tui.tcss"
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "compact"), (72, "wide")]
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("f1", "tab('home')", "Home"),
        ("f2", "tab('checks')", "Checks"),
        ("f3", "tab('settings')", "Settings"),
        ("f4", "tab('agent')", "Agent"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, project: Project, *, port: int = 0) -> None:
        super().__init__()
        self.project = project
        self.port = port
        self.server = None
        self._server_cleanup = ExitStack()

    @property
    def url(self) -> str | None:
        return f"http://127.0.0.1:{self.server.server_address[1]}/" if self.server else None

    def compose(self) -> ComposeResult:
        defaults = load_launcher_settings()
        yield Header()
        with TabbedContent(initial="home", id="workspace"):
            with TabPane("Home", id="home"), VerticalScroll():
                with Horizontal(id="home-cards"):
                    with Vertical(classes="card", id="repository-card"):
                        yield Label("Current repository", classes="heading")
                        yield Static(Text(str(self.project.root)), id="current-project")
                        yield Input(str(self.project.root), id="project-path", placeholder="Repository path")
                        yield Button("Switch repository", id="switch-project")
                        yield Static("This changes the current session. Save a default in Settings.", classes="hint")
                    with Vertical(classes="card", id="server-card"):
                        yield Label("Web console", classes="heading")
                        yield Static("Stopped", id="server-status")
                        yield Static(f"Port: {self.port or 'automatic'}", classes="hint")
                        yield Button("Open web console", id="open-web", variant="primary")
                        yield Button("Start server", id="start-web")
                        yield Button("Stop server", id="stop-web", disabled=True)
                yield Static("Ready. Choose a tab or open the web console.", id="home-feedback", classes="feedback")
            with TabPane("Checks", id="checks"), Horizontal(id="checks-body"):
                with VerticalScroll(id="check-actions", classes="card"):
                    yield Label("Repository commands", classes="heading")
                    for name, (label, _) in _COMMANDS.items():
                        if name == "find-tools":
                            yield Input(placeholder="Search tools", id="tool-query")
                        yield Button(label, id=name, classes="command")
                with Vertical(id="result-card", classes="card"):
                    yield Static("Select a command to inspect this repository.", id="command-status", classes="hint")
                    yield RichLog(id="command-output", wrap=True, highlight=False)
            with TabPane("Settings", id="settings"), VerticalScroll(classes="card"):
                yield Label("Launch defaults", classes="heading")
                yield Static(Text(str(launcher_settings_path())), classes="hint", id="config-path")
                yield Label("Default repository")
                yield Input(defaults.project or "", id="default-project", placeholder="Blank uses the current directory")
                yield Label("Web console port")
                yield Input(str(defaults.port), id="default-port", type="integer")
                yield Checkbox("Open browser with vkit console", value=defaults.open_browser, id="default-browser")
                yield Button("Save defaults", id="save-defaults", variant="primary")
                yield Static("Saved values apply to future launches. CLI options override them.", classes="hint")
                yield Static("", id="settings-feedback", classes="feedback")
            with TabPane("Agent", id="agent"), Vertical(classes="card"):
                yield Label("Connect your agent", classes="heading")
                yield Static("Add this MCP configuration to your harness. It starts the stdio server. "
                             "Ask the agent to call vkit's help tool to discover enabled tools.", classes="hint")
                yield TextArea(read_only=True, id="agent-config")
                yield Button("Copy configuration", id="copy-agent")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_agent()
        self.query_one("#open-web", Button).focus()

    def on_unmount(self) -> None:
        self._server_cleanup.close()

    def action_tab(self, pane: str) -> None:
        self.query_one(Tabs).focus()
        self.query_one(TabbedContent).active = pane

    def _agent_config(self) -> dict:
        return {"mcpServers": {"vkit": {
            "command": sys.executable,
            "args": ["-m", "vkit.cli", "mcp", "serve", "--project", str(self.project.root)],
        }}}

    def _refresh_agent(self) -> None:
        self.query_one("#agent-config", TextArea).text = json.dumps(self._agent_config(), indent=2)

    def _stop_server(self) -> None:
        self._server_cleanup.close()
        self.server = None
        self.query_one("#server-status", Static).update("Stopped")
        self.query_one("#stop-web", Button).disabled = True
        self.query_one("#start-web", Button).disabled = False

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id
        try:
            if name in {"open-web", "start-web"}:
                if self.server is None:
                    self.server = bind(self.project, self.port)
                    thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                    thread.start()
                    self._server_cleanup.callback(thread.join)
                    self._server_cleanup.callback(self.server.server_close)
                    self._server_cleanup.callback(self.server.shutdown)
                self.query_one("#server-status", Static).update(Text(f"Running\n{self.url}", style="green"))
                self.query_one("#stop-web", Button).disabled = False
                self.query_one("#start-web", Button).disabled = True
                if name == "open-web" and not webbrowser.open(self.url):
                    self.query_one("#home-feedback", Static).update(f"Open {self.url} in your browser.")
            elif name == "stop-web":
                self._stop_server()
            elif name == "switch-project":
                selected = open_project(self.query_one("#project-path", Input).value)
                self._stop_server()
                self.project = selected
                self.query_one("#current-project", Static).update(Text(str(selected.root)))
                self.query_one("#home-feedback", Static).update("Repository changed. The previous web server was stopped.")
                self.query_one("#command-output", RichLog).clear()
                self.query_one("#command-status", Static).update("Select a command to inspect this repository.")
                self._refresh_agent()
            elif name == "save-defaults":
                updated = parse_launcher_settings({
                    "version": 1,
                    "project": self.query_one("#default-project", Input).value.strip() or None,
                    "port": int(self.query_one("#default-port", Input).value),
                    "open_browser": self.query_one("#default-browser", Checkbox).value,
                })
                save_launcher_settings(updated)
                self.query_one("#settings-feedback", Static).update("Defaults saved. They apply to the next launch.")
            elif name == "copy-agent":
                self.copy_to_clipboard(json.dumps(self._agent_config(), indent=2))
                self.notify("MCP configuration copied")
            elif name in _COMMANDS:
                label, command = _COMMANDS[name]
                if name == "find-tools":
                    command += ("--query", self.query_one("#tool-query", Input).value)
                self._set_busy(True)
                self.query_one("#command-output", RichLog).clear()
                self.query_one("#command-status", Static).update(f"Running {label.lower()}...")
                self.run_command(label, command, self.project)
        except (OSError, ProjectError, ValueError) as exc:
            target = "#settings-feedback" if name == "save-defaults" else "#home-feedback"
            self.query_one(target, Static).update(Text(str(exc), style="red"))

    def _set_busy(self, busy: bool) -> None:
        for button in self.query(".command, #switch-project"):
            button.disabled = busy

    @work(thread=True, exclusive=True, group="command")
    def run_command(self, label: str, command: tuple[str, ...], project: Project) -> None:
        worker = get_current_worker()
        with TemporaryDirectory(prefix="vkit-tui-") as directory:
            output, errors = Path(directory) / "stdout", Path(directory) / "stderr"
            result = proc.run(
                [sys.executable, "-m", "vkit.cli", *command, "--project", str(project.root)],
                cwd=project.root, stdout_path=output, stderr_path=errors,
                timeout_seconds=float("inf"), should_cancel=lambda: worker.is_cancelled,
                graceful_cancel=True,
                max_output_bytes=1_048_576,
            )
            text = output.read_text(encoding="utf-8", errors="replace") + errors.read_text(encoding="utf-8", errors="replace")
        if not worker.is_cancelled:
            status = result.launch_error or f"{label} finished with exit code {result.code}."
            if result.output_limited:
                status += " Output stopped at 1 MiB."
            self.call_from_thread(self._show_result, status, text)

    def _show_result(self, status: str, output: str) -> None:
        self.query_one("#command-status", Static).update(Text(status))
        self.query_one("#command-output", RichLog).write(Text(output or "No output."))
        self._set_busy(False)


def launch(project: Project, *, port: int = 0) -> int:
    app = LauncherApp(project, port=port)
    app.run()
    return app.return_code or 0
