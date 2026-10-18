"use strict";

const assert = require("node:assert/strict");
const { analyze } = require("./helper.cjs");

function check(pattern, flags = "") {
  return analyze({
    protocol_version: 1,
    pattern,
    flags,
    timeout_ms: 2_000,
  });
}

const exponential = check("^(a+)+$");
assert.equal(exponential.backend_version, "4.5.0");
assert.equal(exponential.diagnostics.status, "vulnerable");
assert.equal(exponential.diagnostics.checker, "automaton");
assert.equal(exponential.diagnostics.complexity.type, "exponential");
assert.equal(exponential.diagnostics.complexity.isFuzz, false);
assert.equal(typeof exponential.diagnostics.attack.pattern, "string");
assert.deepEqual(exponential.diagnostics.hotspot.map(({ start, end }) => [start, end]), [[2, 3]]);

const polynomial = check("^(a|aa)+$");
assert.equal(polynomial.diagnostics.status, "vulnerable");
assert.equal(polynomial.diagnostics.complexity.type, "exponential");
assert.equal(polynomial.diagnostics.complexity.isFuzz, false);
assert.equal(polynomial.diagnostics.hotspot.length, 2);

const linear = check("^a+$");
assert.equal(linear.diagnostics.status, "safe");
assert.equal(linear.diagnostics.complexity.type, "linear");
assert.equal(linear.diagnostics.complexity.isFuzz, false);
assert.equal(linear.diagnostics.attack, null);

const invalid = check("[");
assert.equal(invalid.diagnostics.status, "unknown");
assert.equal(invalid.diagnostics.error.kind, "invalid");

assert.throws(() => analyze({
  protocol_version: 1,
  pattern: "a",
  flags: "",
  timeout_ms: 2_000,
  extra: true,
}), /invalid regex-cost request/);
assert.throws(() => analyze({
  protocol_version: 1,
  pattern: "a",
  flags: "d",
  timeout_ms: 2_000,
}), /invalid regex-cost request/);
