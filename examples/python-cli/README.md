# totals, a real example application

A CLI that totals integer item amounts. It exists to be an honest subject for
`vkit`: it has a real command line and a real arithmetic core, so a FAIL means
the product is genuinely wrong.

## Behavior

`totals.py` reads integer amounts as arguments and prints their sum. With no
arguments it prints `0`, which is the empty cart.

```console
$ python src/totals.py 10 20 12
42
$ python src/totals.py 50 -20 12
42
$ python src/totals.py
0
```

## How it is verified

`verify_totals.py` runs the real CLI once per scenario and compares what was
printed against a literal expected total held in the driver. The driver has no
flag that tells it to report a pass or a fail, so it cannot agree with the
application by construction. The only way a scenario passes is if the process
printed the expected number.

Scenarios cover an empty cart, a single positive, several positives, mixed
signs, negatives only, and a combination that cancels to zero. The driver writes
its findings as a versioned check artifact.

## Reproducing a failure

Break the arithmetic on purpose in `src/totals.py` by changing the accumulator
line from `running += amount` to `running += amount + 1`. Re-run the check and
every scenario prints one more than expected, so the driver reports FAIL with an
observation naming the expected and actual totals. Restore the line and it
reports PASS again.

This is the countercheck Plan 01 requires: the harness detects a real defect
rather than asserting one.
