"""Runs TLC once over one model at one configuration and writes the JSON report `tlc_adapter`
reads; it decides nothing. It is launched by file path (`python tlc_runner.py`) and puts the
source root on `sys.path` for `vkit.nowindow`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPORT_VERSION = 2

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vkit.nowindow import hidden_window  # noqa: E402

OUTPUT_LIMIT = 40000
UNTRUSTED_ENVIRONMENT = (
    "TLA_LIBRARY", "CLASSPATH", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
)


def _digest(path: Path) -> str:
    """sha256 over the jar's exact bytes; no CRLF folding, since a jar is binary."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(argv: list[str], cwd: Path) -> dict:
    done = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace",
        timeout=7200, check=False, env=_java_environment(), **hidden_window(),
    )
    return {
        "argv": list(argv),
        "exit_code": int(done.returncode),
        "stdout": (done.stdout or "")[:OUTPUT_LIMIT],
        "stderr": (done.stderr or "")[:OUTPUT_LIMIT],
    }


def refuse_java_overrides(directory: Path, model_paths: tuple[str, ...]) -> str | None:
    """Why a candidate-supplied Java override blocks this run, or None.

    TLC loads `<Module>.class` beside a `.tla` from the JVM classpath, so a candidate `.class`,
    a `classpath.txt`, or a symlink leaving the project could replace a trusted operator. The
    whole project tree is scanned.
    """
    found = sorted(
        entry.relative_to(directory).as_posix()
        for entry in directory.rglob("*")
        if entry.is_file() and entry.suffix.lower() == ".class"
    )
    classpaths = sorted(
        entry.relative_to(directory).as_posix()
        for entry in directory.rglob("classpath.txt") if entry.is_file()
    )
    external_links = []
    root = directory.resolve()
    for entry in directory.rglob("*"):
        if not entry.is_symlink():
            continue
        try:
            entry.resolve().relative_to(root)
        except (OSError, RuntimeError, ValueError):
            external_links.append(entry.relative_to(directory).as_posix())
    external_links.sort()
    if not found and not classpaths and not external_links:
        return None
    detail = []
    if found:
        detail.append(
            "candidate Java override classes are present in the project tree: "
            f"{', '.join(found)}"
        )
    if classpaths:
        detail.append(
            "candidate classpath.txt files can add unpinned module or Java "
            f"override paths: {', '.join(classpaths)}"
        )
    if external_links:
        detail.append(
            "symlinks can resolve model dependencies outside the admitted project: "
            f"{', '.join(external_links)}"
        )
    return (
        "java_override_unsupported: " + "; ".join(detail) + ". TLC's module "
        "loader could let these replace a trusted operator or resolve a "
        "dependency outside the admitted project. Declared model paths: "
        f"{', '.join(model_paths)}. The run did not start."
    )


def _java_environment() -> dict[str, str]:
    """A child environment without external TLA or JVM configuration."""
    environment = dict(os.environ)
    for name in UNTRUSTED_ENVIRONMENT:
        environment.pop(name, None)
    return environment


def _environment_problem() -> str | None:
    present = sorted(
        name for name in UNTRUSTED_ENVIRONMENT
        if os.environ.get(name, "").strip()
    )
    if not present:
        return None
    return (
        "environment_unsupported: external TLC/JVM configuration is set in "
        f"{', '.join(present)}; the runner cannot bind those dependencies"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", required=True)
    parser.add_argument("--jar", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--model", required=True, help="model module name, no extension")
    parser.add_argument(
        "--model-path", required=True, help="model source path, relative to --root",
    )
    parser.add_argument("--config", required=True, help="config path, relative to --root")
    parser.add_argument(
        "--root", required=True, help="project directory containing declared inputs",
    )
    parser.add_argument("--constants-from-config", choices=("true", "false"), required=True)
    parser.add_argument("--checksum-states", choices=("true", "false"), required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--metadir", required=True)
    parser.add_argument("--jar-sha256", default="", help="expected jar digest, if pinned")
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)

    report: dict = {
        "version": REPORT_VERSION,
        "complete": True,
        "tool": "tlc",
        "tool_version": "",
        "expected_version": args.expected_version,
        "model": args.model,
        "model_path": args.model_path,
        "config": args.config,
        "fingerprint": {
            "constants_from_config": args.constants_from_config == "true",
            "checksum_states": args.checksum_states == "true",
            "workers": args.workers,
        },
        "jar_sha256": "",
        "exit_code": None,
        "stdout": "",
        "stderr": "",
    }

    if (args.constants_from_config, args.checksum_states, args.workers) != (
        "true", "false", 1,
    ):
        report["complete"] = False
        report["failure"] = (
            "fingerprint_unsupported: this runner supports only constants from "
            "the declared config, no state checksums, and one worker"
        )
        return _write(report, Path(args.report))
    if not args.expected_version.strip():
        report["complete"] = False
        report["failure"] = (
            "toolchain_unpinned: the check declares no TLC version"
        )
        return _write(report, Path(args.report))
    if not args.jar_sha256:
        report["complete"] = False
        report["failure"] = (
            "jar_unpinned: the check declares no tla2tools jar digest"
        )
        return _write(report, Path(args.report))
    environment_problem = _environment_problem()
    if environment_problem is not None:
        report["complete"] = False
        report["failure"] = environment_problem
        return _write(report, Path(args.report))

    root = Path(args.root).resolve()
    model_source = (root / args.model_path).resolve()
    config_source = (root / args.config).resolve()
    if not _within(root, model_source) or not _within(root, config_source):
        report["complete"] = False
        report["failure"] = "the declared model or config path escapes the project root"
        return _write(report, Path(args.report))
    if model_source.name != f"{args.model}.tla":
        report["complete"] = False
        report["failure"] = (
            f"the declared model path {args.model_path!r} does not name "
            f"{args.model}.tla"
        )
        return _write(report, Path(args.report))
    if not model_source.is_file() or not config_source.is_file():
        report["complete"] = False
        report["failure"] = "the declared model or config file does not exist"
        return _write(report, Path(args.report))

    workdir = model_source.parent
    override = refuse_java_overrides(
        root, (config_source.name, f"{args.model}.tla")
    )
    if override is not None:
        report["complete"] = False
        report["failure"] = override
        return _write(report, Path(args.report))

    jar = Path(args.jar)

    if not jar.is_file():
        report["complete"] = False
        report["failure"] = f"the pinned tla2tools jar {jar} does not exist"
        return _write(report, Path(args.report))
    report["jar_sha256"] = _digest(jar)
    if args.jar_sha256 and args.jar_sha256 != report["jar_sha256"]:
        report["complete"] = False
        report["failure"] = (
            f"the tla2tools jar measures {report['jar_sha256']} and the check pinned "
            f"{args.jar_sha256}. TLC loads its Java operators from this jar, so a "
            "jar vkit cannot name is a jar vkit cannot audit. The run did not start."
        )
        return _write(report, Path(args.report))

    metadir = Path(args.metadir)
    metadir.mkdir(parents=True, exist_ok=True)

    stem = (
        str(config_source.with_suffix("")).strip()
        if config_source.suffix == ".cfg" else str(config_source)
    )
    argv_tlc = [
        args.java, "-XX:+UseParallelGC", "-Xmx2g", "-cp", str(jar), "tlc2.TLC",
        "-nowarning", "-cleanup", "-workers", str(args.workers), "-metadir", str(metadir),
        "-config", stem, args.model,
    ]
    try:
        version = _run([args.java, "-cp", str(jar), "tlc2.TLC", "-help"], workdir)
        report["tool_version"] = _version_of(version)
        report["version_argv"] = version["argv"]
        actual_version = _numeric_version(report["tool_version"])
        if actual_version != args.expected_version:
            report["complete"] = False
            report["failure"] = (
                f"tool_version_mismatch: the check pins TLC {args.expected_version}, "
                f"but the jar reports {actual_version or 'no readable version'}"
            )
            return _write(report, Path(args.report))
        step = _run(argv_tlc, workdir)
    except (OSError, subprocess.SubprocessError) as exc:
        report["complete"] = False
        report["failure"] = f"the TLC toolchain could not be launched: {exc}"
        return _write(report, Path(args.report))

    report.update(step)
    report["metadir"] = str(metadir)
    return _write(report, Path(args.report))


def _numeric_version(text: str) -> str | None:
    match = re.search(r"\bVersion\s+([0-9]+\.[0-9]+)\b", text, re.IGNORECASE)
    if not match:
        match = re.search(r"\bTLC\s+([0-9]+\.[0-9]+)\b", text, re.IGNORECASE)
    return match.group(1) if match else None


def _within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _version_of(help_run: dict) -> str:
    """TLC's version string from `tlc2.TLC -help`.

    Matches the NAME line's "Version X of DATE" after stripping ANSI escapes, falling back to a
    `TLC2 Version` banner line.
    """
    blob = (help_run.get("stdout") or "") + (help_run.get("stderr") or "")
    plain = _ANSI.sub("", blob)
    match = re.search(
        r"TLC - provides model checking and simulation of TLA\+ specifications"
        r" - Version ([^\r\n]+)",
        plain,
    )
    if match:
        return f"TLC {match.group(1).strip()}"
    for line in plain.splitlines():
        stripped = line.strip()
        if stripped.startswith("TLC2 Version"):
            return stripped
    return "unreported"


def _write(report: dict, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(target.suffix + ".partial")
    staged.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    staged.replace(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
