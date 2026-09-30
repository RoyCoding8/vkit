#!/usr/bin/env node
// A real CLI that splits a bill owed in cents between named people with
// integer weights, and prints each person's share.
//
// The arithmetic core is splitCents, and its invariant is that the shares
// always sum to the amount owed. Flooring an exact share discards a fraction
// of a cent, so the discarded remainder is handed back out one cent at a time,
// largest fraction first. That is why an off-by-one in the leftover loop is a
// money bug rather than a formatting difference: the shares would stop
// summing to the amount owed.
'use strict';

function fail(message) {
  process.stderr.write(`split-bill: ${message}\n`);
  process.exit(2);
}

/** Integer cents as a decimal amount, for example 333 -> "3.33". */
function formatCents(cents) {
  const sign = cents < 0 ? '-' : '';
  const magnitude = Math.abs(cents);
  return `${sign}${Math.floor(magnitude / 100)}.${String(magnitude % 100).padStart(2, '0')}`;
}

/**
 * Split totalCents between people by weight.
 *
 * @param {number} totalCents non-negative integer cents owed
 * @param {number[]} weights one positive integer weight per person
 * @returns {number[]} integer cents per person, summing to totalCents
 */
function splitCents(totalCents, weights) {
  if (!Number.isInteger(totalCents) || totalCents < 0) {
    throw new RangeError(`total must be non-negative integer cents, got ${totalCents}`);
  }
  if (weights.length === 0) {
    throw new RangeError('at least one person is required');
  }
  const weightSum = weights.reduce((a, b) => a + b, 0);

  const exact = weights.map((weight) => (totalCents * weight) / weightSum);
  const shares = exact.map((value) => Math.floor(value));
  const handedOut = shares.reduce((a, b) => a + b, 0);
  const leftover = totalCents - handedOut;

  // Largest fractional part first. Ties keep the order the people were given
  // in, so an even split is decided by the command line and not by sort
  // implementation detail.
  const order = exact
    .map((value, index) => ({ index, fraction: value - Math.floor(value) }))
    .sort((a, b) => b.fraction - a.fraction || a.index - b.index)
    .map((entry) => entry.index);

  for (let handed = 0; handed < leftover; handed += 1) {
    shares[order[handed]] += 1;
  }
  return shares;
}

const USAGE = `usage: split-bill split <total-cents> <name>=<weight> [<name>=<weight> ...] [--tip <cents>]

Splits the total plus the tip between people by weight and prints each share.
The shares always sum to the amount owed.`;

function parseArgs(argv) {
  const command = argv[0];
  if (command === '--help' || command === '-h') {
    process.stdout.write(`${USAGE}\n`);
    process.exit(0);
  }
  if (command !== 'split') {
    fail(`unknown command ${command === undefined ? '<none>' : command}\n${USAGE}`);
  }

  let tipCents = 0;
  const positionals = [];
  for (let i = 1; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--tip') {
      const value = argv[i + 1];
      if (value === undefined || !/^\d+$/.test(value)) {
        fail('--tip needs a non-negative whole number of cents');
      }
      tipCents = Number(value);
      i += 1;
    } else if (arg.startsWith('-')) {
      fail(`unknown option ${arg}\n${USAGE}`);
    } else {
      positionals.push(arg);
    }
  }

  if (positionals.length < 2) {
    fail('a total and at least one name=weight are required\n' + USAGE);
  }
  if (!/^\d+$/.test(positionals[0])) {
    fail(`total must be a non-negative whole number of cents, got ${positionals[0]}`);
  }
  const totalCents = Number(positionals[0]);

  const people = positionals.slice(1).map((token) => {
    const split = token.indexOf('=');
    if (split <= 0) {
      fail(`expected name=weight, got ${token}`);
    }
    const name = token.slice(0, split);
    const weight = token.slice(split + 1);
    if (!/^\d+$/.test(weight) || Number(weight) < 1) {
      fail(`weight for ${name} must be a positive whole number, got ${weight || '<none>'}`);
    }
    return { name, weight: Number(weight) };
  });

  return { totalCents, tipCents, people };
}

function main(argv) {
  const { totalCents, tipCents, people } = parseArgs(argv);
  const owedCents = totalCents + tipCents;
  const shares = splitCents(owedCents, people.map((person) => person.weight));

  const lines = [
    `total ${formatCents(totalCents)}`,
    `tip ${formatCents(tipCents)}`,
    `owed ${formatCents(owedCents)}`,
    ...people.map((person, index) => `${person.name} ${formatCents(shares[index])}`),
  ];
  process.stdout.write(`${lines.join('\n')}\n`);
  return 0;
}

if (require.main === module) {
  try {
    process.exit(main(process.argv.slice(2)));
  } catch (error) {
    fail(error.message);
  }
}

module.exports = { splitCents, formatCents };
