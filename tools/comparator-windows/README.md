# comparator on Windows

[comparator](https://github.com/leanprover/comparator) expects two POSIX tools: `landrun`, a Linux sandbox, and
`which`. This package builds stand-ins for both so comparator runs on Windows.

The `landrun` stand-in runs the command without any sandbox. Comparator still checks that the solution states the
same theorems as the challenge, that it uses only permitted axioms, and that the kernel accepts it. What is lost is
isolation while the solution builds, and Lean code can run programs at build time. vkit records this as
`unsandboxed build` in the evidence.

```powershell
lake build                      # in this directory
$env:COMPARATOR_LANDRUN = "$PWD\.lake\build\bin\landrun.exe"
```

vkit puts `.lake/build/bin` on comparator's PATH so it finds `which`. Build comparator and lean4export from their
repositories with `lake build` and put both on PATH.
