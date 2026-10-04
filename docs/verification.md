# Verification reference

## Registered checks and evidence

`verification/manifest.json` defines available checks. Version 1 supports
scenario drivers. Version 2 supports scenario, pytest, Node test, property,
Lean, and TLC checks. The [schemas](../schemas/manifest.v2.json) define each
variant and its required fields.

Commands are argument arrays. An agent selects a registered check ID.
It cannot supply an arbitrary command through the run API. Registered commands
still execute repository code and need owner review.
The supported command placeholders are `{{run_dir}}` and `{{python}}`.
Substitution inserts the run directory or resolved interpreter without shell
evaluation. The core reads structured artifacts instead of treating printed
prose as proof. It determines the evidence category from the registered check
and obligations.

| Outcome | Meaning |
| --- | --- |
| PASS | The checker completed successfully and its validated evidence satisfies the required obligations. |
| FAIL | Valid evidence reports a required obligation failed. |
| BLOCKED | Missing tools or evidence, malformed output, timeout, cancellation, changed inputs, or a refused capability prevents a decision. |

An exit code alone does not establish PASS. Scenario drivers must publish their
versioned artifact. Native adapters interpret their own checker output and
produce typed receipts. Reports record their source identity and check scope.

Scenario and test results cover named observations. Property results cover
the recorded generator settings and samples. TLC results cover the model and
finite configuration. Lean results cover the admitted theorem and assumptions.
These categories are not interchangeable. Model and theorem results require
separate evidence to establish correspondence with application code.
The core computes verdicts. Clients cannot supply an accepted verdict.
Terminal records are immutable, and report publication is atomic. SQLite
stores durable task and run identities. Source identity uses measurements
before and after a run and can miss an edit made and reverted between them.

## Managed tasks

Create a contract file with registered IDs, for example:

```json
{
  "description": "Verify the totals change",
  "required_checks": ["totals-behavior"]
}
```

Then use the returned IDs in this sequence:

```powershell
vkit task begin --project <repo> --contract <contract.json> --request-id <begin-id>
vkit check start --project <repo> --task <task-id> --check totals-behavior --request-id <start-id>
vkit run show --project <repo> --run <run-id>
vkit task finalize --project <repo> --task <task-id>
```

`check start` returns before completion. Read the run until it is terminal.
Use the same request ID to retry the same operation. Use a new ID for a new
operation. The approved policy's mandatory checks remain required even when
the caller requests fewer checks.

The managed lifecycle pins contracts and policy identities, records resource
ownership, and rejects stale attempt generations. Evidence must satisfy the
current task's requirements. An old PASS cannot automatically make a new task
ready. Parallel workers must declare overlapping resources and use this
lifecycle for its ownership guarantees to apply.

The agent host chooses models and dispatches workers. vkit manages declared
verification and resource ownership. It does not call models or provide a
second worker scheduler.

Cancel a run with `vkit run cancel --project <repo> --run <run-id>
--request-id <cancel-id>`. `vkit recover --project <repo>` inspects interrupted
state. Run `vkit recover --help` for supported actions and their evidence
arguments.

## Cleanup

Configure `verification/cleanup.json` before admitting a task:

```json
{
  "mode": "preview",
  "enabled_rules": [
    "ORDINARY_TRAILING_COMMENT",
    "LOGIC-EMPTY-ELSE-PASS",
    "LOGIC-REDUNDANT-PASS"
  ],
  "excluded_paths": [],
  "python_version": "3.13.14",
  "optimize_levels": [0, 1, 2]
}
```

`off` disables cleanup. `preview` inspects supported changes without writing.
`apply_verified` permits guarded writes after preservation checks and task
ownership checks. An absent policy defaults to off. Editing the policy after
admission invalidates its pinned identity.

The comment rule removes eligible ordinary trailing comments. It preserves
directives and protected comments. Logic rules remove supported empty else
branches and redundant pass statements. Logic checks compare compiled output
at optimization levels 0, 1, and 2 on the approved CPython version.
The guarantees cover the registered transformations and exclude source-text
observations such as line-number changes.

Claude edit hooks inspect supported tool events. Shared pre-verification also
inspects changed files, including edits made through shell tools. The dashboard
shows cleanup records, preservation receipts, and refusals. JavaScript and
TypeScript cleanup are unsupported.

## Agent hosts and hooks

The MCP server uses the optional `mcp` SDK and can serve a client independently
of Claude Code. A successful protocol exchange establishes that client and
server connection, not that another host has installed the plugin.

Claude session hooks use explicit registered session and agent bindings. They
do not infer ownership from timestamps, recent files, or the latest task.
Completion hooks read local records without launching checks or models.
An unbound session has no managed task completion gate. Cleanup hooks use the
registered binding and approved policy before considering an edit.

## Integration

```powershell
vkit integration verify --project <repo> --candidate <commit> --target <base-commit> --policy @<approved-ref>
```

Integration checks one exact candidate against the target and approved policy.
A local policy path produces local evidence and cannot authorize protected
integration. The [CI example](../examples/ci/integration.yml) illustrates how
to connect this to a repository's GitHub workflow. It is a template, not evidence
that protected integration is enabled for your repository.

Native adapters are not yet supported in protected integration. Unreviewed
Lean proofs require an isolated comparator this beta does not provide.
The dashboard's saved verification proposals do not yet change the effective
manifest used by admission and execution.

## CLI output and exit codes

Add `--json` to supported commands for machine-readable output. Diagnostics go
to stderr. Use each command's `--help` for its arguments.

| Code | Meaning |
| --- | --- |
| 0 | The requested operation succeeded. A foreground check passed; starting a background run does not establish its outcome. |
| 1 | A completed check failed. |
| 2 | Invalid request or configuration. |
| 3 | BLOCKED verification. |
| 4 | Internal error. |
| 5 | Unsupported or unavailable capability. |
