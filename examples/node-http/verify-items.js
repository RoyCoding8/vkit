#!/usr/bin/env node
// A verification driver: it starts the real service and speaks real HTTP to it.
//
// Every expected value below is a literal written by hand from the specified
// behaviour, and every assertion is on what the service actually returned over
// a socket. There is no flag that tells this driver what to report, and it
// imports nothing from the application, so it cannot agree with the application
// by construction. A defect in items-server.js changes what comes back over the
// wire and the comparison notices.
//
// The port is never chosen here. The driver starts the process, waits for the
// readiness file the service writes after `listen` resolves, and reads the port
// out of it. That is why no scenario below can pass by talking to the wrong
// server: there is no port to get wrong.
'use strict';

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

const START_TIMEOUT_MS = 20000;
const REQUEST_TIMEOUT_MS = 10000;

/**
 * Parse the driver's own arguments.
 *
 * @param {string[]} argv
 * @returns {{node: string, app: string, out: string, keepFixtures: boolean}}
 */
function parseArgs(argv) {
  const options = {
    node: process.execPath,
    app: null,
    out: null,
    keepFixtures: false,
  };
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
    } else if (arg === '--keep-fixtures') {
      options.keepFixtures = true;
    } else {
      throw new Error(`verify-items: unknown argument ${arg}`);
    }
  }
  if (!options.app || !options.out) {
    throw new Error('verify-items: --app and --out are required');
  }
  return options;
}

/**
 * One HTTP request, as a promise.
 *
 * @param {string} port
 * @param {string} method
 * @param {string} route
 * @param {object|null} body
 * @returns {Promise<{status: number, body: object}>}
 */
function request(port, method, route, body) {
  return new Promise((resolve, reject) => {
    const payload = body === null ? null : Buffer.from(JSON.stringify(body), 'utf8');
    const call = http.request(
      {
        host: '127.0.0.1',
        port: Number(port),
        path: route,
        method,
        headers: payload
          ? { 'content-type': 'application/json', 'content-length': payload.length }
          : {},
        timeout: REQUEST_TIMEOUT_MS,
      },
      (response) => {
        const chunks = [];
        response.on('data', (chunk) => chunks.push(chunk));
        response.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          let parsed;
          try {
            parsed = JSON.parse(text);
          } catch (error) {
            reject(new Error(`${method} ${route} returned non-JSON: ${text.slice(0, 200)}`));
            return;
          }
          resolve({ status: response.statusCode, body: parsed });
        });
      },
    );
    call.on('timeout', () => call.destroy(new Error(`${method} ${route} timed out`)));
    call.on('error', reject);
    if (payload) {
      call.write(payload);
    }
    call.end();
  });
}

/** Sleep, so a poll is not a busy loop. */
function pause(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/**
 * Start one service instance and wait for the port it was actually given.
 *
 * @param {object} options
 * @param {string} fixtureRoot
 * @param {string} label distinguishes concurrent instances
 * @returns {Promise<{port: string, state: string, stop: () => Promise<void>}>}
 */
async function startInstance(options, fixtureRoot, label) {
  const state = path.join(fixtureRoot, `${label}-state.json`);
  const ready = path.join(fixtureRoot, `${label}-ready.json`);
  const stdout = fs.openSync(path.join(fixtureRoot, `${label}.log`), 'a');

  // Port 0 asks the operating system to choose. The service reports what it
  // got; this function never guesses and never probes for a free port.
  const child = spawn(options.node, [options.app, '--port', '0', '--state', state, '--ready', ready], {
    stdio: ['ignore', stdout, stdout],
  });

  let exited = null;
  child.on('exit', (code, signal) => {
    exited = { code, signal };
  });

  const deadline = Date.now() + START_TIMEOUT_MS;
  while (!fs.existsSync(ready)) {
    if (exited) {
      const log = fs.readFileSync(path.join(fixtureRoot, `${label}.log`), 'utf8');
      throw new Error(
        `instance ${label} exited before becoming ready (code ${exited.code}, signal ${exited.signal}): ${log.slice(0, 300)}`,
      );
    }
    if (Date.now() > deadline) {
      child.kill();
      throw new Error(`instance ${label} did not publish a readiness artifact in ${START_TIMEOUT_MS}ms`);
    }
    await pause(25);
  }

  const announcement = JSON.parse(fs.readFileSync(ready, 'utf8'));
  if (!Number.isInteger(announcement.port) || announcement.port <= 0) {
    throw new Error(`instance ${label} announced an unusable port: ${announcement.port}`);
  }

  return {
    port: String(announcement.port),
    state,
    stop: () =>
      new Promise((resolve) => {
        if (exited) {
          resolve();
          return;
        }
        child.on('exit', () => resolve());
        child.kill('SIGTERM');
        setTimeout(() => {
          if (!exited) {
            child.kill('SIGKILL');
          }
        }, 3000).unref();
      }),
  };
}

/**
 * Compare what a service returned with a literal expected value.
 *
 * @param {string} id
 * @param {Promise<void>} body
 * @returns {Promise<{id: string, result: string, observation: string}>}
 */
async function scenario(id, body) {
  try {
    const observation = await body();
    return { id, result: 'PASS', observation };
  } catch (error) {
    return { id, result: 'FAIL', observation: error.message.slice(0, 400) };
  }
}

/** Assert two values are deeply equal, with a readable difference. */
function same(label, actual, expected) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) {
    throw new Error(`${label}: expected ${b}, got ${a}`);
  }
}

async function main(argv) {
  const options = parseArgs(argv);
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'vkit-items-'));
  const scenarios = [];
  const measurements = [];
  const started = [];

  try {
    // One instance for the behaviour scenarios. The two-interaction case is
    // this one: a POST followed by a GET, where the GET can only be right if
    // the POST's effect was actually persisted.
    const main1 = await startInstance(options, fixtureRoot, 'primary');
    started.push(main1);

    scenarios.push(await scenario('get-on-an-empty-service-returns-an-empty-list', async () => {
      const response = await request(main1.port, 'GET', '/items', null);
      same('status', response.status, 200);
      same('body', response.body, { items: [] });
      return 'GET /items on a service that has never been written to returned {items: []}';
    }));

    scenarios.push(await scenario('post-then-get-returns-the-updated-list', async () => {
      const created = await request(main1.port, 'POST', '/items', { name: 'lamp' });
      same('status', created.status, 201);
      same('created body', created.body, { created: { name: 'lamp' }, count: 1 });

      const listed = await request(main1.port, 'GET', '/items', null);
      same('status', listed.status, 200);
      // The literal list, in order. This is the assertion that the two
      // behaviours interact: it can only pass if the POST's write was read back
      // by a later request through a socket.
      same('list after one post', listed.body, { items: [{ name: 'lamp' }] });
      return 'POST /items {name: lamp} then GET /items returned exactly [{name: lamp}]';
    }));

    scenarios.push(await scenario('two-posts-accumulate-in-order', async () => {
      await request(main1.port, 'POST', '/items', { name: 'chair' });
      const listed = await request(main1.port, 'GET', '/items', null);
      same('list after two more posts', listed.body, {
        items: [{ name: 'lamp' }, { name: 'chair' }],
      });
      return 'after adding chair the list was [lamp, chair], in the order posted';
    }));

    scenarios.push(await scenario('a-post-without-a-name-is-rejected', async () => {
      const rejected = await request(main1.port, 'POST', '/items', { name: '' });
      same('status', rejected.status, 400);
      const listed = await request(main1.port, 'GET', '/items', null);
      // The rejection must not have changed the state. A service that answered
      // 400 and still appended would pass a status-only assertion.
      same('list is unchanged', listed.body, {
        items: [{ name: 'lamp' }, { name: 'chair' }],
      });
      return 'POST /items {name: ""} returned 400 and left the list at [lamp, chair]';
    }));

    scenarios.push(await scenario('an-unknown-route-is-a-404', async () => {
      const missing = await request(main1.port, 'GET', '/nothing', null);
      same('status', missing.status, 404);
      return 'GET /nothing returned 404';
    }));

    const samples = [];
    for (let i = 0; i < 40; i += 1) {
      const begun = process.hrtime.bigint();
      await request(main1.port, 'GET', '/items', null);
      samples.push(Number(process.hrtime.bigint() - begun) / 1e6);
    }
    samples.sort((a, b) => a - b);
    measurements.push({ name: 'get_items_p50_ms', value: samples[19], unit: 'ms', better: 'lower' });
    measurements.push({ name: 'get_items_p95_ms', value: samples[37], unit: 'ms', better: 'lower' });

    // A second instance, started alongside the first, on its own port and its
    // own state file. The assertion is that it can see none of the first
    // instance's items, which is the property that lets two instances run
    // without stepping on each other.
    const second = await startInstance(options, fixtureRoot, 'secondary');
    started.push(second);

    scenarios.push(await scenario('two-instances-do-not-share-state', async () => {
      if (second.port === main1.port) {
        throw new Error(`both instances were given port ${second.port}`);
      }
      const isolated = await request(second.port, 'GET', '/items', null);
      same('second instance sees an empty list', isolated.body, { items: [] });

      await request(second.port, 'POST', '/items', { name: 'rug' });
      const afterRug = await request(second.port, 'GET', '/items', null);
      same('second instance has only its own item', afterRug.body, { items: [{ name: 'rug' }] });

      const firstStillHas = await request(main1.port, 'GET', '/items', null);
      same('first instance is unaffected', firstStillHas.body, {
        items: [{ name: 'lamp' }, { name: 'chair' }],
      });
      return (
        `instance two on port ${second.port} listed only [rug] while instance one on port ` +
        `${main1.port} still listed [lamp, chair]`
      );
    }));
  } finally {
    for (const instance of started) {
      await instance.stop();
    }
  }

  const artifact = {
    schema_version: 1,
    description: 'items service behaviour observed over real HTTP',
    scenarios,
    measurements,
  };

  fs.mkdirSync(path.dirname(options.out), { recursive: true });
  const temporary = `${options.out}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(artifact, null, 2)}\n`, 'utf8');
  fs.renameSync(temporary, options.out);

  if (!options.keepFixtures) {
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
  }
  return 0;
}

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (error) => {
    process.stderr.write(`${error.message}\n`);
    process.exit(2);
  },
);
