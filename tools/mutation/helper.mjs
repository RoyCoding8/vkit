import { createInstrumenter } from "@stryker-mutator/instrumenter";
import { readFileSync } from "node:fs";

const instrumenterPackage = JSON.parse(readFileSync(new URL(import.meta.resolve("@stryker-mutator/instrumenter/package.json")), "utf8"));

const limit = 100;
const source = await readInput();
if (instrumenterPackage.version !== "10.0.0" || typeof source.source !== "string" || typeof source.file !== "string"
  || !Number.isInteger(source.limit) || source.limit < 1 || source.limit > limit) {
  process.exitCode = 2;
} else {
  const logger = { debug() {}, info() {}, isDebugEnabled() { return false; } };
  const injector = {
    provideValue() { return this; },
    injectClass(Instrumenter) { return new Instrumenter(logger); },
  };
  try {
    const instrumenter = createInstrumenter(injector);
    const result = await instrumenter.instrument(
      [{ name: source.file, mutate: true, content: source.source }],
      { plugins: null, ignorers: [], excludedMutations: [] },
    );
    const compare = (left, right) => left < right ? -1 : left > right ? 1 : 0;
    const ordered = [...result.mutants].sort((left, right) =>
      left.location.start.line - right.location.start.line
      || left.location.start.column - right.location.start.column
      || left.location.end.line - right.location.end.line
      || left.location.end.column - right.location.end.column
      || compare(left.mutatorName, right.mutatorName)
      || compare(left.replacement, right.replacement)
      || compare(left.id, right.id));
    process.stdout.write(JSON.stringify({ protocol_version: 1, engine_version: instrumenterPackage.version,
      generated: ordered.length, mutants: ordered.slice(0, source.limit) }));
  } catch (error) {
    process.stderr.write(String(error?.message || error).slice(0, 2000));
    process.exitCode = 1;
  }
}

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}
