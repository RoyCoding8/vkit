/-- Stands in for landrun where it cannot run: drops the sandbox flags and runs the command after `--`.
It provides no isolation. -/
def main (args : List String) : IO UInt32 := do
  if args == ["--version"] then
    IO.println "landrun passthrough (no isolation)"
    return 0
  match (args.dropWhile (· != "--")).drop 1 with
  | [] =>
    IO.eprintln "landrun passthrough: no command after --"
    return 2
  | cmd :: rest =>
    let child ← IO.Process.spawn { cmd := cmd, args := rest.toArray }
    child.wait
