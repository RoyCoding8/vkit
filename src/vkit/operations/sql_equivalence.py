"""Exact SQL bag-equivalence checks within a small, declared database model.

The solver ranges over every database in the bundle's finite row/value bounds.
It is deliberately not a general SQL equivalence checker.
"""
from __future__ import annotations

import re
import json
import math
import subprocess
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ._data import encode_json, read_json
from ..nowindow import hidden_window

MAX_BUNDLE_BYTES = 64_000
MAX_SQL_BYTES = 4_096
MAX_COLUMNS_PER_TABLE = 8
MAX_QUERY_AST_NODES = 256
MAX_PROJECTIONS = 8
MAX_DOMAIN_WIDTH = 9
MAX_TIMEOUT_SECONDS = 60
MAX_WORKER_INPUT_BYTES = MAX_BUNDLE_BYTES + 2_000
MAX_WORKER_OUTPUT_BYTES = 128_000
SQLGLOT_VERSION = "30.22.0"
CVC5_VERSION = "1.4.2"
MODEL_SCOPE = (
    "Exact equivalence over every database within the declared bounds: at most two "
    "tables, two active-row slots per table, integer columns in their declared finite "
    "ranges, and independently nullable cells. Queries are restricted to direct SELECT "
    "projections, optional INNER JOIN, and WHERE predicates with SQL three-valued NULL "
    "logic. Results use bag semantics. This does not prove equivalence for unbounded "
    "tables or SQL outside this profile."
)

Status = Literal["EQUIVALENT_WITHIN_BOUNDS", "COUNTEREXAMPLE", "UNSUPPORTED", "UNKNOWN", "UNAVAILABLE"]
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Unsupported(ValueError):
    pass


class _Limit(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Column:
    name: str
    nullable: bool
    minimum: int
    maximum: int


@dataclass(frozen=True, slots=True)
class _Table:
    name: str
    max_rows: int
    columns: tuple[_Column, ...]


@dataclass(frozen=True, slots=True)
class _Ref:
    table: str
    column: str


@dataclass(frozen=True, slots=True)
class _Predicate:
    op: str
    left: Any = None
    right: Any = None


@dataclass(frozen=True, slots=True)
class _Query:
    tables: tuple[str, ...]
    projection: tuple[_Ref, ...]
    join: _Predicate | None
    where: _Predicate | None


@dataclass(frozen=True, slots=True)
class _Cell:
    value: Any
    is_null: Any


@dataclass(frozen=True, slots=True)
class _Context:
    condition: Any
    output: tuple[_Cell, ...]


def _exact_keys(value: Any, required: set[str]) -> None:
    if not isinstance(value, dict) or value.keys() != required:
        raise _Unsupported("bundle object has missing or unknown fields")


def _identifier(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _Unsupported(f"{what} must be a simple unquoted SQL identifier")
    return value


def _parse_bundle(value: Any) -> tuple[tuple[_Table, ...], str, str]:
    _exact_keys(value, {"version", "tables", "before", "after"})
    if type(value["version"]) is not int or value["version"] != 1:
        raise _Unsupported("bundle version must be 1")
    raw_tables = value["tables"]
    if not isinstance(raw_tables, list) or not 1 <= len(raw_tables) <= 2:
        raise _Unsupported("tables must declare one or two tables")
    tables: list[_Table] = []
    table_names: set[str] = set()
    for raw in raw_tables:
        _exact_keys(raw, {"name", "max_rows", "columns"})
        name = _identifier(raw["name"], "table name")
        folded = name.casefold()
        if folded.startswith("sqlite_"):
            raise _Unsupported("table names beginning with sqlite_ are reserved by SQLite")
        if folded in table_names:
            raise _Unsupported("table names must be unique ignoring case")
        table_names.add(folded)
        rows = raw["max_rows"]
        if type(rows) is not int or not 1 <= rows <= 2:
            raise _Unsupported("max_rows must be 1 or 2")
        raw_columns = raw["columns"]
        if not isinstance(raw_columns, list) or not 1 <= len(raw_columns) <= MAX_COLUMNS_PER_TABLE:
            raise _Unsupported(f"each table must declare 1 to {MAX_COLUMNS_PER_TABLE} columns")
        columns: list[_Column] = []
        column_names: set[str] = set()
        for item in raw_columns:
            _exact_keys(item, {"name", "type", "nullable", "min", "max"})
            column = _identifier(item["name"], "column name")
            if column.casefold() in column_names:
                raise _Unsupported("column names must be unique ignoring case")
            column_names.add(column.casefold())
            if item["type"] != "integer" or type(item["nullable"]) is not bool:
                raise _Unsupported("columns require type='integer' and a boolean nullable flag")
            low, high = item["min"], item["max"]
            if type(low) is not int or type(high) is not int or low > high:
                raise _Unsupported("column min/max must be ordered integers")
            if high - low + 1 > MAX_DOMAIN_WIDTH or max(abs(low), abs(high)) > 1_000:
                raise _Unsupported(f"integer domains must contain at most {MAX_DOMAIN_WIDTH} values within [-1000,1000]")
            columns.append(_Column(column, item["nullable"], low, high))
        tables.append(_Table(name, rows, tuple(columns)))
    before, after = value["before"], value["after"]
    if not isinstance(before, str) or not isinstance(after, str):
        raise _Unsupported("before and after must be SQL strings")
    if len(before.encode("utf-8")) > MAX_SQL_BYTES or len(after.encode("utf-8")) > MAX_SQL_BYTES:
        raise _Unsupported(f"each SQL query must be at most {MAX_SQL_BYTES} UTF-8 bytes")
    return tuple(tables), before, after


def _model_summary(tables: tuple[_Table, ...]) -> dict[str, Any]:
    return {"tables": [{
        "name": table.name,
        "max_rows": table.max_rows,
        "columns": [{"name": column.name, "type": "integer", "nullable": column.nullable,
                     "min": column.minimum, "max": column.maximum}
                    for column in table.columns],
    } for table in tables]}


def _node(node: Any, allowed: tuple[type, ...], what: str) -> None:
    if not isinstance(node, allowed):
        raise _Unsupported(f"unsupported {what}: {type(node).__name__}")


def _reject_options(node: Any, allowed: set[str], what: str) -> None:
    """Fail closed on every non-default AST field not in the reviewed fragment."""
    for key, value in node.args.items():
        if key in allowed:
            continue
        if value is not None and value is not False and value != []:
            raise _Unsupported(f"unsupported {what} option or clause: {key}")


def _identifier_node(node: Any) -> str:
    from sqlglot import exp

    _node(node, (exp.Identifier,), "identifier")
    _reject_options(node, {"this", "quoted"}, "identifier")
    if node.args.get("quoted"):
        raise _Unsupported("quoted identifiers are outside this profile")
    return _identifier(node.this, "SQL identifier")


def _parse_column(node: Any, tables: tuple[_Table, ...]) -> _Ref:
    from sqlglot import exp

    _node(node, (exp.Column,), "column reference")
    _reject_options(node, {"this", "table"}, "column reference")
    if any(node.args.get(key) is not None for key in ("db", "catalog")):
        raise _Unsupported("database-qualified column references are unsupported")
    name = _identifier_node(node.this).casefold()
    qualifier = node.args.get("table")
    if qualifier is None:
        candidates = [t for t in tables if any(c.name.casefold() == name for c in t.columns)]
        if len(candidates) != 1:
            raise _Unsupported(f"unqualified column {name!r} is missing or ambiguous")
        table = candidates[0].name
    else:
        qualifier_name = _identifier_node(qualifier).casefold()
        table = qualifier_name
        matches = [t for t in tables if t.name.casefold() == table.casefold()]
        if len(matches) != 1:
            raise _Unsupported(f"unknown table qualifier {qualifier_name!r}")
        table = matches[0].name
    schema = next(t for t in tables if t.name.casefold() == table.casefold())
    if not any(c.name.casefold() == name for c in schema.columns):
        raise _Unsupported(f"unknown column {name!r} on table {table!r}")
    canonical = next(c.name for c in schema.columns if c.name.casefold() == name)
    return _Ref(table, canonical)


def _parse_scalar(node: Any, tables: tuple[_Table, ...]) -> _Ref | int:
    from sqlglot import exp

    if isinstance(node, exp.Column):
        return _parse_column(node, tables)
    if isinstance(node, exp.Literal) and not node.is_string:
        _reject_options(node, {"this", "is_string"}, "literal")
        try:
            number = int(node.this)
        except (TypeError, ValueError) as exc:
            raise _Unsupported("only integer literals are supported") from exc
        if str(number) != str(node.this) or abs(number) > 1_000:
            raise _Unsupported("integer literals must be canonical and within [-1000,1000]")
        return number
    raise _Unsupported(f"unsupported predicate scalar: {type(node).__name__}")


def _parse_predicate(node: Any, tables: tuple[_Table, ...]) -> _Predicate:
    from sqlglot import exp

    if isinstance(node, exp.Paren):
        if node.args.keys() != {"this"}:
            raise _Unsupported("unexpected parenthesis options")
        return _parse_predicate(node.this, tables)
    if isinstance(node, exp.And) or isinstance(node, exp.Or):
        if node.args.keys() != {"this", "expression"}:
            raise _Unsupported("unexpected boolean-expression options")
        return _Predicate("and" if isinstance(node, exp.And) else "or",
                          _parse_predicate(node.this, tables),
                          _parse_predicate(node.expression, tables))
    if isinstance(node, exp.Not):
        if node.args.keys() != {"this"}:
            raise _Unsupported("unexpected NOT options")
        return _Predicate("not", _parse_predicate(node.this, tables))
    if isinstance(node, exp.Is):
        if node.args.keys() != {"this", "expression"} or not isinstance(node.expression, exp.Null):
            raise _Unsupported("IS supports only NULL tests")
        _reject_options(node.expression, set(), "NULL literal")
        return _Predicate("is_null", _parse_column(node.this, tables))
    comparisons = {
        exp.EQ: "eq", exp.NEQ: "ne", exp.LT: "lt", exp.LTE: "le",
        exp.GT: "gt", exp.GTE: "ge",
    }
    for kind, op in comparisons.items():
        if isinstance(node, kind):
            if node.args.keys() != {"this", "expression"}:
                raise _Unsupported("unexpected comparison options")
            return _Predicate(op, _parse_scalar(node.this, tables),
                              _parse_scalar(node.expression, tables))
    raise _Unsupported(f"unsupported predicate: {type(node).__name__}")


def _parse_query(sql: str, tables: tuple[_Table, ...]) -> _Query:
    import sqlglot
    from sqlglot import exp

    if "--" in sql or "/*" in sql or "*/" in sql:
        raise _Unsupported("SQL comments and optimizer hints are outside this profile")
    try:
        from sqlglot.errors import ErrorLevel
        parsed = sqlglot.parse(sql, read="sqlite", error_level=ErrorLevel.RAISE)
    except Exception as exc:
        raise _Unsupported(f"SQL parse failed: {exc}") from exc
    if len(parsed) != 1 or parsed[0] is None:
        raise _Unsupported("exactly one SQL statement is required")
    root = parsed[0]
    _node(root, (exp.Select,), "query")
    if sum(1 for _ in root.walk()) > MAX_QUERY_AST_NODES:
        raise _Limit("query AST exceeds its node budget")
    allowed_select = {"expressions", "from_", "joins", "where"}
    _reject_options(root, allowed_select, "SELECT")
    projections = root.args.get("expressions")
    if not isinstance(projections, list) or not 1 <= len(projections) <= MAX_PROJECTIONS:
        raise _Unsupported(f"SELECT must project 1 to {MAX_PROJECTIONS} columns")
    source = root.args.get("from_")
    if not isinstance(source, exp.From) or set(source.args) != {"this"}:
        raise _Unsupported("query requires exactly one base table")
    base = source.this
    _node(base, (exp.Table,), "FROM source")
    _reject_options(base, {"this"}, "FROM table")
    base_name = _identifier_node(base.this)
    schemas = {table.name.casefold(): table for table in tables}
    if base_name.casefold() not in schemas:
        raise _Unsupported(f"unknown table {base_name!r}")
    referenced = [schemas[base_name.casefold()].name]
    joins = root.args.get("joins") or []
    join_predicate = None
    if not isinstance(joins, list) or len(joins) > 1:
        raise _Unsupported("at most one INNER JOIN is supported")
    if joins:
        join = joins[0]
        _node(join, (exp.Join,), "join")
        _reject_options(join, {"this", "kind", "on"}, "JOIN")
        if join.args.get("kind") not in (None, "INNER"):
            raise _Unsupported("only an explicit INNER JOIN ... ON is supported")
        target = join.this
        _node(target, (exp.Table,), "JOIN source")
        _reject_options(target, {"this"}, "JOIN table")
        target_name = _identifier_node(target.this)
        if target_name.casefold() not in schemas or target_name.casefold() == base_name.casefold():
            raise _Unsupported("JOIN must reference a different declared table")
        referenced.append(schemas[target_name.casefold()].name)
        if join.args.get("on") is None:
            raise _Unsupported("INNER JOIN requires an ON predicate")
    scope_tables = tuple(schemas[name.casefold()] for name in referenced)
    if joins:
        join_predicate = _parse_predicate(joins[0].args["on"], scope_tables)
    output = tuple(_parse_column(item, scope_tables) for item in projections)
    where_node = root.args.get("where")
    if where_node is not None:
        _node(where_node, (exp.Where,), "WHERE clause")
        if set(where_node.args) != {"this"}:
            raise _Unsupported("unexpected WHERE options")
        where_predicate = _parse_predicate(where_node.this, scope_tables)
    else:
        where_predicate = None
    return _Query(tuple(referenced), output, join_predicate, where_predicate)


def _and(solver: Any, cvc5: Any, *terms: Any) -> Any:
    if not terms:
        return solver.mkBoolean(True)
    if len(terms) == 1:
        return terms[0]
    return solver.mkTerm(cvc5.Kind.AND, *terms)


def _or(solver: Any, cvc5: Any, *terms: Any) -> Any:
    if not terms:
        return solver.mkBoolean(False)
    if len(terms) == 1:
        return terms[0]
    return solver.mkTerm(cvc5.Kind.OR, *terms)


def _not(solver: Any, cvc5: Any, term: Any) -> Any:
    return solver.mkTerm(cvc5.Kind.NOT, term)


def _eq(solver: Any, cvc5: Any, left: Any, right: Any) -> Any:
    return solver.mkTerm(cvc5.Kind.EQUAL, left, right)


def _compile_predicate(node: _Predicate | None, row: dict[tuple[str, str], _Cell],
                       solver: Any, cvc5: Any) -> tuple[Any, Any]:
    """Return (is_true, is_false); neither term may hold for SQL UNKNOWN."""
    yes, no = solver.mkBoolean(True), solver.mkBoolean(False)
    if node is None:
        return yes, no
    if node.op in ("and", "or"):
        lt, lf = _compile_predicate(node.left, row, solver, cvc5)
        rt, rf = _compile_predicate(node.right, row, solver, cvc5)
        if node.op == "and":
            return _and(solver, cvc5, lt, rt), _or(solver, cvc5, lf, rf)
        return _or(solver, cvc5, lt, rt), _and(solver, cvc5, lf, rf)
    if node.op == "not":
        truth, falsity = _compile_predicate(node.left, row, solver, cvc5)
        return falsity, truth
    if node.op == "is_null":
        cell = row[(node.left.table, node.left.column)]
        return cell.is_null, _not(solver, cvc5, cell.is_null)
    left = row[(node.left.table, node.left.column)] if isinstance(node.left, _Ref) else _Cell(solver.mkInteger(node.left), no)
    right = row[(node.right.table, node.right.column)] if isinstance(node.right, _Ref) else _Cell(solver.mkInteger(node.right), no)
    nonnull = _and(solver, cvc5, _not(solver, cvc5, left.is_null), _not(solver, cvc5, right.is_null))
    kinds = {"eq": cvc5.Kind.EQUAL, "ne": cvc5.Kind.DISTINCT, "lt": cvc5.Kind.LT,
             "le": cvc5.Kind.LEQ, "gt": cvc5.Kind.GT, "ge": cvc5.Kind.GEQ}
    comparison = solver.mkTerm(kinds[node.op], left.value, right.value)
    return _and(solver, cvc5, nonnull, comparison), _and(solver, cvc5, nonnull, _not(solver, cvc5, comparison))


def _make_rows(tables: tuple[_Table, ...], solver: Any, cvc5: Any) -> tuple[dict[str, list[dict[tuple[str, str], _Cell]]], dict[str, list[Any]]]:
    rows: dict[str, list[dict[tuple[str, str], _Cell]]] = {}
    active: dict[str, list[Any]] = {}
    for table in tables:
        table_rows = []
        table_active = []
        for row_index in range(table.max_rows):
            row_active = solver.mkConst(solver.getBooleanSort(), f"active_{table.name}_{row_index}")
            table_active.append(row_active)
            cell_row = {}
            for column in table.columns:
                value = solver.mkConst(solver.getIntegerSort(), f"value_{table.name}_{row_index}_{column.name}")
                null = (solver.mkConst(solver.getBooleanSort(), f"null_{table.name}_{row_index}_{column.name}")
                        if column.nullable else solver.mkBoolean(False))
                solver.assertFormula(_or(solver, cvc5, _not(solver, cvc5, row_active), null,
                                         solver.mkTerm(cvc5.Kind.GEQ, value, solver.mkInteger(column.minimum))))
                solver.assertFormula(_or(solver, cvc5, _not(solver, cvc5, row_active), null,
                                         solver.mkTerm(cvc5.Kind.LEQ, value, solver.mkInteger(column.maximum))))
                cell_row[(table.name, column.name)] = _Cell(value, null)
            table_rows.append(cell_row)
        rows[table.name] = table_rows
        active[table.name] = table_active
    return rows, active


def _contexts(query: _Query, rows: dict[str, list[dict[tuple[str, str], _Cell]]],
              active: dict[str, list[Any]], solver: Any, cvc5: Any) -> list[_Context]:
    import itertools

    result = []
    for indices in itertools.product(*(range(len(rows[name])) for name in query.tables)):
        environment: dict[tuple[str, str], _Cell] = {}
        conditions = []
        for table, index in zip(query.tables, indices):
            environment.update(rows[table][index])
            conditions.append(active[table][index])
        if query.join is not None:
            join_true, _ = _compile_predicate(query.join, environment, solver, cvc5)
            conditions.append(join_true)
        where_true, _ = _compile_predicate(query.where, environment, solver, cvc5)
        conditions.append(where_true)
        result.append(_Context(_and(solver, cvc5, *conditions),
                               tuple(environment[(ref.table, ref.column)] for ref in query.projection)))
    return result


def _same_tuple(left: tuple[_Cell, ...], right: tuple[_Cell, ...], solver: Any, cvc5: Any) -> Any:
    parts = []
    for a, b in zip(left, right):
        same_nullness = _eq(solver, cvc5, a.is_null, b.is_null)
        same_value = _eq(solver, cvc5, a.value, b.value)
        parts.append(_and(solver, cvc5, same_nullness,
                          _or(solver, cvc5, a.is_null, same_value)))
    return _and(solver, cvc5, *parts)


def _count(contexts: list[_Context], candidate: _Context, solver: Any, cvc5: Any) -> Any:
    terms = [solver.mkTerm(cvc5.Kind.ITE,
                           _and(solver, cvc5, item.condition,
                                _same_tuple(item.output, candidate.output, solver, cvc5)),
                           solver.mkInteger(1), solver.mkInteger(0)) for item in contexts]
    if not terms:
        return solver.mkInteger(0)
    if len(terms) == 1:
        return terms[0]
    return solver.mkTerm(cvc5.Kind.ADD, *terms)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _render_scalar(value: _Ref | int) -> str:
    return f"{_quote(value.table)}.{_quote(value.column)}" if isinstance(value, _Ref) else str(value)


def _render_predicate(node: _Predicate | None) -> str:
    if node is None:
        return "1"
    if node.op in ("and", "or"):
        return f"({_render_predicate(node.left)} {node.op.upper()} {_render_predicate(node.right)})"
    if node.op == "not":
        return f"(NOT {_render_predicate(node.left)})"
    if node.op == "is_null":
        return f"{_render_scalar(node.left)} IS NULL"
    operators = {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
    return f"{_render_scalar(node.left)} {operators[node.op]} {_render_scalar(node.right)}"


def _render_query(query: _Query) -> str:
    select = ", ".join(_render_scalar(ref) for ref in query.projection)
    sql = f"SELECT {select} FROM {_quote(query.tables[0])}"
    if query.join is not None:
        joined = next(name for name in query.tables if name.casefold() != query.tables[0].casefold())
        sql += f" INNER JOIN {_quote(joined)} ON {_render_predicate(query.join)}"
    if query.where is not None:
        sql += f" WHERE {_render_predicate(query.where)}"
    return sql


def _model_value(solver: Any, term: Any) -> int | bool:
    value = solver.getValue(term)
    if value.isBooleanValue():
        return value.getBooleanValue()
    if value.isIntegerValue():
        return int(value.getIntegerValue())
    raise ValueError("solver returned a non-concrete model value")


def _witness(tables: tuple[_Table, ...], rows: dict[str, list[dict[tuple[str, str], _Cell]]],
             active: dict[str, list[Any]], solver: Any) -> dict[str, list[dict[str, int | None]]]:
    witness: dict[str, list[dict[str, int | None]]] = {}
    for table in tables:
        values = []
        for index, row in enumerate(rows[table.name]):
            if not _model_value(solver, active[table.name][index]):
                continue
            record: dict[str, int | None] = {}
            for column in table.columns:
                cell = row[(table.name, column.name)]
                record[column.name] = None if _model_value(solver, cell.is_null) else int(_model_value(solver, cell.value))
            values.append(record)
        witness[table.name] = values
    return witness


def _replay(tables: tuple[_Table, ...], witness: dict[str, list[dict[str, int | None]]],
            before: _Query, after: _Query) -> tuple[bool, dict[str, Any]]:
    connection = sqlite3.connect(":memory:")
    try:
        for table in tables:
            declarations = ", ".join(f"{_quote(column.name)} INTEGER" for column in table.columns)
            connection.execute(f"CREATE TABLE {_quote(table.name)} ({declarations})")
            names = ", ".join(_quote(column.name) for column in table.columns)
            marks = ", ".join("?" for _ in table.columns)
            for record in witness[table.name]:
                connection.execute(f"INSERT INTO {_quote(table.name)} ({names}) VALUES ({marks})",
                                   tuple(record[column.name] for column in table.columns))
        left_rows = connection.execute(_render_query(before)).fetchall()
        right_rows = connection.execute(_render_query(after)).fetchall()
        left, right = Counter(left_rows), Counter(right_rows)
        details = {
            "database": witness,
            "before_rows": [list(row) for row in left_rows],
            "after_rows": [list(row) for row in right_rows],
            "before_only": [{"row": list(row), "count": count - right[row]}
                            for row, count in left.items() if count > right[row]],
            "after_only": [{"row": list(row), "count": count - left[row]}
                           for row, count in right.items() if count > left[row]],
        }
        return left != right, details
    finally:
        connection.close()


def _analyze(bundle: Any, digest: str, deadline: float) -> dict[str, Any]:
    base: dict[str, Any] = {"model_scope": MODEL_SCOPE, "backend": "cvc5", "source_sha256": digest}
    try:
        import cvc5
        import sqlglot
    except ImportError as exc:
        return {**base, "status": "UNAVAILABLE", "reason": str(exc)}
    versions = {"backend_version": getattr(cvc5, "__version__", None),
                "sqlglot_version": getattr(sqlglot, "__version__", None)}
    if versions["backend_version"] != CVC5_VERSION or versions["sqlglot_version"] != SQLGLOT_VERSION:
        return {**base, **versions, "status": "UNAVAILABLE",
                "reason": f"requires cvc5=={CVC5_VERSION} and sqlglot=={SQLGLOT_VERSION}"}
    try:
        tables, before_sql, after_sql = _parse_bundle(bundle)
    except _Unsupported as exc:
        return {**base, **versions, "status": "UNSUPPORTED", "reason": str(exc)}
    details = {**base, **versions, "model": _model_summary(tables),
               "queries": {"before": before_sql, "after": after_sql}}
    try:
        before = _parse_query(before_sql, tables)
        after = _parse_query(after_sql, tables)
        if len(before.projection) != len(after.projection):
            raise _Unsupported("queries must project the same number of columns")
    except _Unsupported as exc:
        return {**details, "status": "UNSUPPORTED", "reason": str(exc)}
    except _Limit as exc:
        return {**details, "status": "UNKNOWN", "reason": str(exc)}
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms < 1:
        return {**details, "status": "UNKNOWN", "reason": "analysis deadline expired before solving"}
    try:
        solver = cvc5.Solver()
        solver.setLogic("ALL")
        solver.setOption("produce-models", "true")
        rows, active = _make_rows(tables, solver, cvc5)
        left = _contexts(before, rows, active, solver, cvc5)
        right = _contexts(after, rows, active, solver, cvc5)
        differences = []
        for candidate in left + right:
            left_count = _count(left, candidate, solver, cvc5)
            right_count = _count(right, candidate, solver, cvc5)
            unequal = _not(solver, cvc5, _eq(solver, cvc5, left_count, right_count))
            differences.append(_and(solver, cvc5, candidate.condition, unequal))
        solver.assertFormula(_or(solver, cvc5, *differences))
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms < 1:
            return {**details, "status": "UNKNOWN", "reason": "analysis deadline expired before solving"}
        solver.setOption("tlimit-per", str(remaining_ms))
        result = solver.checkSat()
        if time.monotonic() >= deadline:
            return {**details, "status": "UNKNOWN", "reason": "analysis deadline expired"}
        if result.isUnsat():
            return {**details, "status": "EQUIVALENT_WITHIN_BOUNDS", "reason": None}
        if result.isUnknown():
            return {**details, "status": "UNKNOWN", "reason": str(solver.getUnknownExplanation())}
        witness = _witness(tables, rows, active, solver)
        replayed, evidence = _replay(tables, witness, before, after)
        if time.monotonic() >= deadline:
            return {**details, "status": "UNKNOWN", "reason": "analysis deadline expired during witness replay"}
        if not replayed:
            return {**details, "status": "UNKNOWN", "reason": "SMT witness did not reproduce a bag difference in SQLite"}
        return {**details, "status": "COUNTEREXAMPLE", "counterexample": evidence, "reason": None}
    except _Unsupported as exc:
        return {**details, "status": "UNSUPPORTED", "reason": str(exc)}
    except Exception as exc:
        return {**details, "status": "UNKNOWN",
                "reason": f"analysis did not complete: {type(exc).__name__}: {exc}"}


def _worker() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > MAX_WORKER_INPUT_BYTES:
            raise ValueError("worker request exceeds the input limit")
        request = json.loads(raw)
        if (not isinstance(request, dict) or set(request) != {"bundle", "digest", "deadline"}
                or not isinstance(request["digest"], str)
                or type(request["deadline"]) not in (int, float)
                or not math.isfinite(request["deadline"])):
            raise ValueError("worker request has invalid fields")
        deadline = float(request["deadline"])
        result = _analyze(request["bundle"], request["digest"], deadline)
    except Exception as exc:
        result = {"status": "UNAVAILABLE", "reason": f"worker request failed: {exc}"}
    try:
        output = encode_json(result, max_bytes=MAX_WORKER_OUTPUT_BYTES)
    except ValueError:
        output = '{"status":"UNKNOWN","reason":"worker result exceeds output limit"}'
    sys.stdout.write(output)
    return 0


def compare_sql_queries(root: Path, path: str, timeout_seconds: int = 10) -> dict[str, Any]:
    """Compare supported queries exactly within a captured finite database model."""
    base: dict[str, Any] = {"operation": "compare_sql_queries", "path": path,
                            "model_scope": MODEL_SCOPE, "backend": "cvc5"}
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        return {**base, "status": "UNSUPPORTED", "reason": f"timeout_seconds must be 1..{MAX_TIMEOUT_SECONDS}"}
    deadline = time.monotonic() + timeout_seconds
    try:
        bundle, digest = read_json(root, path, max_bytes=MAX_BUNDLE_BYTES)
        _parse_bundle(bundle)
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms < 1:
            return {**base, "status": "UNKNOWN", "reason": "analysis deadline expired while capturing input"}
        request = encode_json({"bundle": bundle, "digest": digest, "deadline": deadline},
                              max_bytes=MAX_WORKER_INPUT_BYTES)
    except ValueError as exc:
        return {**base, "status": "UNSUPPORTED", "reason": str(exc)}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {**base, "status": "UNKNOWN", "reason": "analysis deadline expired while encoding input"}
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c",
             "from vkit.operations.sql_equivalence import _worker; raise SystemExit(_worker())"],
            input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8",
            timeout=remaining, check=False, **hidden_window(),
        )
    except subprocess.TimeoutExpired:
        result = {"status": "UNKNOWN", "reason": "isolated SQL analyzer exceeded its wall deadline"}
    except OSError as exc:
        result = {"status": "UNAVAILABLE", "reason": f"cannot start isolated SQL analyzer: {exc}"}
    else:
        if time.monotonic() >= deadline:
            result = {"status": "UNKNOWN", "reason": "isolated SQL analyzer exceeded its wall deadline"}
        elif completed.returncode != 0:
            result = {"status": "UNAVAILABLE", "reason": "isolated SQL analyzer exited without a result"}
        elif len(completed.stdout.encode("utf-8")) > MAX_WORKER_OUTPUT_BYTES:
            result = {"status": "UNAVAILABLE", "reason": "isolated SQL analyzer exceeded its output limit"}
        else:
            try:
                result = json.loads(completed.stdout)
                if not isinstance(result, dict) or result.get("status") not in (
                    "EQUIVALENT_WITHIN_BOUNDS", "COUNTEREXAMPLE", "UNSUPPORTED", "UNKNOWN", "UNAVAILABLE",
                ):
                    raise ValueError("invalid worker result")
            except (ValueError, json.JSONDecodeError):
                result = {"status": "UNAVAILABLE", "reason": "isolated SQL analyzer returned invalid JSON"}
    if time.monotonic() >= deadline:
        result = {"status": "UNKNOWN", "reason": "SQL analysis exceeded its wall deadline"}
    return {**base, "source_sha256": digest, **result}


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    raise SystemExit(_worker())
