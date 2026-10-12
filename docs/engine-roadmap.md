# Engine integration queue

This queue preserves the 22-repository investigation from October 8, 2026. Integration means a
fixed operation with a stated input model, bounded execution, and exercised backend behavior.
A repository's popularity does not establish that an operation is ready.

The [ranked tool backlog](tool-backlog.md) expands this queue with 43 capability proposals,
including research-derived ideas, precise questions, integration boundaries, and consolidation plans.

The shared integration plug is the versioned installed operation contract. Agents query MCP `help`
for enabled capabilities and input schemas. Operator switches in the console or `vkit tools` persist
in the existing Git-excluded settings file and filter discovery and dispatch. Follow
[Add a built-in operation](adding-operations.md) rather than adding a separate registry or static tool list.

## Built operations

| Engine | Operation | Exact scope |
| --- | --- | --- |
| [Batfish](https://github.com/batfish/batfish) | `compare_network_paths` | Successful reachability changes in two captured IPv4 configuration models; real service acceptance is required separately |
| [Perfetto](https://perfetto.dev/) | `explain_trace_interval` | Exact clipped scheduler-state facts in a supplied trace, with loss and coverage limitations |
| [TypeScript](https://github.com/microsoft/TypeScript) | `compare_contracts`, `typescript-api` profile | Strict assignability of checked exported declarations from closed local `.d.ts` inputs; no runtime or universal consumer compatibility claim |
| [Gappa](https://gappa.gitlabpages.inria.fr/) | `analyze_roundoff` | Round-off budget and feasible uniform-format certification for an explicit arithmetic DAG; trusted engine result without a separate proof-kernel check |
| [cvc5](https://github.com/cvc5/cvc5) | `check_rewrite` | Equal return values of supported pure integer/boolean functions |
| [egglog](https://github.com/egraphs-good/egglog) | `simplify_function` | Fixed rewrite rules, fixed expression cost, independent cvc5 check |
| [Perses](https://github.com/uw-pluverse/perses) | `reduce_failure` | Smaller input that still reproduces a recorded approved failure |
| [greenery](https://github.com/qntm/greenery) | `compare_matchsets` | Full-match languages in a fixed dialect and finite alphabet |
| [Porcupine](https://github.com/anishathalye/porcupine) | `check_history` | Linearizability of one supplied register, FIFO, or atomic multi-key transaction history |
| [OR-Tools](https://github.com/google/or-tools) | `minimize_cover` | Minimum-cost coverage of supplied required IDs, with checked selection and exact integer cost |
| [dependency-cruiser](https://github.com/sverweij/dependency-cruiser) | `analyze_dependencies` | Captured JavaScript/TypeScript module graph, paths, cycles, reverse impact, and entrypoint reachability with unresolved edges reported |
| [StrykerJS](https://github.com/stryker-mutator/stryker-js) | `mutation_survivors` | Fixed JavaScript mutations tested against a fresh approved Node baseline, with survivor replay and explicit inconclusive or unexamined cases |
| [oasdiff](https://github.com/oasdiff/oasdiff) | `compare_contracts`, `openapi` profile | Validated local OpenAPI reference closures and fixed breaking-change rules |
| [Buf](https://github.com/bufbuild/buf) | `compare_contracts`, `protobuf-*` profiles | Selected built-in compatibility category, protected configuration, captured local schemas |
| [isl](https://github.com/Meinersbur/isl) via [islpy](https://github.com/inducer/islpy) | `compare_iteration_sets` | Equality of yielded integer-coordinate sets extracted from supported affine generators |
| [CBMC](https://github.com/diffblue/cbmc) | `check_c_safety` | Standalone scalar/array safety and a [closed acyclic heap profile](c-heap-model.md) for lifetime and leak checks, with complete unwinding required |
| [jsonsubschema](https://github.com/IBM/jsonsubschema) | `compare_contracts`, `json-containment` profile | Directional containment in a fixed Draft 4 type/object/array fragment, with unsupported constructs rejected and no generated witness |
| [resolvelib](https://github.com/sarugaku/resolvelib) and packaging | `resolve_dependencies` | Independently checked Python constraint assignment within captured candidates and explicit target marker environment |
| SQLite | `compare_migrations` | Exact stored catalog SQL and supplied ordered observations after captured upgrade and fresh-install SQL |
| [OR-Tools](https://github.com/google/or-tools) | `generate_configurations` | Minimum-cardinality coverage of feasible bounded t-way factor interactions under forbidden partial assignments |
| [Lark](https://github.com/lark-parser/lark) | `find_grammar_ambiguity` | Two checked derivation trees for a supplied string, or absence of ambiguity only in the supplied corpus |
| [cvc5 SyGuS](https://github.com/cvc5/cvc5) | `synthesize_function` | Candidate in a fixed linear integer grammar, returned only after independent full-domain equivalence proof |
| [Recheck](https://github.com/makenowjust-labs/recheck) | `analyze_regex_cost` | Completed automaton backtracking-model complexity estimates and theoretical attack descriptions, with no target runtime replay claim |
| [Cedar SymCC](https://github.com/cedar-policy/cedar) | `compare_authorization_policies` | Schema-scoped authorization equivalence, with concrete authorizer replay of distinguishing requests |
| [SQLGlot](https://github.com/tobymao/sqlglot), cvc5 and SQLite | `compare_sql_queries` | Exact SELECT bag equivalence within declared finite row/value bounds, with SQLite counterexample replay |
| [packaging](https://github.com/pypa/packaging) and wheel inspection | `compare_wheels` | Validated static installation scheme/payload mapping, metadata and executable-bit differences; no runtime installation claim |
| [CrossHair](https://github.com/pschanely/CrossHair) | `find_behavioral_difference` | Concrete differences in a closed Python language, confirmed by two fresh CPython replays; no equivalence claim |
| [SymPy](https://github.com/sympy/sympy) | `compare_rational_expressions` | Rational-function identity over exact coefficients, independently checked by sparse polynomial arithmetic with original division guards |
| cvc5 and vkit's pure function frontend | `check_semantic_merge` | All-input scalar return preservation across captured base/left/right/merged functions, with independent witness evaluation |
| cvc5 and integer loop extraction | `analyze_loop_termination` | Checked linear rankings over all guarded states; replayed fixed-point nontermination witnesses; bounded synthesis may remain unknown |
| Approved scenario drivers and paired bootstrap analysis | `compare_workload_cost` | Alternating before/candidate campaigns in fresh workspaces with captured harness bytes, raw measurements and uncertainty intervals |
| Retained approved scenario drivers | `replay_retained_cases` | Old assertions and fixtures against a current captured subject, after a clean retained control, with per-case observations |
| [Frama-C WP](https://www.frama-c.com/fc-plugins/wp.html) | `prove_c_contract` | Scalar C pre/postconditions and loop invariants beyond unwinding, with precondition feasibility, runtime guards and Qed obligation completion |
| [Joern](https://github.com/joernio/joern) | `find_data_flows` | Captured C interprocedural parameter-to-argument paths with fixed queries, parse-completion checks and modeled endpoint evidence |
| ISL named-array access relations | `analyze_affine_accesses` | Exact ordered RAW, WAR, WAW and read/read relations under explicit alias assumptions, with replayed iteration witnesses |
| TLC 2.19 and exact finite state saturation | `check_protocol_model` | All reachable states of a closed supplied protocol model, checked invariants/deadlocks, explicit crash/restart and replayed counterexamples |
| cvc5 relational function queries | `find_cache_key_collision` | Equal keys with different results over two independent pure-function input vectors, with replayed witnesses |
| Daikon 5.9.0 and exact sample checking | `infer_trace_relations` | Candidate relations among supplied states or consecutive trace pairs, independently checked against all corresponding samples |

These operations use the existing catalog contract. Adding an engine does not justify
another transport, plugin loader, or command executor. Native Windows has no islpy wheel in the pinned
2026.2.2 release. Linux and macOS must exercise that backend; Windows must report its absence accurately.

## Further candidates

| Repository | Next exact question or reason to defer |
| --- | --- |
| [Alive2](https://github.com/AliveToolkit/alive2) | Does this supported LLVM transformation refine the original? First establish a reproducible matching LLVM build and exclude unsupported interprocedural transformations. |
| [Souffle](https://github.com/souffle-lang/souffle) | Which nodes are reachable under a protected dependency relation? First define a useful source extractor and its completeness boundary. |
| [rr](https://github.com/rr-debugger/rr) | Which write preceded this point in a recorded execution? Requires a Linux host with supported performance counters and captured traces. |
| [KLEE](https://github.com/klee/klee) | Which inputs reach a selected supported native-code path? Requires a pinned LLVM runtime and explicit environment model. |
| [LLVM/Clang](https://github.com/llvm/llvm-project) | Reusable source frontend for native-code operations. Compilation alone is not a behavioral proof. |
| [Z3](https://github.com/Z3Prover/z3) | Potential internal solver. Exposing a second generic solver would duplicate existing machinery. |
| [egg](https://github.com/egraphs-good/egg) | Alternative equality-saturation implementation. Current egglog operation already covers the selected question. |
| [automata-lib](https://github.com/caleb531/automata) | Alternative automata implementation. Current greenery operation already covers match-set comparison. |
| [C-Vise](https://github.com/marxin/cvise) | C/C++ reduction alternative. Reuse `reduce_failure` if a native-code backend earns its deployment cost. |
| [Coccinelle](https://github.com/coccinelle/coccinelle) | Source transformation engine. First define a protected transformation with useful independently checkable postconditions. |
| [ESBMC](https://github.com/esbmc/esbmc) | Inspect supported source semantics before exposing language-level claims, especially for Python. |
| [SQLSolver](https://github.com/SJTU-IPADS/SQLSolver) | Its documented negative result can include equivalent queries. Require a validated counterexample before exposing definitive inequivalence. |

An engine stays queued when its operation duplicates an existing question, its source model is unclear,
or its runtime cannot yet be exercised. The queue records opportunities, not promised correctness claims.
