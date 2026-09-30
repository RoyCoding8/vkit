"""Prove the POSIX run does not require pywin32, by importing everything.

pyproject.toml declares pywin32 under `sys_platform == 'win32'`, so a POSIX
install does not have it. Every module in src/vkit must still import there, or a
POSIX user cannot even read a run report.

This imports every module in the package with pywin32 made unimportable, so a
module-level import that escaped its platform guard fails here rather than at a
user's first command. A guarded import stays inside its `if` and never fires.

Run:  bash scripts/posix-run.sh scripts/check_posix_imports.py
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# pywin32's distribution name, and the top-level names it installs. Blocking them
# before anything is imported is what makes this a real check: without the block
# a guarded import would still pass while a missing pywin32 went unnoticed.
BLOCKED = {"pywintypes", "win32api", "win32con", "win32event", "win32file",
           "win32job", "win32process", "win32service", "win32evtlog", "win32api"}


class Blocker:
    """A meta path finder that refuses the Windows-only modules."""

    def find_module(self, fullname, path=None):  # legacy API, still consulted
        return self.find_spec(fullname, path)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError(
                f"{fullname} is blocked: this run is proving the POSIX path does "
                f"not need pywin32"
            )
        return None


sys.meta_path.insert(0, Blocker())
for name in list(sys.modules):
    if name.split(".")[0] in BLOCKED:
        del sys.modules[name]


def main() -> int:
    import vkit

    print(f"python:   {sys.version.split()[0]}")
    print(f"platform: {sys.platform}")
    print(f"vkit:     {vkit.__file__}")
    print(f"blocked:  {', '.join(sorted(BLOCKED))}")
    print()

    modules = [m.name for m in pkgutil.walk_packages(vkit.__path__, prefix="vkit.")]
    failed: list[tuple[str, str]] = []
    for name in ["vkit", *sorted(modules)]:
        try:
            importlib.import_module(name)
            print(f"  ok      {name}")
        except BaseException as exc:
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  FAILED  {name}: {type(exc).__name__}: {exc}")

    print()
    if failed:
        print(f"{len(failed)} module(s) cannot be imported on POSIX without pywin32:")
        for name, why in failed:
            print(f"  {name}: {why}")
        return 1
    print(f"Every one of {len(modules) + 1} modules imports on POSIX with pywin32 blocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
