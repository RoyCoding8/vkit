# Plan 07: verify integration and coordinate parallel engineering

Read [CONTRACT.md](CONTRACT.md). Ready after Plans 02, 04, and 06. This plan implements the workflow machinery; it does not authorize launching an AI fleet.

## Outcome

Several authorized engineers can work independently while one integration path decides whether their combined code satisfies the agreed checks. A passing worker branch cannot stand in for the merged candidate. Work and evidence survive interruption.

Apply the Orchestrate playbook only to a campaign that needs durable coordination. Use `swarm` for independent coverage or work batches and `git-worktree-discipline` for writers. Follow the corrections in [WORKER_WORKFLOW.md](../WORKER_WORKFLOW.md#correct-the-existing-worktree-discipline), especially checking the actual merged checkout and preserving dirty work.

## Integration operation

Implement `vkit integration verify --project <repo> --candidate <commit> --target <commit> --policy <approved-reference> --json` or an equivalent explicit interface. Freeze the exact invocation with its CI example.

1. Resolve all refs to commits and record the repository identity. Require a clean candidate checkout with exclusive ownership. Verify that the candidate represents the intended target/change combination; a label or branch name is insufficient.
2. Obtain required policy and verifier code through the trusted integration configuration. Candidate code must not be able to lower its own required checks merely by changing `manifest.json` or the workflow file. A local user-selected policy is local evidence; only a protected job can claim protected integration context.
3. Run required checks against that exact candidate and retain source, policy, verifier, environment, and fixture identities. Check scope and missing coverage. Do not accept cached worker reports solely because they mention matching filenames.
4. Before any authorized publication, confirm that the target still matches the tested target. A moved target requires a fresh candidate and checks. This command does not push or merge on its own.
5. Provide a CI integration example for the repository's existing provider. For GitHub, cover pull requests and merge groups as appropriate. Use existing branch protection/merge queue mechanisms; do not build a forge queue.

Policy updates need their own reviewed path. First enrollment establishes a baseline approved by the repository owner. For a demonstration repository, explicitly identify the pinned baseline used by its tests. Do not give untrusted candidate code credentials capable of bypassing the protected decision.

## Coordinator workflow

The host coordinator owns the dependency graph and ready task list. The app owns contracts, claims, runs, and computed readiness. Keep one writable authority for each fact.

For each ready unit:

1. Establish its complete outcome, stable dependency revisions, write scope, required checks, runtime resources, and attempt identity.
2. Reserve its writer/runtime capacity through the app before the supported launch path. Create or select an isolated worktree using the host's native facilities.
3. Dispatch the full brief to an authorized engineer. Use explicit context/skill loading and the active model policy.
4. Collect the result through app IDs and evidence. Keep that engineer available for repair.
5. Integrate serially against the current target and rerun combined checks.
6. Release resources only when their processes and effects are reconciled. Preserve dirty source and evidence before retiring a checkout.

Set independent configurable bounds for active registered writers and running verification processes. Start with two writers and one verification slot in the example campaign. Total planned tasks can exceed available slots. Read-only reviewers need no private worktree when they inspect immutable source, but any build outputs or application instances still require isolation.

No unrestricted nested spawning. The coordinator owns global campaign admission. The plugin can guide/check supported launch operations, but cannot claim to constrain every arbitrary host tool or external session. Report that enforcement boundary explicitly.

The app does not need a mirrored issue tracker, model scheduler, or autonomous merge loop. If the existing pstack store is used for campaign bookkeeping, link kit task/run IDs instead of maintaining a second writable acceptance ledger.

## Acceptance

| Exercise | Required outcome |
| --- | --- |
| Two passing branches change disjoint files but conflict semantically | Combined-candidate verification fails |
| Old worker checkout still passes | Its evidence cannot satisfy the integration candidate |
| Target moves after candidate passes | Readiness becomes stale; no authorized publish based on the old base |
| Candidate removes a required check or weakens a threshold | Trusted policy still requires the original check or flags the policy change for review |
| Local report edited to PASS | Protected job recomputes and rejects the actual failing candidate |
| Dependency unit incomplete | Dependent writer is not admitted through the supported workflow |
| Shared lockfile/schema needs changing | One assigned owner; conflicting tasks pause that scope |
| 100 simulated clients with a low capacity bound | Active claims never exceed the bound; progress resumes when slots release |
| Coordinator stops and resumes | Reconstructs actual tasks, claims, and runs without replaying completed work |
| Worker loses contact with a live app process | Resource remains reserved until reconciled |
| Worktree contains uncommitted changes | Teardown preserves changes and evidence |

Run the Git conflict scenario against the product, not only the old research script. Simulated clients use real separate processes and the public app interface. These tests do not establish quality or throughput of 100 model agents.

If the user authorizes a live parallel trial, start with the configured small capacity, measure accepted outcomes and rework, then increase only with capacity evidence. Do not choose a spending budget on the user's behalf.

## Finish and handoff

Return the protected CI example, tested candidate/target provenance, policy-mutation countercheck, simulated contention/recovery results, and coordinator brief. Record which host/forge protections were actually configured versus only illustrated. Plan 09 tests the complete pilot workflow; it must not count synthetic clients as live agents.
