# External verifier protocol

## Roles and accepted declarations

| Role | Responsibility |
| --- | --- |
| Producer | Generate the declared evidence artifacts. |
| Checker | Independently check those artifacts against the accepted claim and return a bound receipt. |
| VKit | Freeze inputs, run pinned commands, create the checker request, validate the receipt, and retain evidence. |

The approved `external` manifest entry fixes:

| Declaration | Accepted contents |
| --- | --- |
| Claim | Formal specification, obligation names, assumptions, and model scope. |
| Assurance | Requested assurance label. |
| Evidence | Exact artifact file list. |
| Commands | Producer and checker argument vectors, executables, and dependency pins. |

The producer cannot choose the checker or change replay's expected obligations.

## Command identity

| Field or rule | Requirement |
| --- | --- |
| Executable | Absolute path and SHA-256 hash. |
| `dependencies` | Pin scripts and other files read as code. |
| `trusted_tool_dependencies` | Pin additional trusted files, such as proof-kernel or model-checker binaries. |
| Pin validation | VKit checks every pin when building the current identity and immediately before command launch. |
| Version strings | Do not substitute for content pins. |
| Arguments | Passed as a vector, without a shell. |
| Dependency closure | Declare all required code and tools. VKit does not infer transitive binary dependencies. |

## Paths and input capture

| Path | Meaning and requirement |
| --- | --- |
| `specification` | Normalized repository-relative POSIX path. Automatically included in declared inputs. |
| `evidence_artifacts` | Normalized paths relative to `run_dir/evidence/`. |
| `run_dir/snapshot` | Frozen input tree. The producer runs with this snapshot as its working directory. |
| `run_dir/evidence/` | Producer output directory for the declared files. |

Before execution, VKit validates the job digest, current identity, complete input digest, and copied snapshot.

Writing evidence under the declared directory is a protocol requirement. It is not a filesystem sandbox.

## Argument placeholders

Both commands receive a JSON request file through `{{request}}`.

| Placeholder | Expanded value |
| --- | --- |
| `{{python}}` | The command's pinned executable. |
| `{{snapshot}}` | Frozen source directory. |
| `{{run_dir}}` | Current attempt directory. |
| `{{request}}` | Current command's JSON request path. |
| `{{evidence}}` | Evidence directory. |
| `{{receipt}}` | Checker receipt output path. |

Each placeholder expands within one argument element. No shell expansion occurs.

## Production and checking sequence

1. VKit launches the producer against the frozen snapshot.
2. The producer writes the declared artifacts under `evidence/`. Any producer receipt output is ignored.
3. After the producer exits, VKit hashes the declared artifacts.
4. VKit generates a fresh checker request from the accepted manifest and current job.
5. The checker validates the evidence and writes a receipt to `{{receipt}}`.
6. VKit validates the receipt and retains the request, receipt, and artifact digests.

## Checker receipt

The receipt is a UTF-8 JSON object with exactly these fields:

```json
{
  "schema_version": 1,
  "job_id": "<current job key>",
  "run_id": "<current run id>",
  "check_id": "<approved check id>",
  "check_digest": "<approved manifest-entry digest>",
  "specification_digest": "<frozen specification digest>",
  "identity": "<complete identity(check) JSON object>",
  "evidence_digest": "<digest of sorted evidence path/hash pairs>",
  "verdict": "accepted",
  "obligations": [
    {"name": "<approved obligation>", "result": "accepted", "evidence": ["proof.bin"]}
  ]
}
```

The example abbreviates `identity`. In a real receipt, replace that string with the complete object returned by `identity(check)`.

| Receipt element | Validation rule |
| --- | --- |
| `identity` | Must equal the complete current VKit identity, including both executables and declared dependencies. |
| `obligations` | Every accepted obligation appears once, in manifest order. |
| Obligation `evidence` | References one or more listed evidence artifacts. |
| Top-level `verdict` | Agrees with every obligation result. |
| Checker exit status | Zero is necessary, but does not suffice without a valid accepting receipt. |

## Verdict semantics

| Checker verdict | VKit outcome | Claim meaning |
| --- | --- | --- |
| `accepted` with a valid receipt | `PASS` | Accepted obligations established under the declared checker contract. |
| `rejected` | `BLOCKED` | Inconclusive. Does not establish a theorem counterexample. |
| `unknown` | `BLOCKED` | Inconclusive. |

A formal `FAIL` would require an explicit counterexample protocol. The current external protocol does not provide one.

## Replay

| Step | Action |
| --- | --- |
| Capture | Copy retained declared evidence into a fresh attempt directory. |
| Check | Run the currently pinned checker against the current accepted specification, job, obligations, and identity. |
| Ignore | Do not trust the stored status or receipt as a checking result. |
| Retain | Save the new checker receipt and each artifact's SHA-256 digest in run metadata. |

Replay runs the checker, not the producer.

## Process control and trust limits

| Mechanism | Guarantee or limit |
| --- | --- |
| `proc.run` | Enforces configured deadlines and cancellation, and stops descendants. |
| User permissions | Producer and checker run as the VKit user. |
| Pinning and post-run checks | Detect ordinary tool or snapshot changes. |
| Filesystem access | Does not prevent a process from mutating and restoring files while it runs. |
| Isolation | No OS filesystem sandbox is claimed. |
| Closure declaration | Required from the backend author. Transitive binary dependencies are not inferred. |

## Change witness artifacts

`witness.v1.json` defines an immutable change-witness artifact. The library parser checks it against a separately supplied `WitnessBinding` and the retained artifact bytes.

| Kind | Meaning |
| --- | --- |
| `proof_rejected` | A proof attempt was rejected; no native playback is attached. |
| `engine_counterexample` | Retains the engine input and counterexample, with explicit native playback status where supported. |
| `model_witness` | Retains the model, configuration and trace; replay is absent or limited to the same model engine. |
| `unknown` | The attempt was incomplete, unsupported, interrupted, timed out or exhausted its bound. |
| `infrastructure_failure` | The engine, importer, compiler, playback or protocol failed. |

Native playback records whether the input reproduced the observation under its pinned runtime. A model-only witness cannot carry native playback. Every artifact reference names a normalized retained path, SHA-256 digest and media type. Import rejects changed bytes, filesystem links and non-regular files.

The caller derives the expected binding from actual trusted-target preflight and independently measured runner/runtime identities. Producer-supplied identity fields do not establish their own authority. The binding covers the request, accepted intent, claim, both source captures, codec, observation rules and environment.

The safe Rust qualification can be run from the source checkout:

```text
python scripts/check_change_witness.py --output tmp/change-witness
```

It creates disposable Git target/candidate revisions, runs actual preflight, imports a labeled Kani transcript fixture, and compiles and runs the captured u32 sources. It retains the runtime identity and verifies the serialized witness again. This command does not run Kani. The artifact protocol does not alter the external checker receipt format or grant an approval, source-proof result or gate verdict.

## Sampled paired execution

`vkit.paired.run_paired_execution` runs captured base and candidate commits against the same selected inputs. The caller supplies the trusted target separately from the request. Preflight loads accepted intent from that target and protects its binding, codec, observation and environment artifacts.

The candidate must retain the target's accepted-intent list and the selected digest-named intent revision byte for byte. Adding an unaccepted proposal revision is allowed, but it grants no authority. Changing or deleting either protected record fails preflight before candidate execution.

The current profile accepts a declared `u32 -> u32` function in Rust, Python or TypeScript. Each process receives four little-endian input bytes and returns one canonical decimal u32 line. Fixed drivers read input before importing the candidate module or calling its function. Both sides run separately with declared tool hashes and a closed set of environment variables. TypeScript uses a directly executable native compiler.

`paired-execution-report.v1.json` describes the retained report:

| Status | Meaning |
| --- | --- |
| `samples_within_contract` | Every selected observation is stable and the candidate meets the request claim on those inputs. |
| `observed_violation` | A stable candidate observation violates the request claim. |
| `unknown` | A timeout, cancellation, budget, unsupported declaration or unstable observation prevents a complete result. |
| `infrastructure_failure` | Build, launch, protocol, pin, source or retained-artifact checks fail. |

The report retains repeated raw observations, selected and omitted input counts, source captures, semantic artifacts, generated drivers, and runner and runtime identities. A behavior difference can be permitted by the request claim. Repeated agreement does not cover omitted inputs or prove deterministic behavior. Shrinking checks only smaller accepted inputs and records its limit and attempted inputs.

The runner permits at most 128 process invocations, including builds, and 64 KiB of combined stdout and stderr per invocation. A 300-second execution deadline starts after Git preflight. Exhaustion returns incomplete evidence. These bounds do not impose an OS quota on captured sources, build products or filesystem writes.

Retained hashes are checked before report serialization, and input bytes are checked before each launch. These checks detect persistent changes. Candidate code runs with the caller's permissions and can access the host filesystem. The report explicitly records `source_proof:false`, `dependency_closure_checked:false`, and `os_sandbox:false`.

This sampled report is separate from engine counterexamples, formal receipts and protected acceptance. It grants no approval or source-proof verdict.

## Protected finite change analysis

`vkit.change_analysis.run_change_analysis` captures the trusted target and candidate once, runs the paired execution, and assesses retained observations against accepted standing intent and the target's difference policy. It selects accepted inputs by default. The target policy lives at `verification/change-intents/differences/<intent-digest>.v1.json`; an absent target policy means no approved difference guards. A candidate cannot add, change or delete that policy for its own analysis.

The analyzer validates retained bytes and live execution identities before using observations. Only successful, validated process outputs count as returned values. Failed-process stdout remains retained evidence. A known violation stays visible when another input fails or coverage is incomplete. Standing output requirements always apply. Overlapping guards intersect their allowed outputs; outside guards, equality or an allowed standing-intent repair is required.

`change-analysis.v1.json` records `observed_conformance`, `observed_violation` or `unknown`, coverage and findings. It also records `source_proof:false` and `approval_authority:false`. Its finite observations and coverage do not establish a universal source result. This Python API does not add finite-request CLI or MCP dispatch.

## Restricted Rust obligation preparation

Install the optional `rust` dependencies to use `vkit.rust_relational.prepare_rust_relational`. It takes a project, finite request and separately supplied trusted target, and returns a `RustRelationalProgram` containing the bound captures, protected policy, source admission identities and generated harness bytes. Preparation runs no verifier and returns no proof or approval verdict. The target's native environment declaration remains bound through intent; preparation does not validate or invoke those local tools, and it has not bound an engine toolchain.

The initial source profile admits one public, non-generic `u32 -> u32` function. It supports primitive literals and expressions, immutable typed locals, blocks, `if` and `return`. It rejects configuration attributes and macros, imports, calls, modules, globals, unsafe code, loops and other unqualified syntax. The compiler remains responsible for typing and arithmetic safety. Exact captured files become separate modules; their bodies are never copied or rewritten.

Tree-sitter runtime 0.26.0 and Rust grammar 0.24.2 parse admitted files in a separate Python process. The profile records native module hashes, grammar ABI, package versions and admission-code hash. Each source is limited to 64 KiB, with 3,000 AST nodes and depth 64 after parsing. The process runner enforces a ten-second deadline and 64 KiB combined output cap. A parser crash, deadline or malformed response fails admission. These limits do not impose an OS memory quota.

The generated obligation ranges over nonempty accepted finite inputs, checks both outputs against the observation universe, requires candidate standing intent, intersects approved guards and requires preservation or an allowed repair elsewhere. Domains are limited to 4,096 inputs and observations, the profile admits at most 4,096 guard requirements, and generated harness bytes are limited to 256 KiB. An engine adapter must still validate its pinned toolchain, exact captures and generated bytes, and require every safety, overflow and named property check to pass. A source-model result would not establish native compiler equivalence or approval authority.

## Captured Rust Kani execution

`vkit.kani_execution.run_kani_relational` takes a prepared `RustRelationalProgram`, a host-selected `KaniToolchain`, and a fresh output directory. The qualified runtime is native x86-64 Linux with Kani 0.68.0 and its selected Rust nightly. Other platforms return `unknown` with reason `unsupported_platform`. Acquisition and public CLI/MCP dispatch are separate from this Python API.

The toolchain record pins every file and directory mode in the selected Kani and Rust installation trees, the rustup binary and proxy layout, and any supplied host libraries. Validation streams file contents before and after a completed engine run. The runner uses a controlled environment and fixed arguments with default safety checks. These identities leave host utilities, the OS, kernel, Python runtime and unpinned libraries in the trust boundary. Pre/post scans do not prevent concurrent changes that are restored between scans.

The runner materializes both captures and checks their complete file inventories and raw bytes. It rejects links and special files, rechecks source admission, and retains generated harness, invocation, toolchain manifests, process outputs and exported results with hashes. Captured Git modes remain in metadata. Staged executable bits are not checked because this profile compiles admitted source files and never executes them.

The Kani process has a configurable deadline of at most 300 seconds, a 1 MiB combined stdout/stderr cap, and a 2 MiB result import limit. Capture, parser admission and toolchain scans are outside the engine deadline. There is no OS memory or filesystem quota. Cancellation, timeout, capped output, changed artifacts and missing exports cannot produce verification.

The decoder requires the qualified export metadata, all four generated assertions at their actual locations, consistent counters, and successful safety properties. Outcomes are `verified`, `proof_rejected`, `unknown` or `infrastructure_failure`. A rejection without an imported input is not a concrete native counterexample. Kani's generation of a complete source-property inventory remains trusted; self-consistent JSON alone cannot establish completeness. Every report records `source_proof:false`, `native_compiler_correspondence:false` and `approval_authority:false`.

## Shadow change-certification jobs

Use `vkit change certify --project PATH --request REQUEST.json --trusted-target COMMIT` to submit a candidate for shadow analysis. The request names its base and candidate commits, accepted intent digest and finite claim. The separately supplied `--trusted-target` is the host's exact target commit. It must match the request base and accepted target history.

The optional `--method` accepts `auto`, `finite_paired` or `rust_kani`. `auto` selects finite paired observations first. It adds the Rust Kani method only for a Rust binding on native x86-64 Linux when the host supplies a closed-format Kani toolchain pin. `rust_kani` on an unsupported binding or host produces a blocked shadow job without falling back to finite sampling. The optional `--kani-toolchain PIN.json` supplies that host-owned pin. The request cannot choose commands, environments, a target commit or a toolchain.

`--wait` accepts a finite number from zero through 60 seconds. Its default is zero. The command returns the durable run summary after that wait. Use `vkit run wait --run RUN_ID` and `vkit run show --run RUN_ID` for the existing job lifecycle.

The named MCP tool `change_certify` accepts the same request, a separate `trusted_target_oid`, an optional method and optional host pin. It uses the same job store, worker and retry path as other VKit jobs. `run_get` and `run_wait` return a compact shadow summary containing the accepted requirement citation, intent digest, each method result and report reference, and `source_proof:false` and `approval_authority:false`. The summary omits the full method plan and input vectors.

Finite paired results are sampled native observations. A Kani result is limited to the captured Rust source, accepted finite u32 inputs, generated relational harness and retained engine model. Neither result establishes native compiler correspondence or approval. A method that is unsupported, incomplete or blocked does not become a passing result.
