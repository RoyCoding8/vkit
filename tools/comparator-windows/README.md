# Comparator on Windows

## Purpose

[comparator](https://github.com/leanprover/comparator) expects two POSIX tools. This package builds Windows stand-ins.

| Tool | Expected role | Windows stand-in |
| --- | --- | --- |
| `landrun` | Linux sandbox. | Runs the command without a sandbox. |
| `which` | Executable lookup. | Supplies lookup for comparator. |

## Preserved checks and isolation limit

| Property | Windows behavior |
| --- | --- |
| Theorem statements | Comparator checks that solution statements match the challenge. |
| Axioms | Comparator checks permitted axioms. |
| Proof acceptance | Lean kernel checking remains active. |
| Build isolation | Absent. Lean code can execute programs at build time. |
| VKit evidence | Records the build as unsandboxed. |

## Build and configure

1. Build comparator and lean4export from their repositories with `lake build`.
2. Put both executables on PATH.
3. From this directory, build the stand-ins and select `landrun`:

```powershell
lake build
$env:COMPARATOR_LANDRUN = "$PWD\.lake\build\bin\landrun.exe"
```

VKit adds `.lake/build/bin` to comparator's PATH so it can find `which`.
