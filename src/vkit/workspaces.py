"""User-local index of repositories and worktrees opened with VKit."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import Project, ProjectError, open_project
from .settings import launcher_settings_path
from .store import STATE_DIR, now, read_json


class WorkspaceError(ValueError):
    """A remembered workspace is missing, changed, or unknown."""


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    path: str
    repository_id: str
    first_seen: str
    last_seen: str
    source: str

    def as_json(self) -> dict[str, str]:
        return asdict(self)


def registry_path() -> Path:
    return launcher_settings_path().parent / "workspaces.sqlite3"


def _key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _workspace_id(root_key: str, common_key: str, repository_token: str) -> str:
    return _digest(f"{root_key}\0{common_key}\0{repository_token}")


def _repository_id(common_key: str, repository_token: str) -> str:
    return _digest(f"{common_key}\0{repository_token}")


def _repository_token(common_path: Path, *, create: bool) -> str:
    identity_path = common_path / STATE_DIR / "workspace-id.json"
    if create and not identity_path.exists():
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = identity_path.with_name(f".workspace-id-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump({"id": str(uuid.uuid4())}, handle, separators=(",", ":"))
                handle.flush()
            try:
                os.link(temporary, identity_path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

    try:
        record = read_json(identity_path)
    except (OSError, ValueError) as exc:
        raise WorkspaceError(f"invalid repository identity at {identity_path}: {exc}") from exc
    if record is None:
        raise WorkspaceError(f"repository identity is unavailable at {identity_path}")
    if not isinstance(record, dict) or set(record) != {"id"} or not isinstance(record["id"], str):
        raise WorkspaceError(f"invalid repository identity at {identity_path}")
    try:
        canonical = str(uuid.UUID(record["id"]))
    except (ValueError, AttributeError) as exc:
        raise WorkspaceError(f"invalid repository identity at {identity_path}") from exc
    if canonical != record["id"]:
        raise WorkspaceError(f"invalid repository identity at {identity_path}")
    return canonical


def _connect() -> sqlite3.Connection:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            path_key TEXT NOT NULL,
            common_path TEXT NOT NULL,
            common_key TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            source TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS workspaces_recent ON workspaces(last_seen DESC)")


def _from_row(row: sqlite3.Row) -> Workspace:
    return Workspace(
        id=row["id"], name=row["name"], path=row["path"],
        repository_id=row["repository_id"], first_seen=row["first_seen"],
        last_seen=row["last_seen"], source=row["source"],
    )


def remember(project: Project, source: str) -> Workspace:
    """Remember one verified worktree; repeated calls update its last-use metadata."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("workspace source must be a non-empty string")
    try:
        checked = open_project(project.root)
    except ProjectError as exc:
        raise WorkspaceError(f"cannot remember workspace {project.root}: {exc}") from exc

    root_key = _key(project.root)
    common_key = _key(project.git_common_dir)
    if _key(checked.root) != root_key or _key(checked.git_common_dir) != common_key:
        raise WorkspaceError(f"workspace Git identity changed: {project.root}")

    root = checked.root.resolve()
    common = checked.git_common_dir.resolve()
    repository_token = _repository_token(common, create=True)
    workspace_id = _workspace_id(root_key, common_key, repository_token)
    repository_id = _repository_id(common_key, repository_token)
    timestamp = now()
    values = (workspace_id, root.name or str(root), str(root), root_key, str(common), common_key,
              repository_id, timestamp, timestamp, source.strip())

    with closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        connection.execute(
            """INSERT INTO workspaces
               (id, name, path, path_key, common_path, common_key, repository_id,
                first_seen, last_seen, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, path=excluded.path, path_key=excluded.path_key,
                 common_path=excluded.common_path, common_key=excluded.common_key,
                 repository_id=excluded.repository_id, last_seen=excluded.last_seen,
                 source=excluded.source""",
            values,
        )
        row = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return _from_row(row)


def known_workspaces() -> list[Workspace]:
    """Return remembered paths in most-recently-used order without scanning disk."""
    path = registry_path()
    if not path.is_file():
        return []
    with closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        rows = connection.execute(
            "SELECT * FROM workspaces ORDER BY last_seen DESC, first_seen, id"
        ).fetchall()
    return [_from_row(row) for row in rows]


def resolve_workspace(workspace_id: str) -> Project:
    """Resolve a remembered path only while it still names the same Git worktree."""
    if not isinstance(workspace_id, str) or not workspace_id:
        raise WorkspaceError("workspace id must be a non-empty string")
    path = registry_path()
    if not path.is_file():
        raise WorkspaceError(f"unknown workspace {workspace_id!r}")
    with closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        row = connection.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        raise WorkspaceError(f"unknown workspace {workspace_id!r}")

    stored_path = Path(row["path"])
    try:
        project = open_project(stored_path)
    except ProjectError as exc:
        raise WorkspaceError(f"workspace is unavailable at {stored_path}: {exc}") from exc
    common_key = _key(project.git_common_dir)
    if _key(project.root) != row["path_key"] or common_key != row["common_key"]:
        raise WorkspaceError(f"workspace Git identity changed at {stored_path}; remember it again")
    repository_token = _repository_token(project.git_common_dir, create=False)
    if (_repository_id(common_key, repository_token) != row["repository_id"]
            or _workspace_id(_key(project.root), common_key, repository_token) != workspace_id):
        raise WorkspaceError(f"workspace repository identity changed at {stored_path}; remember it again")
    return project
