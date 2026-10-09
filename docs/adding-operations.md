# Add a built-in operation

Add an operation by implementing its fixed question and declaring its contract in the installed catalog.
The CLI and MCP derive their inputs and dispatch from that declaration.

## Define the question

Write down the exact question, supported inputs, model assumptions, resource limits, and possible outcomes.
Distinguish a counterexample from an undecided search and an unavailable backend.
Reuse or extend an existing operation when it answers the same question under another built-in model.

Keep solver rules, model implementations, backend selection, and witness validation inside the service.
Accept candidate data through the request. Do not accept commands, import paths, or executable model code.

## Implement the handler

Place the implementation under `src/vkit/operations/`.
Expose a handler that takes the repository root followed by keyword inputs and returns a JSON object.
The root comes from vkit, not from an operation argument.

Import optional engines inside the handler or its execution process.
Listing tools and displaying CLI help must work without those engines installed.
Keep timeout enforcement and backend identity checks inside the operation that owns them.
The common contract does not implement a solver or impose one subprocess protocol on every backend.

Return a `status` together with the operation's evidence, model scope, and limitations.
Validate any claimed witness through the operation's independent checker when one is available.
Preserve an undecided result when the backend cannot establish an answer.

## Declare the operation

Add one `Operation` to `OPERATIONS` in `src/vkit/operations/catalog.py`.
Declare the contract version, unique name, description, input schema, defaults, fixed handler, read-only
behavior, and allowed result statuses with their exit codes.
Declare any CLI file input and its byte limit in the same entry.

Use `FileInput(flag="replacement-file", max_bytes=65536)` for the existing bounded replacement source input.

The contract type and shared adapters live in `src/vkit/operations/contract.py`.
Use `version=1`. The registry rejects unsupported contract versions and duplicate operation names.
Input names use underscores. The generated command and flags replace underscores with hyphens.
Put defaults in each property's JSON Schema `default` field so CLI and MCP use the same values.

For example, the existing regex comparison declares these inputs and outcomes.

```python
Operation(
	version=1,
	name="compare_matchsets",
	description="Compare full-match regex languages over the supplied finite alphabet.",
	properties={
		"old_pattern": {"type": "string", "maxLength": 2048},
		"new_pattern": {"type": "string", "maxLength": 2048},
		"alphabet": {"type": "string", "maxLength": 64},
		"timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60000, "default": 2000},
	},
	required=("old_pattern", "new_pattern", "alphabet"),
	handler=_compare_matchsets,
	outcomes={"EQUIVALENT": 0, "COUNTEREXAMPLE": 1, "UNKNOWN": 3, "UNSUPPORTED": 2, "UNAVAILABLE": 5},
)
```

`_compare_matchsets` adapts the existing result object to the shared JSON handler contract.
A handler that already accepts the root and returns a JSON object needs no forwarding wrapper.

Do not add an operation-specific branch to the CLI or MCP.
The existing `--replacement-file` option is a bounded UTF-8 file adapter for the `replacement` input.
Other string inputs use their text directly unless the contract declares a file adapter.

Declare outcomes explicitly. A missing or undeclared status is an internal contract error, not an
undecided result. CLI reports exit code 4, and MCP returns an error.
The shared contract checks the result object, declared status, and JSON serialization.
Each operation owns the correctness and structure of its evidence fields.

Use string, integer, number, boolean, array, or object schemas for top-level inputs.
Boolean, array, and object CLI values use JSON text, such as `--enabled false` or `--items '[1, 2]'`.
MCP accepts the corresponding JSON values directly.
Nested JSON Schema constraints remain part of the input schema.

## Verify the addition

Add behavior tests for the question and its evidence.
Exercise the generated CLI and MCP entry, including missing inputs, unknown inputs, defaults, timeout,
unsupported data, and an unavailable backend where those cases apply.
Check that computation leaves source files and gate evidence unchanged.

Run `uv run pytest tests/test_operation_contract.py` and the operation's tests before integrating the change.
Configure and exercise the real pinned backend in CI when the operation requires one.
Update [the computation reference](computations.md) with the exact supported model and deployment setup.

## Install the trusted code

Release the implementation and catalog together as part of vkit.
The catalog is an explicit list in the installed package. Candidate repositories cannot register handlers.
There is no request-time plugin loader or repository plugin directory.
Keep the installation and backend configuration under the service owner's control as described in
[Protect the service](computations.md#protect-the-service).
