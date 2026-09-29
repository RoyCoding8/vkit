"""A real CLI that totals integer item amounts.

Deliberately trivial. Its purpose is to be an honest subject for the verifier:
it has a real command line, real output, and a real arithmetic core that a
defect can genuinely break, so a FAIL from the driver means the product is wrong
rather than the harness disagreeing with itself.
"""
from __future__ import annotations

import argparse
import sys


def total(amounts: list[int]) -> int:
    """Sum the amounts. This is the function a defect will be introduced into."""
    running = 0
    for amount in amounts:
        running += amount
    return running


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="totals", description="Total integer item amounts."
    )
    parser.add_argument(
        "amounts", nargs="*", type=int, default=[],
        help="integer amounts; omitting them totals an empty cart",
    )
    args = parser.parse_args(argv)
    print(total(args.amounts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
