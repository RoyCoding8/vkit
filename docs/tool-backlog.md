# Ranked backlog of agent-callable verification tools

Research date: October 10, 2026.

This backlog preserves 43 ranked capabilities for vkit. The agent decides what to build and when to call a tool. Each tool accepts a precise question, performs substantial computation inside vkit, and returns a result with its assumptions and evidence. Implementation status is recorded beside the relevant entries; unmarked entries remain proposals.

The ranking favors usefulness while an agent builds and changes a whole repository. It weighs frequency of the problem, consequences of an incorrect answer, precision of the supported question, implementation reuse, and deployment cost. The ordering is engineering judgment, not a measured benchmark or a ranking by stars.

The first ten deserve implementation investigations first. Later entries preserve substantial opportunities for particular languages, numerical software, infrastructure, or future research. A lower rank does not mean a weaker algorithm.

## Read the evidence labels

- **Engine** means upstream software implements the underlying analysis. A dependable vkit integration still needs development and validation.
- **Prototype** means a publication demonstrates an implementation or method with a restricted research scope. Production readiness is unestablished here.
- **Design** means the proposed vkit operation combines foundations or extrapolates from research. No turnkey implementation of the complete proposed service was established.
- **Model result** means an answer about the stated extracted program, schema, policy, or execution model.
- **Execution result** means an observation from the identified concrete executions.
- **Measured result** means a measurement with its workload, environment, and uncertainty.
- **Candidate** means a hypothesis for later verification. It must never appear as a proved program property.

Official project documentation and original papers support technical claims below. Sources include NIST, Microsoft Research, USENIX, GNU, university research groups, and upstream repositories. Reddit discussions are kept separately as motivation. They do not establish an analyzer's correctness or a problem's prevalence.

The original investigation examined documentation and repository material without installing or benchmarking the proposed engines. Implementation notes below identify the entries subsequently built and exercised with pinned backends. Those checks do not constitute an upstream correctness audit. Other candidates still need platform, license, reproducibility, and release checks when implementation begins.

## Keep the tool contract narrow

The proposed signatures below are sketches, not committed public APIs. `profile` identifies verifier-owned semantics and rules. `snapshot`, `schema`, and `history` identify captured input data. The agent can select a supported question and supply its inputs. It cannot replace the installed verifier's implementation or quietly substitute its internal rules.

A supplied schema or specification defines the question being answered. Verifying that data does not establish that it captures every requirement in the user's original prompt.

Every integration needs a defined input model, protected analyzer configuration, bounded resource use, and a result that distinguishes completion from unsupported input. When an engine proposes a concrete counterexample, replay it in the corresponding real runtime where possible. Keep a symbolic witness separate from a replayed failure.

The substantial service includes extraction, dependency resolution, isolation, model construction, result interpretation, and witness validation where those are needed. A renamed executable invocation alone does not satisfy this backlog.

These 43 capabilities do not require 43 public commands. Several extend an existing operation or share one operation family. Consolidation decisions appear after the detailed entries. The current shipped inventory remains in [the engine roadmap](engine-roadmap.md), and the extension contract remains in [Add a built-in operation](adding-operations.md).

## Shared prerequisite implemented before further engines

Every capability below should use vkit's versioned `Operation` contract as its common integration plug.
One declaration supplies names, scope descriptions, JSON input schemas, defaults, handler dispatch,
read-only metadata, and declared outcome statuses. CLI, MCP, and console reuse that declaration.
An engine remains substantial software internally; the shared plug standardizes its external contract.

Agents discover enabled capabilities by calling MCP `help`, optionally with a text `query`.
`help` returns descriptions and exact input schemas. Agent onboarding files reference this entry point
instead of listing every tool. Adding a catalog declaration updates discovery automatically.

The operator can enable or disable individual tools through console Setup or `vkit tools`.
The saved `disabled_tools` field lives in the existing Git-excluded `config.json` beside display settings.
Disabled tools disappear from MCP `tools/list` and `help`, and the dispatcher refuses direct calls.
`help` always remains available. A fresh client session sees the filtered catalog; existing clients should
reconnect to refresh cached definitions. Operator controls are not exposed as MCP tools.

The installed contract, discovery query, persisted switches, and console controls are implemented.
This shared infrastructure does not itself implement the candidates below or establish their correctness. Engine availability
still depends on backend installation. Shell access to the installation or operator configuration is a
separate deployment boundary. See [the operation integration guide](adding-operations.md).

## Ranked index

| Rank | Capability | Basis |
| --- | --- | --- |
| 1 | Repository dependency paths, cycles, and affected components | Engine |
| 2 | OpenAPI contract compatibility | Engine |
| 3 | Mutations that the supplied tests fail to detect | Engine |
| 4 | Interprocedural source-to-sink data-flow paths | Engine |
| 5 | Stateful API sequence counterexamples | Engine |
| 6 | JSON Schema containment and compatibility direction | Engine |
| 7 | Dependency constraint resolution and conflict explanations | Engine |
| 8 | Database upgrade-path and fresh-install comparison | Design |
| 9 | Clean installation and reproducibility of release artifacts | Design |
| 10 | Controlled exploration of concurrent schedules | Engine |
| 11 | Concrete behavioral differences between Python functions | Engine |
| 12 | Interprocedural memory and resource lifetime analysis | Engine |
| 13 | Public source API and native ABI change analysis | Engine |
| 14 | Semantic conflict checking for three-way merges | Prototype |
| 15 | SQL query equivalence and validated disagreement cases | Engine |
| 16 | Parser and serializer disagreement discovery | Design |
| 17 | Authorization-policy equivalence and permission expansion | Engine |
| 18 | Undeclared build inputs and stale incremental results | Design |
| 19 | Replay of retained cases with retained assertions | Design |
| 20 | Network reachability changes from configuration changes | Engine |
| 21 | Paired workload cost comparison | Design |
| 22 | Bounded Rust safety and assertion verification | Engine |
| 23 | LLVM transformation refinement | Engine |
| 24 | Transaction-history isolation violations | Engine |
| 25 | Crash-recovery violations for persistent state | Prototype |
| 26 | Constrained configuration combination coverage | Engine |
| 27 | Retry, duplicate-delivery, and interruption counterexamples | Design |
| 28 | Rigorous floating-point round-off bounds | Engine |
| 29 | Regular-expression backtracking cost and attack witnesses | Engine |
| 30 | Inputs that reach a selected native-code path | Engine |
| 31 | C function contracts beyond bounded unwinding | Engine |
| 32 | Affine memory-access overlap and loop dependencies | Engine |
| 33 | Queries over deterministically replayed executions | Engine |
| 34 | Scheduling, blocking, and resource facts from traces | Engine |
| 35 | Expression synthesis under a fixed grammar and specification | Engine |
| 36 | Termination and complexity bounds for supported programs | Engine |
| 37 | Floating-point precision selection under an error budget | Prototype |
| 38 | Protected source migrations with checked postconditions | Design |
| 39 | Candidate invariants supported by supplied execution traces | Engine |
| 40 | Cache-key collisions that change observable results | Design |
| 41 | Grammar ambiguity witnesses and parser conflicts | Engine |
| 42 | Counterexamples to fixed metamorphic relations | Design |
| 43 | Exact symbolic algebra in a declared mathematical domain | Engine |

## Detailed candidates

### 1. Repository dependency paths, cycles, and affected components

Proposed call: `analyze_dependencies(snapshot, entrypoints, profile, query)`.

Implemented entry point: `analyze_dependencies(path, query, sources, target, timeout_seconds)`.
The initial profile extracts JavaScript and TypeScript repository-local module dependencies with pinned
dependency-cruiser. It answers graph, path, cycle, reverse-impact, and entrypoint-reachability queries.
It reports unresolved and dynamic edges. Python extraction and symbol-level impact remain future work.
See [built-in computations](computations.md) for the supported model and runtime setup.

Ask for paths between specified modules, strongly connected components, or reverse reachability from changed symbols. A concrete result might show that a UI module reaches the database through two intermediate modules. The same resolved graph can identify files unreachable from the supplied entrypoints.

The service needs language-aware import resolution, workspace boundaries, generated-file handling, symbol identities, graph queries, and path evidence. Dependency-cruiser supplies JavaScript and TypeScript dependency analysis. Soufflé supplies recursive relational analysis. Python needs a separate supported extractor.

Return unresolved and dynamic edges explicitly. Unreachability in a static graph alone does not prove that a file is safe to delete. Keep impact, cycle, and unreachable-code queries in one graph family.

Sources: [dependency-cruiser](https://github.com/sverweij/dependency-cruiser), [Soufflé](https://github.com/souffle-lang/souffle), [Knip issue handling](https://knip.dev/guides/handling-issues).

### 2. OpenAPI contract compatibility

Proposed call: `compare_contracts(before, after, profile="openapi")`.

Implemented entry point: `compare_contracts(old_path, new_path, profile="openapi", timeout_seconds)`.
It uses pinned oasdiff with protected validation and compatibility rules over captured local documents
and their reference closures. Protobuf callers use the same operation with a `protobuf-*` profile.
See [built-in computations](computations.md) for supported formats, limits, and runtime setup.

Ask which changes break the earlier declared API contract. Examples include making an optional request field required or changing a response property to permit null. Return the affected operation, schema location, applicable rule, and definite or unresolved classification.

Oasdiff implements extensive contract-change checks. Vkit must capture referenced schemas, fix the compatibility policy, handle composition, and prevent repository configuration from disabling verifier-owned checks. Its answer concerns the declared contract, not an arbitrary server implementation.

Extend the existing protobuf compatibility family when the shared result contract is ready. Do not create a separate transport or workflow for OpenAPI.

Source: [oasdiff breaking-change semantics](https://raw.githubusercontent.com/oasdiff/oasdiff/main/docs/BREAKING-CHANGES.md).

### 3. Mutations that the supplied tests fail to detect

Proposed call: `mutation_survivors(snapshot, testset, scope, profile)`.

Implemented entry point: `mutation_survivors(run_id, path, max_mutants, timeout_seconds)`.
The initial scope is a JavaScript subject file from a current, approved, passing `node_test` run.
Stryker generates literal edits under installed policy. Vkit captures the recorded inputs, validates a
fresh unmodified baseline, executes each edit in a fresh workspace, and replays passing mutations.
Only observed assertion failures count as detection. Missing tests, load errors, timeouts, unstable
replays, and unexamined mutations remain explicit. The operation returns execution evidence without
collecting coverage or reusing incremental results. It uses the shared operation declaration, discovery,
and saved availability settings. See [the computation contract](computations.md#find-mutations-that-tests-do-not-detect).

Ask which fixed mutations still allow the supplied tests to pass. An example is replacing a permission check with unconditional success while every relevant test remains green. Return the actual edit, baseline execution, test identities, coverage evidence, and mutant outcome.

Stryker provides mutation generation and test integration for JavaScript and TypeScript. The service must isolate mutations, distinguish compilation failure from detection, validate the unmodified baseline, and replay suspicious outcomes. Incremental reuse must account for configuration and dependency changes. Stryker documents inputs its own incremental mode does not track.

A survivor can be behaviorally equivalent. Report observed detection results without turning every survivor into a confirmed defect or an overall quality score.

Sources: [StrykerJS](https://github.com/stryker-mutator/stryker-js), [incremental limitations](https://stryker-mutator.io/docs/stryker-js/incremental/), [equivalent mutants](https://stryker-mutator.io/docs/mutation-testing-elements/equivalent-mutants/).

### 4. Interprocedural source-to-sink data-flow paths

Proposed call: `find_data_flows(snapshot, sources, sinks, profile)`.

Ask whether the extracted model contains a data-flow path from specified sources to specified sinks. Examples include request input reaching a shell command or a secret reaching a logging call. Return each reported path with assignments, calls, and source locations.

Joern provides code property graphs and data-flow traversals. Vkit needs supported language frontends, call resolution, fixed library models, sanitizer semantics, and evidence about unresolved calls. Shared source extraction can support the dependency family, but dependency edges and value-flow edges have different meanings.

A modeled path is not automatically an executable exploit. A missing modeled path does not establish security across unsupported framework behavior.

Source: [Joern data-flow steps](https://docs.joern.io/cpgql/data-flow-steps/).

### 5. Stateful API sequence counterexamples

Proposed call: `exercise_api_sequences(snapshot, schema, profile, bounds)`.

Ask which tested operation sequence violates a fixed check. A sequence could create a resource, update it, fetch it, delete it, and fetch it again. Return the requests, relevant responses, initial state, selected check, and a replayable failure.

Schemathesis connects operations through schemas and response data. Vkit must manage an isolated application and database, authentication fixtures, state reset, bounded generation, and reduction. Fixed checks can cover response conformance and declared lifecycle rules. Application-specific business rules require explicit input contracts.

The completed campaign establishes what happened in its tested sequences. It does not establish all possible API behavior. Reuse existing scenario and property execution where possible.

Source: [Schemathesis stateful testing](https://schemathesis.readthedocs.io/en/stable/explanations/stateful/).

### 6. JSON Schema containment and compatibility direction

Proposed call: `compare_contracts(schema_a, schema_b, profile="json-containment")`.

Ask whether every JSON instance accepted by schema A is also accepted by schema B within a supported schema fragment. This gives an exact direction for producer and consumer compatibility. Request acceptance and response acceptance require different containment directions.

IBM's jsonsubschema provides a containment foundation. The integration needs a declared dialect, supported keywords, captured references, and independently validated witness documents when available. Reject or report unsupported constructs instead of silently dropping them.

This belongs beside OpenAPI and protobuf compatibility. It is more useful than merely validating a few example JSON documents against both schemas.

Source: [IBM jsonsubschema and its ISSTA research reference](https://github.com/IBM/jsonsubschema).

### 7. Dependency constraint resolution and conflict explanations

Proposed call: `resolve_dependency_change(lock, request, platform, profile)`.

Ask for a version assignment satisfying the supplied package constraints, or a conflict explanation. A defined optimization profile could minimize changed locked packages. Never describe a heuristic preference as a proved minimum.

Libsolv provides SAT-based package resolution and explanations. Spack demonstrates a substantial constraint-based concretizer. These are foundations, not ready-made implementations of every Python or npm rule.

Vkit needs ecosystem-specific version ordering, conditional dependencies, peer dependencies where applicable, captured package metadata, platform markers, and verification of the returned assignment. Consistent declared constraints do not prove that packages work together at runtime.

Sources: [libsolv](https://github.com/openSUSE/libsolv), [Spack concretization settings](https://spack.readthedocs.io/en/latest/build_settings.html).

### 8. Database upgrade-path and fresh-install comparison

Proposed call: `compare_migration_paths(migrations, old_version, fixtures, profile)`.

Ask whether an upgraded database and a fresh database have the same normalized schema, and which supplied data assertions fail after upgrading. This catches migrations that work on an empty database but destroy or misinterpret existing records.

The service must create disposable databases, apply both paths, seed versioned fixtures, introspect schemas, compare relevant database objects, and report failed SQL or data observations. Squawk adds PostgreSQL migration rules. Atlas supplies a useful design reference for migration replay.

Atlas's documented migration-test command is a Pro feature outside Community Edition. A fully open implementation needs its own execution layer. Fixture replay does not prove preservation for every production dataset or concurrent workload.

Sources: [Atlas migration testing](https://www.atlasgo.io/testing/migrate), [Atlas edition boundaries](https://www.atlasgo.io/community-edition), [Squawk](https://github.com/sbdchd/squawk).

### 9. Clean installation and reproducibility of release artifacts

Proposed call: `check_release_artifact(snapshot, build_profile, runtime_profile)`.

Ask whether the produced package installs and completes specified entrypoint checks outside its source checkout. Optionally ask whether two builds under the declared conditions produce identical artifacts, and which files differ.

The service needs isolated builds, declared toolchains, package inspection, clean installation, runtime checks, and detailed archive comparison. Diffoscope provides recursive artifact comparison. Bazel's hermeticity model supplies useful input-boundary principles without requiring users to adopt Bazel.

This finds missing assets, undeclared dependencies, checkout-relative paths, and differences hidden by editable installs. Equal build artifacts do not establish functional correctness. Reuse vkit's existing execution and evidence machinery.

Sources: [diffoscope](https://diffoscope.org/), [Bazel hermeticity](https://bazel.build/basics/hermeticity).

### 10. Controlled exploration of concurrent schedules

Proposed call: `explore_schedules(snapshot, scenario, profile, bounds)`.

Ask whether any explored schedule reaches an assertion failure, deadlock, or other specified violation. Return a schedule that can reproduce the finding and the explored-state boundary.

Microsoft Coyote supplies controlled concurrency testing for C#. Loom explores concurrent Rust tests. The service needs supported synchronization instrumentation, scheduler ownership, schedule reduction, replay, and explicit handling of external I/O.

This is distinct from the existing `check_history`, which analyzes a supplied history. A passing bounded search cannot become an unrestricted concurrency claim. Loom also documents unsupported C11 behaviors and possible false alarms. Profiles must expose those restrictions.

Sources: [Coyote](https://github.com/microsoft/coyote), [Loom and its unsupported features](https://github.com/tokio-rs/loom), [CHESS research](https://www.microsoft.com/en-us/research/publication/chess-a-systematic-testing-tool-for-concurrent-software/?lang=ja).

### 11. Concrete behavioral differences between Python functions

Proposed call: `find_behavior_difference(before, after, domain, profile)`.

Ask for an input that makes two supported functions differ in return value, exception, or allowed mutation of input state. A useful result includes the input and both concrete outcomes.

CrossHair's differential symbolic execution is a foundation for Python beyond the existing pure integer and boolean rewrite operation. Vkit must isolate imports, define observable behavior, constrain the supported runtime, and replay symbolic suggestions against real Python.

CrossHair explicitly states that absence of a counterexample does not guarantee a property. Prefer verified concrete disagreement results initially. Keep `UNKNOWN` when exploration finishes without an established equivalence argument.

Sources: [CrossHair diffbehavior](https://crosshair.readthedocs.io/en/latest/diff_behavior.html), [CrossHair limitations](https://crosshair.readthedocs.io/en/latest/limitations.html).

### 12. Interprocedural memory and resource lifetime analysis

Proposed call: `analyze_resource_lifetimes(snapshot, targets, profile)`.

Ask which modeled call paths contain a selected memory or resource error, such as a null dereference or use after release. Return the path, resource identity, allocation or acquisition site, and invalid use.

Infer Pulse supplies interprocedural memory and value analysis. Vkit must capture the correct compilation, fix the analysis configuration, preserve library models, and distinguish conditional summaries from reported manifest errors.

This can analyze relationships across functions rather than checking one isolated C function. It has language-specific support. The examined Pulse documentation does not support Python. Findings remain subject to the selected abstraction and modeled libraries.

Source: [Infer Pulse](https://fbinfer.com/docs/next/checker-pulse/).

### 13. Public source API and native ABI change analysis

Proposed call: `compare_contracts(before, after, profile="typescript-api" | "native-abi")`.

Ask which public declarations or ABI artifacts changed, and which supported compatibility rules those changes violate. Useful examples include removing an exported type, changing a calling convention, or altering a native structure layout.

API Extractor produces structured TypeScript API signatures. Libabigail compares native ABI corpora. Vkit needs complete build inputs, exported-symbol selection, target-platform identity, and profile-specific compatibility interpretation. An API signature extraction alone is not a semantic compatibility proof.

Use the compatibility family rather than exposing a separate public tool for every format. TypeScript compiler checks and native ABI rules must remain separate profiles with separate guarantees.

Sources: [API Extractor model](https://api.rushstack.io/pages/api-extractor-model/), [Libabigail](https://sourceware.org/libabigail/).

### 14. Semantic conflict checking for three-way merges

Proposed call: `check_semantic_merge(base, left, right, merged, profile)`.

Ask whether a candidate merge satisfies a defined relation to the two independent edits. Textually disjoint edits can interact when one changes a condition and the other changes an action controlled by that condition.

SafeMerge research demonstrates relational reasoning about three-way Java merges. A vkit implementation needs supported source extraction, correspondence between versions, a precise conflict-freedom relation, and either discharged obligations or a validated distinguishing execution.

Treat this as a prototype-derived opportunity, not a universal merge oracle. It is especially relevant to isolated worker branches. A failure of the checker to prove the relation must remain distinct from a demonstrated conflict.

Source: [Verified three-way program merge, author-hosted paper](https://www.cs.utexas.edu/~isil/verified-merge.pdf).

### 15. SQL query equivalence and validated disagreement cases

Proposed call: `compare_sql_queries(query_a, query_b, schema, profile)`.

Ask whether two supported queries have the same result under a declared SQL model, or obtain a database instance on which their real results differ. The profile must define nulls, duplicates, ordering, constraints, and supported operators.

SQLSolver implements equivalence reasoning using linear integer arithmetic. The repository states that some equivalent pairs can receive a negative or unknown result. Vkit must not translate its negative result directly into definitive inequivalence.

Initially return established equivalence within an audited fragment, a replayed concrete disagreement, or `UNKNOWN`. Schema interpretation and witness database construction are substantial integration work.

Source: [SQLSolver and its documented result limitations](https://github.com/SJTU-IPADS/SQLSolver).

### 16. Parser and serializer disagreement discovery

Proposed call: `find_parser_disagreement(implementations, format_profile, bounds)`.

Ask for an input on which specified parsers disagree, or a value whose serialization fails the specified round-trip relation. Examples include duplicate JSON keys, malformed Unicode, numeric boundaries, and inconsistent URL interpretation.

Atheris supplies coverage-guided Python fuzzing. ISLa supplies grammar-aware constrained input generation. Vkit must build adapters, normalize only profile-approved differences, reset state, record generated inputs, replay disagreements, and use the existing failure reducer.

A disagreement is a concrete observation. Identifying which implementation is wrong requires the declared format semantics. A finished fuzzing campaign does not prove equivalence.

Sources: [Atheris](https://github.com/google/atheris), [ISLa](https://github.com/rindPHI/isla).

### 17. Authorization-policy equivalence and permission expansion

Proposed call: `compare_authorization_policies(before, after, schema, profile)`.

Ask whether two policies authorize the same modeled requests, or whether the new policy admits a request denied by the old policy. Return a principal, action, resource, and context that distinguish them when available.

Cedar's formal development includes symbolic compilation and verification properties for equivalence and implication. This is a stronger foundation than comparing policy text or running a few example identities.

Vkit still needs a usable analysis interface, supported extensions, captured entity assumptions, and witness evaluation with the concrete authorizer. The answer concerns Cedar's declared authorization model. It does not establish that every application route invokes the authorizer correctly.

Source: [Cedar Lean definitions and verified symbolic-analysis properties](https://github.com/cedar-policy/cedar-spec/blob/main/cedar-lean/README.md).

### 18. Undeclared build inputs and stale incremental results

Proposed call: `audit_build_inputs(snapshot, build_profile, perturbation_profile)`.

Ask which observed reads are outside declared inputs, or which controlled change makes an incremental build disagree with a clean build. A concrete finding could identify a template file omitted from a cache key.

The service needs file and process observation, environment capture, controlled perturbations, clean and incremental execution, and artifact comparison. Riker studies dependency discovery for correct incremental builds. Build Systems à la Carte separates scheduling and rebuilding decisions.

An execution trace only observes executed accesses unless stronger isolation enforces the boundary. State the operating-system coverage. This is an optional analysis service, not a requirement that every user replace their build system.

Sources: [Riker, USENIX ATC 2022](https://www.usenix.org/conference/atc22/presentation/curtsinger), [Build systems a la carte, Microsoft Research](https://www.microsoft.com/en-us/research/publication/build-systems-la-carte/), [Bazel hermeticity](https://bazel.build/basics/hermeticity).

### 19. Replay of retained cases with retained assertions

Proposed call: `replay_cases(case_bundle, candidate, runtime_profile)`.

Ask which captured cases fail against the candidate using their retained assertions, fixtures, and execution configuration. The agent can call this on a supplied bundle without adopting a prescribed development loop.

Vkit needs independent materialization of the test bundle and candidate, import isolation, a baseline control, and per-case outcomes. Reuse the existing runner and evidence store. Protect the assertions from replacement by candidate test files.

Current source inspection found that manifest approval hashes the check entry, while input changes invalidate evidence separately. Those mechanisms do not themselves retain old test bytes. This entry preserves that distinct opportunity, ranked below the computational analyzers because the user prioritized exposed tools.

Sources: [manifest digest](../src/vkit/manifest.py), [check execution](../src/vkit/runner.py), [evidence identity](../src/vkit/state.py).

### 20. Network reachability changes from configuration changes

Proposed call: `compare_network_reachability(before, after, traffic_profile)`.

Ask which modeled packet flows become reachable or unreachable after a configuration change. Return packet-header constraints and routing or filtering paths. This helps repositories containing network configuration and infrastructure changes.

Batfish supplies configuration analysis and differential reachability. Vkit needs complete snapshots, supported vendor syntax, topology assumptions, and a fixed traffic query profile.

The guarantee concerns the modeled network configuration. Missing devices, external services, and runtime state must remain visible assumptions. This is a substantial existing engine with a narrower audience than application dependency analysis.

Source: [Batfish forwarding-change validation](https://batfish.readthedocs.io/en/latest/notebooks/linked/introduction-to-forwarding-change-validation.html).

### 21. Paired workload cost comparison

Proposed call: `compare_workload_cost(before, after, workloads, measurement_profile)`.

Ask which measured workloads exceed a declared cost-change threshold under the recorded experiment. Return raw samples, workload identities, environment metadata, uncertainty, and whether noise prevents a useful conclusion.

Pyperf provides benchmark execution and analysis foundations. Vkit adds paired execution order, independent process state, matching artifacts, output-equivalence controls, and workload selection. Resource counters can complement timing when available.

This must return a measured result. Statistical significance does not establish a universal speedup, and a fixed seed does not eliminate machine noise. Extend existing performance checks instead of creating a second measurement authority.

Source: [pyperf result analysis](https://pyperf.readthedocs.io/en/latest/analyze.html).

### 22. Bounded Rust safety and assertion verification

Proposed call: `check_native_properties(snapshot, target, profile="rust-kani", bounds)`.

Ask whether a selected Rust function violates fixed safety checks or a stated assertion within the supported model. Return the checked property, assumptions, unwinding status, and counterexample when available.

Kani supplies Rust model-checking machinery. Vkit must handle the pinned Rust toolchain, function boundaries, allowed stubs, source restrictions, and witness replay. Candidate code must not alter the verifier-owned property by supplying an unchecked replacement harness.

Share native-verification execution infrastructure with the existing C operation. Preserve language-specific semantics and report incomplete unwinding. Broader proof features require a separate reviewed contract.

Source: [Kani Rust verifier](https://github.com/model-checking/kani).

### 23. LLVM transformation refinement

Proposed call: `check_ir_refinement(before_ir, after_ir, profile)`.

Ask whether the supported transformed LLVM function refines the original. This is useful for compiler work, native optimization, and source transformations lowered through a controlled compiler pipeline.

Alive2 provides LLVM translation, symbolic execution, and refinement checking. The service must pin matching LLVM and Alive2 builds, preserve target semantics, and report unsupported transformations.

Refinement is a directional relation. It must not be renamed unrestricted equivalence. Alive2 explicitly excludes interprocedural transformations and warns that unsupported use can produce spurious counterexamples. Keep those cases outside an accepted vkit profile.

Source: [Alive2](https://github.com/AliveToolkit/alive2).

### 24. Transaction-history isolation violations

Proposed call: `check_history(history, profile="transaction-isolation")`.

Ask which dependency cycles or anomalies in a supported observed transaction history violate the selected isolation model. Return the involved transactions, observed reads and writes, and anomaly explanation.

Elle provides a black-box transactional consistency checker based on cycle detection. Vkit needs a supported history format, object semantics, operation outcomes, and treatment of incomplete observations.

Extend the existing history operation. Register linearizability and transactional isolation are different specifications, so they need distinct protected profiles. An accepted finite history does not establish that all future database executions satisfy the isolation level.

Source: [Elle](https://github.com/jepsen-io/elle).

### 25. Crash-recovery violations for persistent state

Proposed call: `check_crash_recovery(snapshot, storage_profile, workload, bounds)`.

Ask whether any tested crash point produces recovered state that violates the declared persistence contract. Return the workload, crash point, durable writes or storage image, recovery procedure, and observed violation.

CrashMonkey research demonstrates bounded crash-consistency testing for file systems. Adapting that method to application files or embedded databases requires a separate crash and durability model.

This is a high-cost opportunity for storage software, editors, package managers, and durable services. Killing a process is not equivalent to modeling power loss and write persistence. A supported profile must state which failure it actually injects.

Source: [CrashMonkey, USENIX HotStorage 2017](https://www.usenix.org/conference/hotstorage17/program/presentation/martinez).

### 26. Constrained configuration combination coverage

Proposed call: `generate_configuration_cover(factors, constraints, strength, profile)`.

Ask for configurations that cover every valid t-way interaction, plus a checked coverage report. Factors can include runtime version, database backend, feature flags, and deployment mode.

NIST ACTS provides covering-array methods. Vkit must interpret constraints, generate configurations, independently enumerate or symbolically check required interaction coverage, and report any uncovered interactions. Verify an obtainable pinned distribution before choosing ACTS as a runtime backend.

The result guarantees a defined coverage relation, not absence of interaction bugs or minimum suite size. This differs from existing `minimize_cover`, which selects from a supplied coverage matrix. Reuse its selection machinery when appropriate, while adding configuration generation and constraint semantics.

Source: [NIST Automated Combinatorial Testing for Software](https://csrc.nist.gov/projects/automated-combinatorial-testing-for-software).

### 27. Retry, duplicate-delivery, and interruption counterexamples

Proposed call: `exercise_api_sequences(snapshot, operation_profile, delivery_profile, bounds)`.

Ask whether tested duplicate requests, dropped replies, or restarts violate a specified operation contract. An example is a repeated payment request creating two ledger entries despite an idempotency key.

Jepsen and Maelstrom provide workload, fault-injection, and history-analysis foundations. Maelstrom uses a particular node protocol and simulated network. It is not a drop-in tester for arbitrary web applications.

Vkit needs concrete application adapters, resettable state, fixed fault schedules, history capture, and an explicit acceptable-outcome model. Extend the stateful execution family. Report the tested failure schedules, not a universal claim of idempotence.

Source: [Maelstrom design and workloads](https://github.com/jepsen-io/maelstrom).

### 28. Rigorous floating-point round-off bounds

Proposed call: `bound_roundoff(expression, input_domain, arithmetic_profile)`.

Ask for a justified bound on the difference between a supported floating-point computation and its mathematical counterpart over the declared domain. This can catch numerical rewrites that look algebraically sound but lose precision.

FPTaylor provides rigorous round-off analysis. The service must extract the exact computation, rounding modes, supported operations, and input ranges. It must preserve overflow, exceptional-value, and domain restrictions rather than silently treating machine numbers as real numbers.

This is distinct from the current mathematical integer rewrite checker. A failed attempt to establish a small bound is not itself a concrete precision failure.

Source: [FPTaylor](https://github.com/soarlab/FPTaylor).

### 29. Regular-expression backtracking cost and attack witnesses

Proposed call: `analyze_regex_cost(pattern, engine_profile, bounds)`.

Ask for the analyzer's complexity classification under supported matching semantics, and a concrete attack input when available. This addresses expressions that accept the intended language but take excessive work on certain inputs.

Recheck combines static analysis and fuzzing for ReDoS detection. Vkit needs fixed dialect and engine semantics, supported flags, isolated witness execution, and separate reporting of analyzed complexity and measured runtime.

This is distinct from `compare_matchsets`, which compares accepted languages. Equal languages can have different execution costs in a backtracking engine. Unsupported constructs and timeouts must produce an incomplete result.

Sources: [recheck](https://makenowjust-labs.github.io/recheck/), [diagnostic and complexity categories](https://makenowjust-labs.github.io/recheck/docs/usage/diagnostics/).

### 30. Inputs that reach a selected native-code path

Proposed call: `find_path_input(snapshot, target, path_condition, environment_profile, bounds)`.

Ask for an input that reaches a selected branch, assertion, or error condition in supported native code. Return the generated input and a concrete replay trace.

KLEE symbolically executes LLVM bitcode and supports modeled inputs such as files and command-line arguments. The service needs a matching compiler runtime, controlled library models, input extraction, and replay against the intended executable.

This can generate hard-to-reach cases without the agent constructing symbolic execution plumbing. Failure to find an input within a budget does not establish path unreachability. Binary-only support would require a separately assessed frontend or engine.

Source: [KLEE documentation and tutorials](https://klee-se.org/docs/).

### 31. C function contracts beyond bounded unwinding

Proposed call: `prove_c_contract(snapshot, function, contract, profile)`.

Ask whether supported C code satisfies a declared precondition and postcondition under a fixed memory and arithmetic model. Return the obligations, discharged results, and unproved obligations.

Frama-C WP generates proof obligations from ACSL contracts using weakest-precondition calculus. Vkit must validate contract interpretation, dependencies on function summaries, permitted axioms, and proof completion.

This is useful when loop invariants can establish a property beyond a finite unwinding. It extends native verification rather than replacing the current bounded C safety question. An agent-written annotation is input to verification, not trusted proof of the user's intended requirement.

Source: [Frama-C WP](https://www.frama-c.com/fc-plugins/wp.html).

### 32. Affine memory-access overlap and loop dependencies

Proposed call: `find_access_overlap(loop_a, loop_b, access_profile)`.

Ask which iterations can access the same modeled memory coordinate, and whether a supplied ordering creates a dependency. Return a pair of iterations or an exact relation over supported parameters.

ISL provides integer sets and relations suitable for affine loop analysis. Vkit needs faithful source extraction, array identity, bounds, and read versus write classification. Alias assumptions must be explicit.

Reuse the installed ISL backend. This is a different question from equality of yielded coordinate sets. A source extractor that misses an alias cannot establish that a real loop is safe to parallelize.

Source: [ISL manual](https://libisl.sourceforge.io/user.html).

### 33. Queries over deterministically replayed executions

Proposed call: `inspect_recorded_execution(recording, event, query_profile)`.

Ask a question about an actual captured execution, such as which recorded write changed a location before a selected failure. Return the recorded event, relevant state, and replay position.

Rr supplies record-and-replay debugging. Vkit must capture an executable and dependencies, own debugger interaction, map source locations, and package query results without exposing a generic debugger scripting channel.

This requires a supported Linux host and recording environment. A recording explains one execution. It does not establish behavior of executions not recorded. It is particularly valuable for failures that are difficult to reproduce normally.

Source: [rr](https://rr-project.org/).

### 34. Scheduling, blocking, and resource facts from traces

Proposed call: `explain_trace_interval(trace, interval, profile)`.

Ask how much of a captured interval was running, blocked, or attributable to supported recorded activities. Return events and interval arithmetic that support each observation.

Perfetto provides trace ingestion and a substantial SQL analysis engine. Vkit needs fixed queries, trace-format validation, process and thread identity handling, clock-domain treatment, and detection of dropped events.

The service should answer trace questions with evidence, not invent a root cause. Recorded overlap or temporal order alone does not establish causality. Keep this separate from rr replay, while sharing artifact storage and source-location handling.

Source: [PerfettoSQL and Trace Processor](https://perfetto.dev/docs/analysis/perfetto-sql-getting-started).

### 35. Expression synthesis under a fixed grammar and specification

Proposed call: `synthesize_expression(specification, grammar_profile, bounds)`.

Ask for an expression from a supported grammar that satisfies the supplied formal relation. Examples include a boundary predicate or a small pure arithmetic expression. Return the expression and an independent validation result where supported.

Cvc5 supports syntax-guided synthesis. Rosette supplies solver-aided programming foundations. Vkit must own the grammar semantics, lower the specification, constrain resource use, and validate any source-level insertion.

Reuse the existing cvc5 backend first. Exposing arbitrary SMT or arbitrary solver code would leave the main construction work to the agent. Optimality requires a defined cost and completed optimization, not merely the first synthesized answer.

Sources: [cvc5 SyGuS example](https://cvc5.github.io/docs/cvc5-1.3.1/examples/sygus-fun.html), [Rosette](https://github.com/emina/rosette).

### 36. Termination and complexity bounds for supported programs

Proposed call: `prove_termination_bound(program, domain, profile)`.

Ask whether a supported transition system terminates and what established step-complexity bounds apply. Return the model, assumptions, proof status, and available certificate or witness.

AProVE provides termination and complexity analysis for several program representations. Vkit needs a constrained frontend, source-to-model correspondence, certificate interpretation, and accurate result mapping.

This can address accidental nontermination or a proved complexity regression. Unsupported language behavior and an unsuccessful proof remain unresolved. A step-complexity result is not a wall-clock performance guarantee.

Source: [AProVE input and result documentation](https://aprove.informatik.rwth-aachen.de/usage).

### 37. Floating-point precision selection under an error budget

Proposed call: `tune_float_precision(expression, domain, error_budget, profile)`.

Ask for an assignment of permitted precisions satisfying a declared numerical error bound, with a result under the selected cost model. Return the assignment and validated error analysis.

FPTuner explores mixed-precision choices using rigorous numerical analysis. A vkit integration needs faithful expression extraction, supported target arithmetic, reproducible solver configuration, and independent checks of the selected assignment.

Share the numerical model with round-off analysis. An optimal assignment under a bit-width cost model is not automatically fastest on a target CPU. Keep deployment feasibility and current upstream maintenance as implementation questions.

Source: [FPTuner](https://github.com/soarlab/FPTuner).

### 38. Protected source migrations with checked postconditions

Proposed call: `apply_checked_transformation(snapshot, transformation_profile)`.

Ask for a patch produced by a fixed migration whose declared preconditions hold, with checked postconditions. A useful profile could replace a specific deprecated API throughout supported C code and reject sites it cannot classify.

Coccinelle provides semantic patch matching and transformation. Vkit must own the transformation definitions, handle incomplete parsing, check all relevant sites, and validate profile-specific postconditions. Compilation can be one check but does not prove behavioral preservation.

Return the patch, matched sites, rejected sites, and exact verified conditions. Do not describe a generic semantic patch as a verified refactoring. This remains a design until at least one substantial migration has a defensible contract.

Source: [Coccinelle semantic patch examples](https://coccinelle.gitlabpages.inria.fr/website/rules/).

### 39. Candidate invariants supported by supplied execution traces

Proposed call: `infer_candidate_invariants(traces, variable_profile)`.

Ask which predicates from a fixed candidate family hold on the supplied recorded observations. Return each predicate, its observation coverage, and counterobservations for rejected candidates where useful.

Daikon dynamically detects likely invariants. Vkit would need reliable trace instrumentation, variable identity, candidate management, and a separate proof or counterexample-search stage for promotion.

This is deliberately a candidate-producing tool. It must never report that a sampled invariant holds for all executions. Keep it low priority if vkit is restricted to conclusive model results and concrete execution facts. The idea is preserved because it can supply useful formal questions for other tools.

Source: [Daikon dynamic invariant detector](https://plse.cs.washington.edu/daikon/).

### 40. Cache-key collisions that change observable results

Proposed call: `find_cache_key_collision(function, key_function, domain, profile)`.

Ask whether two supported inputs can produce the same cache key but different results. A witness is a pair `x` and `y` with equal keys and unequal specified outputs.

This is a proposed relational-analysis operation built from solver-aided foundations. Vkit can initially reuse the existing pure-function extraction and cvc5 machinery. Broader support needs effect and environment modeling.

The tool is especially relevant to memoization, build caches, and request caching. A proof about pure functions does not cover hidden database state, clocks, or environment variables. Do not expose a general cache-correctness claim when those inputs are unmodeled.

Sources: [Rosette solver-aided programming](https://github.com/emina/rosette), [vkit's current computations](computations.md).

### 41. Grammar ambiguity witnesses and parser conflicts

Proposed call: `find_grammar_ambiguity(grammar, parser_profile, bounds)`.

Ask for a token sequence with two distinct derivations, or a reported parser conflict with its distinguishing derivations. This is useful for DSLs, configuration languages, and compiler repositories.

Bison generates counterexamples for grammar conflicts. Vkit needs grammar capture, safe handling of embedded actions, token identity, derivation validation, and result classification.

A parser conflict alone does not prove grammar ambiguity. Some conflicts reflect limits of the parser construction. A validated pair of derivations for the same sequence is the stronger witness. An unsuccessful bounded search must not claim that an arbitrary grammar is unambiguous.

Source: [GNU Bison counterexamples](https://www.gnu.org/software/bison/manual/html_node/Counterexamples.html).

### 42. Counterexamples to fixed metamorphic relations

Proposed call: `check_metamorphic_relation(snapshot, target, relation_profile, domain, bounds)`.

Ask whether tested related inputs violate a fixed relation between outputs. Examples include lossless codec round trips or permutation invariance for a function whose specified result is a multiset statistic.

Metamorphic testing research addresses cases where a useful relation is available without a full expected-output oracle. Vkit needs protected domain-specific relations, valid input transformations, state isolation, replay, and reduction. A relation must apply to the selected function's contract.

This extends existing property checks. It does not justify inventing universal relations for arbitrary functions. A relation violation is useful evidence. Passing generated cases remains a bounded execution result.

Sources: [IEEE and ACM paper on the oracle problem](https://ieeexplore.ieee.org/document/7166267/), [author-hosted ACM Computing Surveys review](https://i.cs.hku.hk/~tse/Papers/2010s/hlmtCSUR.html).

### 43. Exact symbolic algebra in a declared mathematical domain

Proposed call: `compare_symbolic_expressions(expression_a, expression_b, domain_profile)`.

Ask whether two supported polynomial or rational expressions denote the same function on the declared domain. Return a checked normal-form identity or a domain-qualified disagreement result.

SymPy supplies computer-algebra foundations. Vkit must define coefficient domains, variable assumptions, excluded poles, and the exact decision procedure. A generic simplification result or an inconclusive equality heuristic cannot become a universal equality answer.

Keep this separate from Python execution semantics. It can extend the mathematical-expression family when it adds a domain not covered by the current integer and boolean rewrite checker. Conditional identities must preserve their conditions.

Source: [SymPy](https://github.com/sympy/sympy).

## Consolidate before adding public operations

| Shared family | Ranked capabilities | Integration decision |
| --- | --- | --- |
| Source graphs | 1, 4, 12 | Share extraction and identities. Keep dependency reachability, data flow, and lifetime semantics distinct. |
| Contract compatibility | 2, 6, 13 | Extend or replace the existing protobuf-specific interface once callers can migrate together. Use explicit format profiles. |
| Scenario and property execution | 5, 16, 19, 27, 42 | Reuse state reset, execution isolation, replay, and case storage. Different oracle relations stay explicit. |
| Native verification | 22, 23, 30, 31 | Share toolchain capture and result handling. Keep safety, refinement, path search, and deductive proof as distinct questions. |
| History analysis | 24 and existing `check_history` | Add transaction models to the existing operation. Do not create a second generic history checker. |
| Coverage optimization | 26 and existing `minimize_cover` | Add valid configuration generation and coverage checking before reusing matrix selection. |
| Numerical analysis | 28, 32, 37, 43 | Reuse appropriate backends. Integer sets, floating-point error, and symbolic algebra retain different mathematical domains. |
| Solver-backed pure functions | 11, 35, 40 and existing `check_rewrite` | Reuse source extraction where semantics match. Concrete difference search, synthesis, and cache-key verification are different result relations. |
| Build and performance execution | 9, 18, 21 | Share captured inputs and isolated execution. Installation, incremental correctness, and measurement remain separate claims. |
| Reduction | All concrete failing cases | Reuse `reduce_failure` when its failure predicate and input model fit. New reducers are internal backends. |

Do not add a generic workflow language, arbitrary executable endpoint, or dynamic verifier plugin loader to host this backlog. The current [operation contract](adding-operations.md) already supplies the extension point. A substantial internal implementation can still have a small public interface.

## Preserve the original 22-engine investigation

The earlier candidate set is retained here by disposition. Shipped status comes from the current [engine roadmap](engine-roadmap.md), not from this research list.

| Original engine | Current disposition |
| --- | --- |
| cvc5 | Shipped in `check_rewrite`. Reuse for synthesis and supported relational questions at ranks 35 and 40. |
| egglog | Shipped in `simplify_function`. Extend only with a new exact domain and independently checked rules. |
| Perses | Shipped in `reduce_failure`. Reuse for new counterexamples when supported. |
| greenery | Shipped in `compare_matchsets`. Regex cost analysis at rank 29 asks a different question. |
| Porcupine | Shipped in `check_history`. Keep schedule exploration at rank 10 distinct. |
| OR-Tools | Shipped in `minimize_cover`. Rank 26 adds configuration generation rather than another supplied-matrix selector. |
| Buf | Shipped in `compare_contracts` with `protobuf-*` profiles, consolidated with OpenAPI at rank 2. Ranks 6 and 13 remain proposals. |
| ISL | Shipped through islpy in `compare_iteration_sets`. Reuse for rank 32. |
| CBMC | Shipped in restricted `check_c_safety`. Native extensions need their own explicit supported models. |
| Alive2 | Preserved at rank 23. Matching LLVM builds and transformation scope remain prerequisites. |
| Soufflé | Preserved as a foundation for rank 1 and other recursive source analyses. Extraction is separate work. |
| SymPy | Preserved at rank 43. Require a new declared mathematical domain. |
| rr | Preserved at rank 33. Requires a supported recording host. |
| KLEE | Preserved at rank 30. Requires a pinned bitcode runtime and environment model. |
| LLVM and Clang | Internal frontend and toolchain foundation for ranks 12, 13, 23, 30, and 31. No standalone compilation-as-verification endpoint. |
| Z3 | Possible internal solver. Another generic solver endpoint does not add a new useful question. |
| egg | Alternative equality-saturation backend. Avoid duplicating the existing egglog operation. |
| automata-lib | Alternative automata backend. Avoid duplicating the existing match-set operation. |
| C-Vise | Potential native-code reducer behind `reduce_failure`. Not a separate public function by default. |
| Coccinelle | Preserved at rank 38. Requires a fixed useful migration and checked postconditions. |
| ESBMC | Alternative native-verification foundation. Inspect each language frontend before exposing its claims. |
| SQLSolver | Preserved at rank 15. Its negative result requires careful interpretation and preferably a concrete replayed witness. |

The original candidate identities and their dispositions are preserved above. The tracked engine roadmap contains their upstream repository links. This backlog does not depend on temporary session artifacts remaining available.

## Keep research-derived ideas without overstating readiness

These ideas deserve preservation even when the complete vkit operation does not exist:

- Rank 14 adapts relational merge verification to isolated agent edits. The paper demonstrates a restricted implementation. General source-language support remains open.
- Rank 18 applies incremental-build correctness research to an optional audit service. The audit need not replace the user's build system.
- Rank 25 adapts bounded crash-consistency exploration to application persistence. A faithful durability model is the difficult part.
- Rank 38 combines semantic transformations with explicit preconditions and postconditions. Transformation machinery alone does not provide the verification contract.
- Rank 40 checks a relational cache property with a solver. Hidden state and effects are the main barrier to a broad implementation.
- Rank 42 packages domain-specific metamorphic relations. The relation's validity must be established for the selected domain.

Additional research foundations include [CHESS controlled concurrency testing](https://www.microsoft.com/en-us/research/publication/chess-a-systematic-testing-tool-for-concurrent-software/?lang=ja), [NIST combinatorial coverage measurement](https://www.nist.gov/publications/combinatorial-coverage-measurement), and [Rosette solver-aided programming](https://github.com/emina/rosette). These support methods. They do not establish the correctness of an unbuilt vkit integration.

## Community discussions are leads

- [Mutation testing discussion on r/programming](https://www.reddit.com/r/programming/comments/mpoe5y/) connects practical concerns about test effectiveness with property and round-trip testing. It is a motivation lead for ranks 3, 16, and 42. Technical contracts above rely on upstream documentation and research.
- [Technical debt discussion on r/ExperiencedDevs](https://www.reddit.com/r/ExperiencedDevs/comments/1wgiket/whats_tech_debt_nowadays/) raises questions about how generated changes affect accumulated complexity. It motivates examining dependency and change-analysis tools. It is anecdotal and does not establish how often generated code causes defects.

## Do not spend implementation effort on these substitutes

- An overall slop score, maintainability score, or LLM-quality verdict. None answers the exact questions above.
- A checklist reminding the agent to test, review, or remember earlier decisions. The product supplies computations the agent elects to invoke.
- Comment removal or syntax compaction as a major verification capability. They can remain utilities if useful.
- A new public endpoint for each interchangeable solver, reducer, or automata library.
- Clone counts labeled semantic redundancy. Similar source text does not establish that two components have interchangeable behavior.
- Automatic conversion of prose requirements into trusted formal specifications. A proposed specification is still input requiring assessment.
- Unqualified pass results after timeouts, unsupported extraction, skipped obligations, or incomplete histories.

## Recommended implementation investigations

Start with dependency analysis, OpenAPI compatibility, and mutation analysis. They cover common whole-repository failures and have reusable implementations. Define one supported language or format profile for each first.

Investigate data-flow analysis and stateful API execution next. Both are substantial, but framework modeling and execution isolation determine whether the returned evidence is useful.

Keep JSON containment close to the compatibility work. Keep migration replay and release-artifact execution close to the existing runner. Preserve the specialized and research entries until a concrete repository need or a validated integration makes their cost worthwhile.

For each investigation, settle the exact question, input model, protected logic, backend behavior, witness format, unsupported cases, and overlap with current operations before adding another catalog entry.
