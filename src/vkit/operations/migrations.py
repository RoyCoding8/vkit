"""Compare a bounded SQLite migration replay with a fresh-install database."""
from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_BUNDLE_BYTES = 256_000
MAX_WORKER_INPUT_BYTES = 1_600_000
MAX_SQL_BYTES = 200_000
MAX_MIGRATIONS = 100
MAX_OBSERVATIONS = 100
MAX_TIMEOUT_SECONDS = 60
MAX_STATEMENTS = 1_000
MAX_SCHEMA_OBJECTS = 2_000
MAX_OBSERVATION_ROWS = 10_000
MAX_OUTPUT_BYTES = 400_000
MODEL_SCOPE = (
    "Compares exact SQLite-stored catalog SQL and metadata from this runtime, "
    "excluding physical rootpage numbers, plus exact ordered rows returned by "
    "supplied observations, with foreign-key enforcement enabled in both databases. "
    "Use ORDER BY for stable row assertions. This does not "
    "prove preservation of production data, application behavior, or compatibility "
    "with another SQL engine."
)

_FUNCTIONS = frozenset({
    "abs", "avg", "coalesce", "count", "ifnull", "length", "like", "lower", "max",
    "min", "nullif", "printf", "quote", "round", "substr", "sum", "trim", "typeof",
    "upper",
})
_WRITE_ACTIONS = frozenset({
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE,
})
_TEMP_ACTIONS = frozenset({
    sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
})


class _ProfileRefusal(Exception):
    pass


class _LimitReached(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Observation:
    name: str
    query: str


@dataclass(frozen=True, slots=True)
class _Bundle:
    old_sql: str
    migrations: tuple[str, ...]
    fresh_sql: str
    observations: tuple[_Observation, ...]


@dataclass(frozen=True, slots=True)
class _ObservedRows:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    schema: tuple[tuple[Any, ...], ...]
    observations: tuple[tuple[str, _ObservedRows], ...]


def _validate_bundle(bundle: Any) -> _Bundle:
    if not isinstance(bundle, dict) or set(bundle) - {
        "version", "old_sql", "migrations", "fresh_sql", "observations"
    }:
        raise ValueError("bundle must be an object with only version, old_sql, migrations, fresh_sql and observations")
    if bundle.get("version") != 1 or type(bundle.get("version")) is not int:
        raise ValueError("version must be 1")
    for key in ("old_sql", "fresh_sql"):
        value = bundle.get(key)
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a SQL string")
    migrations = bundle.get("migrations")
    if not isinstance(migrations, list) or len(migrations) > MAX_MIGRATIONS:
        raise ValueError(f"migrations must be an array of at most {MAX_MIGRATIONS} SQL strings")
    if any(not isinstance(sql, str) for sql in migrations):
        raise ValueError("each migration must be a SQL string")
    observations = bundle.get("observations", [])
    if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS:
        raise ValueError(f"observations must be an array of at most {MAX_OBSERVATIONS} queries")
    seen: set[str] = set()
    normalized: list[_Observation] = []
    for observation in observations:
        if (not isinstance(observation, dict) or set(observation) != {"name", "query"}
                or not isinstance(observation["name"], str) or not observation["name"]
                or len(observation["name"].encode("utf-8")) > 128
                or not isinstance(observation["query"], str)):
            raise ValueError("each observation must contain a non-empty name and a query string")
        if observation["name"] in seen:
            raise ValueError("observation names must be unique")
        seen.add(observation["name"])
        normalized.append(_Observation(observation["name"], observation["query"]))
    total_sql = sum(len(sql.encode("utf-8")) for sql in
                    [bundle["old_sql"], *migrations, bundle["fresh_sql"],
                     *(item.query for item in normalized)])
    if total_sql > MAX_SQL_BYTES:
        raise ValueError(f"combined SQL must be at most {MAX_SQL_BYTES} bytes")
    return _Bundle(bundle["old_sql"], tuple(migrations), bundle["fresh_sql"], tuple(normalized))


def _bundle_json(bundle: _Bundle) -> dict[str, Any]:
    return {
        "version": 1,
        "old_sql": bundle.old_sql,
        "migrations": list(bundle.migrations),
        "fresh_sql": bundle.fresh_sql,
        "observations": [{"name": item.name, "query": item.query} for item in bundle.observations],
    }


def _authorizer(readonly: bool, state: dict[str, Any]):
    def authorize(action: int, arg1: str | None, arg2: str | None,
                  database: str | None, trigger: str | None) -> int:
        del database, trigger
        if action == sqlite3.SQLITE_PRAGMA:
            state["refusal"] = "PRAGMA statements are outside the supported SQL profile"
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            state["refusal"] = "ATTACH and DETACH are outside the supported SQL profile"
            return sqlite3.SQLITE_DENY
        if action in _TEMP_ACTIONS:
            state["refusal"] = "temporary tables, indexes, views and triggers are unsupported"
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_CREATE_VTABLE or action == sqlite3.SQLITE_DROP_VTABLE:
            state["refusal"] = "virtual tables are unsupported"
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            name = (arg2 or arg1 or "").lower()
            if name not in _FUNCTIONS and not name.startswith("sqlite_rename_"):
                state["refusal"] = f"SQL function {name!r} is outside the deterministic allowlist"
                return sqlite3.SQLITE_DENY
        if readonly and (action in _WRITE_ACTIONS or action in
                         (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT)):
            state["refusal"] = "observations must be read-only queries"
            return sqlite3.SQLITE_DENY
        if readonly and action not in (
            sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        ):
            state["refusal"] = "observation uses an operation outside the read-only query profile"
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return authorize


def _connection(deadline: float, state: dict[str, Any]) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    limits = (
        ("SQLITE_LIMIT_LENGTH", 1_000_000),
        ("SQLITE_LIMIT_SQL_LENGTH", MAX_SQL_BYTES),
        ("SQLITE_LIMIT_COLUMN", 100),
        ("SQLITE_LIMIT_EXPR_DEPTH", 100),
        ("SQLITE_LIMIT_COMPOUND_SELECT", 50),
        ("SQLITE_LIMIT_VDBE_OP", 100_000),
        ("SQLITE_LIMIT_FUNCTION_ARG", 100),
        ("SQLITE_LIMIT_ATTACHED", 0),
        ("SQLITE_LIMIT_TRIGGER_DEPTH", 50),
    )
    for name, limit in limits:
        code = getattr(sqlite3, name, None)
        if code is not None:
            connection.setlimit(code, limit)
    connection.execute("PRAGMA page_size=4096")
    connection.execute("PRAGMA max_page_count=128")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.set_authorizer(_authorizer(False, state))
    ticks = state.setdefault("ticks", 0)

    def progress() -> int:
        state["ticks"] = state.get("ticks", ticks) + 1
        if time.monotonic() >= deadline or state["ticks"] > 10_000:
            state["limit"] = "SQLite virtual-machine or time limit reached"
            return 1
        return 0

    connection.set_progress_handler(progress, 1_000)
    return connection


def _execute_script(connection: sqlite3.Connection, script: str,
                    state: dict[str, Any]) -> None:
    try:
        start = 0
        for index, character in enumerate(script):
            if character != ";" or not sqlite3.complete_statement(script[start:index + 1]):
                continue
            statement = script[start:index + 1]
            if statement.strip():
                state["statements"] += 1
                if state["statements"] > MAX_STATEMENTS:
                    raise _LimitReached(f"at most {MAX_STATEMENTS} SQL statements are supported")
                connection.execute(statement)
            start = index + 1
        tail = script[start:]
        if tail.strip():
            state["statements"] += 1
            if state["statements"] > MAX_STATEMENTS:
                raise _LimitReached(f"at most {MAX_STATEMENTS} SQL statements are supported")
            connection.execute(tail)
    except sqlite3.Error as exc:
        if state.get("limit"):
            raise _LimitReached(state["limit"]) from exc
        if state.get("refusal"):
            raise _ProfileRefusal(state["refusal"]) from exc
        if "not authorized" in str(exc).lower():
            raise _ProfileRefusal("SQLite authorizer rejected the SQL") from exc
        raise ValueError(f"invalid SQL: {exc}") from exc


def _schema(connection: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
    connection.set_authorizer(None)
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name, tbl_name, sql"
    ).fetchall()
    if len(rows) > MAX_SCHEMA_OBJECTS:
        raise _LimitReached(f"schema has more than {MAX_SCHEMA_OBJECTS} objects")
    return tuple(tuple(row) for row in rows)


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return ("$sqlite_blob_hex", value.hex())
    if isinstance(value, float) and not math.isfinite(value):
        raise _ProfileRefusal("non-finite SQLite values cannot be represented in observations")
    return value


def _exact_json(value: Any) -> str:
    """Serialize before comparing so SQLite integer and real values stay distinct."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _observe(connection: sqlite3.Connection, observations: tuple[_Observation, ...],
             deadline: float, state: dict[str, Any]) -> tuple[tuple[str, _ObservedRows], ...]:
    results: list[tuple[str, _ObservedRows]] = []
    output_bytes = 0
    for item in observations:
        state.pop("refusal", None)
        connection.set_authorizer(_authorizer(True, state))
        try:
            cursor = connection.execute(item.query)
            if cursor.description is None:
                raise _ProfileRefusal("observations must be read-only queries that return columns")
            columns = [column[0] for column in cursor.description]
            output_bytes += len(json.dumps(columns, ensure_ascii=True, separators=(",", ":")))
            if output_bytes > MAX_OUTPUT_BYTES:
                raise _LimitReached("observation output exceeded the response size limit")
            rows = []
            while True:
                if time.monotonic() >= deadline:
                    state["limit"] = "observation query exceeded its deadline"
                    raise _LimitReached(state["limit"])
                row = cursor.fetchone()
                if row is None:
                    break
                if len(rows) >= MAX_OBSERVATION_ROWS:
                    raise _LimitReached(f"observation returned more than {MAX_OBSERVATION_ROWS} rows")
                values = [_json_value(value) for value in row]
                encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
                output_bytes += len(encoded)
                if output_bytes > MAX_OUTPUT_BYTES:
                    raise _LimitReached("observation output exceeded the response size limit")
                rows.append(values)
            results.append((item.name, _ObservedRows(tuple(columns), tuple(tuple(row) for row in rows))))
        except sqlite3.Error as exc:
            if state.get("limit"):
                raise _LimitReached(state["limit"]) from exc
            if state.get("refusal") or "not authorized" in str(exc).lower():
                raise _ProfileRefusal(state.get("refusal") or "SQLite authorizer rejected the query") from exc
            raise ValueError(f"invalid observation SQL: {exc}") from exc
        finally:
            connection.set_authorizer(_authorizer(False, state))
    return tuple(results)


def _snapshot_json(snapshot: _Snapshot) -> dict[str, Any]:
    def value_json(value: Any) -> Any:
        if isinstance(value, tuple) and len(value) == 2 and value[0] == "$sqlite_blob_hex":
            return {"$sqlite_blob_hex": value[1]}
        return value

    return {
        "schema": [list(row) for row in snapshot.schema],
        "observations": {
            name: {"columns": list(result.columns),
                   "rows": [[value_json(value) for value in row] for row in result.rows]}
            for name, result in snapshot.observations
        },
    }


def _compare_bundle(raw_bundle: Any, timeout_seconds: int) -> dict[str, Any]:
    bundle = raw_bundle if isinstance(raw_bundle, _Bundle) else _validate_bundle(raw_bundle)
    deadline = time.monotonic() + timeout_seconds
    state: dict[str, Any] = {"statements": 0, "ticks": 0}
    migrated = _connection(deadline, state)
    fresh = _connection(deadline, state)
    try:
        _execute_script(migrated, bundle.old_sql, state)
        for sql in bundle.migrations:
            _execute_script(migrated, sql, state)
        _execute_script(fresh, bundle.fresh_sql, state)
        migrated_schema = _schema(migrated)
        fresh_schema = _schema(fresh)
        migrated_snapshot = _Snapshot(
            migrated_schema, _observe(migrated, bundle.observations, deadline, state)
        )
        fresh_snapshot = _Snapshot(
            fresh_schema, _observe(fresh, bundle.observations, deadline, state)
        )
        left = _snapshot_json(migrated_snapshot)
        right = _snapshot_json(fresh_snapshot)
        status = "MATCH" if _exact_json(left) == _exact_json(right) else "DIFFERENT"
        differences = {}
        if status == "DIFFERENT":
            if _exact_json(left["schema"]) != _exact_json(right["schema"]):
                migrated_only = [row for row in left["schema"] if row not in right["schema"]]
                fresh_only = [row for row in right["schema"] if row not in left["schema"]]
                differences["schema"] = {
                    "migrated_only": migrated_only[:100],
                    "migrated_only_count": len(migrated_only),
                    "migrated_only_truncated": len(migrated_only) > 100,
                    "fresh_only": fresh_only[:100],
                    "fresh_only_count": len(fresh_only),
                    "fresh_only_truncated": len(fresh_only) > 100,
                }
            if _exact_json(left["observations"]) != _exact_json(right["observations"]):
                differences["observations"] = {
                    name: {"migrated": left["observations"][name], "fresh": right["observations"][name]}
                    for name in left["observations"]
                    if _exact_json(left["observations"][name]) != _exact_json(right["observations"][name])
                }
        result = {
            "status": status,
            "schema_object_count": len(migrated_schema),
            "fresh_schema_object_count": len(fresh_schema),
            "observation_count": len(bundle.observations),
            "model_scope": MODEL_SCOPE,
        }
        if differences:
            result["differences"] = differences
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise _LimitReached("comparison result exceeded the response size limit")
        return result
    except _ProfileRefusal as exc:
        return {"status": "UNSUPPORTED", "reason": str(exc), "failure_kind": "unsupported_sql",
                "model_scope": MODEL_SCOPE}
    except _LimitReached as exc:
        return {"status": "UNKNOWN", "reason": str(exc), "model_scope": MODEL_SCOPE}
    except ValueError as exc:
        return {"status": "UNSUPPORTED", "reason": str(exc), "failure_kind": "invalid_sql",
                "model_scope": MODEL_SCOPE}
    except sqlite3.Error as exc:
        return {"status": "UNAVAILABLE", "reason": f"SQLite runtime failure: {exc}",
                "model_scope": MODEL_SCOPE}
    finally:
        migrated.close()
        fresh.close()


def _worker() -> int:
    try:
        request = json.loads(sys.stdin.read(MAX_WORKER_INPUT_BYTES + 1))
        if len(json.dumps(request, ensure_ascii=True).encode("utf-8")) > MAX_WORKER_INPUT_BYTES:
            raise ValueError("worker request exceeds size limit")
        bundle = _validate_bundle(request["bundle"])
        timeout = request["timeout_seconds"]
        if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds is outside the supported range")
        result = _compare_bundle(bundle, timeout)
    except Exception as exc:
        result = {"status": "UNAVAILABLE", "reason": f"worker request failed: {exc}",
                  "model_scope": MODEL_SCOPE}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


def compare_migrations(root: Path, path: str,
                       timeout_seconds: int = 10) -> dict[str, Any]:
    """Compare one JSON migration bundle using bounded in-memory SQLite databases."""
    from ._data import read_json

    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}")
    bundle, digest = read_json(root, path, max_bytes=MAX_BUNDLE_BYTES, reject_symlinks=True)
    normalized = _validate_bundle(bundle)
    request = json.dumps({"bundle": _bundle_json(normalized), "timeout_seconds": timeout_seconds},
                         ensure_ascii=True, separators=(",", ":"))
    if len(request.encode("utf-8")) > MAX_WORKER_INPUT_BYTES:
        raise ValueError(f"serialized migration request must be at most {MAX_WORKER_INPUT_BYTES} bytes")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"],
            input=request, capture_output=True, encoding="utf-8", timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result = {"status": "UNKNOWN", "reason": f"isolated SQLite worker exceeded {timeout_seconds} seconds"}
    except OSError as exc:
        result = {"status": "UNAVAILABLE", "reason": f"cannot start isolated SQLite worker: {exc}"}
    else:
        if completed.returncode != 0:
            result = {"status": "UNAVAILABLE", "reason": "isolated SQLite worker exited without a result"}
        else:
            try:
                result = json.loads(completed.stdout)
                if not isinstance(result, dict) or result.get("status") not in (
                    "MATCH", "DIFFERENT", "UNSUPPORTED", "UNKNOWN", "UNAVAILABLE",
                ):
                    raise ValueError("invalid worker result")
            except (ValueError, json.JSONDecodeError):
                result = {"status": "UNAVAILABLE", "reason": "isolated SQLite worker returned invalid JSON"}
    return {
        "operation": "compare_migrations", "path": path, "source_sha256": digest,
        "model_scope": MODEL_SCOPE, **result,
    }


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_worker())
