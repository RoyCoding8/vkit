# Pure-function cache-key collisions

`find_cache_key_collision(path, function, key_function, timeout_ms=2000)` asks whether two inputs can share a key while producing different values. Both top-level functions are parsed from the same repository-relative Python file through the existing `FunctionModel` frontend. The operation searches for two independent argument vectors `x` and `y` satisfying:

```text
key(x) == key(y) and function(x) != function(y)
```

The functions must have the same positional parameter sorts; parameter names and the two return sorts may differ. The supported domain is the complete mathematical integer and boolean domain described by their annotations. Supported source remains the existing pure subset: integer and boolean literals, parameters, local assignments, returns, conditionals, arithmetic, comparisons and boolean operations. Calls, global reads, effects, loops, defaults, decorators, and unsupported syntax are rejected. The module is parsed but never imported or executed.

`COLLISION` includes both argument vectors, keys, and results. cvc5's witness is independently evaluated by vkit's IR evaluator; if it does not reproduce equal keys and different values, the operation returns `UNKNOWN`. `NO_COLLISION_WITHIN_MODEL` means cvc5 proved the relational formula unsatisfiable over the entire supported domain. `UNKNOWN` means the bounded solver worker did not finish or could not validate its result. `UNSUPPORTED` covers invalid source or incompatible parameter sorts. `UNAVAILABLE` means the pinned cvc5 1.4.2 backend is not installed or the worker process could not be launched.

The response includes the source SHA-256, backend version, model scope, and either the replayed pair or the proof status. The query runs in an isolated Python worker with a hard wall timeout from 1 to 60 seconds, a 65,536-byte source limit, a 1,000-node frontend limit, and bounded request/result JSON. The solver also receives the same per-query time limit. The optional `engines` extra pins cvc5 to 1.4.2.

This is a result about two pure functions in this model. It is not a proof about a real cache implementation, key serialization or hashing, cache state, callers, mutable objects, clocks, databases, environment variables, or code outside the selected function bodies. `NO_COLLISION_WITHIN_MODEL` must not be presented as general cache correctness.
