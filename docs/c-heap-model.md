# C heap and call-graph safety

`check_c_safety` accepts `profile="heap-callgraph"` to check a captured C source file across
up to 16 acyclic functions. Its existing `standalone` profile remains the default. The heap
profile uses CBMC 6.11.0 under C11, x86_64 Linux LP64 and little-endian arithmetic, regardless
of the host platform. Entry parameters are unconstrained `int`, `unsigned int` or `_Bool`.

Helpers may pass and return single-level pointers. Locals need explicit initializers.
Allocation sizes are fixed positive expressions of integer literals and supported scalar
`sizeof`, at most 4096 bytes. The installed verifier supplies allocator declarations and
helper prototypes. It rejects headers, macros, user declarations of allocators, globals,
recursion, structs, arrays, function pointers, external calls and CBMC intrinsics. Calls and
mutations occupy complete statements or supported initializer, assignment and return positions.
The source limit is 64 KiB, with at most 4096 AST nodes and depth 64.

The fixed checks cover pointer accesses, bounds, division by zero, signed overflow, shifts,
allocator preconditions and memory leaks. Allocation explicitly may return null. `SAFE`
requires completion of every generated property, including loop unwinding assertions.
The heap profile requires a completed memory-leak property even in a source without allocation.
In the standalone profile, a function with no potentially unsafe operations can have no generated
properties; a completed CBMC success then establishes that empty obligation set.
`COUNTEREXAMPLE` requires a recognized failing property and its actual CBMC failure trace.
A witnessed violation remains useful when unrelated properties are unfinished. Insufficient
unwinding without such a witness returns `UNKNOWN`.

Traces refer to the normalized captured source, whose bytes and SHA-256 are returned for a
counterexample. These are trusted model-checker traces, not independent native executions.
Checks do not establish all forms of C undefined-behavior absence, initialized heap reads,
thread safety, resource use in external libraries or behavior outside the declared model.
Memory leaks are checked on terminating modeled paths.

The operator configures `VKIT_CBMC_BIN` and `VKIT_CBMC_SHA256`. This profile also needs
`VKIT_C_PREPROCESSOR_BIN` and `VKIT_C_PREPROCESSOR_SHA256`, pointing to an installed `gcc`
or `gcc.exe`. CBMC uses that GCC runtime to preprocess its own built-in library models.
Candidate source is parsed and normalized into a preprocessed `.i` snapshot; project build
scripts and compiler configuration do not execute. The child environment exposes only the
configured GCC directory through `PATH`, and the result records its binary digest. The GCC
installation and its dependencies belong to the trusted verifier deployment. The standalone
profile needs no GCC runtime. Missing or mismatched configured binaries return `UNAVAILABLE`.
