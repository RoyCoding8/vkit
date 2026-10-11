"use strict";

const path = require("node:path");
const ts = require("typescript");

const PROTOCOL_VERSION = 1;
const TYPESCRIPT_VERSION = "5.9.3";
const MAX_REQUEST_BYTES = 4_194_304;
const MAX_RESPONSE_BYTES = 1_048_576;
const MAX_FILES = 128;
const MAX_FILE_BYTES = 262_144;
const MAX_AST_NODES = 100_000;

class UnsupportedProfileError extends Error {}

function readRequest() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    process.stdin.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_REQUEST_BYTES) {
        reject(new Error("request exceeds limit"));
        process.stdin.destroy();
      } else chunks.push(chunk);
    });
    process.stdin.on("end", () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
      catch { reject(new Error("request is not valid JSON")); }
    });
    process.stdin.on("error", reject);
  });
}

function validRelative(value) {
  return typeof value === "string" && value.length > 0 && !value.includes("\\")
    && !value.includes("\0") && !path.posix.isAbsolute(value)
    && value.split("/").every((part) => part && part !== "." && part !== "..");
}

function parseSide(side, label) {
  if (!side || typeof side !== "object" || !validRelative(side.entry) || !Array.isArray(side.files)
      || side.files.length === 0 || side.files.length > MAX_FILES) throw new Error(`${label} snapshot shape is invalid`);
  const files = new Map();
  let total = 0;
  for (const item of side.files) {
    if (!item || !validRelative(item.path) || !item.path.endsWith(".d.ts") || typeof item.text !== "string"
        || files.has(item.path)) throw new Error(`${label} declaration file is invalid`);
    const bytes = Buffer.byteLength(item.text, "utf8");
    total += bytes;
    if (bytes > MAX_FILE_BYTES || total > MAX_REQUEST_BYTES || item.text.includes("\0")) {
      throw new Error(`${label} declaration snapshot exceeds limits`);
    }
    files.set(item.path, item.text);
  }
  if (!files.has(side.entry)) throw new Error(`${label} entry is not captured`);
  return { entry: side.entry, files };
}

function candidates(from, specifier) {
  if (!specifier.startsWith(".")) return [];
  const joined = path.posix.normalize(path.posix.join(path.posix.dirname(from), specifier));
  if (!validRelative(joined)) return [];
  const values = [joined];
  if (joined.endsWith(".js") || joined.endsWith(".mjs")) values.push(joined.replace(/\.(?:m)?js$/, ".d.ts"));
  else if (joined.endsWith(".d.ts")) values.push(joined);
  else values.push(`${joined}.d.ts`, `${joined}/index.d.ts`);
  return [...new Set(values)];
}

function moduleSpecifiers(source) {
  const found = [];
  let nodes = 0;
  function visit(node) {
    nodes += 1;
    if (nodes > MAX_AST_NODES) throw new Error("declaration AST node limit exceeded");
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
      if (node.moduleSpecifier) {
        if (!ts.isStringLiteral(node.moduleSpecifier)) throw new Error("computed module specifier is unsupported");
        found.push(node.moduleSpecifier.text);
      }
    } else if (ts.isImportTypeNode(node)) {
      if (!ts.isLiteralTypeNode(node.argument) || !ts.isStringLiteral(node.argument.literal)) {
        throw new Error("nonliteral import type is unsupported");
      }
      found.push(node.argument.literal.text);
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
      const expression = node.moduleReference.expression;
      if (!expression || !ts.isStringLiteral(expression)) throw new Error("nonliteral import-equals is unsupported");
      found.push(expression.text);
    } else if (ts.isModuleDeclaration(node) && ts.isStringLiteral(node.name)) {
      throw new Error("ambient module declarations are unsupported");
    } else if (ts.isModuleDeclaration(node) && ts.isIdentifier(node.name) && node.name.text === "global") {
      throw new UnsupportedProfileError("global declaration augmentations are unsupported");
    } else if (node.kind === ts.SyntaxKind.NamespaceExportDeclaration || ts.isExportAssignment(node)) {
      throw new UnsupportedProfileError("namespace exports and export assignments are unsupported");
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  if (source.parseDiagnostics.length) throw new Error("declaration syntax is invalid");
  if (source.typeReferenceDirectives.length) throw new Error("triple-slash type references are unsupported");
  if (source.referencedFiles.some((item) => !item.fileName.endsWith(".d.ts"))) {
    throw new Error("non-declaration triple-slash references are unsupported");
  }
  if (source.libReferenceDirectives.length) throw new UnsupportedProfileError("explicit compiler lib references are unsupported");
  return found;
}

function closure(side, label) {
  const seen = new Set();
  const pending = [side.entry];
  const parsed = new Map();
  while (pending.length) {
    const file = pending.pop();
    if (seen.has(file)) continue;
    const text = side.files.get(file);
    if (text === undefined) throw new UnsupportedProfileError(`${label} local declaration import is not captured: ${file}`);
    seen.add(file);
    const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
    parsed.set(file, source);
    for (const specifier of moduleSpecifiers(source)) {
      const matches = candidates(file, specifier).filter((candidate) => side.files.has(candidate));
      if (matches.length !== 1) throw new UnsupportedProfileError(`${label} import ${specifier} does not resolve uniquely within the captured declaration tree`);
      pending.push(matches[0]);
    }
    for (const ref of source.referencedFiles) {
      const target = path.posix.normalize(path.posix.join(path.posix.dirname(file), ref.fileName));
      if (!validRelative(target) || !side.files.has(target)) throw new UnsupportedProfileError(`${label} triple-slash path is not captured`);
      pending.push(target);
    }
  }
  return { names: [...seen].sort(), parsed };
}

function buildProgram(oldSide, newSide, oldClosure, newClosure) {
  const root = path.resolve(process.cwd(), ".vkit-typescript-api");
  const sourceMap = new Map();
  const nameMaps = [new Map(), new Map()];
  function add(side, closureData, sideName, index) {
    for (const name of closureData.names) {
      const absolute = path.join(root, sideName, ...name.split("/"));
      sourceMap.set(path.resolve(absolute), side.files.get(name));
      nameMaps[index].set(name, path.resolve(absolute));
    }
  }
  add(oldSide, oldClosure, "old", 0);
  add(newSide, newClosure, "new", 1);
  const options = {
    strict: true,
    strictFunctionTypes: true,
    exactOptionalPropertyTypes: true,
    noEmit: true,
    skipLibCheck: false,
    target: ts.ScriptTarget.ESNext,
    module: ts.ModuleKind.CommonJS,
    moduleResolution: ts.ModuleResolutionKind.Node10,
    types: [],
  };
  const host = ts.createCompilerHost(options, true);
  const defaultGetSourceFile = host.getSourceFile.bind(host);
  const defaultFileExists = host.fileExists.bind(host);
  const defaultReadFile = host.readFile.bind(host);
  host.getSourceFile = (fileName, languageVersion, onError, shouldCreateNewSourceFile) => {
    const normalized = path.resolve(fileName);
    if (sourceMap.has(normalized)) return ts.createSourceFile(normalized, sourceMap.get(normalized), languageVersion, true);
    return defaultGetSourceFile(fileName, languageVersion, onError, shouldCreateNewSourceFile);
  };
  host.fileExists = (fileName) => sourceMap.has(path.resolve(fileName)) || defaultFileExists(fileName);
  host.readFile = (fileName) => sourceMap.get(path.resolve(fileName)) ?? defaultReadFile(fileName);
  host.resolveModuleNames = (moduleNames, containingFile) => moduleNames.map((specifier) => {
    if (specifier === "vkit-api:old") return { resolvedFileName: nameMaps[0].get(oldSide.entry),
      extension: ts.Extension.Dts, isExternalLibraryImport: false };
    if (specifier === "vkit-api:new") return { resolvedFileName: nameMaps[1].get(newSide.entry),
      extension: ts.Extension.Dts, isExternalLibraryImport: false };
    if (!specifier.startsWith(".")) return undefined;
    const sideName = path.relative(root, containingFile).split(path.sep)[0];
    const sideIndex = sideName === "old" ? 0 : sideName === "new" ? 1 : -1;
    if (sideIndex < 0) return undefined;
    const relative = path.relative(path.join(root, sideName), containingFile).split(path.sep).join("/");
    const match = candidates(relative, specifier).find((candidate) => nameMaps[sideIndex].has(candidate));
    if (!match) return undefined;
    return { resolvedFileName: nameMaps[sideIndex].get(match), extension: ts.Extension.Dts, isExternalLibraryImport: false };
  });
  const roots = [nameMaps[0].get(oldSide.entry), nameMaps[1].get(newSide.entry)];
  const program = ts.createProgram(roots, options, host);
  return { program, sourceMap, nameMaps, root, host, options, roots, oldSide, newSide };
}

function exportTable(program, entry) {
  const checker = program.getTypeChecker();
  const source = program.getSourceFile(entry);
  if (!source || !source.symbol) {
    throw new Error("entry declaration must be an external module");
  }
  const table = new Map();
  for (const exported of checker.getExportsOfModule(source.symbol)) {
    const resolved = (exported.flags & ts.SymbolFlags.Alias) ? checker.getAliasedSymbol(exported) : exported;
    table.set(exported.getName(), { exported, resolved });
  }
  return { table, checker };
}

function declarationKind(symbol) {
  const supported = new Set([
    ts.SyntaxKind.FunctionDeclaration, ts.SyntaxKind.VariableDeclaration,
    ts.SyntaxKind.InterfaceDeclaration, ts.SyntaxKind.TypeAliasDeclaration,
    ts.SyntaxKind.ClassDeclaration, ts.SyntaxKind.EnumDeclaration,
  ]);
  const declarations = symbol.declarations || [];
  if (!declarations.length || declarations.some((item) => !supported.has(item.kind))) return false;
  if (declarations.length > 1 && declarations.some((item) => !ts.isFunctionDeclaration(item))) return false;
  return true;
}

function typeParameters(symbol) {
  const declarations = (symbol.declarations || []).filter((item) => item.typeParameters);
  if (!declarations.length) return [];
  const first = declarations[0].typeParameters;
  return first.map((parameter, index) => ({
    name: parameter.name.getText(),
    constraint: parameter.constraint ? parameter.constraint.getText() : null,
    defaultType: parameter.default ? parameter.default.getText() : null,
    variance: parameter.modifiers?.map((modifier) => modifier.kind).filter((kind) =>
      kind === ts.SyntaxKind.InKeyword || kind === ts.SyntaxKind.OutKeyword) || [],
    index,
  }));
}

function hasAnyType(symbol, checker, sourceMap) {
  const roots = [];
  for (const declaration of symbol.declarations || []) roots.push(checker.getTypeAtLocation(declaration));
  const visited = new Set();
  let count = 0;
  function inspect(type) {
    if (!type || visited.has(type)) return false;
    visited.add(type);
    count += 1;
    if (count > 4_000) throw new Error("public type graph exceeds inspection limit");
    if (type.flags & ts.TypeFlags.Any) return true;
    for (const parameter of checker.getTypeArguments?.(type) || []) if (inspect(parameter)) return true;
    for (const property of checker.getPropertiesOfType(type)) {
      const declaration = property.valueDeclaration || property.declarations?.[0];
      if (declaration && sourceMap.has(path.resolve(declaration.getSourceFile().fileName))
          && inspect(checker.getTypeOfSymbolAtLocation(property, declaration))) return true;
    }
    for (const signature of [...checker.getSignaturesOfType(type, ts.SignatureKind.Call),
      ...checker.getSignaturesOfType(type, ts.SignatureKind.Construct)]) {
      for (const parameter of signature.getParameters()) {
        const declaration = parameter.valueDeclaration || parameter.declarations?.[0];
        if (declaration && sourceMap.has(path.resolve(declaration.getSourceFile().fileName))
            && inspect(checker.getTypeOfSymbolAtLocation(parameter, declaration))) return true;
      }
      const declaration = signature.getDeclaration();
      if (declaration && sourceMap.has(path.resolve(declaration.getSourceFile().fileName))
          && inspect(checker.getReturnTypeOfSignature(signature))) return true;
    }
    return false;
  }
  return roots.some(inspect);
}

function simpleTypeNode(node) {
  if (!node) return true;
  if (ts.isParenthesizedTypeNode(node)) return simpleTypeNode(node.type);
  if (ts.isUnionTypeNode(node) || ts.isIntersectionTypeNode(node)) return node.types.every(simpleTypeNode);
  if (ts.isLiteralTypeNode(node)) return ts.isStringLiteral(node.literal) || ts.isNumericLiteral(node.literal)
    || node.literal.kind === ts.SyntaxKind.TrueKeyword || node.literal.kind === ts.SyntaxKind.FalseKeyword;
  return new Set([ts.SyntaxKind.StringKeyword, ts.SyntaxKind.NumberKeyword, ts.SyntaxKind.BooleanKeyword,
    ts.SyntaxKind.ObjectKeyword, ts.SyntaxKind.UnknownKeyword, ts.SyntaxKind.NeverKeyword,
    ts.SyntaxKind.UndefinedKeyword, ts.SyntaxKind.NullKeyword, ts.SyntaxKind.VoidKeyword]).has(node.kind);
}

function signatureCompatible(oldType, newType, checker, kind) {
  const oldSignatures = checker.getSignaturesOfType(oldType, kind);
  const newSignatures = checker.getSignaturesOfType(newType, kind);
  for (const oldSignature of oldSignatures) {
    if (oldSignature.typeParameters?.length) throw new UnsupportedProfileError("generic call and construct signatures are unsupported");
    const oldParams = oldSignature.getParameters();
    const oldMin = oldSignature.minArgumentCount ?? oldParams.filter((parameter) =>
      !(parameter.flags & ts.SymbolFlags.Optional)).length;
    const oldRest = oldParams.length > 0 && Boolean(oldParams[oldParams.length - 1].valueDeclaration?.dotDotDotToken);
    const oldMax = oldRest ? Infinity : oldParams.length;
    const matched = newSignatures.some((next) => {
      if (next.typeParameters?.length) throw new UnsupportedProfileError("generic call and construct signatures are unsupported");
      const nextParams = next.getParameters();
      const nextMin = next.minArgumentCount ?? nextParams.filter((parameter) =>
        !(parameter.flags & ts.SymbolFlags.Optional)).length;
      const nextRest = nextParams.length > 0 && Boolean(nextParams[nextParams.length - 1].valueDeclaration?.dotDotDotToken);
      const nextMax = nextRest ? Infinity : nextParams.length;
      if (nextMin > oldMin || nextMax < oldMax) return false;
      const count = oldRest ? Math.max(oldParams.length, nextParams.length) + 1 : oldParams.length;
      for (let index = 0; index < count; index += 1) {
        const before = oldParams[Math.min(index, oldParams.length - 1)];
        const after = nextParams[Math.min(index, nextParams.length - 1)];
        if (!before || !after) return false;
        const beforeDeclaration = before.valueDeclaration || before.declarations?.[0];
        const afterDeclaration = after.valueDeclaration || after.declarations?.[0];
        if (!beforeDeclaration || !afterDeclaration) return false;
        let beforeType = checker.getTypeOfSymbolAtLocation(before, beforeDeclaration);
        let afterType = checker.getTypeOfSymbolAtLocation(after, afterDeclaration);
        if (index >= oldParams.length - 1 && oldRest) {
          beforeType = checker.getIndexTypeOfType(beforeType, ts.IndexKind.Number) || beforeType;
        }
        if (index >= nextParams.length - 1 && nextRest) {
          afterType = checker.getIndexTypeOfType(afterType, ts.IndexKind.Number) || afterType;
        }
        if (!sameTypeParameter(beforeType, afterType) && !checker.isTypeAssignableTo(beforeType, afterType)) return false;
      }
      const beforeReturn = checker.getReturnTypeOfSignature(oldSignature);
      const afterReturn = checker.getReturnTypeOfSignature(next);
      return sameTypeParameter(beforeReturn, afterReturn) || checker.isTypeAssignableTo(afterReturn, beforeReturn);
    });
    if (!matched) return false;
  }
  return true;
}

function sameTypeParameter(before, after) {
  return Boolean(before.flags & ts.TypeFlags.TypeParameter) && Boolean(after.flags & ts.TypeFlags.TypeParameter)
    && before.symbol?.getName() === after.symbol?.getName();
}

function methodVarianceCompatible(oldType, newType, checker, sourceMap, seen = new Set(), budget = { nodes: 0 }) {
  if (!oldType || !newType) return true;
  const key = `${oldType.id || checker.typeToString(oldType)}\0${newType.id || checker.typeToString(newType)}`;
  if (seen.has(key)) return true;
  seen.add(key);
  budget.nodes += 1;
  if (budget.nodes > 4_000) throw new UnsupportedProfileError("public member graph exceeds inspection limit");
  const oldCalls = checker.getSignaturesOfType(oldType, ts.SignatureKind.Call);
  const newCalls = checker.getSignaturesOfType(newType, ts.SignatureKind.Call);
  if (oldCalls.length && !signatureCompatible(oldType, newType, checker, ts.SignatureKind.Call)) return false;
  const oldConstructors = checker.getSignaturesOfType(oldType, ts.SignatureKind.Construct);
  const newConstructors = checker.getSignaturesOfType(newType, ts.SignatureKind.Construct);
  if (oldConstructors.length && !signatureCompatible(oldType, newType, checker, ts.SignatureKind.Construct)) return false;
  for (const property of checker.getPropertiesOfType(oldType)) {
    const before = property.valueDeclaration || property.declarations?.[0];
    if (!before || !sourceMap.has(path.resolve(before.getSourceFile().fileName))) continue;
    const afterProperty = checker.getPropertyOfType(newType, property.getName());
    if (!afterProperty) continue;
    const after = afterProperty.valueDeclaration || afterProperty.declarations?.[0];
    if (!after || !sourceMap.has(path.resolve(after.getSourceFile().fileName))) continue;
    const beforeType = checker.getTypeOfSymbolAtLocation(property, before);
    const afterType = checker.getTypeOfSymbolAtLocation(afterProperty, after);
    const isMethod = before.kind === ts.SyntaxKind.MethodSignature || before.kind === ts.SyntaxKind.MethodDeclaration;
    if (isMethod && !signatureCompatible(beforeType, afterType, checker, ts.SignatureKind.Call)) return false;
    if (!methodVarianceCompatible(beforeType, afterType, checker, sourceMap, seen, budget)) return false;
  }
  const oldArguments = checker.getTypeArguments?.(oldType) || [];
  const newArguments = checker.getTypeArguments?.(newType) || [];
  if (oldArguments.length === newArguments.length) {
    for (let index = 0; index < oldArguments.length; index += 1) {
      if (!methodVarianceCompatible(oldArguments[index], newArguments[index], checker, sourceMap, seen, budget)) return false;
    }
  }
  return true;
}

function withGenericProbes(build) {
  const oldEntry = build.nameMaps[0].get(build.oldSide.entry);
  const newEntry = build.nameMaps[1].get(build.newSide.entry);
  const oldTable = exportTable(build.program, oldEntry).table;
  const newTable = exportTable(build.program, newEntry).table;
  const lines = ['import type * as __VkitOld from "vkit-api:old";',
    'import type * as __VkitNew from "vkit-api:new";', "type __VkitAssert<T extends true> = T;"];
  const lineToName = new Map();
  for (const [name, old] of oldTable) {
    if (!(old.resolved.flags & ts.SymbolFlags.Type)) continue;
    const current = newTable.get(name);
    const declaration = old.resolved.declarations?.find((item) => item.typeParameters);
    if (!current || !declaration) continue;
    if (!/^[$A-Z_a-z][$\w]*$/.test(name)) throw new UnsupportedProfileError(`generic export ${name} has an unsupported name`);
    const newDeclaration = current.resolved.declarations?.find((item) => item.typeParameters);
    if (!newDeclaration || newDeclaration.typeParameters.length !== declaration.typeParameters.length) continue;
    const parameters = declaration.typeParameters.map((parameter, index) => {
      if (!simpleTypeNode(parameter.constraint) || !simpleTypeNode(parameter.default)) {
        throw new UnsupportedProfileError(`generic export ${name} has a nonlocal constraint or default`);
      }
      const typeName = `__VkitT${index}`;
      const constraint = parameter.constraint ? ` extends ${parameter.constraint.getText()}` : "";
      const defaultType = parameter.default ? ` = ${parameter.default.getText()}` : "";
      return `${typeName}${constraint}${defaultType}`;
    });
    const argumentsText = declaration.typeParameters.map((_parameter, index) => `__VkitT${index}`).join(", ");
    const line = lines.length + 1;
    lines.push(`type __VkitProbe${line}<${parameters.join(", ")}> = __VkitAssert<__VkitNew.${name}<${argumentsText}> extends __VkitOld.${name}<${argumentsText}> ? true : false>;`);
    lineToName.set(line, name);
  }
  if (!lineToName.size) return { ...build, lineToName };
  const probe = path.resolve(build.root, "generic-probes.ts");
  build.sourceMap.set(probe, `${lines.join("\n")}\n`);
  return { ...build, program: ts.createProgram([...build.roots, probe], build.options, build.host), lineToName };
}

function inspectApi(oldSide, newSide, oldClosure, newClosure, build) {
  build = withGenericProbes(build);
  const { program, nameMaps } = build;
  const diagnostics = ts.getPreEmitDiagnostics(program).filter((item) => item.category === ts.DiagnosticCategory.Error);
  const inputDiagnostics = diagnostics.filter((item) => !item.file || !item.file.fileName.endsWith("generic-probes.ts"));
  if (inputDiagnostics.length) {
    return { status: "UNKNOWN", exports: [], diagnostics: inputDiagnostics.slice(0, 100).map((item) => ({
      code: item.code,
      file: item.file ? path.relative(build.root, item.file.fileName).split(path.sep).join("/") : null,
      message: ts.flattenDiagnosticMessageText(item.messageText, " ").slice(0, 500),
    })), reason: "captured declarations have TypeScript compiler errors" };
  }
  const oldEntry = nameMaps[0].get(oldSide.entry);
  const newEntry = nameMaps[1].get(newSide.entry);
  const oldExports = exportTable(program, oldEntry);
  const newExports = exportTable(program, newEntry);
  const checker = oldExports.checker;
  if (typeof checker.isTypeAssignableTo !== "function") throw new Error("pinned TypeScript checker lacks assignability support");
  const findings = [];
  let breaking = false;
  for (const [name, old] of [...oldExports.table.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const current = newExports.table.get(name);
    if (!current) {
      findings.push({ name, compatible: false, reason: "export was removed", old_kinds: ["type", "value"].filter((kind) =>
        kind === "type" ? Boolean(old.resolved.flags & ts.SymbolFlags.Type) : Boolean(old.resolved.flags & ts.SymbolFlags.Value)),
        new_kinds: [] });
      breaking = true;
      continue;
    }
    const oldKinds = ["type", "value"].filter((kind) => kind === "type"
      ? Boolean(old.resolved.flags & ts.SymbolFlags.Type) : Boolean(old.resolved.flags & ts.SymbolFlags.Value));
    const newKinds = ["type", "value"].filter((kind) => kind === "type"
      ? Boolean(current.resolved.flags & ts.SymbolFlags.Type) : Boolean(current.resolved.flags & ts.SymbolFlags.Value));
    let reason = "new declaration is assignable to the old exported type";
    let compatible = true;
    if (!declarationKind(old.resolved) || !declarationKind(current.resolved)) {
      throw new UnsupportedProfileError(`export ${name} uses an unsupported declaration form`);
    }
    if (oldKinds.some((kind) => !newKinds.includes(kind))) {
      compatible = false;
      reason = "export no longer provides an old type or value meaning";
    }
    if (compatible && (hasAnyType(old.resolved, checker, build.sourceMap)
        || hasAnyType(current.resolved, checker, build.sourceMap))) {
      throw new UnsupportedProfileError(`export ${name} contains any and is outside the checked profile`);
    }
    if (compatible && oldKinds.includes("type")) {
      const oldParams = typeParameters(old.resolved);
      const newParams = typeParameters(current.resolved);
      if (oldParams.length !== newParams.length) {
        compatible = false;
        reason = "generic type parameter arity changed";
      } else {
        for (let index = 0; index < oldParams.length; index += 1) {
          const before = oldParams[index];
          const after = newParams[index];
          if (before.defaultType !== after.defaultType || JSON.stringify(before.variance) !== JSON.stringify(after.variance)) {
            compatible = false;
            reason = `generic parameter ${index + 1} default or variance marker changed`;
            break;
          }
        }
      }
    }
    if (compatible) {
      if (oldKinds.includes("type")) {
        const oldType = checker.getDeclaredTypeOfSymbol(old.resolved);
        const newType = checker.getDeclaredTypeOfSymbol(current.resolved);
        if (!typeParameters(old.resolved).length) compatible = checker.isTypeAssignableTo(newType, oldType);
        if (compatible) compatible = methodVarianceCompatible(oldType, newType, checker, build.sourceMap);
      }
      if (compatible && oldKinds.includes("value")) {
        const oldDeclaration = old.resolved.valueDeclaration || old.resolved.declarations?.[0];
        const newDeclaration = current.resolved.valueDeclaration || current.resolved.declarations?.[0];
        const newType = checker.getTypeOfSymbolAtLocation(current.resolved, newDeclaration);
        const oldType = checker.getTypeOfSymbolAtLocation(old.resolved, oldDeclaration);
        compatible = checker.isTypeAssignableTo(newType, oldType)
          && signatureCompatible(oldType, newType, checker, ts.SignatureKind.Call)
          && signatureCompatible(oldType, newType, checker, ts.SignatureKind.Construct)
          && methodVarianceCompatible(oldType, newType, checker, build.sourceMap);
      }
      if (!compatible) reason = "new exported type is not assignable to the old exported type under strict mode";
    }
    const genericLine = [...build.lineToName.entries()].find(([, exportedName]) => exportedName === name)?.[0];
    if (genericLine && diagnostics.some((item) => item.file?.fileName.endsWith("generic-probes.ts")
        && item.file.getLineAndCharacterOfPosition(item.start || 0).line + 1 === genericLine)) {
      compatible = false;
      reason = "generic declaration fails the pinned compiler assignability probe";
    }
    findings.push({ name, compatible, reason, old_kinds: oldKinds, new_kinds: newKinds });
    if (!compatible) breaking = true;
  }
  const probeDiagnostics = diagnostics.filter((item) => item.file?.fileName.endsWith("generic-probes.ts"));
  return { status: (breaking || probeDiagnostics.length) ? "BREAKING" : "COMPATIBLE", exports: findings,
    diagnostics: probeDiagnostics.slice(0, 100).map((item) => ({code: item.code, file: "generic-probes.ts",
      line: item.file.getLineAndCharacterOfPosition(item.start || 0).line + 1,
      message: ts.flattenDiagnosticMessageText(item.messageText, " ").slice(0, 500)})),
    reason: breaking ? "one or more old exports fail the pinned compiler assignability checks" :
      "all old exports pass the pinned compiler assignability checks" };
}

async function main() {
  let result;
  try {
    const request = await readRequest();
    if (!request || request.protocol_version !== PROTOCOL_VERSION || request.typescript_version !== TYPESCRIPT_VERSION
        || Object.keys(request).sort().join(",") !== "new,old,protocol_version,typescript_version") {
      throw new Error("request identity or shape is invalid");
    }
    if (ts.version !== TYPESCRIPT_VERSION) throw new Error("installed TypeScript version does not match the protocol");
    const oldSide = parseSide(request.old, "old");
    const newSide = parseSide(request.new, "new");
    const oldClosure = closure(oldSide, "old");
    const newClosure = closure(newSide, "new");
    const build = buildProgram(oldSide, newSide, oldClosure, newClosure);
    result = { protocol_version: PROTOCOL_VERSION, typescript_version: ts.version,
      closure: { old: oldClosure.names, new: newClosure.names },
      ...inspectApi(oldSide, newSide, oldClosure, newClosure, build) };
  } catch (error) {
    result = { protocol_version: PROTOCOL_VERSION, typescript_version: ts.version,
      status: error instanceof UnsupportedProfileError ? "UNSUPPORTED" : "UNKNOWN",
      exports: [], diagnostics: [], reason: String(error.message || error).slice(0, 500) };
  }
  const output = JSON.stringify(result);
  if (Buffer.byteLength(output, "utf8") > MAX_RESPONSE_BYTES) {
    process.stdout.write(JSON.stringify({ protocol_version: PROTOCOL_VERSION, typescript_version: ts.version,
      status: "UNKNOWN", exports: [], diagnostics: [], reason: "compiler output exceeds limit" }));
  } else process.stdout.write(output);
}

main().catch(() => {
  process.stdout.write(JSON.stringify({ protocol_version: PROTOCOL_VERSION, typescript_version: ts.version,
    status: "UNKNOWN", exports: [], diagnostics: [], reason: "compiler helper failed" }));
});
