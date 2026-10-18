#!/usr/bin/env node
// A verification driver: it runs the real CLI and records what actually happened.
//
// The driver has no way to be told the answer. Every expected output below is a
// literal string written by hand from the specified behaviour, and the driver
// compares it against the real process output. A defect in split-bill.js changes
// what the process prints, and the comparison notices. There is no flag that
// reports a pass or a fail, so the driver cannot manufacture agreement.
'use strict';

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

function parseArgs(argv) {
  const options = { node: process.execPath, app: null, out: null };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--node') {
      options.node = argv[i + 1];
      i += 1;
    } else if (arg === '--app') {
      options.app = argv[i + 1];
      i += 1;
    } else if (arg === '--out') {
      options.out = argv[i + 1];
      i += 1;
    } else {
      throw new Error(`verify-split: unknown argument ${arg}`);
    }
  }
  if (!options.app || !options.out) {
    throw new Error('verify-split: --app and --out are required');
  }
  return options;
}

// Each case is (id, argv tail, the exact stdout the real CLI must print).
// An even split gives an exact answer. An uneven one with a leftover cent
// gives the cent to the first person, because ties keep command line order.
const CASES = [
  ['even-two-way', ['1000', 'ann=1', 'bob=1'], 'total 10.00\ntip 0.00\nowed 10.00\nann 5.00\nbob 5.00\n'],
  [
    'even-three-way-weights',
    ['1000', 'ann=2', 'bob=1', 'cyd=1'],
    'total 10.00\ntip 0.00\nowed 10.00\nann 5.00\nbob 2.50\ncyd 2.50\n',
  ],
  ['single-person', ['100', 'ann=1'], 'total 1.00\ntip 0.00\nowed 1.00\nann 1.00\n'],
  [
    'zero-total',
    ['0', 'ann=1', 'bob=2', 'cyd=3'],
    'total 0.00\ntip 0.00\nowed 0.00\nann 0.00\nbob 0.00\ncyd 0.00\n',
  ],
  [
    'leftover-cent-goes-to-first',
    ['1', 'ann=2', 'bob=1'],
    'total 0.01\ntip 0.00\nowed 0.01\nann 0.01\nbob 0.00\n',
  ],
  [
    'tip-joins-the-split',
    ['1000', 'ann=1', 'bob=1', '--tip', '333'],
    'total 10.00\ntip 3.33\nowed 13.33\nann 6.67\nbob 6.66\n',
  ],
  [
    'three-way-leftover-two-cents',
    ['999', 'ann=3', 'bob=3', 'cyd=1'],
    'total 9.99\ntip 0.00\nowed 9.99\nann 4.28\nbob 4.28\ncyd 1.43\n',
  ],
  [
    'four-way-leftover-and-tip',
    ['7000', 'a=5', 'b=3', 'c=1', '--tip', '999'],
    'total 70.00\ntip 9.99\nowed 79.99\na 44.44\nb 26.66\nc 8.89\n',
  ],
];

function runCase(options, scenarioId, tail, expected) {
  const done = spawnSync(options.node, [options.app, 'split', ...tail], {
    encoding: 'utf8',
    timeout: 60000,
  });

  if (done.error) {
    return {
      id: scenarioId,
      result: 'FAIL',
      observation: `could not run the CLI: ${done.error.message}`,
    };
  }
  if (done.status !== 0) {
    const reason = (done.stderr || '').trim().slice(0, 200);
    return { id: scenarioId, result: 'FAIL', observation: `exited ${done.status}: ${reason}` };
  }
  if (done.stdout === expected) {
    return { id: scenarioId, result: 'PASS', observation: `printed the expected split\n${expected}` };
  }
  return {
    id: scenarioId,
    result: 'FAIL',
    observation: `expected\n${expected}printed\n${done.stdout || '<nothing>'}`,
  };
}

function main(argv) {
  const options = parseArgs(argv);
  const scenarios = CASES.map(([id, tail, expected]) =>
    runCase(options, id, tail, expected),
  );

  const artifact = {
    schema_version: 1,
    description: 'split-bill CLI behaviour observed by running the real command',
    scenarios,
  };

  fs.mkdirSync(path.dirname(options.out), { recursive: true });
  // Write atomically: a reader must never see a half-written artifact, because
  // "the artifact existed" is the difference between PASS and BLOCKED.
  const temp = `${options.out}.tmp`;
  fs.writeFileSync(temp, `${JSON.stringify(artifact, null, 2)}\n`, 'utf8');
  fs.renameSync(temp, options.out);
  return 0;
}

try {
  process.exit(main(process.argv.slice(2)));
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exit(2);
}
