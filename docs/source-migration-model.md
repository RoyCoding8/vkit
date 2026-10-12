# TypeScript symbol rename preview

`preview_typescript_symbol_rename(root, path, old_name, new_name)` returns a patch for one TypeScript symbol rename. It never writes to the project. The patch covers source files captured under `root` and reports each matched byte span.

The operation captures `.ts` and `.tsx` files, including `.d.ts` declarations, while skipping `.git`, `.hg`, `node_modules`, `.venv`, `dist`, `build`, and `coverage` directories. The target must be a `.ts` or `.tsx` source file. The capture allows at most 128 files, 262,144 bytes per file, 2 MiB total source, and 10,000 tree entries. It rejects symlinks, reparse points, invalid UTF-8, oversized files, and a source tree that changes during capture.

The helper uses TypeScript 5.9.3 with fixed strict, no-emit, ES2022, and bundler-resolution options. It does not read `tsconfig.json`. Imports and exports must use literal relative paths that resolve inside the captured project. Type references, package imports, dynamic imports, `require`, `eval`, ambient external modules, and string literals equal to the selected name are unsupported. The TypeScript default libraries come from the installed pinned compiler.

The target name must identify one source declaration in `path`. The supported declarations are functions, classes, interfaces, type aliases, enums, and variables. The helper requires a clean baseline program, asks the TypeScript Language Service for rename sites, and compares those sites with a compiler-checker binding graph. It applies the edits in memory, checks the result with a new compiler program, and verifies that each captured direct reference and retained import alias still resolves to the renamed declaration. A shorthand import or export becomes an explicit alias so its existing local name and uses stay unchanged.

`PREVIEW` means that all these checks passed and includes the patch, matched sites, and postconditions. `UNSUPPORTED` means that the project or rename falls outside the fixed profile or has compiler diagnostics. `UNKNOWN` means the helper timed out or could not return consistent evidence. `UNAVAILABLE` means the pinned helper, lockfile, or Node runtime is missing or has the wrong identity.

The claim is limited to compiler bindings within this captured source tree. It does not cover consumers outside the tree, JavaScript files, runtime behavior, reflective lookups, names stored in comments or computed dynamically, or the behavior of the selected function.

Install the existing TypeScript backend with:

```powershell
npm ci --prefix tools/typescript --ignore-scripts
$env:VKIT_TYPESCRIPT_RENAME_HELPER = (Resolve-Path tools/typescript/migration-helper.cjs).Path
$env:VKIT_TYPESCRIPT_RENAME_HELPER_SHA256 = (Get-FileHash $env:VKIT_TYPESCRIPT_RENAME_HELPER -Algorithm SHA256).Hash.ToLower()
$env:VKIT_TYPESCRIPT_LOCKFILE = (Resolve-Path tools/typescript/package-lock.json).Path
$env:VKIT_TYPESCRIPT_LOCK_SHA256 = (Get-FileHash $env:VKIT_TYPESCRIPT_LOCKFILE -Algorithm SHA256).Hash.ToLower()
$env:VKIT_NODE_BIN = (Get-Command node).Source
```

The configured Node executable must report `v24.16.0`. The helper and lockfile must match their configured SHA-256 values. The focused native tests run with:

```powershell
python -m pytest -q tests/test_source_migrations.py
```
