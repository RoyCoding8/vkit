"use strict";

const fs = require("node:fs");
const packageInfo = require("recheck/package.json");
const { checkSync } = require("recheck/lib/browser.js");

const PROTOCOL_VERSION = 1;
const BACKEND_VERSION = "4.5.0";
const MAX_PATTERN_BYTES = 2048;
const MAX_FLAGS = 6;
const MAX_REQUEST_BYTES = 16_384;
const MAX_RESPONSE_BYTES = 262_144;
const SUPPORTED_FLAGS = new Set("gimsuy");

function diagnosticsFor(source, flags, timeoutMs) {
  const diagnostics = checkSync(source, flags, {
    checker: "automaton",
    timeout: timeoutMs,
    recallTimeout: -1,
  });
  return {
    source: diagnostics.source,
    flags: diagnostics.flags,
    status: diagnostics.status,
    checker: diagnostics.checker ?? null,
    complexity: diagnostics.complexity ?? null,
    attack: diagnostics.attack ? {
      pattern: diagnostics.attack.pattern,
      base: diagnostics.attack.base,
      suffix: diagnostics.attack.suffix,
      pumps: diagnostics.attack.pumps,
    } : null,
    hotspot: diagnostics.hotspot ?? [],
    error: diagnostics.error ?? null,
  };
}

function analyze(request) {
  if (packageInfo.version !== BACKEND_VERSION) {
    return { protocol_version: PROTOCOL_VERSION, backend_version: packageInfo.version, diagnostics: null };
  }
  if (!request || typeof request !== "object" || Array.isArray(request)
      || Object.keys(request).sort().join(",") !== "flags,pattern,protocol_version,timeout_ms"
      || request.protocol_version !== PROTOCOL_VERSION
      || typeof request.pattern !== "string"
      || Buffer.byteLength(request.pattern, "utf8") > MAX_PATTERN_BYTES
      || typeof request.flags !== "string"
      || request.flags.length > MAX_FLAGS
      || [...request.flags].some((flag) => !SUPPORTED_FLAGS.has(flag))
      || new Set(request.flags).size !== request.flags.length
      || !Number.isSafeInteger(request.timeout_ms)
      || request.timeout_ms < 1 || request.timeout_ms > 60_000) {
    throw new TypeError("invalid regex-cost request");
  }
  return {
    protocol_version: PROTOCOL_VERSION,
    backend_version: packageInfo.version,
    diagnostics: diagnosticsFor(request.pattern, request.flags, request.timeout_ms),
  };
}

function main() {
  const input = Buffer.alloc(MAX_REQUEST_BYTES + 1);
  let length = 0;
  while (length < input.length) {
    const read = fs.readSync(0, input, length, input.length - length, null);
    if (read === 0) break;
    length += read;
  }
  if (length > MAX_REQUEST_BYTES) throw new RangeError("request exceeds the input limit");
  const request = JSON.parse(input.subarray(0, length).toString("utf8"));
  const result = analyze(request);
  const output = Buffer.from(JSON.stringify(result), "utf8");
  if (output.length > MAX_RESPONSE_BYTES) throw new RangeError("diagnostics exceed the output limit");
  fs.writeSync(1, output);
}

module.exports = { analyze };

if (require.main === module) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`${error.name}: ${error.message}\n`);
    process.exitCode = 1;
  }
}
