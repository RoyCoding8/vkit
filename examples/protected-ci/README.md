# Check a combined Git candidate

The [workflow example](github-actions.yml.example) runs fresh verification under the manifest in the target commit. The candidate can change or delete its own manifest without removing target requirements.

```sh
/opt/vkit/bin/vkit gate --project /checkout \
  --candidate FULL_COMBINED_COMMIT --target FULL_TARGET_COMMIT \
  --output /private/new-evidence-directory --json
```

The command exits successfully only when every target check and required formal component has a valid passing receipt for that exact candidate. It creates a private Store and uses the existing job, worker, container and receipt implementation. It imports no branch receipts or local approvals.

| Host prerequisite | Required configuration |
| --- | --- |
| Trusted installation | Install a reviewed, pinned VKit wheel at `/opt/vkit` before checking candidate code. Never install VKit or its dependencies from the candidate. |
| Verifier assets | Provision the official pinned Verus archive and extracted tools, Rust/Cargo homes and immutable Docker image IDs. The executor inventories their actual bytes. |
| Empirical runtime | Set `VKIT_CI_IMAGE` to an installed `sha256:` image ID containing `/usr/bin/python3`. The initial sealed profile accepts scenario commands shaped as `{{python}} -I -c CODE ...`, with driver code fixed in target policy. It blocks candidate-owned checker scripts and other registered-check profiles. Their local routes remain available. |
| Execution | Use a disposable Linux x86_64 runner with Docker. Only the check command receives read-only candidate/runtime mounts and its own writable output. Host authority, credentials, job files and receipt publication stay outside. |
| Policy governance | Protect the host workflow, VKit installation, runner configuration and changes to target policy. Configure the host's required check or organization-required workflow so a candidate cannot replace or remove it. |

GitHub's [`pull_request` event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request) identifies the PR merge candidate. [`merge_group`](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#merge_group) triggers checking of the queue's combined candidate. The example takes candidate and target IDs from these host events, fetches their history, and never executes a setup script from the candidate checkout.

A green standalone run establishes the recorded candidate checks. It becomes an enforced merge requirement only after the host controls above are configured. This temporary repository does not configure organization rulesets. Empirical results remain empirical; the formal receipt covers the declared API under the Verus trust boundary.
