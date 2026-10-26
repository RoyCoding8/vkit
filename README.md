<p align="center"><img src="docs/assets/logo.svg" width="80" height="80" alt="VKit"></p>

# VKit

Verification infrastructure for coding agents, through CLI and MCP.

## What VKit does

| Capability | Behavior |
| --- | --- |
| Verification jobs | Run accepted definitions against frozen input files. |
| Owned mathematical package | Check six declared resource-ledger operations against fixed accounting, ownership and atomic-batch contracts. |
| External verifiers | Connect a producer and a separately pinned checker through one protocol. |
| Evidence | Retain inputs, artifacts, checker identity, and receipts. |
| Freshness gates | Check whether intact passing evidence still applies to the current software. |
| Agent access | Use the same job service through CLI and MCP. |

VKit owns the resource-ledger model and expected contracts. An agent submits a component and candidate revision; VKit captures source, checks the accepted verifier, proves the model and candidate, and checks a separate unchanged client against all six bound operations. The first package uses Verus. Its result has `trusted_engine` assurance, not an independently checked kernel certificate.

The separate external-verifier protocol retains the producer/checker distinction illustrated in [Anthropic's formal verification research](https://www.anthropic.com/research/formal-verification). A producer's success message cannot establish the claim. Each method records its own scope and trust boundary.

The computation catalog is retired and retained in Git history. The core consists of jobs, verifier adapters, receipts, and gates.

## Start here

| Goal | Read |
| --- | --- |
| Understand the system | [Architecture](docs/architecture.md) |
| Understand what a result proves | [Verification semantics](docs/verification.md) |
| Add an external verifier | [Backend protocol](docs/backend-protocol.md) |
| Connect and configure an agent | [Tool discovery](docs/tool-discovery.md) |
| Try an existing project | [Python CLI](examples/python-cli/README.md), [Node CLI](examples/node-cli/README.md), [HTTP service](examples/node-http/README.md), or [Lean proofs](examples/lean-proof/README.md) |
| Inspect the refactor and its evidence | [Session report](TASKS.md) |
| Understand the owned model and exact implementation scope | [Formal core plan](PLAN.md), [two API interpretations](examples/ledger-reuse/README.md) |
| Gate a combined merge candidate | [Protected CI example](examples/protected-ci/README.md) |

## Install

| Requirement | Purpose |
| --- | --- |
| Python 3.11+ | Run VKit. |
| Git | Identify the repository and store verification state. |
| [uv](https://docs.astral.sh/uv/) | Install the package and dependencies. |
| Verifier tools | Install separately for the checks you register. |

```sh
uv sync --extra mcp
uv run vkit doctor --project /path/to/repository
```

`doctor` reports missing declared prerequisites. Native adapters support Lean, TLC, scenario, pytest, property, Node test, and static-analysis checks. Their guarantees differ.

## Run a verification job

1. Define checks or formal components in `verification/manifest.json`, schema version 3. Use an [example](examples/), the [formal component shape](schemas/manifest.v3.json), or the [external protocol](docs/backend-protocol.md).
2. Review and accept the definition and checker identity.
3. Submit the job, wait for its result, and inspect the gate.

```sh
uv run vkit accept --project /path/to/repository
uv run vkit check run --project /path/to/repository --check proof --detach --json
uv run vkit run wait --project /path/to/repository --run RUN_ID --json
uv run vkit gate --project /path/to/repository --json
```

| Operation | Behavior |
| --- | --- |
| `check run` | Submit and wait for completion. |
| `check run --detach` | Capture inputs, launch a worker, and return. The worker survives client exit. |
| `run show` | Read the attempt and bounded logs. |
| `run cancel` | Stop the process tree. |
| `run retry` | Create a new attempt for the same frozen job. |

| Identity | Meaning |
| --- | --- |
| `job_id` | Content-derived identity for the accepted definition, inputs, and checking environment. |
| `run_id` | Unique execution attempt. |
| Receipt | Binds the outcome, obligations, input digests, checker identity, and retained artifacts. |

Changed inputs, checkers, policies, artifacts, or receipts invalidate passing gate evidence. Checker changes also require acceptance again.

For a configured formal component, use the same lifecycle:

```sh
uv run vkit accept --project /path/to/repository --component allocation
uv run vkit verify allocation --project /path/to/repository --candidate HEAD --wait 0 --json
uv run vkit run wait --project /path/to/repository --run RUN_ID --json
```

The initial `verus-linux-x86_64-v1` profile requires Linux x86_64, Docker, the pinned official Verus archive and expanded tools, and matching Rust/Cargo homes. Host configuration supplies `VKIT_VERUS_IMAGE`, `VKIT_VERUS_ARCHIVE`, `VKIT_VERUS_ROOT`, `VKIT_RUSTUP_HOME` and `VKIT_CARGO_HOME`. Missing or changed assets block verification. `--working-tree` captures provisional source; committed CI uses exact Git objects.

## Export and recheck evidence

```sh
uv run vkit evidence export --project /path/to/repository --run RUN_ID --output /path/to/bundle
uv run vkit evidence replay --project /path/to/repository --bundle /path/to/bundle
```

| Replay stage | Action |
| --- | --- |
| Validate | Check bundle integrity and match current inputs, specification, and checker identity. |
| Capture | Copy the bundle before launching a worker. |
| External check | Run only the checker on retained proof artifacts. |
| Lean or TLC check | Rerun the native checker on frozen proof sources or the model. |
| Formal component | Independently recapture the candidate and rerun model, candidate and fixed expected-client verification. |
| Publish | Create a new attempt and receipt. Stored success flags never satisfy replay. |

Replay requires matching accepted policy and tool identity. Formal jobs bind tool bytes rather than host paths. Registered-check path requirements depend on their adapter. Bundles are not signed attestations.

## Add a verifier

An `external` manifest entry declares the following:

| Declaration | Contents |
| --- | --- |
| Claim | Specification, obligations, assumptions, and model scope. |
| Inputs | Complete declared source and dependency closure. |
| Commands | Pinned producer and pinned checker, including executable and code-dependency SHA-256 hashes. |
| Evidence | Artifact names and the requested assurance label. |

The checker receives a VKit-generated request and returns a versioned receipt bound to that request and the evidence digests.

- Adding a backend requires no CLI, MCP, or console registry changes.
- The [backend protocol](docs/backend-protocol.md) defines the commands and receipt format.
- Python fixtures test orchestration. Real CI separately exercises Lean, TLC and the installed Verus component procedure.

## Connect an agent

```sh
uv run vkit mcp serve --project /path/to/repository
```

| Interface | Use |
| --- | --- |
| Compact MCP | Exposes `help` and `invoke`. Use `help(all=true)` to discover tools. |
| Exact schema | Use `help(name="check_run")`. |
| Formal request | Use `help(name="verify")` for component, goal and candidate fields. |
| Named MCP | Add `--exposure named` to advertise enabled tools individually. |
| Acceptance | Remains a CLI action. Agents can submit registered checks and propose definitions. |
| Claude Code | The optional [plugin](plugin/) supplies guidance and hooks. |

See [discovery and settings](docs/tool-discovery.md) for availability controls. An agent with shell access can run `accept`; MCP restrictions are not an operating-system security boundary.

## Open the local console

```sh
uv run vkit console --project /path/to/repository
uv run vkit tui --project /path/to/repository
```

| Component | Purpose or location |
| --- | --- |
| Console | Jobs, logs, gates, features, and settings. |
| Terminal launcher | Selects a repository and owns its console server. |
| Checkout wrappers | `start.bat` and `start.sh`. |
| Launch defaults | Platform user config directory, or the file selected by `VKIT_CONFIG`. |
| Repository settings | Git common directory. |
| Override order | Explicit launch flags override defaults. |

## Trust and scope

| Evidence or mechanism | Boundary |
| --- | --- |
| Formal proof | Establishes the accepted statement under its assumptions. |
| TLC | Covers one finite configuration. |
| Tests | Provide empirical evidence. |
| Rejected proof | Leaves the theorem's truth unknown. |
| File digests | Provide integrity, not authenticity or OS isolation. |
| Trusted components | Service installation, accepted checker, and declared trusted dependencies. |
| Process permissions | Producer and checker run as the service user. Protect checker code and local state from untrusted writers. |
| Formal execution | Runs in a restricted container with read-only source/tools, no network, and private output. Host installation and container runtime remain trusted. |
| Dependency closure | Must be declared. VKit cannot infer every dependency of arbitrary code. |

These results alone do not establish source-to-binary correspondence, termination, or correctness of an undeclared environment. See [verification semantics](docs/verification.md) and [architecture](docs/architecture.md).

## Develop and verify VKit

```sh
uv sync --extra test
uv run pytest
uv run python scripts/verify_formal.py --backend all
uv build
```

| Check | Requirements and scope |
| --- | --- |
| Core CI | Tests on Windows, Linux, and macOS. |
| Formal CI | Real Lean proof and axiom controls, plus TLC exhaustive and counterexample controls on Linux. |
| Local formal script | Lean and Java on PATH. `VKIT_TLA2TOOLS_JAR` points to the pinned TLC jar. Missing tools fail the script. |
