# Affine array access relations

`analyze_affine_accesses(path, function, timeout_ms=2000)` reports exact access
overlap and ordered memory-dependence relations for a restricted Python function.
It reuses the installed `islpy==2026.2.2` integer-set backend and the affine
integer parser used by `compare_iteration_sets`.

The initial profile accepts one selected top-level synchronous function, plain
`int` parameters, one lexical nest of built-in `range` loops, affine bounds and
subscripts, affine boolean branch conditions, named array bases, scalar arithmetic
and subscript stores. Descending and strided ranges retain Python's execution order.
Every modeled access must belong to the same loop nest. Array-dependent branch
conditions, scalar assignment, calls, indirect bases, slices, pointer semantics,
and separate loop nests are unsupported. The function source is parsed, never run.

Each access is modeled as a relation from mathematical-integer parameters and loop
iteration coordinates to a named array and affine element coordinate. For ordered
access instances to the same name and coordinate, the result distinguishes:

- `RAW`: an earlier write and later read;
- `WAR`: an earlier read and later write;
- `WAW`: an earlier write and later write;
- `READ_READ`: an overlap between reads.

Relations include the source branch and loop constraints and the Python loop schedule.
Each nonempty relation includes an exact ISL relation plus one witness, checked again
with a separate integer evaluator against both accesses' guards, coordinates and
execution order. Empty results apply only to the captured source model.

The fixed assumptions treat different named array bases as different abstract arrays
and treat each subscript as a pure memory access with an unbounded mathematical
coordinate. The operation does not infer aliases, Python object identity, allocation,
lengths, bounds, values, or custom `__getitem__`/`__setitem__` effects. Consequently an
empty dependence set is not a general proof that arbitrary Python code can be
parallelized. The caller must accept this abstract named-distinct-array model. No
partial result is returned when a source construct cannot be modeled.

Input is bounded by the existing 64 KiB source and 2,000-node frontend limits. The
worker bounds access events, pair checks, relation count and JSON output; the outer
worker deadline is 1–60 seconds. Linux and macOS use the exact pinned ISL backend.
Native Windows returns `UNAVAILABLE` because the pinned islpy release has no native
wheel there.

Statuses are `ANALYZED`, `UNSUPPORTED`, `UNAVAILABLE`, and `UNKNOWN`. A timeout,
relation explosion, or failed witness replay is `UNKNOWN`; it is never interpreted as
no dependence.
