# Plan 11: checked cleanup and automatic hooks

Status: ready for implementation after Plan 10's shared evidence contract exists.
Read [the proposed contract extension](NEXT-CONTRACT.md), [Plan 10](10-native-verifiers.md), and [the worker workflow](../MASTER-PLAN.md#skills-and-parallel-work). Record checkpoint evidence in the master plan.

## Deliver automatic cleanup with a stated guarantee

Remove approved incidental inline comments and narrowly defined logic bloat from agent edits. Apply a transformation only after an independent preservation check passes. Use Plan 10's evidence and current ownership records.

Start with Python. JavaScript and TypeScript stay unsupported for automatic cleanup until a separate parser and preservation adapter exists. The verification engine still supports Node tests. Language support is a cleanup capability, not a reason to remove ordinary verification.

Editorial judgment and preservation are separate. A policy identifies removable comments. The checker establishes the supported preservation claim. Passing tests or an agent saying that code is redundant never establishes equivalence.

## Reuse the current integration

Trace `plugin/scripts/vkit_hook.py`, `plugin/hooks/hooks.json`, host-to-task binding, `tasks.verify_ownership_in`, source inventory, execution, and finalization. Current hooks cover session/subagent start, MCP pre-tool use, stop, and task completion. There is no cleanup hook yet.

Add one small cleanup implementation under the vkit package. CLI, MCP, hooks, and the dashboard call the same functions. Do not add a watcher, background formatter daemon, second lock database, or model call.

Define explicit preview and apply operations. Proposed CLI:

```text
vkit cleanup preview --project ROOT --task TASK --path RELATIVE_FILE
vkit cleanup apply --project ROOT --task TASK --proposal PROPOSAL_ID --request-id KEY
```

These commands do not exist at the baseline. Use bounded structured results containing the patch, rule IDs, preservation claim, digests, and refusal reason. If agents need explicit access, add one typed `cleanup` MCP tool with discriminated preview/apply actions. Do not accept arbitrary transformation scripts or raw filesystem roots.

## Define the initial rules

### Ordinary inline comments

Use Python's standard-library tokenizer to locate comments. Apply surgical byte edits rather than regenerating a whole file. Preserve encoding, newline style, line count, and unaffected bytes.

Default automatic scope is approved ordinary trailing comments on lines changed by the bound task. Preserve standalone explanations, shebangs, encoding declarations, licenses, type comments, type-ignore directives, lint/formatter/coverage/security directives, and explicit keep markers. Preserve comment-like text inside strings and all docstrings. Allow project-specific protected directives.

Parse before and after with `type_comments=True`, compare the AST including directive information, and compile both files. Require identical protected directives and executable content. Python [tokenization](https://docs.python.org/3.13/library/tokenize.html) identifies comments, and [AST parsing](https://docs.python.org/3/library/ast.html) can retain type-comment information.

Unknown directive-shaped comments stay unchanged. A malformed file returns a preview refusal and remains untouched. Never delete comments with regular expressions.

### Narrow logic bloat

Initial candidates are empty `else: pass` branches and redundant `pass` statements in otherwise nonempty statement lists. The checker independently validates that the patch contains only the registered syntactic transformation.

Require equality of the compiled executable representation before automatic application. Compare the entire module and every nested code object, including bytecode, constants with their exact types, names, argument counts, variable/free/cell names, flags, stack sizes, and exception tables. Keep the compiler version and flags in the receipt. Support only explicitly tested CPython versions. Unknown code-object fields or unsupported versions cause refusal.

Compare constants by exact representation, including floating-point bits and recursively nested constants. Ordinary Python equality can conflate values such as `0.0` and `-0.0`. It must not certify those constants as identical.

Exclude only explicitly documented source-location metadata from this comparison. Validate at optimization levels 0, 1, and 2. Do not normalize away NOPs, jump changes, constants, or exception handling to make a patch pass. If executable fields differ, leave the patch as a suggestion.

A small baseline probe on CPython 3.13.14 found an empty `else: pass` example with equal executable fields. An interior redundant `pass` example changed bytecode. This is feasibility evidence for one case, not acceptance for the rule implementation.

Guard against docstring promotion. Removing `pass` before a leading string expression can change `__doc__`. Protect annotations, future imports, local binding, closures, and public definitions. Do not generalize to dead-branch deletion, algebraic identities, imports, duplicate calls, boolean simplification, or unused functions.

Compiled equality is a scoped preservation check under the recorded compiler and runtime assumptions. Python code can inspect its source and code metadata or use tracing. Excluding location metadata does not preserve every observable behavior. Enrollment must show this scope. Projects requiring those observations use preview-only cleanup.

Deeper refactors need a new, explicit equivalence backend or certified rule with a checked connection to the actual language. A theorem about a simplified Lean syntax tree alone cannot authorize arbitrary Python refactoring.

## Apply safely

The owner enables automatic cleanup once through approved project policy. Use explicit modes `off`, `preview`, and `apply_verified`, plus enabled rule IDs and path exclusions. Enabling a mode does not authorize stripping every comment in the repository.

Bind proposals to task generation, approved cleanup policy, path, before bytes, after bytes, rule/checker identity, and preservation receipt. Validate ownership and the current before digest immediately before applying. Use existing claims for participating writers. Reject path escapes, symlinks, unrelated working changes, generated files, and edits outside the task's scope.

Write through a same-directory temporary file and replace only after validation. Preserve required file metadata and the original bytes as an artifact. Never automatically roll back over a later edit. A crash after replacement must be recoverable by comparing actual before/after digests. Repeating the same request converges to the same result.

Atomic replacement is not an operating-system compare-and-swap against arbitrary external writers. State the participating-writer assumption. Do not claim protection against uncoordinated editors from the app's resource claims alone.

## Wire hooks and authoritative checks

Add a fast `PostToolUse` hook for supported file editing tools. Normalize Windows paths and resolve the current host binding before acting. If the edit cannot be attributed to a task, leave it unchanged and return a bounded explanation.

Claude's [hook reference](https://code.claude.com/docs/en/hooks) states that Edit/Write hooks do not cover Bash or external-process edits. Therefore hooks accelerate cleanup, while the shared pre-verification path checks all relevant changed files, including task-owned untracked files.

Apply verified cleanup before capturing a run's source identity. If a required cleanup remains pending or fails, block the check with its concrete reason. An unapproved optional suggestion does not become a mandatory failure.

Finalization performs a non-mutating freshness check. Stop hooks never change files after evidence was collected. Every cleanup write invalidates earlier evidence through the existing source inventory.

Protected integration checks are non-mutating. They must verify the exact candidate revision. If required cleanup is missing, report the gap and require a new candidate commit. Never clean a checkout and then attest to its original commit.

Hooks run no model, network call, full suite, or Lean build. Keep them within the existing hook budget. Unsupported files and timeouts remain unchanged. Prevent repeated-hook loops through idempotent before/after identities.

## Build in checkpoints

1. Add preview and receipt validation for Python comments. Exercise malformed files and protected directives before any automatic write.
2. Add guarded apply and the restricted compiled-equality logic rules. Verify idempotency, conflicts, and crash recovery.
3. Connect fast hooks and the shared pre-verification check. Verify source invalidation and non-mutating integration behavior.
4. Expose modes, exclusions, receipts, and refused suggestions in Plan 12's dashboard.

## Acceptance exercises

| Exercise | Required result |
| --- | --- |
| Ordinary approved trailing comment on a task-owned changed line | Removed with a preservation receipt |
| String, docstring, license, type-ignore, unknown directive, or keep marker | Preserved |
| Empty else instance with identical executable fields | Can apply under the scoped policy |
| Redundant pass changes bytecode or promotes a docstring | Cannot automatically apply |
| Tampered receipt or change to constants, exception table, closure, or binding | Refused |
| Another task owns the file, or before bytes changed | No overwrite |
| Same proposal/request is repeated | Same result without another edit |
| Change arrives through a shell command | Shared pre-verification check catches relevant pending cleanup |
| Cleanup follows a successful earlier verification | Earlier evidence cannot finalize changed source |
| Protected candidate requires cleanup | Candidate stays unchanged and cannot pass that requirement |
| Missing host binding, unsupported runtime, malformed source, or timeout | File remains unchanged |

Use small local Python checks and GitHub CI for the broad suite. Exercise an actual supported hook payload and distinguish replayed payload checks from live host behavior. No WSL or local full suites.

## Handback

Provide commits, rule coverage, observed refusals, receipts, tested runtime versions, CI SHAs, and the exact preservation scope. Do not claim universal equivalence or complete slop removal. Do not silently add JavaScript cleanup to finish a checklist.
