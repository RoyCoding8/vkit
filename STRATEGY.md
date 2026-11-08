# VKit — Consolidated Strategy & Review

*Assembled from three independent deep-dive reviews (source-level audit, design-vs-philosophy critique, external landscape research) plus the owner's stated vision. Written to be read cold, by whoever decides what VKit becomes next.* *Revised after six subagent probes and two external audits (GPT-6.1-Sol and Claude Opus 5.5 — `tmp/novelty/review-*.md`); where this document and `tmp/novelty/PLAN.md` (v2) disagree, PLAN v2 is authoritative for phasing and decisions.*

---

## 0. The core concept, explained from scratch

*For a reader new to these ideas: what "evidence bound to code" actually means, with no jargon.*

### The problem: a passing test is a statement about the past

You run the test suite; everything is green. That result is true *at that moment, for the code as it was at that moment*. The next edit — even one line — makes the green result quietly meaningless: it describes code that no longer exists. Nothing in a normal test runner tells you this. The checkmark just sits there, and humans and agents alike read it as "the code is fine."

This is exactly VKit's README demo: a totals function passes its tests; someone changes one line and `10 + 20 + 12` now returns `45`; the previous run is still green; nobody notices.

### The idea: treat every result like a batch certificate

A food-safety certificate is not a statement about a restaurant forever. It is a statement about **one batch of product, made under specific conditions, on a specific date**. Swap the ingredients and the old certificate says nothing about the new product.

VKit does the same for test and verification results. Every run produces a record ("evidence") that carries **digital fingerprints of exactly what it was about**:

- the **source bytes** — not "version 1.2", not "the latest commit", but a SHA-256 hash of the actual tested files;
- the **declared inputs** — the config, fixtures, and data the check reads;
- the **tool digests** — which test runner / verifier binary produced the result;
- the **check definition** — what the check claims to exercise.

The result is never "the totals function works." The result is: *"this check passed — and here is exactly which bytes, which inputs, and which tools that statement is about."*

### The three mechanisms, one at a time

**1. Binding.** The fingerprints are written into the record *together with* the result. A result can never float free of what it describes. Anyone handed a "PASS" can see precisely what it is a PASS *of*.

**2. Freshness (staleness).** Before anyone trusts a record, VKit recomputes the fingerprints of the *current* code and compares. If any fingerprint changed — one edited line is enough — the old record becomes **stale**. Stale does not mean "failed": the check might still pass. It means "this says nothing about the code you have now." Re-running the check produces a fresh record bound to the new bytes.

**3. The gate.** A rule that refuses to call work done: *no "done" until fresh passing evidence exists for every registered check that covers the change.* Normal test runners fail open — silence means green. The gate fails closed — silence (missing or stale evidence) means BLOCKED.

### "Says what it does not establish"

The last piece is honesty about scope. A passing test proves *those cases ran and passed* — it does not prove "the program is correct." Every record also lists its limits: its assurance level (empirical test / finite model check / kernel-checked proof / trusted-engine run), which parts of the code it covers, and what was **not** checked (e.g. "does not establish: dependency-tree identity, OS sandboxing"). The reader can then weight the evidence instead of treating all green as equal. (Honest caveat: the *gate* itself is assurance-blind — it reads only the result (`state.py:185-186`), so an `empirical` PASS and a `kernel_proof` PASS are equal to READY; the weighting is a reader-side act, by design.)

### Why agents specifically need this

A human holds a rough map in their head: "I tested this, I didn't touch that." An agent has no reliable memory of what it verified — its context is constantly overwritten, and its default behavior is to claim "tests pass" with no idea of scope or age. The ledger is the agent's external, checkable memory; the gate is the rule that keeps it from lying to itself. That is what "evidence backed" must mean: **not "we ran tests once", but "here is what is true about these exact bytes, how strongly, and what is not covered."**

### Worked example: watching the machinery on real files

*Everything below is real output from running VKit's own quickstart (`examples/python-cli`, a tiny `totals.py` CLI with one scenario check), then editing one line.*

**The registered check** (real `examples/python-cli/verification/manifest.json`; the repo-root `verification/manifest.json` is VKit's own self-check manifest) declares what it runs and — crucially — its `inputs`: the exact files whose bytes the result is about:

```json
{
  "id": "totals-behavior",
  "command": ["python", "verify_totals.py", "--app", "src/totals.py", "--out", "{{run_dir}}/result.json"],
  "required_scenarios": ["empty-cart", "single-positive", "several-positives",
                         "mixed-sign", "negatives-only", "cancels-to-zero"],
  "inputs": ["src/totals.py", "verify_totals.py"]
}
```

**What a run produces** — every run gets a directory (`.git/vkit/runs/<run-id>/`) containing a frozen `snapshot/` of the tested files plus `job.json`, the sealed identity of what this result is *about*. Excerpt from a quickstart run (quickstart ledgers live under `tmp/`, so the exact run is not retained):

```json
{
  "job_id": "949954fa8c12c121feaa8b313153dc3c91dafb2649820d3b7d2114e2d2f82ccf",
  "source": {
    "kind": "working_tree",
    "files": [
      {"path": "src/totals.py",     "raw_sha256": "bdb1e7f1777cadb7ac489e1d3cb2adf5166e7156..."},
      {"path": "verify_totals.py",  "raw_sha256": "2e9dd40e789ad54a2ae0128ccb7e81fce69ba2f9..."}
    ]
  },
  "inputs": {"declared": ["src/totals.py", "verify_totals.py"], "digest": "…", "files": ["…2 files, each with sha256…"]},
  "backend": {"implementation": {"budgets.py": "f3e6393c727e2c...", "...": "..."}}
}
```

Note the layers of fingerprinting: each tested **file** gets its own SHA-256; the **whole source** gets a digest over those; the **checker code itself** (`backend.implementation` — the vkit adapter modules) is hashed too. `job_id` is then a digest over the entire semantic payload (definition + source + inputs + checker). **Collision resistance makes it practically impossible for the result to describe anything other than these exact bytes.** (Scope: exact for the raw source capture; the inputs digest normalizes CRLF (`inputs.py:80-81`) — and this is only as sound as what is *in* the declared set, see §5.)

**The live sequence** (real commands, real output):

```text
$ vkit status --project demo/
  fresh_pass  totals-behavior  [scenario] declared inputs (2 files)
$ vkit gate --project demo/
READY: every required check passed against the current inputs
```

Now the README bug — one edited line, *without running anything*:

```text
$ sed -i 's/running += amount/running += amount + 1/' demo/src/totals.py
$ vkit status --project demo/
  stale  totals-behavior  [scenario] declared inputs (2 files)
$ vkit gate --project demo/
BLOCKED: some required checks have no passing evidence for the current inputs
```

The fingerprints no longer match, so the old PASS **cannot be found under the current key** — the ledger effectively says "I have no proof about *this* code." Rerunning with the bug in place shows the failure path:

```text
$ vkit check run --project demo/ --check totals-behavior
  [FAIL] negatives-only:   expected -12, printed -10
  [FAIL] cancels-to-zero:  expected 0, printed 2
$ vkit gate --project demo/
REJECTED: a required check failed against the current inputs
  fresh_fail  totals-behavior
```

Fix the line, rerun, and the gate returns to `READY` with a new record bound to the restored bytes. The ledger keeps **all three records** (old pass, failed run, new pass) — evidence is append-only history, never overwritten.

**The three gate verdicts in practice:** `READY` (fresh passing evidence for everything) · `REJECTED` (fresh evidence exists, and it *failed* — the code is observably wrong) · `BLOCKED` (no fresh evidence — *unknown*, could be fine, could be broken; VKit refuses to guess). Notice what this replaces: a bare test runner would have shown the stale green from Tuesday with no complaint.

**What formal-side records add** (from the evidence schema): every record also carries `assurance` (empirical / finite_model / kernel_proof / trusted_engine), `claim_outcome` (`established` / `refuted` / `unknown`), and explicit honesty flags like `"source_proof": false`, `"approval_authority": false`. The schema *requires* `assurance` and `claim_outcome`; the honesty flags (`source_proof`, `approval_authority`) are optional fields the adapters set — and read-time revalidation rejects any receipt that is inconsistent with its run record (`evidence.py:322-331`) — note a forger who writes record *and* flags consistently passes; the authenticating boundary is elsewhere (see §5.8).

### Does evidence expire? Does it go stale with time?

**No. Time never enters the equation — for hermetic checks.** Only change does. The evidence identity is purely content-based (hash of definition + tools + source bytes + inputs). A result from a year ago stays `fresh_pass` forever if none of those bytes changed. This guarantee covers hermetic checks only: the clock, network and environment variables are undeclared inputs (`os_sandbox: false`), and non-hermetic checks are out of scope until a sandbox or output-hash tier covers them. That is the correct design: "how old is this result?" is the wrong question — the right one is "is this result *about* what I'm looking at now?"

The subtlety is what counts as "change":

- **Stales without you editing code:** upgrading VKit itself (the machinery's own identity is in the key — all evidence goes stale, and `vkit accept` must be redone) or upgrading a tool binary (new pytest/TLC digest).
- **Should stale but currently doesn't (known hole):** upgrading a library dependency — `node_modules`/`.venv` are excluded from the fingerprint universe, so a package bump can change semantics while evidence stays green. PLAN v2 (§3.1) fixes this via observed-closure receipts and dependency-tree digests — not lockfile hashing alone, which is necessary but not sufficient.
- **Never stales:** the passage of time, reboots, weekends.

Precisely: **evidence expires against *content*, never against *clock*.** Stale means "this describes bytes that are gone" — not "this is old."

### In one line

**Evidence = result + fingerprints of what it describes. Staleness = the fingerprints no longer match. Gate = refuse "done" without fresh, passing, in-scope evidence.**

---

## 1. The vision, in precise words

The owner's idea, properly phrased (it took several rounds of conversation to nail this):

> **Give coding agents a way to formally verify their own work *while they work*, so that the guarantees become decision signals the agent can act on — instead of the current anti-pattern where agents spawn reviewer subagents and accrete defensive code until the codebase balloons to 50–100k useless lines. The human is the bottleneck; the human should only ever face decisions, never raw verifier output.**

Read its last clause as an *interface requirement*, not a promise to hide evidence: every decision put to the human arrives as a concrete choice with its evidence link — the statement, its assumptions, any evaluator change, and the replay — and a clear account of what accepting it changes. What leaves the human's routine path is raw verifier interpretation, never the evidence.

Three claims inside that, and it is worth separating them because they have different truth values:

1. **AI has collapsed the cost of producing machine-checked proof artifacts** (the owner's source: Tibo/Thibault Sottiaux of OpenAI — see §2). ✅ Empirically supported (see §3).
2. **Therefore the bottleneck moves from proof production to specification integrity** — statements being about the right thing, staying in sync with intent, and staying honest about scope. ✅ Supported by every skeptic we surveyed, including Lauren Tan herself.
3. **Agents that can rely on such guarantees make better decisions and stop bloating code.** ➡️ Plausible and largely untested — this is VKit's bet to prove.

A fourth, foundational point that emerged during review, and which the owner endorsed:

> **Formal verification is the one place where "no scope for error" is literally true — for the theorem. The engineering problem worth building is everything that keeps the theorem about the right thing.**

A proved statement is certain *relative to* the statement, the axioms, and the trusted kernel. Cheap AI proofs do not eliminate error; they concentrate error at the statement boundary (wrong property, vacuous property, stale property, out-of-scope property). **Keeping statements honest is the product.**

---

## 2. The sources, properly identified — a correction

The owner drew on two sources and conflated three things. The actual lineup:

**Source A — Tibo (Thibault Sottiaux, @thsottiaux), OpenAI** (works on Codex and ChatGPT). He is the origin of the hot claim. His exact words on X (~Nov 6, 2026, ~445K views):

> *"Formal proofs of tens of millions of lines of code are not a ridiculous thing anymore and within the realm of what should be considered when designing new systems."*

And in the same thread: *"I can confidently say that humans were not very good at writing secure infrastructure. The endstate will be hypersecure infrastructure and software for all, but we're going to go through some turbulence before we reach that."* The owner's remembered phrasing ("tens of thousands of lines of formal verification is trivially cheap") is actually a *stronger* claim than Tibo's — "trivially cheap" differs from "not ridiculous... should be considered when designing new systems" in both certainty and cost; Tibo said **tens of millions** and hedged with "should be considered". Note the sourcing caveat: Tibo works on OpenAI's Codex/ChatGPT and speaks as a vendor; Guillermo Rauch (Vercel) made the same case the same week (Vercel Security Reviews: 6,817 vulnerabilities flagged in 24h, 1,411 patched by an agent — note this is AI vulnerability *review*, which supports "AI changes security economics", not "formal proofs at scale"). Carina Hong's formal-reasoning startup **Axiom** (@axiommathai) replied directly: "talking about formal proofs of tens of millions of lines — hit us up" — i.e. a funded company forming around exactly this claim.

**Source B — Lauren Tan (@poteto).** The owner believed "Lauren Tan talking about formal verification" and "the creator of my skill stack" were separate sources. **They are the same person.** Lauren Tan (@poteto) is the author of **pstack** (github.com/cursor/plugins/tree/main/pstack), which contains `create-verification-skill`, `statechart`, `create-formal-verification-skill`, `maintain-formal-verification-skill`. React Compiler core team, ex-Cursor.

Her actual positions (sourced from her X posts and the pstack skill texts themselves):

- Agents change the **economics** of formal methods (citing seL4: ~23 proof lines per line of C, ~25 person-years). "Agents change the economics of formal methods" — also echoed by Ron Minsky/Jane Street.
- Verified intent "gives agents superpowers." **Invest in verification tooling agents run themselves, not more prompting.**
- Her stack is **empirical-first**: `create-verification-skill` (drive the real app, capture evidence) is the base layer; `statechart` (TLA+ model checking) is the targeted escalation for concurrency/state-machine problems. Formal verification is the scalpel, not the default.
- She names the **spec-sync problem** (keeping specs/proofs synchronized with changing requirements) as *the* historic blocker. She expects agents to help innovate here — she does **not** declare victory.
- She reports teams **cutting adversarial AI review** (subagent-style review theater) in favor of empirical + formal checks — support for the owner's core anti-pattern observation; it does not establish that VKit's loop reduces bloat.

**What she did not say:** the "tens of millions of lines" claim is Tibo's, not hers — she is the more careful voice (economics changed; spec-sync is still the blocker; empirical-first, formal as escalation). "Agents get absolute guarantees" is the owner's extrapolation from both. The synthesis of A and B is exactly the position this document takes: *proof artifact production is now cheap in narrow, precisely-stated settings (Tibo's claim, validated by Anthropic×Theorem); specification and maintenance remain hard (Tan's claim, validated by the skeptics).*

**What this means:** VKit is not competing with pstack. Pstack is a *skill-level* practice (write specs, drive apps, hand humans charts). VKit is the *infrastructure-level* version of the same philosophy (persistent evidence, freshness, gates, agent-facing APIs). They compose: pstack's patterns are arguably the spec-source for VKit's guarantee engine (§6).

---

## 3. The evidence base: is the thesis right?

### Supporting evidence (strong)

- **Anthropic × Theorem (Oct 2026):** machine-checked no-crash proofs for 239 OSS-Fuzz entry points across 123 libraries, RISC-V binaries, checked by the Rocq kernel. **~180M lines of kernel-checked proof in a few weeks**; bugs found that fuzzing never reached. (Caveats that matter: these are *proof-artifact* lines for bounded properties — 64MB input bound per entry point — not verified program lines; proof production consumed hundreds of millions of tokens. Specific fuzz tests, not every function; semantics of RISC-V/Linux are in the TCB.)
- **Vericoding benchmark (Sep 2025, 12,504 formal specs):** off-the-shelf LLMs verify 82% of Dafny / 44% Verus / 27% Lean problems; **pure Dafny verification success rose 68% → 96% in one year**. Adding natural-language descriptions doesn't significantly help — plausibly because in vericoding the model writes code *from* the formal spec (there is no code to read), so the spec already carries everything an NL gloss would add.
- **Sun's result:** a verification task at <$500 and <1 day that previously took ~5 person-months (uncited in the research record — treat as an anecdote, not evidence).
- **Capital interest** (funding signals attention, not feasibility or useful economics): Harmonic ($295M), &Truth ($70M), Truidic. AWS (TLA+/Dafny/Verus in production), Apple (~2.5B devices carry some verified software), Boeing — industrial use is old news; AI economics are the new news.

### Skeptical evidence (equally strong — and each item is a product opportunity)

| # | Failure mode | Source | VKit relevance |
|---|---|---|---|
| 1 | **Specification problem**: "guarantees are only as strong as their trusted specifications... specs cannot be proven correct — a fundamental Achilles' heel." Inconsistencies found even in Arm's *formally verified* CCA spec. | IronSpec (OSDI'24); Scope (2026) | VKit inherits this wholesale. Its guarantees are as strong as declared `inputs`/requirements. |
| 2 | **Vacuous proofs**: a green proof of a property nothing can violate is worthless; vacuity detection is not default practice in agent workflows. | practitioner literature | Mutation-based adequacy ("can this check fail?") should be a product feature. |
| 3 | **TCB cracks**: the **Lean 4 kernel itself had a soundness bug (Aug 2026)** enabling proof forgery; SMT-backed tools inherit solver trust. | oss-sec; de Moura postmortem | Every evidence record must surface its trust boundary (VKit already does this — see §4). |
| 4 | **Freshness/maintenance is the unsolved half**: codebase, spec, and tool evolution each cause "substantial proof rework"; re-verification gets cheap, **re-specing after intent changes stays human-shaped**. | security-certification experience report; CICM 2012 | Literally what VKit's freshness layer attacks. The literature validates this as the real operational cost. |
| 5 | **Self-verification risk**: agents game their own checks; formal checkers are exogenous (good) but *property selection* is endogenous (bad). | verifier–deployment gap literature | "Agents verify their own work" works only with human-audited or independently sourced properties. |

*(Web figures in this section — view counts, vulnerability counts, the 180M-line figure, funding rounds, the Lean kernel bug — are carried from the research review without independent re-verification.)*

**Verdict on Tibo's claim ("formal proofs of tens of millions of lines... not a ridiculous thing anymore"):** true as a statement about *proof-artifact volume* in narrow, precisely-stated settings (Anthropic×Theorem's 180M proof lines supports feasibility at that scale — with the units caveat above; "tens of millions of *program* lines verified" remains an extrapolation). "Should be considered when designing new systems" is the right design advice. But it is a statement about *producing* proofs, not about *trusting systems* — the specification bottleneck did not move, and Tibo's own "some turbulence" hedge lands exactly on the failure modes in the table above.

**Competitive landscape:** the binding + freshness + gate loop is *narrowly* differentiated, not unowned. In-toto already defines test-result attestations (subject identities + policies requiring passing tests) — but not freshness by content-change, nor an agent edit-loop gate. Dafny/Verus verify supplied program sources; what they don't provide is durable cross-session evidence reuse and staleness. Nearest live neighbor is **Hashloom** (content-addressed contracts + verification cache + gate + MCP). Compliance-traceability tools (Ketryx, Jama Connect, Codebeamer) own "requirements ↔ tests" but are human-auditor-first. AI review tools (Qodo, Bugbot, CodeRabbit) are the very category being cut. **The specific combination VKit targets — content-bound evidence + observed closure + adequacy + intent binding + fail-closed gate in the agent's edit loop — is open; each ingredient alone is not.**

---

## 4. What has been built — honest inventory

### The product in one sentence

*(word-by-word explanation of this sentence in §0)*

**VKit is a verification-evidence ledger with a gate: every check run is recorded bound to the exact source bytes, declared inputs, and tool digests that produced it; edits to anything bound make the evidence stale; a gate refuses to call the work done until fresh passing evidence exists — and the evidence record says exactly what it does and does not establish.**

### Scale (source audit, Nov 2026; line/test counts re-measured Nov 7, 2026)

20,161 src lines / 68 Python files (64 modules + 4 `__init__.py`) · 12,318 test lines / **821 tests collected** · 2,723 schema lines (16 files) · 715 docs lines · 156 commits over ~2.3 weeks (Oct 18 – Nov 3, 2026) · its own TLA+ models (`verification/`) including a deliberate mutant-config suite.

### What is genuinely strong (audit-confirmed)

1. **The freshness core is real, not theater.** Content-addressed job identity over definition + tools + code + inputs (`job.py:117-120`); the gate looks up evidence by that key (`state.py:173-183`); read-time full-bundle revalidation (`store.py:205-233`, `evidence.py:251-386`); execution-time drift checks (`runner.py:748-778`). Purely content-based — time never enters. The README demo works exactly as claimed within declared scope.
2. **Fail-closed discipline everywhere in the evidence pipeline** (consumer: the local advisory loop; the enforcement boundary is the protected CI gate, §4 strong #7). Every anomaly becomes `BLOCKED` with a reason, never `PASS`: malformed artifacts, odd exits, interrupted workers ("worker loss is interrupted execution, never a pass").
3. **Anti-weakening protections.** Lean statement freezing via `challenge_sha256` (`manifest.py:213-217`) blocks silent theorem-weakening *for Lean* — and its own enforcement path says a human must accept new statements (`lean_adapter.py:94-96`), which is `--yes`-able today (see §5.8); entailment violations are reported with witnesses (`intent.py:189-204`).
4. **Counterexamples are not trusted blindly.** A TLC safety counterexample only refutes an invariant *after replay* against the frozen model reproduces the violation (`docs/verification.md`). Matches pstack's "if a bug path doesn't reproduce, the model is wrong" rule.
5. **Honest scope language as a design principle.** Pervasive "does not establish X" statements; per-result `assurance` (`empirical` / `finite_model` / `kernel_proof` / `trusted_engine`) and explicit trust boundaries (e.g. paired-execution and change-analysis records hard-code `source_proof:false`, `dependency_closure_checked:false`, `os_sandbox:false` — `paired.py:295-297`; Verus source-verification records instead carry `assurance: trusted_engine`, `method: verus_source` — `formal/verus.py:544`). This is what makes evidence *decision-grade* rather than vibes — and it is rare in the industry.
6. **The in-flight seeds exist:** `status --path <file>` (pre-edit: which checks cover this file, what will my edit stale), `check run --needed` (incremental), and a **Stop/SubagentStop hook** (`plugin/scripts/vkit_hook.py`) — accurate description: *advisory with one forced retry* (it blocks once, then `stop_hook_active` allows; it ignores `not_approved`; it fails open on exceptions). 7. **The protected CI gate is the one component with a real authority boundary** (`ci.py`, `vkit gate --candidate --target`: target-owned policy, private store, refuses candidate-owned checkers; `tests/test_ci_gate.py`) — the earlier inventory of this document wrongly omitted it; it is now the center of PLAN v2's authority decision.

### What is weak (audit-confirmed, with locations)

1. **Tool/library freshness gap.** `evidence.py:142-180` hashes executables only; `inputs.py:52-77` excludes gitignored dependency dirs (and they *cannot* even be declared). Phase-1 probes (Nov 2026) confirmed hard: drifting an imported site-packages/PYTHONPATH module left evidence `fresh_pass`, gate `READY`; a re-run then failed 6/6. The closure hole has a weirder shape than expected: checks run from a snapshot of *declared files only*, so tree-local undeclared reads actually **fail closed** (FileNotFoundError → `artifact_missing` → BLOCKED) — the real hole is reads that *escape* the snapshot: absolute paths, `..`-relative paths, and interpreter/library modules. Tool identity is also invocation-sensitive: it keys tools by raw `sys.executable` string, so `D:\AI\vkit\…` vs `D:\ai\vkit\…` spellings (same file, same sha256) yield different identities — the same repo state was observed reporting `not_approved` via one invocation path and `fresh_pass` via another.
2. **Freshness is only as sound as hand-declared `inputs`** (`inputs.py:139-155`). No undeclared-dependency detection for the escaping reads above. Forget a file in `inputs` (or reach it via an absolute path) → edits to it don't stale evidence → the agent's "absolute guarantee" is silently false. *The absolute guarantee is only as absolute as a hand-maintained list.* This is the deepest irony in the design and the #1 soundness gap.
3. **Check-weakening is invisible at the decision surface** (probe-confirmed): weakening the check file produces the *same* `stale` state as a code change — no "check changed" signal; after re-running, the gate is `READY` over a broken app. Manifest changes force re-accept, but the accept prompt shows **no diff** against the prior acceptance. Vacuity likewise untested: a driver that never runs the app (or compares output with itself over a mutated app) gets PASS + `READY`.
4. **Requirement drift is mostly invisible to the gate.** Code/tool/policy changes stale evidence, and manifest `description` edits do too (definition digest) — but a description that *contradicts* the check earns fresh PASS after re-run; `features.json` behavior text is invisible; intent preflight "retains the label but does not resolve it or check that external text matches the accepted statement." Edit the requirement doc and nothing notices.
5. **Change certification is ~27% of src lines (~5,400 across certification/change_analysis/change_methods/change_service/intent/claim_acceptance/paired/rust_*/kani_*/witness; ~31% only if their schemas and tests are counted) and feeds no gate.** Lowest decision value per line in the project — and worse, it sits *inside* `policy_identity()` (`evidence.py:86-138`), so editing it stales every check and forces re-accept, while `query.py`, which does feed the gate, is omitted (§4 weak #8): the identity list is inverted relative to decision value.
6. **Operational tax.** `vkit accept` must be redone after *every vkit upgrade* (approval embeds `policy_identity()`, `state.py:180-184`) and all existing evidence goes stale on upgrade — undocumented in getting-started. `status`/`gate` re-hash every byte (O(evidence size)) per refresh — probe-measured ≈ +1.8–1.9 ms per declared file (status: 1.3s at 2 files → 5.0s at 2,002 files; `check run` cold capture of 2,002 files cost 46.8s once). Test suite runs ~1.5–2h (carried over from the earlier audit; not re-measured). Surface: 20 CLI verbs (31 commands counting subcommands), 16 schemas; manifest schema is 1,145 lines / 26 `$defs` even though per-check config is only ~8–15 fields.
7. **Structural friction.** 53+ hand-written `to_json` methods, triple validation (manual `from_json` + JSON schema + cross-object equality), 24 private cross-module imports, 15 single-field dataclasses (only 2 of them witness classes; the rest are request/report envelopes), `job.py`/`jobs.py` naming collision. Makes the codebase *feel* bigger than it is and raises the cost of every future change.
8. **Trusted-core bookkeeping gaps** (architecture review): `policy_identity()` hand-lists 34 modules and omits query/MCP/categories (`evidence.py:86-138`) — edits to the gate's own query surface do not reliably stale evidence; manifest `subject`/`expectations` are identity-bound but enforcement-free; the Stop hook is fail-open and does not block `not_approved`.

### On the "extra utilities" (console, proposals, multi-workspace, etc.)

The owner clarifies these were **intentional completeness utilities** — the polish people add to make an app feel finished — not thesis drift. That is legitimate and the reviews' "cut the bloat" verdict should be read with that in mind: *they are not mistakes.* The only caution that survives: these utilities are the largest code area (~455KB for the console alone: 122.6KB of code in console.py/js/css/html plus 342KB of bundled assets) and every hour spent on them is an hour not spent on the soundness gaps above. The test: **utilities must never become the thing the project is about.** Keep them thin, keep them optional, and let the ledger core set the pace.

### One-line verdicts from the reviewers

- **Code audit:** "A disciplined, honest, slightly over-grown tool whose strongest subsystem is also its simplest one. If the next unit of effort goes into dependency-closure honesty instead of more certification machinery, the 'absolute guarantee' story becomes materially truer."
- **Design critique:** "The ledger core and the Stop-hook loop are the real product and genuinely serve 'verify while you work' — but the philosophy's 'absolute guarantees' currently rest on hand-declared input lists and one demo ledger contract, while ~50% of the codebase is periphery."

---

## 5. What will help — the gap list, ranked

These are the deliverables that make VKit's guarantees *true enough to act on*, in priority order. The first four are soundness (making existing claims true); the last four are the philosophy (making guarantees decision signals).

1. **Dependency-closure honesty** (policy settled by external audit: observe + enforce + auto-fix — the Bazel/BuildXL/tup convergence). Observe what the check actually reads; observed-but-undeclared reads *violate* (block freshness) with the declaration auto-drafted for one-command repair; discovered deps bind as identity-only inputs (stale-on-change, no re-accept churn); explicit verdict `does not establish: dependency closure` for unobservable worlds — never approximate coverage as assurance.
2. **Anti-weakening** (PLAN v2 §1.2 ranks this above requirement binding): accepted-evaluator reference + bound obligation inventory + 2×2 cross-run test (a new check passing what a prior run rejected ⇒ `weakened_check`) plus strengthening/weakening labels on the accept card; today a weakened check is invisible at the decision surface (§4 weak #3).
3. **Requirement-drift detection.** Hash requirement *sources* (the actual requirement docs/text) into evidence identity, so editing intent stales evidence the way editing code already does. The change-certification tier already has the vocabulary (`intent.py`, `accepted-intents.v1.json`) — it needs to feed the everyday gate, not sit beside it.
4. **Tool/library freshness.** Extend tool identity beyond executable bytes to dependency trees where cheap (lockfiles, package metadata); at least *surface* the gap in every evidence record ("does not establish: dependency-tree identity").
5. **Adequacy / vacuity detection as a product feature.** "Can this check fail?" — run every check against a deliberately broken variant (mutation) and report detected/survivor per property. The `verification/` mutant configs (`Differences.mutant-*.cfg`) already prove the pattern works; promote it from bespoke artifact to first-class feature for all registered checks. An agent that knows its green checks *could have seen* its bug trusts them proportionally.
6. **Counterexample → fix guidance.** Failures must return as *advice*, not `FAIL`: map each failure to the smallest implicated scenario/obligation/rule, narrated in the reader's words (the `statechart` hand-off model), with the minimal repair surface. This is what turns verification from a verdict into a decision signal.
7. **Speed.** Parallel check execution and incremental re-verification so feedback lands in seconds during editing. The in-flight loop is the thesis; minutes-long loops push agents back to guessing. (`check run --needed` and `status --path` are already the right API shape.)
8. **Authority** (found by external audit; now PLAN v2's fifth decision): the agent must not be able to make its own proposals authorize themselves (`accept --yes` is shell-accessible today). The protected CI gate is the landing authority; the local ledger is explicitly the advisory loop; obligation inventories are bound so a deleted check cannot silently leave the gate.

---

## 6. How to build something absolutely great and substantial

### The strategic center

**VKit's moat is not running verifiers (commodity). It is the statement-integrity layer: the machinery that keeps every guarantee about the right thing, honest about its scope, fresh against change, and actionable by an agent.** The owner's "internal complex logic for proper guarantees" idea and the market's biggest unsolved problem (§3, skeptics #1, #2, #4) are the same thing.

The proven architecture — from Lean/Verus ecosystems and from the Anthropic×Theorem pipeline itself — is:

> **Complex, even AI-driven intelligence on the *generation* side; small, dumb, trusted checkers on the *validation* side. The TCB stays small; generated claims stay cheap to check.**
>
> *(Audit refinement: count a component's **effective** TCB — a tiny predicate atop a big observer is a big TCB; "no new trusted code" is not by itself a rule. Each untrusted component must name the independent recheck that validates it.)*

Applied to VKit: internal logic may be as sophisticated as you like for *deriving* what should be verified, *maintaining* proofs across edits, and *narrating* failures — but every claim it emits must be re-checkable by a small trusted core (the ledger, the hash bindings, the gate), and the repo should keep specifying its own critical machinery in TLA+ as it already does (`verification/lifecycle`, `verification/certification`). "Internal complex logic that is itself specified and checked" is the coherent version of the idea — with its stated limits: the repo's own certification README documents bounded models and selected runtime comparisons, not a general implementation-refinement proof, and its authority model excludes host authorization and human acceptance (the very boundary §5.8 / PLAN v2's fifth decision now address).

### The four layers of the guarantee engine

| Layer | Job | State today | Target |
|---|---|---|---|
| **1. Statement integrity** | Make sure guarantees are about the right thing: dependency closure, requirement binding, vacuity/adequacy | Weakest (§5.1–3, §5.5) | **The moat.** Each claim *qualified*: non-vacuous where detectability is demonstrated, scope-closed where closure is observed — never a universal "proven correct". |
| **2. Guarantee production** | Run tests + formal verifiers; keep proofs alive across edits | Strong adapters; no proof maintenance | Incremental re-verification, proof repair, spec-drift → re-spec prompts. |
| **3. Evidence honesty** | Bind results to code/tools/intent; freshness; assurance labels; fail-closed | **Already excellent** (§4 strong 1–5) | Extend to requirement text + dependency trees. |
| **4. Decision interface** | Put the right guarantee in front of the agent/human at the right moment | Seeds: Stop hook, `status --path`, `gate` | Counterexample→fix narration; "what breaks if I edit this file"; guarantees phrased in the reader's words (pstack's chart/feature-map discipline). |

### Where pstack fits

Pstack's skills are not competition — they are the **specification patterns** for layer 1 and 4:

- `statechart`'s invariants ("never weaken a rule to make it hold") → VKit's adequacy feature (§5.5).
- `statechart`'s hand-off ("the reader's words", bug-path replay) → VKit's counterexample narration (§5.6).
- `create-verification-skill`'s proof standards ("verify side effects, not just the final screen"; "don't trust a dry-run's name — observe what it skips") → VKit's evidence-scope discipline.

Bake those rules into the product so agents inherit them by default instead of by skill-file luck.

### The bet to prove

Claim 3 of the vision is still unproven (§1): *do agents with in-flight guarantees write less bloat?* Build the demonstration that settles it — the same tasks run under PLAN v2 §5.0's four named arms (`oracle`, `empirical-only`, `vkit-advisory`, `reviewer`), measuring lines of defensive/reviewative code produced and defects caught. That experiment is both the marketing and the product roadmap's compass.

### Suggested phasing

- **Phasing is now owned by `tmp/novelty/PLAN.md` (v2)** — revised after two external audits: Phase 0 trust/authority fixes → **oracle-arm upper-bound experiment** (does perfect verification reduce agent bloat at all? run before building mechanisms) → statement integrity (execution-world contract, accepted-evaluator anti-weakening, coverage) → deterministic decision envelope → the staged bet. The earlier four-phase sketch in this document is superseded; proof maintenance (spec-drift → re-spec assistance) remains the decade-defining problem, started last.

Throughout: **keep utilities (console, proposals, multi-workspace) thin and optional; never let them set the pace.**

---

## 7. Bottom line

- Three separate conclusions stand in for a single verdict: **demonstrated feasibility** — proof artifacts are cheap in narrow, precisely-stated settings (§2–3); **unresolved statement integrity** — *honest statements about the right thing* remain the bottleneck; **untested workflow benefit** — claim 3 is the bet to prove (§1, §6). The trust layer between verification results and agent decisions is narrowly differentiated along VKit's intended combination (§3), not unowned.
- What's built is **substantive, not a wrapper**: the freshness/binding/gate core is real, fail-closed, and industry-unusual in its honesty discipline (§4). It is also heavily periphery-loaded by volume (~27% of src Python lines are the uncertified change-family; roughly half the repo only when console assets and tests are counted) — fine as completeness utilities, but they must not grow further.
- The gap between VKit today and the vision is precise and nameable: **guarantees must become true within their stated scope (closure, drift, adequacy) and then become advice (counterexample→fix, speed)** (§5).
- Build the statement-integrity engine — complex generation logic checked by a small trusted core (§6) — that is the "something absolutely great and substantial," and it is the exact problem the entire field, Lauren Tan included, says is next.
