# Semantic merge preservation

`check_semantic_merge(path, timeout_seconds=10)` checks a captured quartet of Python
functions with cvc5 1.4.2. It reuses the installed pure integer/boolean source frontend.
The functions must have the same name, parameter names, annotations and return sort.

```json
{
  "version": 1,
  "function": "score",
  "base": "versions/base.py",
  "left": "versions/left.py",
  "right": "versions/right.py",
  "merged": "versions/merged.py"
}
```

```powershell
vkit compute check-semantic-merge --project . --path merge.json --json
```

For every input in the annotated mathematical integer/boolean domain, the installed
relation requires all of these conditions:

- If left returns a different value from base, merged must return left's value.
- If right returns a different value from base, merged must return right's value.
- If both branches return base's value, merged must also return that value.

Different changes on disjoint input regions can coexist. Identical overlapping changes
can coexist. If both branches change the same input to different return values, the
relation cannot hold for that input. The tool checks the proposed merge; it does not
choose which branch should win or generate a merge.

`PROVED` means no violating input exists in this supported function model. A
`COUNTEREXAMPLE` includes the inputs, all four return values and violated obligations,
after an independent evaluation of the installed IR confirms the solver witness.
Unfinished computation or a replay disagreement returns `UNKNOWN`. Unsupported source
or differing signatures return `UNSUPPORTED`; missing or mismatched cvc5 returns
`UNAVAILABLE`. These results concern scalar returns, not exceptions, mutation, imports,
source-edit intent or whole-repository integration. Project code is never executed.

The operation captures the request and all four sources with SHA-256 digests. Each
source is limited to 64 KiB and 1,000 frontend nodes. The request is limited to 16 KB
and output to 80 KB. An isolated worker deadline covers parsing, encoding and solving;
it defaults to ten seconds and can be raised through sixty. A deadline is not an OS
sandbox or a memory quota.

The relation follows the scalar-return specialization of
[Verified Three-Way Program Merge](https://www.microsoft.com/en-us/research/publication/verified-three-way-program-merge/).
This integration uses vkit's own source model and cvc5, rather than the original
SafeMerge implementation or its general object/state semantics.
