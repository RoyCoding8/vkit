/* The built-in test runner's cases for split-bill.
 *
 * These exercise the arithmetic core directly, which the scenario driver in
 * verify-split.js does not: that one runs the CLI and compares whole printed
 * outputs, so a bug that cancelled out across a print would not show. This
 * file asserts the invariant the module exists to keep, the shares summing to
 * the amount owed, across the shapes a hand-written example list forgets.
 *
 * `node --test` is the runner and there is no framework here, because the
 * project's own test command is the one a maintainer already types. */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { splitCents, formatCents } = require('../src/split-bill.js');

test('an even two-way split divides the total exactly', () => {
  assert.deepEqual(splitCents(1000, [1, 1]), [500, 500]);
});

test('the shares always sum to the amount owed', () => {
  // Every weight pattern a caller can actually write: lengths one through six,
  // weights from one through four. This is the invariant an off-by-one in the
  // leftover loop would break, and a fixed list of examples would not.
  for (let count = 1; count <= 6; count += 1) {
    for (let weight = 1; weight <= 4; weight += 1) {
      const weights = new Array(count).fill(weight);
      for (const total of [0, 1, 7, 100, 999, 12345]) {
        const shares = splitCents(total, weights);
        const sum = shares.reduce((a, b) => a + b, 0);
        assert.equal(
          sum, total,
          `${count} people at weight ${weight} split ${total} into ${shares}`,
        );
      }
    }
  }
});

test('a share never exceeds the exact proportion it is owed', () => {
  // Largest-remainder rounding can only hand a cent to a fraction that was
  // rounded down, so no share may exceed its exact value. A test that only
  // checked the sum would pass on a distribution that overpaid one person by
  // taking from another.
  const weights = [1, 2, 3];
  for (const total of [100, 1000, 10000]) {
    const weightSum = weights.reduce((a, b) => a + b, 0);
    const shares = splitCents(total, weights);
    shares.forEach((share, index) => {
      assert.ok(
        share <= Math.ceil((total * weights[index]) / weightSum),
        `person ${index} received ${share} of a total of ${total}`,
      );
    });
  }
});

test('a single person receives the whole amount', () => {
  assert.deepEqual(splitCents(4242, [7]), [4242]);
});

test('formatCents renders integer cents as a decimal amount', () => {
  assert.equal(formatCents(0), '0.00');
  assert.equal(formatCents(5), '0.05');
  assert.equal(formatCents(123456), '1234.56');
  assert.equal(formatCents(-250), '-2.50');
});

test('a negative total is refused rather than split', () => {
  assert.throws(() => splitCents(-1, [1]), RangeError);
});

test('an empty weight list is refused rather than split', () => {
  assert.throws(() => splitCents(100, []), RangeError);
});