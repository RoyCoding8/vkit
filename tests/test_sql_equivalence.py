"""Behavior checks for exact bounded SQL bag equivalence."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkit.operations.sql_equivalence import compare_sql_queries
from vkit.operations.sql_equivalence import _analyze, _parse_bundle, _parse_query


def _bundle(tmp_path: Path, before: str, after: str, *, nullable: bool = True,
            tables: list[dict] | None = None) -> tuple[Path, str]:
    tables = tables or [{
        "name": "t", "max_rows": 2, "columns": [
            {"name": "id", "type": "integer", "nullable": nullable, "min": -1, "max": 1},
            {"name": "v", "type": "integer", "nullable": nullable, "min": -1, "max": 1},
        ],
    }]
    root = tmp_path
    path = "sql-equivalence.json"
    (root / path).write_text(json.dumps({
        "version": 1, "tables": tables, "before": before, "after": after,
    }), encoding="utf-8")
    return root, path


def _compare(tmp_path: Path, before: str, after: str, **kwargs: object) -> dict:
    root, path = _bundle(tmp_path, before, after, **kwargs)
    return compare_sql_queries(root, path, timeout_seconds=10)


def test_identical_query_is_exactly_equivalent_inside_bounds(tmp_path: Path) -> None:
    result = _compare(tmp_path, "SELECT id FROM t", "SELECT id FROM t")
    assert result["status"] == "EQUIVALENT_WITHIN_BOUNDS"
    assert result["model_scope"].startswith("Exact equivalence over every database")
    assert result["source_sha256"]
    assert result["model"]["tables"][0]["name"] == "t"
    assert result["queries"]["before"] == "SELECT id FROM t"


def test_null_predicate_semantics_prove_is_not_null_rewrite(tmp_path: Path) -> None:
    result = _compare(tmp_path, "SELECT id FROM t WHERE id IS NOT NULL",
                      "SELECT id FROM t WHERE NOT (id IS NULL)")
    assert result["status"] == "EQUIVALENT_WITHIN_BOUNDS"


def test_counterexample_is_an_actual_sqlite_bag_difference(tmp_path: Path) -> None:
    result = _compare(tmp_path, "SELECT id FROM t WHERE id >= 0",
                      "SELECT id FROM t WHERE id > 0")
    assert result["status"] == "COUNTEREXAMPLE"
    witness = result["counterexample"]
    assert witness["before_only"] or witness["after_only"]
    assert all(isinstance(row, list) for row in witness["before_rows"] + witness["after_rows"])
    assert len(witness["database"]["t"]) <= 2


def test_counterexample_replay_preserves_not_null_predicate(tmp_path: Path) -> None:
    result = _compare(tmp_path, "SELECT id FROM t WHERE NOT (id IS NULL)",
                      "SELECT id FROM t WHERE id > 0")
    assert result["status"] == "COUNTEREXAMPLE"
    assert result["counterexample"]["before_only"] or result["counterexample"]["after_only"]


def test_bag_multiplicity_detects_duplicate_rows(tmp_path: Path) -> None:
    result = _compare(tmp_path, "SELECT id FROM t", "SELECT id FROM t WHERE id = 0")
    assert result["status"] == "COUNTEREXAMPLE"
    assert any(item["count"] > 0 for item in result["counterexample"]["before_only"])


def test_join_comparison_uses_bag_semantics_and_null_logic(tmp_path: Path) -> None:
    tables = [
        {"name": "a", "max_rows": 2, "columns": [
            {"name": "id", "type": "integer", "nullable": True, "min": 0, "max": 1},
        ]},
        {"name": "b", "max_rows": 2, "columns": [
            {"name": "id", "type": "integer", "nullable": True, "min": 0, "max": 1},
        ]},
    ]
    result = _compare(
        tmp_path,
        "SELECT a.id FROM a INNER JOIN b ON a.id = b.id WHERE a.id IS NOT NULL",
        "SELECT a.id FROM a JOIN b ON b.id = a.id WHERE NOT (a.id IS NULL)",
        tables=tables,
    )
    assert result["status"] == "EQUIVALENT_WITHIN_BOUNDS"


def test_column_resolution_uses_query_scope_not_unused_bundle_tables(tmp_path: Path) -> None:
    tables = [
        {"name": name, "max_rows": 1, "columns": [
            {"name": "id", "type": "integer", "nullable": False, "min": 0, "max": 1},
        ]} for name in ("a", "b")
    ]
    result = _compare(tmp_path, "SELECT id FROM a", "SELECT a.id FROM a", tables=tables)
    assert result["status"] == "EQUIVALENT_WITHIN_BOUNDS"
    out_of_scope = _compare(tmp_path, "SELECT b.id FROM a", "SELECT a.id FROM a", tables=tables)
    assert out_of_scope["status"] == "UNSUPPORTED"


@pytest.mark.parametrize("query", [
    "SELECT * FROM t",
    "SELECT DISTINCT id FROM t",
    "SELECT id FROM t LIMIT 1",
    "SELECT id FROM t ORDER BY id",
    "SELECT id AS value FROM t",
    "SELECT /*+ INDEX(t i) */ id FROM t",
    "SELECT id FROM t INDEXED BY idx",
    "SELECT id FROM t WHERE id COLLATE NOCASE = 0",
    "SELECT id FROM t UNION SELECT id FROM t",
    "DELETE FROM t",
    "SELECT id + 1 FROM t",
    "SELECT (SELECT id FROM t) FROM t",
    "SELECT id FROM t; SELECT id FROM t",
    "SELECT FROM",
    "SELECT id FROM t OFFSET 1",
    "WITH c AS (SELECT id FROM t) SELECT id FROM c",
])
def test_unreviewed_sql_features_fail_closed(tmp_path: Path, query: str) -> None:
    result = _compare(tmp_path, query, "SELECT id FROM t")
    assert result["status"] == "UNSUPPORTED"


def test_duplicate_bag_rows_can_differ_by_join_multiplicity(tmp_path: Path) -> None:
    tables = [
        {"name": "a", "max_rows": 2, "columns": [
            {"name": "id", "type": "integer", "nullable": False, "min": 0, "max": 0},
        ]},
        {"name": "b", "max_rows": 2, "columns": [
            {"name": "id", "type": "integer", "nullable": False, "min": 0, "max": 0},
        ]},
    ]
    result = _compare(tmp_path, "SELECT a.id FROM a INNER JOIN b ON a.id = b.id",
                      "SELECT a.id FROM a", tables=tables)
    assert result["status"] == "COUNTEREXAMPLE"
    assert result["counterexample"]["before_only"] or result["counterexample"]["after_only"]


def test_bad_bundle_and_timeout_are_explicitly_refused(tmp_path: Path) -> None:
    root, path = _bundle(tmp_path, "SELECT id FROM t", "SELECT id FROM t")
    assert compare_sql_queries(root, path, timeout_seconds=0)["status"] == "UNSUPPORTED"
    (root / path).write_text('{"version":1,"tables":[],"before":"SELECT 1","after":"SELECT 1"}',
                             encoding="utf-8")
    assert compare_sql_queries(root, path)["status"] == "UNSUPPORTED"


def test_sqlite_reserved_table_name_is_refused_before_analysis(tmp_path: Path) -> None:
    tables = [{"name": "SQLite_internal", "max_rows": 1, "columns": [
        {"name": "id", "type": "integer", "nullable": False, "min": 0, "max": 0},
    ]}]
    root, path = _bundle(tmp_path, "SELECT id FROM SQLite_internal",
                         "SELECT id FROM SQLite_internal", tables=tables)
    result = compare_sql_queries(root, path)
    assert result["status"] == "UNSUPPORTED"
    assert "reserved by SQLite" in result["reason"]


def test_backend_version_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "cvc5", SimpleNamespace(__version__="9.9.9"))
    monkeypatch.setitem(sys.modules, "sqlglot", SimpleNamespace(__version__="30.22.0"))
    result = _analyze({}, "digest", time.monotonic() + 1)
    assert result["status"] == "UNAVAILABLE"
    assert result["backend_version"] == "9.9.9"
    assert "requires cvc5==1.4.2" in result["reason"]


def test_captured_timeout_bounds_the_isolated_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vkit.operations.sql_equivalence as sql_equivalence

    root, path = _bundle(tmp_path, "SELECT id FROM t", "SELECT id FROM t")
    observed: dict[str, float] = {}

    def timeout_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["timeout"] = float(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(sql_equivalence.subprocess, "run", timeout_run)
    result = compare_sql_queries(root, path, timeout_seconds=1)
    assert result["status"] == "UNKNOWN"
    assert 0 < observed["timeout"] <= 1


def test_pinned_parser_raises_for_malformed_sql() -> None:
    tables, _, _ = _parse_bundle({
        "version": 1,
        "tables": [{"name": "t", "max_rows": 1, "columns": [
            {"name": "id", "type": "integer", "nullable": False, "min": 0, "max": 0},
        ]}],
        "before": "SELECT FROM", "after": "SELECT id FROM t",
    })
    with pytest.raises(ValueError, match="SQL parse failed"):
        _parse_query("SELECT FROM", tables)
