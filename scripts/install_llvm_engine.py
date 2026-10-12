#!/usr/bin/env python3
"""Build the pinned Alive2 and LLVM IR validator under an explicit Linux root."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ALIVE2_COMMIT = "c0f5434f402ad91714ee0952f686cd0f524920ad"
ALIVE2_VERSION = "v20.0"
LLVM_VERSION = "20.1.8"
LLVM_CONFIG = "llvm-config-20"
MAX_BUILD_OUTPUT_BYTES = 16 * 1024 * 1024
BUILD_TIMEOUT_SECONDS = 20 * 60


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path,
                        help="dedicated directory for the source, build, and installed binaries")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        return _fail("Alive2 v20.0 install is supported only on Linux")
    root = args.root.expanduser().absolute()
    if root.exists() and root.is_symlink():
        return _fail("--root must not be a symlink")
    try:
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
        return _install(root)
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail(str(exc))


def _install(root: Path) -> int:
    llvm_config = shutil.which(LLVM_CONFIG)
    cmake = shutil.which("cmake")
    ninja = shutil.which("ninja")
    compiler = shutil.which("g++")
    git = shutil.which("git")
    if not all((llvm_config, cmake, ninja, compiler, git)):
        return _fail("install llvm-20-dev, cmake, ninja-build, g++, and git before running this installer")
    llvm_version = _command([llvm_config, "--version"])
    if llvm_version != LLVM_VERSION:
        return _fail(f"expected {LLVM_CONFIG} {LLVM_VERSION}, got {llvm_version!r}")
    llvm_dir = Path(_command([llvm_config, "--cmakedir"]))
    lib_dir = Path(_command([llvm_config, "--libdir"]))
    include_dir = Path(f"/usr/include/llvm-{LLVM_VERSION.split('.')[0]}")
    c_include_dir = Path(f"/usr/include/llvm-c-{LLVM_VERSION.split('.')[0]}")
    if not all(path.is_dir() for path in (llvm_dir, lib_dir, include_dir, c_include_dir)):
        return _fail("LLVM 20 CMake files, C++ headers, C headers, or library directory are missing")

    source = root / "alive2-source"
    if source.is_symlink():
        return _fail("the Alive2 source directory must not be a symlink")
    if not source.exists():
        _logged([git, "clone", "--branch", ALIVE2_VERSION, "--depth", "1",
                 "https://github.com/AliveToolkit/alive2.git", str(source)], root, "clone.log")
    commit = _command([git, "-C", str(source), "rev-parse", "HEAD"])
    if commit != ALIVE2_COMMIT:
        return _fail(f"expected Alive2 commit {ALIVE2_COMMIT}, got {commit!r}")
    if _command([git, "-C", str(source), "status", "--porcelain", "--untracked-files=all"]):
        return _fail("the pinned Alive2 source checkout has local changes")
    if not (source / "CMakeLists.txt").is_file():
        return _fail("the pinned Alive2 source checkout did not contain CMakeLists.txt")

    build = root / "alive2-build"
    if build.is_symlink():
        return _fail("the Alive2 build directory must not be a symlink")
    build.mkdir(exist_ok=True)
    _logged([cmake, "-S", str(source), "-B", str(build), "-G", "Ninja",
             "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_TV=1", f"-DLLVM_DIR={llvm_dir}"], root, "cmake.log")
    _logged([cmake, "--build", str(build), "--target", "alive-tv", "--parallel", "4"], root, "build.log")
    alive_tv = build / "alive-tv"
    if not alive_tv.is_file():
        return _fail("the pinned Alive2 build did not produce alive-tv")

    binaries = root / "bin"
    if binaries.is_symlink():
        return _fail("the engine binary directory must not be a symlink")
    binaries.mkdir(exist_ok=True)
    installed_alive = binaries / "alive-tv"
    installed_validator = binaries / "llvm-ir-validator"
    if installed_alive.is_symlink() or installed_validator.is_symlink():
        return _fail("engine binary destinations must not be symlinks")
    shutil.copy2(alive_tv, installed_alive)
    validator_source = Path(__file__).resolve().parents[1] / "tools" / "llvm-refinement" / "validate.cpp"
    if not validator_source.is_file():
        return _fail("tools/llvm-refinement/validate.cpp is missing")
    _logged([compiler, f"-I{include_dir}", f"-I{c_include_dir}", "-std=c++17", "-O2",
             "-fno-exceptions", "-funwind-tables", "-D_GNU_SOURCE", "-D__STDC_CONSTANT_MACROS",
             "-D__STDC_FORMAT_MACROS", "-D__STDC_LIMIT_MACROS", str(validator_source),
             f"-L{lib_dir}", "-lLLVM-20", "-o", str(installed_validator)], root, "validator.log")
    alive_sha = _sha256(installed_alive)
    validator_sha = _sha256(installed_validator)
    manifest = {
        "alive2_version": ALIVE2_VERSION,
        "alive2_commit": ALIVE2_COMMIT,
        "llvm_version": llvm_version,
        "alive2_binary": str(installed_alive),
        "alive2_binary_sha256": alive_sha,
        "validator_binary": str(installed_validator),
        "validator_binary_sha256": validator_sha,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True))
    return 0


def _command(argv: list[str]) -> str:
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, timeout=20)
    if result.returncode != 0 or result.stderr:
        raise ValueError(f"version probe failed for {Path(argv[0]).name}")
    return result.stdout.decode("utf-8", errors="strict").strip()


def _logged(argv: list[str], root: Path, name: str) -> None:
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from vkit import proc

    stdout_path, stderr_path = root / f"{name}.out", root / f"{name}.err"
    result = proc.run(argv, cwd=root, stdout_path=stdout_path, stderr_path=stderr_path,
                      timeout_seconds=BUILD_TIMEOUT_SECONDS, max_output_bytes=MAX_BUILD_OUTPUT_BYTES)
    if result.launch_error:
        raise ValueError(f"{Path(argv[0]).name} could not start: {result.launch_error}")
    if result.timed_out:
        raise ValueError(f"{Path(argv[0]).name} exceeded {BUILD_TIMEOUT_SECONDS} seconds")
    if result.output_limited:
        raise ValueError(f"{Path(argv[0]).name} exceeded {MAX_BUILD_OUTPUT_BYTES} output bytes")
    if result.code != 0:
        tail = _log_tail(stdout_path) + _log_tail(stderr_path)
        raise ValueError(f"{Path(argv[0]).name} failed; see {stdout_path} and {stderr_path}\n{tail}")


def _log_tail(path: Path) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as output:
            output.seek(max(0, size - 2_000))
            return output.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
