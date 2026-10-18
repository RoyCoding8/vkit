# Exact rational expressions

`compare_rational_expressions(path, timeout_seconds=10)` compares two expressions in
the rational-function field `Q(x1,...,xn)`. It uses SymPy 1.14.0 and independently
checks the resulting normal form with exact sparse polynomial arithmetic.

```json
{
  "version": 1,
  "variables": ["x"],
  "before": {"div": [
    {"sub": [{"pow": [{"var": "x"}, 2]}, {"rational": [1, 1]}]},
    {"sub": [{"var": "x"}, {"rational": [1, 1]}]}
  ]},
  "after": {"add": [{"var": "x"}, {"rational": [1, 1]}]}
}
```

Save the bundle inside the subject repository, then run:

```powershell
vkit compute compare-rational-expressions --project . --path algebra.json --json
```

The closed JSON grammar has `var`, exact `rational: [numerator, denominator]`, binary
`add`, `sub`, `mul`, `div`, and `pow: [expression, nonnegative_integer]`. Division by
an identically zero expression is unsupported. No strings are evaluated as code.

`IDENTICAL` establishes an identity wherever every original denominator is nonzero.
The example retains the guard `x - 1 != 0`. It does not assert equal domains at the
removed pole. `NOT_IDENTICAL` includes a nonzero exact cross-product polynomial.
That coefficient evidence establishes nonidentity without a sampled evaluation.

Each result carries the captured JSON SHA-256, engine version, model scope, normal
difference and original denominator guards. The independent checker verifies the
backend normal form by cross multiplication, using `Fraction` coefficients.

Limits are 40 KB of input, four variables, 128 expression nodes, depth 24, powers
through eight, degree 64, 1,024 terms per polynomial, 2,048 coefficient bits and
250,000 coefficient products. Evidence is capped at 250 KB. The default worker
deadline is ten seconds, configurable through 60. Exhausted computation or output
budgets return `UNKNOWN`. Invalid syntax returns `UNSUPPORTED`; a missing or
mismatched pinned engine returns `UNAVAILABLE`.

This operation models exact rational algebra. Use `check_rewrite` for the existing
integer/boolean Python AST semantics. Neither operation models floating-point error.

The backend domain is documented in [SymPy's polynomial domains](https://docs.sympy.org/latest/modules/polys/domainsintro.html).
