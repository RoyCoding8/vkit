# Engine integration queue

This queue preserves the 22-repository investigation from October 8, 2026. Integration means a
fixed operation with a stated input model, bounded execution, and exercised backend behavior.
A repository's popularity does not establish that an operation is ready.

## Built operations

| Engine | Operation | Exact scope |
| --- | --- | --- |
| [cvc5](https://github.com/cvc5/cvc5) | `check_rewrite` | Equal return values of supported pure integer/boolean functions |
| [egglog](https://github.com/egraphs-good/egglog) | `simplify_function` | Fixed rewrite rules, fixed expression cost, independent cvc5 check |
| [Perses](https://github.com/uw-pluverse/perses) | `reduce_failure` | Smaller input that still reproduces a recorded approved failure |
| [greenery](https://github.com/qntm/greenery) | `compare_matchsets` | Full-match languages in a fixed dialect and finite alphabet |
| [Porcupine](https://github.com/anishathalye/porcupine) | `check_history` | Linearizability of one supplied register or FIFO history |
| [OR-Tools](https://github.com/google/or-tools) | `minimize_cover` | Minimum-cost coverage of supplied required IDs, with checked selection and exact integer cost |
| [Buf](https://github.com/bufbuild/buf) | `check_proto_compatibility` | Selected built-in compatibility category, protected configuration, captured local schemas |
| [isl](https://github.com/Meinersbur/isl) via [islpy](https://github.com/inducer/islpy) | `compare_iteration_sets` | Equality of yielded integer-coordinate sets extracted from supported affine generators |

These operations use the existing catalog contract. Adding an engine does not justify
another transport, plugin loader, or command executor. Native Windows has no islpy wheel in the pinned
2026.2.2 release. Linux and macOS must exercise that backend; Windows must report its absence accurately.

## Current batch

| Engine | Operation being built | Acceptance condition |
| --- | --- | --- |
| [CBMC](https://github.com/diffblue/cbmc) | `check_c_safety` | Fixed memory and integer safety checks on a supported standalone C function, with unwinding assertions and no compiler execution |

## Further candidates

| Repository | Next exact question or reason to defer |
| --- | --- |
| [Alive2](https://github.com/AliveToolkit/alive2) | Does this supported LLVM transformation refine the original? First establish a reproducible matching LLVM build and exclude unsupported interprocedural transformations. |
| [Souffle](https://github.com/souffle-lang/souffle) | Which nodes are reachable under a protected dependency relation? First define a useful source extractor and its completeness boundary. |
| [SymPy](https://github.com/sympy/sympy) | Are two exact symbolic expressions equal under a declared mathematical domain? Avoid duplicating the existing integer rewrite operation. |
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
