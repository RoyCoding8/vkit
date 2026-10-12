"use strict";

const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");

const PROTOCOL_VERSION = 1;
const TYPESCRIPT_VERSION = "5.9.3";
const MAX_REQUEST_BYTES = 4_194_304;
const MAX_RESPONSE_BYTES = 1_048_576;
const MAX_FILES = 128;
const MAX_FILE_BYTES = 262_144;
const MAX_AST_NODES = 200_000;
const MAX_DIAGNOSTICS = 100;
const MAX_BINDING_SITES = 20_000;

class Unsupported extends Error {}

function fail(status, reason, diagnosticsBefore = [], diagnosticsAfter = []) {
  return { protocol_version: PROTOCOL_VERSION, typescript_version: TYPESCRIPT_VERSION,
    status, reason: String(reason).slice(0, 500), diagnostics_before: diagnosticsBefore,
    diagnostics_after: diagnosticsAfter };
}

function readRequest() {
  if (process.argv.length !== 3) throw new Error("request file argument is required");
  const stat = fs.statSync(process.argv[2]);
  if (!stat.isFile() || stat.size > MAX_REQUEST_BYTES) throw new Error("request file exceeds its limit");
  return JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
}

function validRelative(value, suffixes) {
  return typeof value === "string" && value.length > 0 && !value.includes("\\")
    && !value.includes("\0") && !path.posix.isAbsolute(value)
    && value.split("/").every((part) => part && part !== "." && part !== "..")
    && suffixes.some((suffix) => value.endsWith(suffix));
}

function parseRequest(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)
      || raw.protocol_version !== PROTOCOL_VERSION || raw.typescript_version !== TYPESCRIPT_VERSION
      || !validRelative(raw.target, [".ts", ".tsx"]) || raw.target.endsWith(".d.ts")
      || typeof raw.old_name !== "string" || !/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(raw.old_name)
      || typeof raw.new_name !== "string" || !/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(raw.new_name)
      || raw.old_name === raw.new_name || !Array.isArray(raw.files)
      || raw.files.length === 0 || raw.files.length > MAX_FILES) {
    throw new Unsupported("rename request shape is outside the fixed profile");
  }
  const files = new Map();
  let total = 0;
  for (const item of raw.files) {
    if (!item || typeof item !== "object" || Array.isArray(item)
        || !validRelative(item.path, [".ts", ".tsx"]) || typeof item.text !== "string"
        || files.has(item.path) || item.path.endsWith(".d.ts") && item.path === raw.target) {
      throw new Unsupported("captured TypeScript source file is invalid");
    }
    const bytes = Buffer.byteLength(item.text, "utf8");
    total += bytes;
    if (bytes > MAX_FILE_BYTES || total > MAX_REQUEST_BYTES || item.text.includes("\0")) {
      throw new Unsupported("captured TypeScript sources exceed the backend bounds");
    }
    files.set(item.path, item.text);
  }
  if (!files.has(raw.target)) throw new Unsupported("target file is outside the captured source project");
  return { target: raw.target, oldName: raw.old_name, newName: raw.new_name, files };
}

function sourceFiles(capture) {
  const root = path.resolve(process.cwd(), "captured-project");
  const files = new Map();
  const names = new Map();
  for (const [relative, text] of capture.files) {
    const absolute = path.resolve(root, ...relative.split("/"));
    if (!absolute.startsWith(root + path.sep)) throw new Unsupported("captured source path escaped its root");
    files.set(absolute, text);
    names.set(absolute, relative);
  }
  return { root, target: capture.target, oldName: capture.oldName, newName: capture.newName, files, names };
}

function makeHost(source, options) {
  const defaultLib = path.dirname(path.resolve(ts.getDefaultLibFilePath(options)));
  const libName = /^lib\.[A-Za-z0-9_.-]+\.d\.ts$/;
  const directoryNames = new Set([source.root, defaultLib]);
  for (const file of source.files.keys()) {
    let parent = path.dirname(file);
    while (parent.startsWith(source.root + path.sep)) {
      directoryNames.add(parent);
      parent = path.dirname(parent);
    }
  }
  const isLib = (file) => path.dirname(path.resolve(file)) === defaultLib
    && libName.test(path.basename(file));
  const read = (file) => {
    const normalized = path.resolve(file);
    if (source.files.has(normalized)) return source.files.get(normalized);
    if (isLib(normalized)) return ts.sys.readFile(normalized);
    return undefined;
  };
  const exists = (file) => read(file) !== undefined;
  const resolve = (name, containingFile) => {
    if (!name.startsWith(".")) return undefined;
    return ts.resolveModuleName(name, containingFile, options, {
      fileExists: exists, readFile: read,
      directoryExists: (directory) => directoryNames.has(path.resolve(directory)),
      getCurrentDirectory: () => source.root,
      realpath: (file) => path.resolve(file),
    }).resolvedModule;
  };
  return {
    getScriptFileNames: () => [...source.files.keys()],
    getScriptVersion: () => "1",
    getScriptSnapshot(file) {
      const text = read(file);
      return text === undefined ? undefined : ts.ScriptSnapshot.fromString(text);
    },
    getCurrentDirectory: () => source.root,
    getCompilationSettings: () => options,
    getDefaultLibFileName: (settings) => ts.getDefaultLibFilePath(settings),
    fileExists: exists,
    readFile: read,
    directoryExists: (directory) => directoryNames.has(path.resolve(directory)),
    getDirectories: () => [],
    readDirectory: () => [],
    realpath: (file) => path.resolve(file),
    useCaseSensitiveFileNames: () => ts.sys.useCaseSensitiveFileNames,
    resolveModuleNames: (names, containingFile) => names.map((name) => resolve(name, containingFile)),
    resolveModuleNameLiterals: (literals, containingFile) => literals.map((literal) => ({
      resolvedModule: resolve(literal.text, containingFile),
    })),
  };
}

function moduleSpecifier(node) {
  if (!node || !ts.isStringLiteral(node) || !node.text.startsWith(".")) {
    throw new Unsupported("only literal relative project imports are supported");
  }
}

function inspectSyntax(source, capture) {
  let nodes = 0;
  for (const [fileName, text] of source.files) {
    const kind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const file = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, kind);
    if (file.parseDiagnostics.length) throw new Unsupported("a captured source file has syntax diagnostics");
    const preprocessed = ts.preProcessFile(text, true, true);
    if (preprocessed.referencedFiles.some((ref) => !ref.fileName.startsWith("."))
        || preprocessed.typeReferenceDirectives.length) {
      throw new Unsupported("triple-slash references outside captured relative source are unsupported");
    }
    const visit = (node) => {
      nodes += 1;
      if (nodes > MAX_AST_NODES) throw new Unsupported("captured source AST exceeds the node limit");
      if (ts.isImportDeclaration(node)) moduleSpecifier(node.moduleSpecifier);
      if (ts.isExportDeclaration(node) && node.moduleSpecifier) moduleSpecifier(node.moduleSpecifier);
      if (ts.isImportTypeNode(node)) {
        const argument = node.argument;
        if (!ts.isLiteralTypeNode(argument)) throw new Unsupported("computed import types are unsupported");
        moduleSpecifier(argument.literal);
      }
      if (ts.isImportEqualsDeclaration(node)) {
        if (!ts.isExternalModuleReference(node.moduleReference)
            || !node.moduleReference.expression || !ts.isStringLiteral(node.moduleReference.expression)) {
          throw new Unsupported("nonliteral import-equals declarations are unsupported");
        }
        moduleSpecifier(node.moduleReference.expression);
      }
      if (ts.isModuleDeclaration(node) && ts.isStringLiteral(node.name)) {
        throw new Unsupported("ambient external module declarations are unsupported");
      }
      if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword
          || ts.isIdentifier(node.expression) && ["require", "eval"].includes(node.expression.text))) {
        throw new Unsupported("dynamic import, require and eval are outside the closed project profile");
      }
      if (ts.isStringLiteral(node) && node.text === capture.oldName) {
        throw new Unsupported("a string literal matches the selected name; reflection and string references are outside scope");
      }
      ts.forEachChild(node, visit);
    };
    visit(file);
  }
}

function programFor(source, options) {
  const host = makeHost(source, options);
  const service = ts.createLanguageService(host, ts.createDocumentRegistry());
  const program = service.getProgram();
  if (!program) throw new Error("language service did not create a program");
  const unexpected = program.getSourceFiles().filter((file) => !source.files.has(path.resolve(file.fileName))
    && path.dirname(path.resolve(file.fileName)) !== path.dirname(path.resolve(ts.getDefaultLibFilePath(options))));
  if (unexpected.length) throw new Unsupported("compiler loaded a source file outside the captured project and TypeScript default libraries");
  return { service, program, checker: program.getTypeChecker() };
}

function diagnosticEvidence(program, source) {
  const all = [...program.getOptionsDiagnostics(), ...program.getGlobalDiagnostics()];
  for (const file of program.getSourceFiles()) {
    if (source.files.has(path.resolve(file.fileName))) {
      all.push(...program.getSyntacticDiagnostics(file), ...program.getSemanticDiagnostics(file));
    }
  }
  return all.slice(0, MAX_DIAGNOSTICS).map((diagnostic) => {
    const fileName = diagnostic.file ? source.names.get(path.resolve(diagnostic.file.fileName)) : null;
    const pos = diagnostic.file && diagnostic.start !== undefined
      ? diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start) : null;
    return { code: diagnostic.code, file: fileName, line: pos ? pos.line + 1 : null,
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, " ").slice(0, 300) };
  });
}

function declarationName(node) {
  if (!ts.isIdentifier(node)) return false;
  const parent = node.parent;
  return ts.isFunctionDeclaration(parent) && parent.name === node
    || ts.isClassDeclaration(parent) && parent.name === node
    || ts.isInterfaceDeclaration(parent) && parent.name === node
    || ts.isTypeAliasDeclaration(parent) && parent.name === node
    || ts.isEnumDeclaration(parent) && parent.name === node
    || ts.isVariableDeclaration(parent) && parent.name === node;
}

function followAlias(checker, symbol) {
  let current = symbol;
  const seen = new Set();
  while (current && current.flags & ts.SymbolFlags.Alias) {
    if (seen.has(current)) return undefined;
    seen.add(current);
    try { current = checker.getAliasedSymbol(current); }
    catch { return undefined; }
  }
  return current;
}

function identifierNodes(program, capture) {
  const items = [];
  let count = 0;
  for (const file of program.getSourceFiles()) {
    if (!capture.files.has(path.resolve(file.fileName))) continue;
    const visit = (node) => {
      count += 1;
      if (count > MAX_AST_NODES) throw new Unsupported("bound symbol graph exceeds the node limit");
      if (ts.isIdentifier(node)) items.push({ file, node });
      ts.forEachChild(node, visit);
    };
    visit(file);
  }
  return items;
}

function siteKey(file, start, length) {
  return `${path.resolve(file)}\0${start}\0${length}`;
}

function utf16ToByte(text, offset) {
  return Buffer.byteLength(text.slice(0, offset), "utf8");
}

function mapOffset(offset, edits) {
  let mapped = offset;
  for (const edit of edits) {
    if (edit.start + edit.length <= offset) mapped += edit.newText.length - edit.length;
  }
  return mapped;
}

function renamedSite(capture, source, program, service, checker, targetSymbol) {
  const targetFile = path.resolve(source.root, ...capture.target.split("/"));
  const file = program.getSourceFile(targetFile);
  if (!file) throw new Unsupported("target file is not in the TypeScript program");
  const candidates = identifierNodes(program, source).filter(({ file: owner, node }) =>
    path.resolve(owner.fileName) === targetFile && node.text === capture.oldName && declarationName(node));
  const symbols = new Set(candidates.map(({ node }) => checker.getSymbolAtLocation(node)).filter(Boolean));
  if (symbols.size !== 1) throw new Unsupported(`selected declaration name is missing or ambiguous (${candidates.length} candidates, ${symbols.size} symbols, ${targetFile})`);
  const symbol = [...symbols][0];
  if (symbol !== targetSymbol) throw new Unsupported("selected declaration does not resolve to one source symbol");
  if (!symbol.declarations || !symbol.declarations.length
      || symbol.declarations.some((declaration) => !declaration.name || !declarationName(declaration.name)
        || !source.files.has(path.resolve(declaration.getSourceFile().fileName)))) {
    throw new Unsupported("merged or external declarations are outside the rename profile");
  }
  const primary = candidates[0].node;
  const info = service.getRenameInfo(file.fileName, primary.getStart(file));
  if (!info.canRename) throw new Unsupported(info.localizedErrorMessage || "TypeScript rejected this rename target");
  const locations = service.findRenameLocations(file.fileName, primary.getStart(file), false, false, {
    providePrefixAndSuffixTextForRename: false,
  });
  if (!locations || locations.length === 0 || locations.length > MAX_BINDING_SITES) {
    throw new Unsupported("TypeScript did not return a bounded rename-site set");
  }

  const allIdentifiers = identifierNodes(program, source);
  const direct = new Map();
  const related = [];
  for (const { file: owner, node } of allIdentifiers) {
    const found = checker.getSymbolAtLocation(node);
    const resolved = followAlias(checker, found);
    if (resolved !== symbol) continue;
    const parent = node.parent;
    const importOrExportToken = (ts.isImportSpecifier(parent) || ts.isExportSpecifier(parent))
      && (parent.propertyName === node || parent.propertyName === undefined && parent.name === node);
    const isDirect = found === symbol || importOrExportToken;
    const item = { file: owner, node, direct: isDirect };
    related.push(item);
    if (isDirect) direct.set(siteKey(owner.fileName, node.getStart(owner), node.getWidth(owner)), item);
  }
  if (related.length === 0 || related.length > MAX_BINDING_SITES) {
    throw new Unsupported("compiler binding graph exceeds the supported reference bound");
  }

  const edits = [];
  const seen = new Set();
  for (const location of locations) {
    const key = siteKey(location.fileName, location.textSpan.start, location.textSpan.length);
    const expected = direct.get(key);
    if (!expected) {
      const preservedAlias = related.some((item) => !item.direct
        && path.resolve(item.file.fileName) === path.resolve(location.fileName)
        && item.node.getStart(item.file) === location.textSpan.start
        && item.node.getWidth(item.file) === location.textSpan.length);
      if (preservedAlias) continue;
      throw new Unsupported("TypeScript returned a rename site outside the checked binding graph");
    }
    if (seen.has(key) || location.prefixText || location.suffixText) {
      throw new Unsupported("TypeScript rename locations do not match the compiler-resolved binding sites");
    }
    seen.add(key);
    const text = expected.file.text;
    if (text.slice(location.textSpan.start, location.textSpan.start + location.textSpan.length) !== capture.oldName) {
      throw new Unsupported("rename location does not cover the complete selected identifier");
    }
    const parent = expected.node.parent;
    let newText = capture.newName;
    if ((ts.isImportSpecifier(parent) || ts.isExportSpecifier(parent))
        && parent.propertyName === undefined && parent.name === expected.node) {
      newText = `${capture.newName} as ${capture.oldName}`;
    }
    edits.push({ path: source.names.get(path.resolve(location.fileName)),
      start: location.textSpan.start, length: location.textSpan.length, newText,
      startByte: utf16ToByte(text, location.textSpan.start),
      lengthByte: Buffer.byteLength(text.slice(location.textSpan.start,
        location.textSpan.start + location.textSpan.length), "utf8"), fileName: path.resolve(location.fileName) });
  }
  if (seen.size !== direct.size) throw new Unsupported("language service omitted a compiler-resolved binding site");
  const byFile = new Map();
  for (const edit of edits) {
    const group = byFile.get(edit.fileName) || [];
    group.push(edit);
    byFile.set(edit.fileName, group);
  }
  for (const group of byFile.values()) {
    group.sort((a, b) => a.start - b.start);
    for (let i = 1; i < group.length; i++) {
      if (group[i - 1].start + group[i - 1].length > group[i].start) {
        throw new Unsupported("TypeScript returned overlapping rename locations");
      }
    }
  }
  return { symbol, file, candidates, related, edits, primary };
}

function applyToSources(source, edits) {
  const updated = new Map(source.files);
  const grouped = new Map();
  for (const edit of edits) {
    const group = grouped.get(edit.fileName) || [];
    group.push(edit);
    grouped.set(edit.fileName, group);
  }
  for (const [file, changes] of grouped) {
    let text = updated.get(file);
    for (const edit of [...changes].sort((a, b) => b.start - a.start)) {
      text = text.slice(0, edit.start) + edit.newText + text.slice(edit.start + edit.length);
    }
    updated.set(file, text);
  }
  return { ...source, files: updated };
}

function verifyBindings(source, before, after, rename) {
  const options = after.options;
  const programAfter = programFor(after, options);
  const diagnostics = diagnosticEvidence(programAfter.program, after);
  if (diagnostics.length) return { ok: false, diagnostics };
  const checker = programAfter.checker;
  const targetPath = path.resolve(after.root, ...source.target.split("/"));
  const afterFile = programAfter.program.getSourceFile(targetPath);
  if (!afterFile) return { ok: false, diagnostics: [], reason: "renamed target file disappeared" };
  const editsByFile = new Map();
  for (const edit of rename.edits) {
    const group = editsByFile.get(edit.fileName) || [];
    group.push(edit);
    editsByFile.set(edit.fileName, group);
  }
  const targetDecl = rename.candidates[0].node;
  const targetName = mapOffset(targetDecl.getStart(rename.file), editsByFile.get(targetPath) || []);
  const targetNode = findIdentifierAt(afterFile, targetName, source.newName);
  const targetSymbol = targetNode && checker.getSymbolAtLocation(targetNode);
  if (!targetSymbol) return { ok: false, diagnostics: [], reason: "renamed declaration does not bind" };
  const beforeIds = identifierNodes(before.program, source);
  const bindingKey = (symbol, edited) => {
    if (!symbol) return null;
    const declarations = symbol.declarations || [];
    if (!declarations.length) throw new Unsupported("a captured identifier has no stable compiler declaration");
    const keys = declarations.map((declaration) => {
      const fileName = path.resolve(declaration.getSourceFile().fileName);
      const start = declaration.getStart(declaration.getSourceFile());
      const mapped = edited ? mapOffset(start, editsByFile.get(fileName) || []) : start;
      return `${fileName}\0${mapped}\0${declaration.kind}`;
    }).sort();
    return JSON.stringify(keys);
  };
  const sameBinding = (oldSymbol, newSymbol) =>
    bindingKey(oldSymbol, true) === bindingKey(newSymbol, false);

  for (const { file, node } of beforeIds) {
    const fileName = path.resolve(file.fileName);
    const changes = editsByFile.get(fileName) || [];
    const edit = changes.find((item) => item.start === node.getStart(file));
    const expandedAlias = edit && edit.newText === `${source.newName} as ${source.oldName}`
      && (ts.isImportSpecifier(node.parent) || ts.isExportSpecifier(node.parent));
    const mapped = expandedAlias
      ? edit.start + source.newName.length + 4
      : mapOffset(node.getStart(file), changes);
    const expectedText = expandedAlias ? source.oldName : edit ? source.newName : node.text;
    const afterFile = programAfter.program.getSourceFile(fileName);
    const replacement = afterFile && findIdentifierAt(afterFile, mapped, expectedText);
    if (!replacement) return { ok: false, diagnostics: [], reason: "a captured identifier disappeared after rename" };

    const oldDirect = before.checker.getSymbolAtLocation(node);
    const newDirect = checker.getSymbolAtLocation(replacement);
    const oldResolved = followAlias(before.checker, oldDirect);
    const newResolved = followAlias(checker, newDirect);
    const oldShorthand = before.checker.getShorthandAssignmentValueSymbol(node);
    const newShorthand = checker.getShorthandAssignmentValueSymbol(replacement);
    if (!sameBinding(oldDirect, newDirect) || !sameBinding(oldResolved, newResolved)
        || !sameBinding(oldShorthand, newShorthand)) {
      return { ok: false, diagnostics: [], status: "UNSUPPORTED",
        reason: "rename changes the compiler binding of a captured identifier" };
    }
  }

  for (const edit of rename.edits) {
    const afterSource = programAfter.program.getSourceFile(edit.fileName);
    const changed = afterSource && findIdentifierAt(afterSource, edit.start, source.newName);
    const resolved = changed && followAlias(checker, checker.getSymbolAtLocation(changed));
    if (resolved !== targetSymbol) return { ok: false, diagnostics: [], status: "UNSUPPORTED",
      reason: "a renamed token does not resolve to the selected declaration" };
  }
  return { ok: true, diagnostics: [], targetSymbol };
}

function findIdentifierAt(file, start, text) {
  let found;
  function visit(node) {
    if (node.pos <= start && node.end >= start && ts.isIdentifier(node)
        && node.getStart(file) === start && node.text === text) found = node;
    if (node.pos <= start && node.end >= start) ts.forEachChild(node, visit);
  }
  visit(file);
  return found;
}

function execute(raw) {
  let capture;
  try { capture = parseRequest(raw); }
  catch (error) { return fail("UNSUPPORTED", error.message); }
  try {
    const source = sourceFiles(capture);
    inspectSyntax(source, capture);
    const options = { strict: true, noEmit: true, target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler,
      skipLibCheck: false, allowJs: false, checkJs: false, types: [] };
    const before = programFor(source, options);
    const diagnosticsBefore = diagnosticEvidence(before.program, source);
    if (diagnosticsBefore.length) return fail("UNSUPPORTED", "captured project has diagnostics under the fixed compiler profile", diagnosticsBefore);
    const targetFile = path.resolve(source.root, ...capture.target.split("/"));
    const file = before.program.getSourceFile(targetFile);
    if (!file) throw new Unsupported("target file is not in the TypeScript program");
    const targetCandidates = identifierNodes(before.program, source).filter(({ file: owner, node }) =>
      path.resolve(owner.fileName) === targetFile && node.text === capture.oldName && declarationName(node));
    const targetSymbols = new Set(targetCandidates.map(({ node }) => before.checker.getSymbolAtLocation(node)).filter(Boolean));
    if (targetSymbols.size !== 1) throw new Unsupported("selected declaration name is missing or ambiguous in the target file");
    const rename = renamedSite(capture, source, before.program, before.service, before.checker, [...targetSymbols][0]);
    const afterSource = applyToSources(source, rename.edits);
    afterSource.options = options;
    const checked = verifyBindings(source, before, afterSource, rename);
    if (!checked.ok) {
      return fail(checked.status || (checked.diagnostics && checked.diagnostics.length ? "UNSUPPORTED" : "UNKNOWN"),
        checked.reason || "post-rename compiler diagnostics are present", diagnosticsBefore, checked.diagnostics || []);
    }
    const relatedAliasSites = rename.related.filter((item) => !rename.edits.some((edit) =>
      edit.fileName === path.resolve(item.file.fileName) && edit.start === item.node.getStart(item.file))).length;
    const edits = rename.edits.map((edit) => ({ path: edit.path, start_byte: edit.startByte,
      length_byte: edit.lengthByte, new_text: edit.newText }));
    const response = { protocol_version: PROTOCOL_VERSION, typescript_version: TYPESCRIPT_VERSION,
      status: "PREVIEW", reason: "", edits,
      binding_counts: { renamed_sites: rename.edits.length, preserved_alias_sites: relatedAliasSites },
      postconditions: { diagnostics_clear: true, binding_graph_preserved: true, rename_edits_complete: true },
      diagnostics_before: diagnosticsBefore, diagnostics_after: [] };
    return response;
  } catch (error) {
    if (error instanceof Unsupported) return fail("UNSUPPORTED", error.message);
    return fail("UNKNOWN", `TypeScript rename analysis failed: ${error && error.name || "Error"}: ${error && error.message || "no diagnostic"}`);
  }
}

function main() {
  try {
    const result = execute(readRequest());
    const text = JSON.stringify(result);
    if (Buffer.byteLength(text, "utf8") > MAX_RESPONSE_BYTES) {
      process.stdout.write(JSON.stringify(fail("UNKNOWN", "rename evidence exceeds its output limit")));
    } else process.stdout.write(text);
  } catch (error) {
    process.stdout.write(JSON.stringify(fail("UNKNOWN", `rename request failed: ${error && error.name || "Error"}`)));
  }
}

if (require.main === module) main();
