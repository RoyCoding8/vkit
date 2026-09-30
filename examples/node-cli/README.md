# split-bill, a real example application

A CLI that splits a bill owed in cents between people, splitting it by weight and
adding any tip to the pot before dividing. It exists to be an honest subject for
`vkit`. The shares it prints always sum to the amount owed, so a defect here is a
money bug and not a formatting difference.

This is the Node half of the example pair. It is deliberately a different shape
from `examples/python-cli`, which totals integer amounts. This one is a
command with an option, a variable number of people, weighted division with a
remainder, and a rejection path. A feature map that only fits the Python example
would not really be language independent.

## Behavior

`split-bill split <total-cents> <name>=<weight> [...] [--tip <cents>]`

The total and the tip are in whole cents. The tip joins the pot before the split,
so the shares always add up to the total plus the tip. Each person gets an
integer weight. A person's exact share is the pot times their weight divided by
the sum of the weights, and the leftover from flooring is handed back one cent
at a time, largest fractional part first. Ties keep the order the people were
given in, so an even split is decided by the command line rather than by sort
implementation detail.

```console
$ node src/split-bill.js split 1000 ann=1 bob=1
total 10.00
tip 0.00
owed 10.00
ann 5.00
bob 5.00

$ node src/split-bill.js split 1000 ann=1 bob=1 --tip 333
total 10.00
tip 3.33
owed 13.33
ann 6.67
bob 6.66
```

`6.67 + 6.66` is `13.33`, which is the amount owed. A single cent of `1333` over
two people cannot divide evenly, so it goes to the first person.

A weight of zero or less, a negative total, a missing `name=weight`, or an
unknown option makes the CLI print the reason to stderr and exit `2`.

## How it is verified

`verify-split.js` runs the real CLI once per scenario, as a subprocess, and
compares what was printed against a literal expected string held in the driver.
The driver has no flag that reports a pass or a fail, so it cannot agree with the
application by construction. The only way a scenario passes is if the process
printed exactly the expected split. The driver writes its findings to a versioned
check artifact, atomically, so a reader never sees a half-written file.

The eight scenarios cover an even two way split, an even weighted three way
split, a single person, a zero total, a one cent pot, a tip that creates a
leftover cent, a three way split with a two cent leftover, and a four way
weighted split with both a tip and a leftover.

## Reproducing a failure

In `src/split-bill.js`, change the leftover loop from `handed < leftover` to
`handed < leftover - 1`. That drops the final leftover cent. Re-run the driver.
The three way leftover scenario now prints `4.28 4.28 1.42`, which sums to
`9.98` against an owed `9.99`, a cent that vanished. The driver reports FAIL for
the four scenarios that have a leftover and still PASS for the four that do not,
with an observation naming the expected and the printed split. Restore the loop
and it reports PASS again.

This is the countercheck the plan requires. The harness detects a real defect
rather than asserting one, and a driver where everything failed would prove
nothing, so the four remainder-free scenarios stay green under the defect.

## Running it through vkit

Enroll this directory in a Git repository, then run the check by id. The
manifest is at `verification/manifest.json`, and the check is
`split-bill-behavior`.
