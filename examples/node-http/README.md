# node-http example

A small HTTP service with two behaviours that interact. `POST /items` creates
an item. `GET /items` returns the current list. The second is only observable
after the first has run, which is what makes this a real exercise of the
application's state rather than of two independent endpoints.

The driver starts the service, speaks real HTTP to it, and compares what came
back with literal expected values written by hand. There is no flag that tells
the driver what to report, and it imports nothing from the application, so it
cannot agree with the application by construction.

## Two decisions worth naming

**The port is allocated by the operating system and published by the process
that received it.** The service binds port 0 and writes the port it was given
to a readiness file whose path the caller chose. A driver that found a free
port, closed the probe, and then bound the same number would be assuming the
number stayed free, and on a busy machine it does not. Nothing here guesses a
port.

**State lives in a file the caller names, not in the process.** Each instance
gets its own state path, so two instances on two ports cannot read each other's
fixtures, and cleaning up after one run cannot disturb a concurrent one. The
`two-instances-do-not-share-state` scenario asserts this by writing to one
instance and reading from both.

## Run the check

```console
$ vkit check run --project . --check items-api
run 4f2a...  check items-api
  PASS get-on-an-empty-service-returns-an-empty-list: GET /items on a service that has never been written to returned {items: []}
  PASS post-then-get-returns-the-updated-list: POST /items {name: lamp} then GET /items returned exactly [{name: lamp}]
  PASS two-posts-accumulate-in-order: after adding chair the list was [lamp, chair], in the order posted
  PASS a-post-without-a-name-is-rejected: POST /items {name: ""} returned 400 and left the list at [lamp, chair]
  PASS an-unknown-route-is-a-404: GET /nothing returned 404
  PASS two-instances-do-not-share-state: instance two on port 62319 listed only [rug] while instance one on port 62317 still listed [lamp, chair]
PASS
```

The two port numbers in the last observation are the ones the operating system
assigned. They differ on every run and the check does not care what they are.

## What the feature map says is not covered

`vkit features` reports two features with no check, and both are there on
purpose:

- `items-update-an-existing-item`. The service has no update route. The feature
  is recorded so its absence is visible rather than assumed.
- `items-concurrent-writes`. The state file is read and rewritten per request,
  so two overlapping writes can lose one. No check exercises this. It is a real
  limitation of the example, written down so nobody reads the passing check as
  evidence that the service is concurrency-safe.
