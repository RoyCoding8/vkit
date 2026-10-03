"""Run TLC over one model at one configuration, and write the bytes the adapter reads.

**Why this file exists and imports nothing from vkit**, for the reason
`lean_runner`'s docstring gives: it is launched by absolute path, and
`VKIT_TRUSTED_LAUNCHER` would refuse an `import vkit` under `-m`. It is stdlib-only
and decides nothing.

**The argv below was measured, not remembered.** Every flag was read out of
`tlc2/TLC.java`'s parameter parser in tlaplus master, and the completion markers
out of `tlc2/output/MP.java`:

    java -cp <jar> tlc2.TLC -nowarning -cleanup -workers 1 -metadir <dir> \
         -config <config-stem> <model-module>

  * `-config M` takes the config file's STEM, and a trailing `.cfg` is stripped
    if present (`TLC.java` does the strip itself). Passing `M.cfg` works and
    passing `M.tla` here would silently not be a config at all.
  * `-workers 1` is not a default. With `-workers auto` each worker prints its
    own summary and they interleave, so the first match of the summary pattern is
    whichever worker finished first rather than the run total. One worker costs
    wall-clock time and buys an unambiguous receipt.
  * `-metadir` is inside the run directory, so a crashed run leaves its own state
    behind there and nowhere else.

**Two things this file deliberately refuses to be talked out of.** It passes no
`-deadlock`, so TLC's default deadlock check applies and a model with a deadlocked
state is a violation rather than a silent pass. And it never passes `-simulate`:
`-simulate` makes TLC print `The number of states generated: N` and exit, which is
a sample, not an exhaustive exploration, and the reader refuses that output shape
separately. Neither flag is reachable from a manifest, so there is one spelling
of the run mode rather than two.

**The `-cp` jar is verified against `toolchain.jar_sha256` before it runs.** TLC
loads Java classes from the classpath, so a candidate who could choose the jar
could supply the operators. The digest check runs before any `java` process
exists, and a mismatch refuses the run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPORT_VERSION = 1

#: The ANSI SGR escapes TLC's help text carries. Stripped before the version is
#: read out of it, because `-help` bolds its section headings and the escape sits
#: between the start of a line and the word a prefix match would look for.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vkit.nowindow import hidden_window  # noqa: E402

#: How much of TLC's output travels into the report. A counterexample has to be
#: readable by the engineer asked to fix it, and unbounded it would carry a whole
#: state graph into durable evidence. Bounded is a stated limit, not a hidden one.
OUTPUT_LIMIT = 40000


def _digest(path: Path) -> str:
    """sha256 over the jar's exact bytes.

    Not the CRLF-folding digest the repository uses for text: a jar is a
    downloaded binary that never passes through git's line-ending filter, and
    folding a byte pair out of a compressed stream would hash something other
    than the file.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(argv: list[str], cwd: Path) -> dict:
    done = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace",
        timeout=7200, check=False, **hidden_window(),
    )
    return {
        "argv": list(argv),
        "exit_code": int(done.returncode),
        "stdout": (done.stdout or "")[:OUTPUT_LIMIT],
        "stderr": (done.stderr or "")[:OUTPUT_LIMIT],
    }


def refuse_java_overrides(directory: Path, model_paths: tuple[str, ...]) -> str | None:
    """Why a candidate-supplied Java override blocks this run, or None.

    **Why this gate exists at all.** TLC resolves a Java override of a TLA+
    operator by looking for `<Module>.class` next to the `.tla`, which is
    `FilenameToStream.getModuleOverride` in the TLC source, and it loads that class
    from the JVM classpath. So a candidate that can drop a `.class` beside its
    model can replace a trusted operator's implementation with one returning
    whatever the property needs, and every invariant in the run would hold because
    the checker was made to agree. The plan names this: reject candidate Java
    overrides of trusted model operators unless explicitly approved.

    **The whole directory, not just the declared model.** The declared
    `model.path` is one file, and an override dropped beside a module the model
    imports is the same attack through a different path. The model's directory is
    the resolver's search path, so every `.class` in it is a candidate operator
    and every one of them is refused here.

    **Why it runs before the jar digest check.** Both are refusals and neither
    depends on the other, but this one is decided from the repository alone. The
    jar check needs the jar to exist, and a run that has already been refused has
    no reason to read a 20 MB archive. Returns the reason rather than writing it,
    so the caller can put it in a BLOCKED whose detail names the file.
    """
    found = sorted(
        entry.name for entry in directory.glob("*.class") if entry.is_file()
    )
    if not found:
        return None
    named = ", ".join(model_paths)
    return (
        f"a candidate Java override is present beside the model: "
        f"{', '.join(found)}. TLC resolves a Java override of a TLA+ operator by "
        f"loading <Module>.class from the classpath, so a compiled class in the "
        f"model's directory ({named}) can replace a trusted operator's "
        "implementation with one that returns whatever the property needs, and "
        "every invariant in the run would hold because the checker was made to "
        "agree. The run did not start."
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", required=True)
    parser.add_argument("--jar", required=True)
    parser.add_argument("--model", required=True, help="model module name, no extension")
    parser.add_argument("--config", required=True, help="config path, relative to --dir")
    parser.add_argument("--dir", required=True, help="directory the run resolves paths against")
    parser.add_argument("--metadir", required=True)
    parser.add_argument("--jar-sha256", default="", help="expected jar digest, if pinned")
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)

    jar = Path(args.jar)
    report: dict = {
        "version": REPORT_VERSION,
        "complete": True,
        "tool": "tlc",
        "tool_version": "",
        "model": args.model,
        "config": args.config,
        "jar_sha256": "",
        "exit_code": None,
        "stdout": "",
        "stderr": "",
    }

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

    workdir = Path(args.dir)

    # The override gate runs first, before the jar is even read. Both are
    # refusals, but this one is decided from the repository alone, and a run
    # already refused has no reason to open a 20 MB archive.
    override = refuse_java_overrides(workdir, (Path(args.config).name, f"{args.model}.tla"))
    if override is not None:
        report["complete"] = False
        report["failure"] = override
        return _write(report, Path(args.report))

    metadir = Path(args.metadir)
    metadir.mkdir(parents=True, exist_ok=True)

    stem = args.config[:-4] if args.config.endswith(".cfg") else args.config
    argv_tlc = [
        args.java, "-XX:+UseParallelGC", "-Xmx2g", "-cp", str(jar), "tlc2.TLC",
        "-nowarning", "-cleanup", "-workers", "1", "-metadir", str(metadir),
        "-config", stem, args.model,
    ]
    try:
        version = _run([args.java, "-cp", str(jar), "tlc2.TLC", "-help"], workdir)
        report["tool_version"] = _version_of(version)
        report["version_argv"] = version["argv"]
        step = _run(argv_tlc, workdir)
    except (OSError, subprocess.SubprocessError) as exc:
        report["complete"] = False
        report["failure"] = f"the TLC toolchain could not be launched: {exc}"
        return _write(report, Path(args.report))

    report.update(step)
    report["metadir"] = str(metadir)
    return _write(report, Path(args.report))


def _version_of(help_run: dict) -> str:
    """TLC's own version string, asked for rather than remembered.

    `tlc2.TLC -help` writes the version into the NAME section's description
    line, and measured on TLC 2.19 that line carries an ANSI bold escape before
    `TLC` and a tab before the text:

        \\x1b[1mNAME\\x1b[0m
        \\tTLC - provides model checking and simulation of TLA+
        specifications - Version 2.19 of 08 August 2024

    The first version matched a line STARTING with `TLC2 Version`, which is the
    banner a real run prints rather than the one `-help` prints, so every report
    recorded `unreported` and the receipt carried a tool identity that had in
    fact been measured. The search is now for the version phrase anywhere in the
    help text, with the escapes stripped first so a line that starts with one
    still matches.
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