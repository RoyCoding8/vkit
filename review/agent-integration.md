# Integration audit

Scope: `src/vkit/integration`, Plans 07–09, `plans/CONTRACT.md`, the CI example, and Plan 08 formal checks. No product files changed. The runnable false-acceptance probe and its output are in `review/probe_integration.py` and `review/probe-integration-results.json`.

## Findings

### P1: candidate-controlled check code can forge a protected acceptance

`verify.py` loads the approved manifest at `policy.manifest_revision` (lines 209–216) but executes each command with its working directory rooted in the candidate checkout (lines 262–265). `policy.compare` checks required check IDs, required scenarios, and timeout changes; it does not bind the command's input files to approved bytes (`policy.py:328–383`). The fixture's approved command invokes `verify_price.py` from that checkout (`tests/fixtures.py:57`). The trusted manifest therefore preserves the command line while candidate code at that path controls the result.

Repro: `review/probe_integration.py` creates a temporary fixture repository, replaces `pricing/quote.py` with a broken CLI, and replaces `verify_price.py` with a script that writes a schema-valid artifact containing `PASS` for all six approved scenario IDs. It commits those edits and runs the real `vkit integration verify --candidate <commit> --target <baseline> --policy @main --json` CLI. The result in `review/probe-integration-results.json` is exit 0, `ACCEPTED`, with all six scenarios accepted. The fixture is isolated and offline. This is a protected-context false acceptance, not a fixture-only assertion.

### P2: receipt omits the verifier identity and claimed fixture digests

`_verifier_identity` looks for `.git` under `Path(__file__).resolve().parents[2]` (`verify.py:312–339`), which is this checkout's `src` directory, so it records only `vkit 0.1.0`, not the verifier commit or source digest. `_fixture_identity` says it records digests of input files, but returns only the declared paths and counts (`verify.py:566–581`). Candidate and target commit SHAs let a reviewer recover candidate bytes when Git objects remain available, but the receipt does not name the checker bytes that produced its PASS (`verify.py:608–626`). This falls short of Plan 07's verifier and fixture provenance, and directly compounds the forged-checker finding.

## Formal evidence boundary

The saved TLC and Lean receipts match the committed model inputs. The current working files have CRLF line endings, so their raw byte hashes differ; normalizing line endings to LF reproduces the recorded hashes. This is a checkout-format difference, not evidence of changed theorems or configuration.

The formal evidence still has a narrower runtime reach than its model. The property-test wrapper compares `compute_readiness`, not MCP `task_finalize` (`tests/test_formal_correspondence.py:169–173`). `task_finalize` additionally unions the caller's checks with contract checks and the server manifest baseline (`src/vkit/mcp/_tools.py:870–887`). The wrapper's `Core.decide(owner, revision)` does not use its `revision` argument; the real readiness path also does not match evidence against a changed source/policy identity. So the correspondence tests do not establish runtime safety for source/policy changes. Neither those tests nor the TLA+ model exercises protected integration policy loading, candidate checkout execution, or the CI workflow.

## Verified boundaries

- Reproduced the false acceptance twice with `.venv\Scripts\python.exe review\probe_integration.py`; each run used a temporary Git fixture and removed it afterward.
- Checked the saved formal input hashes against committed Git blobs and LF-normalized working files.
- Git combination handling correctly rejects candidates behind or divergent from the target and runs checks against the exact candidate commit in the covered fixtures (`verify.py:492–531`, `tests/test_integration.py:266–359`). The CI example is documented as unexecuted; no GitHub remote is configured.
- Did not run the full suite, a live provider/CI job, Lean, or TLC.
