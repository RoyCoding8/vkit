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
