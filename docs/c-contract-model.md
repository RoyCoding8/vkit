# Scalar C contract proofs

`prove_c_contract` captures a C source and a JSON contract, generates an owned ACSL harness, and runs Frama-C 33.0 WP with its built-in Qed prover. It can prove loop properties beyond a finite unwind bound. `PROVED` requires every reported proof obligation to be valid, a completed proof summary, expected postcondition and invariant goals, and a feasible typed input satisfying the precondition. An unfinished or unproved obligation returns `UNKNOWN`, with available obligations and diagnostics. It is not a counterexample. `INFEASIBLE_PRECONDITION` identifies a contradictory input domain rather than presenting a vacuous proof.

The JSON file has exactly these fields:

```json
{
  "schema_version": 1,
  "source": "counter.c",
  "function": "count",
  "requires": ["0 <= n <= 1000000"],
  "ensures": ["result == n"],
  "loops": [{"invariants": ["0 <= i <= n"], "variant": "n-i"}]
}
```

For `int count(int n) { int i=0; while(i<n) { i++; } return i; }`, this proves the postcondition, invariant establishment and preservation, decreasing nonnegative loop variant, assigns clauses and signed-overflow guard. Loop entries follow source AST preorder. Each requires/ensures/invariants list contains one to eight expressions; each loop needs an integer variant. Up to eight loops and sixteen parameters are supported.

Expressions use vkit's typed mathematical integer/boolean grammar: literals, names, `+`, `-`, `*`, comparisons, `and`, `or`, `not`, and conditional expressions. Arithmetic in ACSL contracts is mathematical; integral C variable values are promoted to mathematical integers. C execution retains machine arithmetic semantics. `result` denotes the return value. Preconditions can reference parameters; postconditions can reference parameters and `result`; loop conditions can also reference locals. Parameters are immutable in this profile. A cvc5 1.4.2 feasibility witness is independently evaluated before WP runs. Signed inputs range over int32, unsigned inputs over uint32, and `_Bool` inputs over booleans.

Source contains exactly one comment-free, preprocessed function with scalar `int`, `unsigned int`, or `_Bool` parameters, locals and return. Local declarations precede statements, have explicit initializers, and cannot shadow names. The function ends with an explicit return. Headers, macros, arbitrary ACSL, axioms, calls, pointers, arrays, globals and verifier overrides are rejected. `result` and `vkit_` identifiers are reserved. The fixed machine model is x86_64 with 32-bit integers. Frama-C's two standard warnings about unsupported pointer-alignment and function-pointer guards are inapplicable to this pointer-free profile; other warnings prevent `PROVED`. This is a trusted WP/Qed result, not an independently checked proof-kernel artifact or confirmation that the submitted contract captures the user's intended requirement.

Install the Linux x86_64 runtime with `python scripts/install_contract_engine.py --dest /operator-owned/frama-c-33`. The installer checks the official bundle's fixed SHA-256 and extracts with `--noexec`, avoiding its system installation script. Configure the returned `VKIT_FRAMAC_BIN` and `VKIT_FRAMAC_SHA256` in the verifier service. The full bundle, plugins, shared libraries, parser and solver belong to the protected verifier deployment; the executable digest alone does not protect a same-user editable installation. Requests do not download engines, invoke project build scripts, or load project proof configuration.

Contract and source captures are each limited to 64 KiB, source AST to 2,048 nodes, expression trees to 256 nodes/depth 32, constants to 256 bits, and combined contract expansion to 4,000 nodes. The public timeout is 1–60 seconds; subprocesses share its remaining budget. cvc5 has an additional one-second query limit. Raw logs/report and the returned response each have a 256 KiB cap. The response includes original-byte digests, generated harness digest and text, runtime identity, admissible input, obligations and diagnostics.

References: [Frama-C WP](https://www.frama-c.com/fc-plugins/wp.html), [ACSL integer arithmetic](https://www.frama-c.com/download/acsl-implementation-18.0-Argon.pdf), [Frama-C 33.0 Linux bundle](https://www.frama-c.com/download/frama-c-linux-x86-64-33.0-Arsenic.run).
