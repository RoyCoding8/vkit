# Round-off model

`analyze_roundoff` proves an absolute error budget for a closed arithmetic DAG, or finds a
uniform binary32/binary64 format in a finite caller-supplied grid for which that same budget
is proved. It uses Gappa 1.8.3 over exact rational interval assumptions and fixed generated
obligations. The operation does not accept Gappa source, hints, Rocq/Coq source, or solver
queries from the caller.

The model has a `schema_version` of `1`, a `request`, `variables`, topologically ordered
`nodes`, an `output` node ID, and a nonnegative rational `error_budget` encoded as
`[numerator, denominator]`. Each variable contains its own closed rational `lower` and `upper`
pair. Rational numerators and denominators are limited to 2,048 bits. Nodes are `var`, `rational`, `round`, `add`, `sub`, `mul`, or `div`. A `round` node
explicitly models conversion of an exact real value to the requested format. Arithmetic nodes
round each exact operation result. Rational constants are exact values until a `round` node or
an arithmetic node consumes them. Variables are exact reals in the declared intervals; the
model does not assert that a variable is already representable. This interval semantics is a
conservative superset when a caller's actual inputs are already representable floats.

For each candidate format, the generated obligation proves that every rounded operation's
pre-round result lies within the largest finite value of that IEEE format, every divisor in
both the rounded computation and real reference is nonzero, and the final rounded result's
absolute difference from the real-reference DAG is at most the requested budget. The
absolute budget is compared as an exact rational inequality. A failed or timed-out proof is
`UNKNOWN`; it is never evidence of a numerical counterexample. A successful single-format
request returns `PROVED_BY_GAPPA` with the exact generated obligation and SHA-256. A
precision-grid request returns `CERTIFIED_FEASIBLE_FORMAT`; it makes no minimality claim.

The arithmetic profile is IEEE-754 radix-2 binary32 or binary64, round-to-nearest ties-to-even,
with gradual subnormals. The model excludes overflow, division by zero, NaNs, infinities,
flush-to-zero and alternate rounding modes. It does not prove correspondence to source code,
compiler settings, instruction contraction, or hardware behavior. Gappa's exit status is
trusted as the proving-engine result; this operation does not generate or check a Rocq/Coq
kernel certificate.

On Linux, install the build dependencies documented by Gappa (`GMP`, `MPFR`, Boost headers,
Flex and Bison), download the official Gappa 1.8.3 source archive, verify SHA-256
`9b3e14e95a5b6aa0ee909b1d1e5c1fac04042024f29b66377cfb9e1923b25f5c`, then run
`./configure && ./remake`. Configure `VKIT_GAPPA_BIN` with the resulting binary path and
`VKIT_GAPPA_SHA256` with that binary's SHA-256. The operation checks both values and the exact
version string before proving. Native binaries are not provisioned by this operation on
Windows or macOS; those hosts return `UNAVAILABLE` until an independently pinned build is
configured.

Example model:

CI builds the [official Gappa 1.8.3 release](https://gappa.gitlabpages.inria.fr/) from its pinned source archive on Linux using `scripts/install_roundoff_engine.py`. Install `build-essential libgmp-dev libmpfr-dev libboost-dev flex bison` first. Configure `VKIT_GAPPA_BIN` and `VKIT_GAPPA_SHA256` from the deployed artifact. The installer is a deployment action; analysis requests never download or compile an engine.

```json
{
  "schema_version": 1,
  "request": {"kind": "bound", "format": "binary32"},
  "variables": [{"name": "x", "lower": [0, 1], "upper": [1, 1]}],
  "nodes": [
    {"id": "x", "op": "var", "name": "x"},
    {"id": "one", "op": "rational", "value": [1, 1]},
    {"id": "difference", "op": "sub", "args": ["one", "x"]},
    {"id": "product", "op": "mul", "args": ["x", "difference"]}
  ],
  "output": "product",
  "error_budget": [3, 67108864]
}
```
