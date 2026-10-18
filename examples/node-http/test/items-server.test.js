/* The built-in test runner's cases for the items service.
 *
 * These drive the real service over a real socket, started as a child process,
 * because the module runs its server on import and there is nothing to assert
 * about it without a socket to ask. That is a deliberate overlap with
 * verify-items.js rather than a duplicate of it: that driver is the scenario
 * check and reports named scenarios, and this is the runner's check, whose
 * result is a set of named cases the receipt can require individually.
 *
 * `node --test` is the runner and there is no framework here, because the
 * project's own test command is the one a maintainer already types. */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const SERVER = path.join(__dirname, '..', 'src', 'items-server.js');

/* Starts the service and waits for the file it publishes the port to. The
   server chooses its own port and writes it down, so nothing here probes for a
   free number and then hopes it stayed free. */
async function startService() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'items-server-'));
  const ready = path.join(directory, 'ready.json');
  const child = spawn(
    process.execPath,
    [SERVER, '--state', path.join(directory, 'items.json'), '--ready', ready],
    { stdio: ['ignore', 'pipe', 'pipe'] },
  );

  const deadline = Date.now() + 20000;
  let published = null;
  while (Date.now() < deadline) {
    if (fs.existsSync(ready)) {
      published = JSON.parse(fs.readFileSync(ready, 'utf8'));
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  if (published === null) {
    child.kill();
    throw new Error('the service never published the port it was given');
  }
  return {
    port: published.port,
    async stop() {
      child.kill('SIGTERM');
      await new Promise((resolve) => child.once('exit', resolve));
      fs.rmSync(directory, { recursive: true, force: true });
    },
  };
}

async function request(port, method, path_, body) {
  const response = await fetch(`http://127.0.0.1:${port}${path_}`, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, payload: await response.json() };
}

test('a created item is observable by reading the list back', async (t) => {
  const service = await startService();
  t.after(() => service.stop());

  const before = await request(service.port, 'GET', '/items');
  assert.equal(before.status, 200);
  assert.deepEqual(before.payload.items, [], 'a service nobody wrote to is empty');

  const created = await request(service.port, 'POST', '/items', { name: 'lamp' });
  assert.equal(created.status, 201);
  assert.deepEqual(created.payload.created, { name: 'lamp' });

  const after = await request(service.port, 'GET', '/items');
  assert.deepEqual(after.payload.items, [{ name: 'lamp' }]);
});

test('an unknown route is refused rather than served', async (t) => {
  const service = await startService();
  t.after(() => service.stop());

  const missing = await request(service.port, 'GET', '/nothing');

  assert.equal(missing.status, 404);
});