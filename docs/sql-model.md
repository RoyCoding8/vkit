# Bounded SQL query equivalence

`compare_sql_queries` checks whether two read-only queries return the same **bag** of
rows for every database admitted by a captured finite model. It uses SQLGlot to parse
a deliberately small SQLite SQL fragment and cvc5 to decide whether any admitted
database gives different result multiplicities. `EQUIVALENT_WITHIN_BOUNDS` is an exact
answer for that model. It does not imply equivalence on larger tables, other value
ranges, or general SQL.

The JSON bundle is version 1 and has exactly these top-level fields:

```json
{
  "version": 1,
  "tables": [
    {
      "name": "items",
      "max_rows": 2,
      "columns": [
        {"name": "id", "type": "integer", "nullable": false, "min": 0, "max": 2},
        {"name": "group_id", "type": "integer", "nullable": true, "min": 0, "max": 2}
      ]
    }
  ],
  "before": "SELECT id FROM items WHERE group_id IS NOT NULL",
  "after": "SELECT id FROM items WHERE NOT (group_id IS NULL)"
}
```

Declare one or two tables, one to eight columns per table, and one or two possible
active rows per table. Each column has an inclusive integer domain no wider than nine
values and within `[-1000,1000]`. Every row slot may be inactive. Nullable cells choose
NULL independently of their integer value. Duplicate rows are allowed; there are no
implicit primary keys or uniqueness constraints.

The SQL fragment accepts one `SELECT` with one to eight direct column projections, one
declared base table, optionally one `INNER JOIN ... ON`, and optionally one `WHERE`.
Plain `JOIN` has SQLite's `INNER JOIN` meaning and is accepted. Predicates use column or
integer-literal comparisons (`=`, `!=`, `<`, `<=`, `>`, `>=`), `IS NULL`, parentheses,
`NOT`, `AND`, and `OR`. Unqualified columns must resolve to exactly one table. Names
are simple unquoted identifiers and are resolved case-insensitively as in SQLite.

All other clauses and constructs fail closed, including `DISTINCT`, wildcard
projections, output aliases, outer joins, `USING`, arithmetic, functions, aggregates,
subqueries, set operations, ordering, limits, collations, hints, CTEs, and writes.
SQL comments and optimizer hints are also rejected so the parser cannot silently discard
text outside the reviewed fragment. Table names beginning with SQLite's reserved
`sqlite_` prefix are refused.
Unsupported syntax is not normalized into a weaker query. Query pairs must project
the same number of columns; each query may reference any subset of the declared tables.

The analyzer models SQL three-valued predicate logic: only TRUE rows satisfy a join or
WHERE condition; FALSE and UNKNOWN do not. Results are bags, so duplicate row counts
must match. cvc5 searches for a concrete difference by comparing symbolic tuple
multiplicities across all result-row contexts. An UNSAT result proves equality for the
entire finite model without enumerating a sampled subset. A SAT model is reported as a
counterexample only after the operation constructs a fresh in-memory SQLite database,
runs canonical SQL rendered from the validated query model, and independently confirms
the bag difference. A replay mismatch is `UNKNOWN`, never a counterexample.

Statuses are:

- `EQUIVALENT_WITHIN_BOUNDS`: cvc5 proved there is no bag difference in the declared
  finite model.
- `COUNTEREXAMPLE`: cvc5 found a model and SQLite replay confirmed its concrete bag
  difference.
- `UNSUPPORTED`: the bundle or either query is outside the reviewed profile.
- `UNKNOWN`: the solver timed out or failed, or a SAT witness could not be replayed.
- `UNAVAILABLE`: the optional SQLGlot or cvc5 dependency is missing.
  The operation also reports unavailable when either installed backend version differs
  from the reviewed pins, SQLGlot 30.22.0 and cvc5 1.4.2.

Every result after bundle capture includes the digest, normalized finite-model
declaration, and both submitted query strings. A counterexample also includes the
concrete table rows and replayed result bags with multiplicity differences, so a
consumer can repeat the independent check without trusting the solver-only model.

The bundle is captured through vkit's repository-relative JSON reader and limited to
64 KiB. Query text is limited to 4 KiB and 256 parsed AST nodes each; the complete
isolated analysis has a one-to-sixty-second wall deadline, with cvc5 receiving only the
remaining analysis time.
No submitted SQL executes against a caller database. SQLite is used only as an
independent witness replay on a newly created in-memory database.
