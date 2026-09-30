#!/usr/bin/env node
// A small HTTP service with two behaviours that interact.
//
// `POST /items` appends an item. `GET /items` returns the current list. Neither
// is interesting alone: the second behaviour is only observable *after* the
// first has run, which is what makes this a real test of the application's
// state rather than of two independent endpoints.
//
// Two design choices are deliberate and are what make two instances safe to run
// beside each other.
//
// **The port is allocated by the operating system, and the process that was
// given it publishes the fact.** The server binds port 0 and writes the port
// it actually got to a file whose path the caller chose. A driver that wanted
// to find a free port, close the probe, and then bind the same number would be
// assuming a number stays free between the probe and the bind, and on a busy
// machine it does not. Nothing here guesses a port.
//
// **State lives in a file the caller names, not in the process.** Each instance
// is started with its own state path, so two instances on two ports cannot read
// each other's fixtures, and removing a fixture after a run cannot disturb a
// concurrent one.
'use strict';

const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

/**
 * Read the request body as UTF-8.
 *
 * @param {import('node:http').IncomingMessage} request
 * @returns {Promise<string>}
 */
function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('error', reject);
    request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
  });
}

/**
 * Load the item list from disk.
 *
 * Read on every request rather than cached, so a driver can seed an instance's
 * fixtures before starting it and a second instance's files can never appear
 * in this one. A missing file is an empty list, which is the state a service
 * that has never been written to should be in.
 *
 * @param {string} statePath
 * @returns {{name: string}[]}
 */
function loadItems(statePath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    if (error.code === 'ENOENT') {
      return [];
    }
    throw error;
  }
}

/**
 * Write the item list to disk, atomically.
 *
 * @param {string} statePath
 * @param {{name: string}[]} items
 */
function saveItems(statePath, items) {
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  const temporary = `${statePath}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(items, null, 2)}\n`, 'utf8');
  fs.renameSync(temporary, statePath);
}

/**
 * Parse the driver's arguments.
 *
 * @param {string[]} argv
 * @returns {{port: number, state: string, ready: string}}
 */
function parseArgs(argv) {
  const options = { port: 0, state: null, ready: null };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--port') {
      options.port = Number(argv[i + 1]);
      i += 1;
    } else if (arg === '--state') {
      options.state = argv[i + 1];
      i += 1;
    } else if (arg === '--ready') {
      options.ready = argv[i + 1];
      i += 1;
    } else {
      throw new Error(`items-server: unknown argument ${arg}`);
    }
  }
  if (!options.state || !options.ready) {
    throw new Error('items-server: --state and --ready are required');
  }
  return options;
}

/**
 * Start the service and publish the port it was given.
 *
 * @param {{port: number, state: string, ready: string}} options
 * @returns {Promise<import('node:http').Server>}
 */
function start(options) {
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, 'http://localhost');
    const send = (status, body) => {
      const payload = `${JSON.stringify(body, null, 2)}\n`;
      response.writeHead(status, {
        'content-type': 'application/json',
        'content-length': Buffer.byteLength(payload),
      });
      response.end(payload);
    };

    if (request.method === 'GET' && url.pathname === '/items') {
      let items;
      try {
        items = loadItems(options.state);
      } catch (error) {
        send(500, { error: `state unreadable: ${error.message}` });
        return;
      }
      send(200, { items });
      return;
    }

    if (request.method === 'POST' && url.pathname === '/items') {
      readBody(request).then(
        (raw) => {
          let parsed;
          try {
            parsed = JSON.parse(raw);
          } catch (error) {
            send(400, { error: `body is not JSON: ${error.message}` });
            return;
          }
          if (!parsed || typeof parsed.name !== 'string' || parsed.name === '') {
            send(400, { error: 'a non-empty string "name" is required' });
            return;
          }
          let items;
          try {
            items = loadItems(options.state);
          } catch (error) {
            send(500, { error: `state unreadable: ${error.message}` });
            return;
          }
          items.push({ name: parsed.name });
          try {
            saveItems(options.state, items);
          } catch (error) {
            send(500, { error: `state not writable: ${error.message}` });
            return;
          }
          // 201 with the created item, so a client can tell a creation from an
          // update without reading the list back.
          send(201, { created: { name: parsed.name }, count: items.length });
        },
        (error) => send(400, { error: `body not readable: ${error.message}` }),
      );
      return;
    }

    send(404, { error: `no route for ${request.method} ${url.pathname}` });
  });

  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(options.port, '127.0.0.1', () => {
      const address = server.address();
      // The readiness artifact is written only after the socket is listening,
      // and it names the port the kernel assigned. A driver that sees this file
      // knows the service is accepting connections, which is a stronger claim
      // than "the process started".
      fs.mkdirSync(path.dirname(options.ready), { recursive: true });
      const temporary = `${options.ready}.tmp`;
      fs.writeFileSync(
        temporary,
        `${JSON.stringify({ port: address.port, pid: process.pid, state: options.state }, null, 2)}\n`,
        'utf8',
      );
      fs.renameSync(temporary, options.ready);
      resolve(server);
    });
  });
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const server = await start(options);
  // SIGTERM is how a driver stops the instance. The handler exists so the exit
  // is clean rather than a kill, which matters when two instances share a host.
  const stop = () => {
    server.close(() => process.exit(0));
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}

main().catch((error) => {
  process.stderr.write(`items-server: ${error.message}\n`);
  process.exit(2);
});
