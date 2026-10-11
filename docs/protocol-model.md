# Finite protocol models

`check_protocol_model` exhaustively checks a caller-supplied finite transition system with the
repository's pinned TLC runner. It answers properties about that JSON model. It does not execute
the service, inspect its source, or establish that the model matches a deployed implementation.
The model author owns that correspondence.

The input is a repository-local UTF-8 JSON file, at most 65,536 bytes. Version 1 has this shape:

```json
{
  "schema_version": 1,
  "variables": {
    "seen": {"domain": [false, true], "initial": [false], "durability": "durable"},
    "volatile_cache": {
      "domain": [0, 1], "initial": [0], "durability": "volatile", "restart": 0
    }
  },
  "transitions": [
    {
      "name": "first_delivery",
      "guard": {"not": {"var": "seen"}},
      "updates": {"seen": {"bool": true}}
    },
    {
      "name": "duplicate_delivery",
      "guard": {"var": "seen"},
      "updates": {"seen": {"bool": true}}
    }
  ],
  "invariants": [
    {"name": "cache_is_bounded", "expression": {"le": [{"var": "volatile_cache"}, {"int": 1}]}}
  ],
  "crash_recovery": true,
  "check_deadlock": true
}
```

Variables have a nonempty finite domain and a nonempty initial subset. Values are 64-bit-range JSON
integers, booleans, or short ASCII strings containing letters, digits, `_`, `.`, `:`, `/`, or `-`.
There are at most 12 variables, 16 values per domain, 32 transitions, and 32 invariants. The product
of domain sizes, including the process-running bit when crash recovery is enabled, may not exceed
1,000,000 states. Exact independent saturation is limited to 50,000 reachable states and 1,000,000
transition visits. The requested time limit is shared by saturation and TLC. Exceeding any limit
returns `UNKNOWN`; it cannot silently remove a state or transition.

Expressions use one-key JSON objects. A variable is `{"var":"seen"}`. Literals use `int`, `bool`,
or `string`. Operators are `not` with one expression, and `add`, `sub`, `mul`, `eq`, `ne`, `lt`,
`le`, `gt`, `ge`, `and`, and `or` with a two-item expression array. The checker type-checks each
guard, update, and invariant before generating TLA+. It maps caller names to generated identifiers
and accepts no TLA+ text, solver query, module name, or engine argument from the model.

Each transition guard reads the current state. Every update expression also reads the same
pre-transition state, and all assigned variables change simultaneously. Variables omitted from an
update remain unchanged. Transitions with an update that leaves the declared domain remain enabled.
TLC checks the domain invariant and returns a domain-escape counterexample with an independently
replayed trace. The checker never turns an out-of-domain update into a disabled transition.

When `crash_recovery` is true, the model includes a running bit and two fixed transitions. `Crash`
stops the process while preserving every variable's value. While stopped, user transitions cannot
run, and invariants still inspect the preserved state. `Restart` resumes the process, preserves
durable values, and assigns each volatile variable its declared `restart` value. Further crashes
and restarts remain possible. This is the declared state-reset relation, not an operating-system
crash or storage-device durability guarantee. User transitions can encode retries, duplicate
delivery, and interruption handling as ordinary guarded atomic steps.

When `check_deadlock` is true, every reachable running state must enable at least one user
transition. A stopped state is considered able to progress through the fixed restart action. The
fault-injection crash transition does not count as protocol progress, so it cannot hide a deadlock.
Turn this check off when a terminal state is an intended result, or model an explicit completion
transition and state. The service checks only state invariants and this optional enabledness
property. It does not check fairness, liveness, timing, message loss probabilities, or unbounded
execution.

`EXHAUSTIVE` requires both the pinned TLC run and an exact independent breadth-first saturation of
the same generated finite model to complete. The reference saturation stores full typed state tuples
and checks equality after hashing; it does not rely on TLC's probabilistic state fingerprints. Every
reachable state is checked for the domain invariant, each caller invariant, and optional deadlock
condition. TLC's distinct reachable-state count must match the reference count. `COUNTEREXAMPLE`
includes a trace independently replayed against the declared transition semantics. `DOMAIN_ESCAPE`
identifies an enabled update outside a declared domain. `UNKNOWN` covers either engine/reference
disagreement, state or transition limits, timeouts, incomplete exploration, malformed reports, and
traces that fail replay. `UNAVAILABLE` means the configured `VKIT_TLA2TOOLS_JAR` is missing or does
not match the engine SHA-256 pin. Unsupported input shapes return `UNSUPPORTED`. The operation
records the model digest, TLC version, jar digest, exact reachable-state count, and depth for a
completed run.
