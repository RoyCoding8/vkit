# Discover and control tools

Each installed tool declares one primary category. The operator controls availability through the
web console or the existing settings file. Agents filter discovery but cannot enable tools through MCP.

## MCP requests

Compact exposure is the default. Its tool list contains only `help` and `invoke`.

| Request | Result |
| --- | --- |
| `help()` | Enabled categories, labels, and available counts |
| `help(category="safety")` | Tool summaries in that category |
| `help(query="overflow")` | Matching enabled tool summaries |
| `help(category="safety", query="overflow")` | Intersection of category and text search |
| `help(all=true)` | Summaries across all enabled categories |
| `help(name="check_c_safety")` | One exact input schema, description, category, and annotations |
| `invoke(name="check_c_safety", arguments={...})` | The existing handler's result after visibility and schema validation |

Help responses use `contract_version: 2`. Tool lists accept a nonnegative `offset` and a `limit` from
1 to 25, defaulting to 10. Responses contain `total` and `next_offset`. Continue until `next_offset`
is null to list the whole catalog. Results are ordered by category ID and tool name. Settings changes
between pages can change the list. Only an exact name request includes an input schema.

An exact name cannot be combined with other selectors. `all=true` cannot be combined with a category.
Unknown and unavailable names receive the same refusal. A category without available tools is unavailable.
Search uses deterministic text matching over names, descriptions, and schemas.

Invocation applies existing defaults, validation, outcomes, and errors. It cannot target help or itself.
Discovery creates no authorization token or mandatory handshake. The executor accepts no commands or
executable paths of its own. Its `readOnlyHint` is false because some tools run checks, stage proposals,
or exercise test services.

Named exposure advertises enabled tools individually for clients that need separate tool approval controls.
It uses the same handlers and availability rules. Compact mode refuses direct MCP calls to domain names.

## Categories

| ID | Label |
| --- | --- |
| `repository` | Repository checks and evidence |
| `contracts_languages` | Contracts and languages |
| `source` | Source analysis |
| `equivalence` | Equivalence and transformations |
| `safety` | Safety and proofs |
| `testing` | Testing and failure analysis |
| `builds` | Builds and dependencies |
| `data` | SQL and migrations |
| `runtime` | Traces and performance |
| `systems` | Concurrency, policies, and networks |

Current membership comes from help rather than a second documented tool inventory.

## Operator controls

Open **Setup & tools > Project settings > Show tools**. Search or filter by category, switch a category,
or change individual tool choices. Disabling a category overrides individual choices without erasing them.
New tools in that category stay disabled. Restrictions apply to discovery, exact-contract lookup,
invocation, CLI computations, and the console's computation picker.

Choose **MCP exposure** in Project settings. The tool dialog also selects categories for new agent connections.
All enabled categories is the default. A subset narrows the enabled catalog. An empty subset exposes no
domain tools. The operator console can still manage the whole installed catalog.

Save settings, then copy the JSON from **Connect an agent**. Reconnect after changing exposure or scope.
Availability changes apply on the next request. A named-mode client may retain cached definitions until
reconnection, but disabled calls are refused immediately. Neither mode erases existing conversation content.

The existing `<git common directory>/vkit/config.json` stores `disabled_tools`, `disabled_categories`,
`mcp_exposure`, and `mcp_categories` alongside display preferences. Missing new fields default to no disabled
categories, compact exposure, and all enabled categories. `mcp_categories` is null for all categories or an
array for a subset. MCP exposure and scope are fixed at process startup. Help filters cannot widen that scope.
The settings file stays outside commits. Enabling a tool does not install its runtime.

CLI controls include `vkit tools --disable-category safety`, `--enable-category safety`, `--disable NAME`,
and `--enable NAME`. `vkit tools --category safety` lists summaries. Explicit
`vkit mcp serve --exposure named --categories repository safety` overrides saved startup defaults.
`--categories all` includes all enabled categories. Empty `--categories` exposes no domain tools.

## Extension boundary

Declare a valid category beside every installed `Operation` or core `Tool`. Help and invoke are reserved
infrastructure names and cannot be shadowed by computations. Installed metadata generates category groups
and counts. `Settings.allows` owns category and individual availability.
See [Add a built-in operation](adding-operations.md) for the remaining contract.
