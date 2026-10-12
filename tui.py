"""Repository entry point shared by start.bat and start.sh."""
import sys

from vkit.cli import main


if __name__ == "__main__":
    sys.exit(main(["tui", *sys.argv[1:]]))
