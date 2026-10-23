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
