# Concrete Python behavioral differences

`find_behavioral_difference(path, timeout_seconds=10)` searches for a concrete
behavior difference with CrossHair 0.0.111. Every reported witness is replayed twice
in fresh CPython processes without CrossHair. The result observes returns,
exception types/messages, and arguments after execution.

```json
{
  "version": 1,
  "function": "score",
  "before": "def score(xs: list[int]) -> int:\n    return sum(xs)\n",
  "after": "def score(xs: list[int]) -> int:\n    return len(xs)\n"
}
```

```powershell
vkit compute find-behavioral-difference --project . --path behavior.json --json
```

This is a closed Python language profile, checked before a worker starts. Each
source must contain exactly one plain function. Both functions need identical
parameter names, order and annotations. Parameters and return annotations can be
`int`, `bool`, `str`, or `list[int]`. Up to four parameters are allowed.

The profile admits local assignments, branches, `for`/`while` loops, indexing,
slicing, list literals, integer and string operations, and local list `append`.
Installed calls are `abs`, `len`, `range`, `min`, `max`, `sum`, and `sorted`.
Imports, global/free names, recursion, arbitrary calls or attributes, decorators,
defaults, async code and expanded call arguments are rejected. It does not import
a repository module or execute its surrounding statements. Arbitrary application
code continues to require the existing owner-approved check mechanism.

`COUNTEREXAMPLE` includes exact concrete inputs, observations and `replay_count: 2`.
`UNKNOWN` means no replayable difference was established, including unfinished
search. It never means equivalence. Search suggestions that fail concrete replay
cannot become counterexamples. No smallest-input guarantee is made.

Input is capped at 40 KB, each function at 16 KB and 1,000 AST nodes. Replayed
inputs/outputs admit integers through 256 bits, strings through 256 characters,
integer lists through 64 elements, booleans and `None` return values. Exception
messages are capped at 1,000 characters. Evidence is capped at 40 KB. Search has
fixed path/iteration budgets. The total deadline covers search and both replays,
defaults to ten seconds, and can be raised through 60.

Worker processes use service permissions. The accepted language cannot import or
call filesystem/network/process APIs. A timeout is a resource limit, not an OS
sandbox or a heap limit. Unsupported syntax returns `UNSUPPORTED`; an absent or
mismatched CrossHair installation returns `UNAVAILABLE`.

The upstream search semantics and caveats are in [CrossHair's differential behavior documentation](https://crosshair.readthedocs.io/en/latest/diff_behavior.html).
