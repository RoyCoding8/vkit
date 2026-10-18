"""Saved operator settings shared by the console and agent interfaces."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeVar

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
        "disabled_tools": {"type": "array", "uniqueItems": True,
                           "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$", "not": {"const": "help"}}},
    },
    "required": ["version", "theme", "sidebar_collapsed", "refresh_seconds", "history_limit"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Settings:
    version: int = 1
    theme: str = "system"
    sidebar_collapsed: bool = False
    refresh_seconds: int = 2
    history_limit: int = 50
    disabled_tools: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "theme": self.theme,
            "sidebar_collapsed": self.sidebar_collapsed,
            "refresh_seconds": self.refresh_seconds,
            "history_limit": self.history_limit,
            "disabled_tools": list(self.disabled_tools),
        }


class SettingsFileError(ValueError):
    """The saved operator settings cannot be read or validated."""


DEFAULT_SETTINGS = Settings()


LAUNCHER_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "integer", "const": 1},
        "project": {"type": ["string", "null"], "minLength": 1},
        "port": {"type": "integer", "minimum": 0, "maximum": 65535},
        "open_browser": {"type": "boolean"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class LauncherSettings:
    version: int = 1
    project: str | None = None
    port: int = 0
    open_browser: bool = True

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def launcher_settings_path() -> Path:
    override = os.environ.get("VKIT_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "vkit" / "config.json"


def parse_launcher_settings(value: Any) -> LauncherSettings:
    try:
        validate(value, LAUNCHER_SCHEMA)
    except ValidationError as exc:
        raise ValueError(f"invalid launch settings: {exc.message}") from exc
    settings = LauncherSettings(**value)
    if settings.project is not None:
        project = Path(settings.project).expanduser()
        if not project.is_absolute():
            raise ValueError("default repository must be an absolute path")
        settings = LauncherSettings(**{**value, "project": str(project)})
    return settings


def load_launcher_settings() -> LauncherSettings:
    return _load(launcher_settings_path(), parse_launcher_settings, LauncherSettings())


def save_launcher_settings(settings: LauncherSettings) -> None:
    write_json(launcher_settings_path(), settings.as_json())


def settings_path(project: Project) -> Path:
    return Store.of(project).root / "config.json"


def parse_settings(value: Any) -> Settings:
    try:
        validate(value, SETTINGS_SCHEMA)
    except ValidationError as exc:
        raise ValueError(f"invalid settings: {exc.message}") from exc
    disabled = dict.fromkeys("compare_contracts" if name == "check_proto_compatibility" else name
                             for name in value.get("disabled_tools", ()))
    return Settings(**{**value, "disabled_tools": tuple(disabled)})


def load_settings(project: Project) -> Settings:
    return _load(settings_path(project), parse_settings, DEFAULT_SETTINGS)


_T = TypeVar("_T")


def _load(path: Path, parse: Callable[[Any], _T], default: _T) -> _T:
    try:
        value = read_json(path)
    except json.JSONDecodeError as exc:
        raise SettingsFileError(f"invalid settings at {path}: invalid JSON: {exc}") from exc
    except (OSError, UnicodeError) as exc:
        raise SettingsFileError(f"cannot read settings at {path}: {exc}") from exc
    if value is None:
        if path.exists():
            raise SettingsFileError(f"invalid settings at {path}: expected a JSON object")
        return default
    try:
        return parse(value)
    except ValueError as exc:
        raise SettingsFileError(f"invalid settings at {path}: {exc}") from exc


def save_settings(project: Project, settings: Settings) -> None:
    write_json(settings_path(project), settings.as_json())
