"""Validated preferences for the local console."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from .paths import Project
from .store import Store, read_json, write_json

SETTINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "integer", "const": 1},
        "theme": {"type": "string", "enum": ["system", "light", "dark"]},
        "sidebar_collapsed": {"type": "boolean"},
        "refresh_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
        "history_limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "required": ["version", "theme", "sidebar_collapsed", "refresh_seconds", "history_limit"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ConsoleSettings:
    version: int = 1
    theme: str = "system"
    sidebar_collapsed: bool = False
    refresh_seconds: int = 2
    history_limit: int = 50

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "theme": self.theme,
            "sidebar_collapsed": self.sidebar_collapsed,
            "refresh_seconds": self.refresh_seconds,
            "history_limit": self.history_limit,
        }


class ConsoleSettingsFileError(ValueError):
    """The saved console preferences cannot be read or validated."""


DEFAULT_SETTINGS = ConsoleSettings()


def settings_path(project: Project) -> Path:
    return Store.of(project).root / "config.json"


def parse_settings(value: Any) -> ConsoleSettings:
    try:
        validate(value, SETTINGS_SCHEMA)
    except ValidationError as exc:
        raise ValueError(f"invalid console settings: {exc.message}") from exc
    return ConsoleSettings(**value)


def load_settings(project: Project) -> ConsoleSettings:
    path = settings_path(project)
    try:
        value = read_json(path)
    except json.JSONDecodeError as exc:
        raise ConsoleSettingsFileError(f"invalid console settings at {path}: invalid JSON: {exc}") from exc
    except (OSError, UnicodeError) as exc:
        raise ConsoleSettingsFileError(f"cannot read console settings at {path}: {exc}") from exc
    if value is None:
        if path.exists():
            raise ConsoleSettingsFileError(f"invalid console settings at {path}: expected a JSON object")
        return DEFAULT_SETTINGS
    try:
        return parse_settings(value)
    except ValueError as exc:
        raise ConsoleSettingsFileError(f"invalid console settings at {path}: {exc}") from exc


def save_settings(project: Project, settings: ConsoleSettings) -> None:
    write_json(settings_path(project), settings.as_json())
