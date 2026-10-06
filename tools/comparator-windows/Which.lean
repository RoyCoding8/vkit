/-- Prints the first PATH entry that holds the named executable, like POSIX `which`. -/
def main (args : List String) : IO UInt32 := do
  let some name := args.head? | return 2
  let path := (← IO.getEnv "PATH").getD ""
  let exts := ["", ".exe", ".cmd", ".bat"]
  for dir in path.splitOn ";" do
    for ext in exts do
      let candidate : System.FilePath := (dir : System.FilePath) / (name ++ ext)
      if ← candidate.pathExists then
        IO.println candidate.toString
        return 0
  return 1
