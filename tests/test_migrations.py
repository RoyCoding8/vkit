from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vkit.operations import migrations
from vkit.operations.migrations import _compare_bundle


def _bundle(old_sql: str, migrations: list[str], fresh_sql: str,
            observations: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "old_sql": old_sql,
        "migrations": migrations,
        "fresh_sql": fresh_sql,
        "observations": observations or [],
    }


def _compare(bundle: dict[str, Any], timeout: int = 2) -> dict[str, Any]:
    return _compare_bundle(bundle, timeout)


def test_added_column_with_default_matches_fresh_schema_and_rows() -> None:
    result = _compare(_bundle(
        "CREATE TABLE users(id integer primary key, name text); INSERT INTO users VALUES (1, 'Ada');",
        ["ALTER TABLE users ADD COLUMN role text NOT NULL DEFAULT 'member';"],
        "CREATE TABLE users(id integer primary key, name text, role text NOT NULL DEFAULT 'member'); "
        "INSERT INTO users VALUES (1, 'Ada', 'member');",
        [{"name": "users", "query": "SELECT id, name, role FROM users ORDER BY id"}],
    ))

    assert result["status"] == "MATCH", result.get("reason", result)
    assert result["schema_object_count"] >= 1


def test_rename_table_matches_fresh_install() -> None:
    result = _compare(_bundle(
        "CREATE TABLE old_name(id integer primary key, value text);",
        ["ALTER TABLE old_name RENAME TO new_name;"],
        'CREATE TABLE "new_name"(id integer primary key, value text);',
    ))

    assert result["status"] == "MATCH", result.get("reason", result)


@pytest.mark.parametrize("old_sql,migrations,fresh_sql", [
    (
        "CREATE TABLE records(id integer primary key, value text);",
        [],
        "CREATE TABLE records(id integer primary key, value text); CREATE INDEX records_value ON records(value);",
    ),
    (
        "CREATE TABLE records(id integer primary key, value text);",
        [],
        "CREATE TABLE records(id integer primary key, value text CHECK(length(value) > 0));",
    ),
    (
        "CREATE TABLE parent(id integer primary key); CREATE TABLE child(parent_id integer);",
        [],
        "CREATE TABLE parent(id integer primary key); CREATE TABLE child(parent_id integer REFERENCES parent(id));",
    ),
])
def test_catalog_detects_missing_index_foreign_key_or_check(
    old_sql: str, migrations: list[str], fresh_sql: str,
) -> None:
    result = _compare(_bundle(old_sql, migrations, fresh_sql))

    assert result["status"] == "DIFFERENT"
    assert "schema" in result["differences"]


@pytest.mark.parametrize("old_sql,fresh_sql", [
    (
        "CREATE TABLE records(id integer primary key, value text);",
        "CREATE TABLE records(id integer primary key, value text); "
        "CREATE INDEX records_value ON records(value) WHERE value IS NOT NULL;",
    ),
    (
        "CREATE TABLE records(value text COLLATE BINARY);",
        "CREATE TABLE records(value text COLLATE NOCASE);",
    ),
    (
        "CREATE TABLE records(value text); CREATE VIEW visible_records AS SELECT value FROM records;",
        "CREATE TABLE records(value text); CREATE VIEW visible_records AS SELECT upper(value) FROM records;",
    ),
    (
        "CREATE TABLE records(value text); CREATE TABLE audit(value text);",
        "CREATE TABLE records(value text); CREATE TABLE audit(value text); "
        "CREATE TRIGGER records_audit AFTER INSERT ON records BEGIN "
        "INSERT INTO audit(value) VALUES (new.value); END;",
    ),
])
def test_catalog_preserves_partial_indexes_collations_views_and_triggers(
    old_sql: str, fresh_sql: str,
) -> None:
    result = _compare(_bundle(old_sql, [], fresh_sql))

    assert result["status"] == "DIFFERENT"
    assert result["differences"]["schema"]["fresh_only"]


def test_observation_detects_data_loss_and_incorrect_backfill() -> None:
    result = _compare(_bundle(
        "CREATE TABLE customers(id integer primary key, name text); INSERT INTO customers VALUES (1, 'Ada');",
        ["ALTER TABLE customers ADD COLUMN region text; UPDATE customers SET region='unknown';"],
        "CREATE TABLE customers(id integer primary key, name text, region text); "
        "INSERT INTO customers VALUES (1, 'Ada', 'north');",
        [{"name": "customers", "query": "SELECT id, name, region FROM customers ORDER BY id"}],
    ))

    assert result["status"] == "DIFFERENT"
    assert result["differences"]["observations"]["customers"]["migrated"]["rows"] == [[1, "Ada", "unknown"]]
    assert result["differences"]["observations"]["customers"]["fresh"]["rows"] == [[1, "Ada", "north"]]


@pytest.mark.parametrize("sql", [
    "ATTACH DATABASE ':memory:' AS other;",
    "SELECT load_extension('not-a-real-extension');",
    "CREATE TEMP TABLE scratch(value);",
    "PRAGMA foreign_keys=OFF;",
])
def test_rejects_sql_outside_the_supported_profile(sql: str) -> None:
    result = _compare(_bundle("", [sql], ""))

    assert result["status"] == "UNSUPPORTED"
    assert result["failure_kind"] == "unsupported_sql"


def test_syntax_error_is_unsupported_and_separate_from_a_difference() -> None:
    result = _compare(_bundle("", ["CREATE TABLE broken ("], ""))

    assert result["status"] == "UNSUPPORTED"
    assert result["failure_kind"] == "invalid_sql"


def test_catalog_comparison_preserves_whitespace_inside_literals() -> None:
    result = _compare(_bundle(
        "CREATE TABLE labels(value text DEFAULT 'north  west');",
        [],
        "CREATE TABLE labels(value text DEFAULT 'north west');",
    ))

    assert result["status"] == "DIFFERENT"
    assert result["differences"]["schema"]["migrated_only"]
    assert result["differences"]["schema"]["fresh_only"]


def test_observations_must_be_read_only() -> None:
    result = _compare(_bundle(
        "CREATE TABLE records(value integer); INSERT INTO records VALUES (1);",
        [],
        "CREATE TABLE records(value integer); INSERT INTO records VALUES (1);",
        [{"name": "mutating", "query": "DELETE FROM records RETURNING value"}],
    ))

    assert result["status"] == "UNSUPPORTED"
    assert result["failure_kind"] == "unsupported_sql"


def test_observation_comparison_preserves_sqlite_integer_versus_real_types() -> None:
    result = _compare(_bundle(
        "CREATE TABLE values_table(value); INSERT INTO values_table VALUES (1);",
        [],
        "CREATE TABLE values_table(value); INSERT INTO values_table VALUES (1.0);",
        [{"name": "value", "query": "SELECT value FROM values_table"}],
    ))

    assert result["status"] == "DIFFERENT"
    assert result["differences"]["observations"]["value"]["migrated"]["rows"] == [[1]]
    assert result["differences"]["observations"]["value"]["fresh"]["rows"] == [[1.0]]


def test_rejects_special_sqlite_time_values_as_nondeterministic() -> None:
    result = _compare(_bundle(
        "",
        [],
        "",
        [{"name": "time", "query": "SELECT CURRENT_TIMESTAMP"}],
    ))

    assert result["status"] == "UNSUPPORTED"
    assert result["failure_kind"] == "unsupported_sql"


def test_recursive_observation_hits_progress_limit_without_match() -> None:
    query = ("WITH RECURSIVE seq(value) AS (VALUES(0) UNION ALL "
             "SELECT value + 1 FROM seq) SELECT value FROM seq")
    result = _compare(_bundle("", [], "", [{"name": "unbounded", "query": query}]), timeout=1)

    assert result["status"] == "UNKNOWN"


@pytest.mark.parametrize("bundle", [
    {"version": 1, "old_sql": "", "migrations": [], "fresh_sql": "", "extra": True},
    {"version": 1, "old_sql": "", "migrations": "not-a-list", "fresh_sql": ""},
    {"version": 1, "old_sql": "", "migrations": [], "fresh_sql": "", "observations": [
        {"name": "same", "query": "SELECT 1"}, {"name": "same", "query": "SELECT 2"},
    ]},
])
def test_rejects_malformed_bundle(bundle: dict[str, Any]) -> None:
    from vkit.operations.migrations import _validate_bundle

    with pytest.raises(ValueError):
        _validate_bundle(bundle)


def test_isolated_worker_runs_with_ignored_site_packages() -> None:
    request = {
        "bundle": _bundle("CREATE TABLE t(value);", [], "CREATE TABLE t(value);"),
        "timeout_seconds": 2,
    }
    completed = subprocess.run(
        [sys.executable, "-I", str(Path(migrations.__file__).resolve()), "--worker"],
        input=json.dumps(request), capture_output=True, text=True, timeout=5, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "MATCH"


def test_public_handler_captures_repository_json_and_runs_isolated_worker(tmp_path: Path) -> None:
    raw = json.dumps(_bundle(
        "CREATE TABLE items(id integer primary key, value text);",
        ["ALTER TABLE items ADD COLUMN active integer NOT NULL DEFAULT 1;"],
        "CREATE TABLE items(id integer primary key, value text, active integer NOT NULL DEFAULT 1);",
    )).encode("utf-8")
    (tmp_path / "bundle.json").write_bytes(raw)

    result = migrations.compare_migrations(tmp_path, "bundle.json", timeout_seconds=5)

    assert result["status"] == "MATCH", result.get("reason", result)
    assert result["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["path"] == "bundle.json"


def test_public_handler_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    (tmp_path / "bundle.json").write_text(
        '{"version":1,"old_sql":"","old_sql":"","migrations":[],"fresh_sql":""}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        migrations.compare_migrations(tmp_path, "bundle.json")


def test_public_handler_rejects_symlink_bundle(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_bundle("", [], "")), encoding="utf-8")
    link = tmp_path / "bundle.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available on this host")

    with pytest.raises(ValueError, match="symlinks"):
        migrations.compare_migrations(tmp_path, "bundle.json")
