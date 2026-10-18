import { readFileSync, realpathSync, statSync } from "node:fs";
import { extname, isAbsolute, relative, resolve, sep } from "node:path";
import { cruise } from "dependency-cruiser";
import ts from "typescript";

const PROTOCOL_VERSION = 1;
const DEPENDENCY_CRUISER_VERSION = "18.5.0";
const TYPESCRIPT_VERSION = "5.9.3";
const MAX_REQUEST_BYTES = 4_096;
const MAX_RESPONSE_BYTES = 8_388_608;
const SOURCE_FILTER = String.raw`\.(?:[cm]?[jt]sx?)$`;

function readRequest() {
  return new Promise((resolveRequest, rejectRequest) => {
    const chunks = [];
    let size = 0;
    process.stdin.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_REQUEST_BYTES) {
        rejectRequest(new Error("request exceeds the size limit"));
        process.stdin.destroy();
      } else {
        chunks.push(chunk);
      }
    });
    process.stdin.on("end", () => {
      try {
        resolveRequest(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch {
        rejectRequest(new Error("request is not valid JSON"));
      }
    });
    process.stdin.on("error", rejectRequest);
  });
}

function snapshotPath(value) {
  if (typeof value !== "string" || value !== "snapshot") {
    throw new Error("snapshot path is invalid");
  }
  const root = realpathSync(resolve(process.cwd(), value));
  if (!isAbsolute(root) || !statSync(root).isDirectory()) {
    throw new Error("snapshot is not a directory");
  }
  return root;
}

function relativeToSnapshot(root, value) {
  if (typeof value !== "string") return null;
  const absolute = resolve(process.cwd(), value);
  const path = relative(root, absolute);
  if (path === "" || path === ".." || path.startsWith(`..${sep}`) || isAbsolute(path)) return null;
  return path.split(sep).join("/");
}

function inspectSources(root, modules) {
  const incomplete = [];
  const normalizedModules = [];
  for (const module of modules) {
    const file = resolve(root, module.source);
    const source = readFileSync(file, "utf8");
    const extension = extname(file).toLowerCase();
    const scriptKind = extension === ".tsx" ? ts.ScriptKind.TSX
      : extension === ".jsx" ? ts.ScriptKind.JSX
        : extension === ".js" || extension === ".mjs" || extension === ".cjs" ? ts.ScriptKind.JS
          : ts.ScriptKind.TS;
    const parsed = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, scriptKind);
    if (parsed.parseDiagnostics.length > 0) incomplete.push(`${module.source}: parser diagnostics`);
    if (parsed.referencedFiles.length > 0 || parsed.typeReferenceDirectives.length > 0) {
      incomplete.push(`${module.source}: ambient or triple-slash references are outside the graph`);
    }
    const expected = new Map();
    const add = (specifier, dynamic = false, ambient = false) => {
      const previous = expected.get(specifier);
      expected.set(specifier, { dynamic: dynamic || previous?.dynamic === true });
      if (ambient) incomplete.push(`${module.source}: ambient imports are outside the graph`);
    };
    const visit = (node, ambient = false) => {
      const inAmbientModule = ambient || (ts.isModuleDeclaration(node)
        && (node.flags & ts.NodeFlags.Ambient) !== 0 && ts.isStringLiteral(node.name));
      if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node))
          && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
        add(node.moduleSpecifier.text, false, inAmbientModule);
      } else if (ts.isImportTypeNode(node)) {
        if (ts.isLiteralTypeNode(node.argument) && ts.isStringLiteral(node.argument.literal)) {
          add(node.argument.literal.text, false, inAmbientModule);
        } else {
          incomplete.push(`${module.source}: unsupported import type reference`);
        }
      } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
        const expression = node.moduleReference.expression;
        if (expression && ts.isStringLiteral(expression)) add(expression.text, false, inAmbientModule);
        else incomplete.push(`${module.source}: unsupported import-equals reference`);
      } else if (ts.isCallExpression(node)) {
        const isImport = node.expression.kind === ts.SyntaxKind.ImportKeyword;
        const isRequire = ts.isIdentifier(node.expression) && node.expression.text === "require";
        const isRequireResolve = ts.isPropertyAccessExpression(node.expression)
          && ts.isIdentifier(node.expression.expression) && node.expression.expression.text === "require"
          && node.expression.name.text === "resolve";
        if (isImport || isRequire || isRequireResolve) {
          const argument = node.arguments.length === 1 ? node.arguments[0] : undefined;
          if (argument && ts.isStringLiteral(argument)) add(argument.text, isImport, inAmbientModule);
          else incomplete.push(`${module.source}: nonliteral or unsupported import/require call`);
        }
      }
      ts.forEachChild(node, (child) => visit(child, inAmbientModule));
    };
    visit(parsed);
    const extracted = new Map();
    for (const dependency of module.dependencies) {
      const previous = extracted.get(dependency.module);
      extracted.set(dependency.module, { ...dependency, dynamic: dependency.dynamic || previous?.dynamic === true });
    }
    for (const [specifier, details] of expected) {
      const dependency = extracted.get(specifier);
      if (dependency) {
        dependency.dynamic = dependency.dynamic || details.dynamic;
      } else if (specifier.startsWith(".") || specifier.startsWith("/")) {
        incomplete.push(`${module.source}: dependency-cruiser omitted local import ${specifier}`);
      } else {
        extracted.set(specifier, {
          module: specifier,
          resolved: null,
          dynamic: details.dynamic,
          couldNotResolve: true,
          coreModule: false,
          dependencyTypes: ["npm-no-pkg"],
        });
      }
    }
    normalizedModules.push({ ...module, dependencies: [...extracted.values()] });
  }
  return { incompleteReasons: [...new Set(incomplete)].sort(), modules: normalizedModules };
}

async function main() {
  const request = await readRequest();
  if (request.protocol_version !== PROTOCOL_VERSION) throw new Error("protocol version mismatch");
  const root = snapshotPath(request.snapshot);
  const output = await cruise([request.snapshot], {
    baseDir: process.cwd(),
    includeOnly: SOURCE_FILTER,
    doNotFollow: { path: String.raw`(^|/)node_modules/` },
    moduleSystems: ["es6", "cjs", "tsd"],
    tsPreCompilationDeps: true,
    detectJSDocImports: false,
    detectImportsInAmbientModules: false,
    detectProcessBuiltinModuleCalls: false,
    progress: "none",
  }, undefined, { tsConfig: { compilerOptions: {} } });
  if (output.outputType || !output.output || !Array.isArray(output.output.modules)
      || !output.output.summary || output.output.summary.environment?.version !== DEPENDENCY_CRUISER_VERSION) {
    throw new Error("dependency-cruiser returned an invalid result");
  }
  const omittedModules = [];
  const modules = output.output.modules.flatMap((module) => {
    const source = relativeToSnapshot(root, module.source);
    if (!source) {
      omittedModules.push(module.source);
      return [];
    }
    return [{
      source,
      dependencies: module.dependencies.map((dependency) => ({
        module: dependency.module,
        resolved: relativeToSnapshot(root, dependency.resolved),
        dynamic: dependency.dynamic,
        couldNotResolve: dependency.couldNotResolve,
        coreModule: dependency.coreModule,
        dependencyTypes: dependency.dependencyTypes,
      })),
    }];
  });
  const inspected = inspectSources(root, modules);
  for (const source of omittedModules) inspected.incompleteReasons.push(`${source}: unresolved module outside snapshot`);
  inspected.incompleteReasons = [...new Set(inspected.incompleteReasons)].sort();
  if (ts.version !== TYPESCRIPT_VERSION) throw new Error("TypeScript version does not match the pin");
  const result = {
    protocol_version: PROTOCOL_VERSION,
    backend_version: output.output.summary.environment.version,
    typescript_version: ts.version,
    complete: output.exitCode === 0 && output.output.summary.error === 0
      && output.output.summary.totalCruised === inspected.modules.length && inspected.incompleteReasons.length === 0,
    incomplete_reasons: inspected.incompleteReasons,
    modules: inspected.modules,
  };
  const bytes = Buffer.from(JSON.stringify(result));
  if (bytes.length > MAX_RESPONSE_BYTES) throw new Error("graph exceeds the output size limit");
  process.stdout.write(bytes);
}

main().catch((error) => {
  process.stderr.write(`${error?.message ?? "dependency helper failed"}\n`);
  process.exitCode = 2;
});
