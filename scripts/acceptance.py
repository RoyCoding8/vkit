"""Walk the Plan 01 acceptance table against the real command.

This is the artifact a reviewer reruns instead of trusting a narrative. Every row
runs `vkit` as a subprocess against a throwaway repository, and every assertion is
a literal expected value observed from that process.

## The command under test is this checkout's

The rows are evidence about the tree they live in, so the command has to come
from that tree. `Path(sys.executable).parent / "vkit.exe"` is not it: that is the
virtualenv, and a virtualenv shared with another worker holds an editable
install pointing at somebody else's `src`. Every row would then pass against code
this revision does not contain, and a green run would certify the wrong tree.

So the command is resolved from this tree's own sources, and `main` refuses to
start unless the process it is about to spawn will import exactly `ROOT/src`.
Refusing is the only honest answer: a script that is not standing beside its own
sources has not tested this tree, and printing `22/22` for it would be the false
pass this script exists to prevent.

Run:  .venv/Scripts/python.exe scripts/acceptance.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
EXAMPLE = ROOT / "examples" / "python-cli"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def _this_checkout() -> dict[str, str]:
    """The environment a child must see to import this tree's `vkit` and nothing else.

    Only `PYTHONPATH` is load-bearing here. It is placed ahead of the site-packages
    entries the interpreter adds at startup, which is what the editable install is:
    a `.pth` line naming another checkout's `src`. Naming the interpreter's own
    directory would be no better, because the console script beside it is a
    launcher for that same other checkout.
    """
    return {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONNOUSERSITE": "1"}


#: The exact argv every row runs. `[python, "-m", "vkit.cli"]` rather than the
#: console script, because the console script is a launcher whose shebang names
#: whichever `vkit` is installed next to it. `-m` names this tree's module and
#: cannot be diverted by PATH, VIRTUAL_ENV, or an installed entry point.
COMMAND = [sys.executable, "-m", "vkit.cli"]

CHILD_ENV = _this_checkout()


def _tree_under_test() -> str | None:
    """The `vkit` a row would import, or None if this environment cannot say.

    Asked of a real child process rather than of this one, because a child is
    what the rows actually run and the two can disagree about a shared
    environment.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import vkit.cli; print(vkit.cli.__file__)"],
        capture_output=True, text=True, timeout=120, env=CHILD_ENV,
    )
    return probe.stdout.strip() or None


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*COMMAND, *args], capture_output=True, text=True, timeout=300, cwd=cwd,
        env=CHILD_ENV,
    )


def record(row: str, verdict: str, note: str) -> None:
    results.append((row, verdict, note))
    print(f"[{verdict}] {row}\n       {note}")


def check(row: str, condition: bool, note: str) -> None:
    record(row, PASS if condition else FAIL, note)


def make_repo(name: str = "späce repo") -> Path:
    base = Path(tempfile.mkdtemp()) / name
    shutil.copytree(EXAMPLE, base)
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    subprocess.run(["git", "add", "-A"], cwd=base, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=base, check=True,
    )
    return base


def write_manifest(repo: Path, check_body: dict) -> None:
    (repo / "verification" / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "checks": [check_body]}), encoding="utf-8"
    )


def row_installed_package_drives_example() -> None:
    repo = make_repo()
    done = run("check", "run", "--project", str(repo), "--check", "totals-behavior", "--json")
    payload = json.loads(done.stdout or "{}")
    scenarios = {s["id"] for s in payload.get("outcome", {}).get("scenarios", [])}
    expected = {"empty-cart", "single-positive", "several-positives",
                "mixed-sign", "negatives-only", "cancels-to-zero"}
    check(
        "This checkout drives the example",
        done.returncode == 0 and payload.get("outcome", {}).get("result") == "PASS" and scenarios == expected,
        f"rc={done.returncode} scenarios={len(scenarios)}/6",
    )
    shown = run("run", "show", "--project", str(repo), "--run", payload["run_id"], "--json")
    report = json.loads(shown.stdout or "{}")
    # The row's subject is that the report carries REAL provenance: the interpreter
    # that actually ran, and the mechanism that actually contained the process.
    # Both are facts about the host, so the expectations are the host's own
    # values. An earlier version asserted the literal "python" and
    # "windows_job_object", which made this row fail on every POSIX host for a
    # reason that had nothing to do with provenance: on POSIX the argv[0] is the
    # venv's full path and the containment mechanism is a process group.
    argv0 = report.get("command", {}).get("argv", [""])[0]
    ownership = report.get("process", {}).get("ownership")
    expected_ownership = "windows_job_object" if sys.platform == "win32" else "posix_process_group"
    check(
        "  ... with actual command provenance",
        Path(argv0).name.startswith("python")
        and bool(report.get("source", {}).get("head"))
        and ownership == expected_ownership,
        f"argv0={argv0} ownership={ownership}",
    )


def row_introduced_defect() -> None:
    repo = make_repo()
    app = repo / "src" / "totals.py"
    good = app.read_text(encoding="utf-8")
    app.write_text(good.replace("running += amount", "running += amount + 1"), encoding="utf-8")
    done = run("check", "run", "--project", str(repo), "--check", "totals-behavior", "--json")
    payload = json.loads(done.stdout or "{}")
    failing = [s for s in payload.get("outcome", {}).get("scenarios", []) if s["result"] == "FAIL"]
    obs = failing[0]["observation"] if failing else ""
    check(
        "Introduced arithmetic defect",
        done.returncode == 1 and payload.get("outcome", {}).get("result") == "FAIL" and bool(failing),
        f"rc={done.returncode} failing={len(failing)} first={obs!r}",
    )
    app.write_text(good, encoding="utf-8")
    back = run("check", "run", "--project", str(repo), "--check", "totals-behavior", "--json")
    check("  ... and restoring the source restores PASS", back.returncode == 0, f"rc={back.returncode}")


def row_tool_missing() -> None:
    repo = make_repo("missing tool")
    write_manifest(repo, {
        "id": "ghost", "command": ["definitely-not-installed-xyzzy"],
        "timeout_seconds": 10, "required_scenarios": ["s1"], "artifact": "result.json",
        "prerequisites": [{"name": "ghost-tool", "executable": "definitely-not-installed-xyzzy"}],
    })
    done = run("check", "run", "--project", str(repo), "--check", "ghost", "--json")
    payload = json.loads(done.stdout or "{}")
    outcome = payload.get("outcome", {})
    check(
        "Tool missing",
        done.returncode == 3 and outcome.get("reason") == "prerequisite_missing"
        and "ghost-tool" in outcome.get("detail", ""),
        f"rc={done.returncode} reason={outcome.get('reason')} detail={outcome.get('detail', '')[:60]!r}",
    )
    report = json.loads(run("run", "show", "--project", str(repo),
                            "--run", payload.get("run_id", ""), "--json").stdout or "{}")
    check(
        "  ... no subprocess claimed as executed",
        report.get("process") is None and report.get("lifecycle") == "terminal",
        f"process={report.get('process')}",
    )


def row_zero_exit_no_artifact() -> None:
    repo = make_repo("no artifact")
    write_manifest(repo, {
        "id": "silent", "command": [sys.executable, "-c", "pass"],
        "timeout_seconds": 30, "required_scenarios": ["s1"], "artifact": "result.json",
    })
    done = run("check", "run", "--project", str(repo), "--check", "silent", "--json")
    outcome = json.loads(done.stdout or "{}").get("outcome", {})
    check(
        "Command returns zero but omits artifact",
        done.returncode == 3 and outcome.get("result") == "BLOCKED"
        and outcome.get("reason") == "artifact_missing",
        f"rc={done.returncode} reason={outcome.get('reason')}",
    )


def row_malformed_artifact() -> None:
    """Every way a check can fail to produce usable evidence.

    The writer takes a named payload rather than a raw argument, because the
    manifest schema correctly refuses an empty argv element, and passing "" as
    an argument is indistinguishable from that rejection.
    """
    repo = make_repo("malformed")
    (repo / "writer.py").write_text(
        "import pathlib,sys\n"
        "kind=sys.argv[2]\n"
        "out=pathlib.Path(sys.argv[1])\n"
        "bodies={\n"
        "  'empty': '',\n"
        "  'garbage': 'not json at all',\n"
        "  'no-scenarios': '{\"schema_version\":1,\"scenarios\":[]}',\n"
        "  'wrong-version': '{\"schema_version\":2,\"scenarios\":[{\"id\":\"s1\","
        "\"result\":\"PASS\",\"observation\":\"o\"}]}',\n"
        "}\n"
        "out.write_text(bodies[kind])\n", encoding="utf-8")
    expected = {
        "empty": ("artifact_malformed", "artifact_empty"),
        "garbage": ("artifact_malformed",),
        "no-scenarios": ("artifact_empty",),
        "wrong-version": ("artifact_malformed",),
    }
    for kind, reasons in expected.items():
        (repo / "verification" / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "checks": [{
                "id": "bad",
                "command": [sys.executable, "writer.py", "{{run_dir}}/result.json", kind],
                "timeout_seconds": 30, "required_scenarios": ["s1"], "artifact": "result.json",
            }]}), encoding="utf-8")
        done = run("check", "run", "--project", str(repo), "--check", "bad", "--json")
        outcome = json.loads(done.stdout or "{}").get("outcome", {})
        check(
            f"Malformed artifact ({kind})",
            done.returncode == 3 and outcome.get("result") == "BLOCKED"
            and outcome.get("reason") in reasons,
            f"rc={done.returncode} reason={outcome.get('reason')} detail={outcome.get('detail', '')[:56]!r}",
        )


def row_unknown_check_and_schema() -> None:
    repo = make_repo("rejects")
    bad = run("check", "run", "--project", str(repo), "--check", "nope", "--json")
    (repo / "verification" / "manifest.json").write_text(
        json.dumps({"schema_version": 99, "checks": []}), encoding="utf-8")
    schema = run("check", "run", "--project", str(repo), "--check", "totals-behavior", "--json")
    check(
        "Unknown check rejected before launch",
        bad.returncode == 2 and "unknown check" in json.loads(bad.stdout)["error"],
        f"rc={bad.returncode}",
    )
    check(
        "Unsupported schema version rejected before launch",
        schema.returncode == 2 and "schema_version" in json.loads(schema.stdout)["error"],
        f"rc={schema.returncode}",
    )


def row_source_changes() -> None:
    repo = make_repo("mutating")
    # A check that edits the source while it runs, so the post-run digest differs.
    (repo / "mutator.py").write_text(
        "import pathlib,time\n"
        "time.sleep(1.0)\n"
        "p=pathlib.Path('src/totals.py'); p.write_text(p.read_text()+'\\n# touched\\n')\n",
        encoding="utf-8")
    (repo / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "checks": [{
            "id": "mutator",
            "command": [sys.executable, "mutator.py"],
            "timeout_seconds": 60, "required_scenarios": ["s1"], "artifact": "result.json",
        }]}), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "m"],
                   cwd=repo, check=True)
    done = run("check", "run", "--project", str(repo), "--check", "mutator", "--json")
    outcome = json.loads(done.stdout or "{}").get("outcome", {})
    check(
        "Source changes during execution",
        done.returncode == 3 and outcome.get("reason") == "source_changed",
        f"rc={done.returncode} reason={outcome.get('reason')} detail={outcome.get('detail', '')[:60]!r}",
    )


def row_dirty_tree() -> None:
    """The report must describe the tree as it was when the check ran.

    Dirty the tree BEFORE the run. Reading a report back after dirtying the tree
    proves nothing, because the recorded identity is the one from run time.
    """
    repo = make_repo("dirty")
    (repo / "src" / "totals.py").write_text(
        (repo / "src" / "totals.py").read_text(encoding="utf-8") + "\n# uncommitted work\n",
        encoding="utf-8",
    )
    done = run("check", "run", "--project", str(repo), "--check", "totals-behavior", "--json")
    report = json.loads(run("run", "show", "--project", str(repo),
                            "--run", json.loads(done.stdout)["run_id"], "--json").stdout)
    source = report["source"]
    check(
        "Dirty development tree reported honestly",
        source["dirty"] is True and "src/totals.py" in source["dirty_paths"] and bool(source["head"]),
        f"dirty={source['dirty']} paths={source['dirty_paths']} head={source['head'][:12]}",
    )


def row_timeout_descendants() -> None:
    repo = make_repo("timeout")
    (repo / "sleeper.py").write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
        "time.sleep(120)\n", encoding="utf-8")
    (repo / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "checks": [{
            "id": "hangs", "command": [sys.executable, "sleeper.py"],
            "timeout_seconds": 2, "required_scenarios": ["s1"], "artifact": "result.json",
        }]}), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "s"],
                   cwd=repo, check=True)
    done = run("check", "run", "--project", str(repo), "--check", "hangs", "--json")
    outcome = json.loads(done.stdout or "{}").get("outcome", {})
    report = json.loads(run("run", "show", "--project", str(repo),
                            "--run", json.loads(done.stdout)["run_id"], "--json").stdout)
    check(
        "Timeout with child and grandchild",
        done.returncode == 3 and outcome.get("reason") == "timeout"
        and report.get("process", {}).get("timed_out") is True,
        f"rc={done.returncode} reason={outcome.get('reason')} detail={outcome.get('detail', '')[:50]!r}",
    )
    check(
        "  ... readable terminal report retained",
        report.get("lifecycle") == "terminal" and bool(report.get("run_id")),
        f"lifecycle={report.get('lifecycle')}",
    )


def row_large_output() -> None:
    repo = make_repo("chatty")
    (repo / "loud.py").write_text(
        "import sys\nsys.stdout.write('o'*5_000_000)\nsys.stderr.write('e'*5_000_000)\n",
        encoding="utf-8")
    (repo / "verification" / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "checks": [{
            "id": "loud", "command": [sys.executable, "loud.py"],
            "timeout_seconds": 120, "required_scenarios": ["s1"], "artifact": "result.json",
        }]}), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "l"],
                   cwd=repo, check=True)
    done = run("check", "run", "--project", str(repo), "--check", "loud", "--json")
    run_dir = json.loads(done.stdout or "{}").get("report_path", "")
    logs = Path(run_dir).parent if run_dir else None
    sizes = {p.name: p.stat().st_size for p in logs.glob("*.log")} if logs and logs.is_dir() else {}
    check(
        "Large output, no deadlock or unbounded buffering",
        done.returncode == 3 and sizes.get("stdout.log") == 5_000_000
        and sizes.get("stderr.log") == 5_000_000,
        f"rc={done.returncode} log sizes={sizes} (10 MB written, terminated by design)",
    )


def row_evidence_survives_checkout() -> None:
    """Evidence must outlive the worker's checkout.

    A linked git worktree is the real case: its files and its .git file are
    disposable, while the state lives in the main repository's common directory.
    Retiring the worktree must leave the report readable. This is also the
    arrangement that makes two clones of one repository share evidence, which is
    why state is stored under the common dir and not in the working tree.
    """
    base = Path(tempfile.mkdtemp())
    main = base / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=main, check=True)
    shutil.copytree(EXAMPLE / "src", main / "src")
    shutil.copytree(EXAMPLE / "verification", main / "verification")
    shutil.copy(EXAMPLE / "verify_totals.py", main / "verify_totals.py")
    subprocess.run(["git", "add", "-A"], cwd=main, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "e"],
                   cwd=main, check=True)

    worker = base / "worker"
    subprocess.run(["git", "worktree", "add", "-q", str(worker)], cwd=main, check=True)
    done = run("check", "run", "--project", str(worker), "--check", "totals-behavior", "--json")
    payload = json.loads(done.stdout or "{}")
    report_path = Path(payload.get("report_path", ""))

    # Both halves are resolved before comparing, because the product resolves
    # the path it publishes and this script's own side never has. On a runner
    # whose TEMP is an 8.3 short name (`C:\Users\RUNNER~1\...`) the unresolved
    # side keeps the short form while the resolved one spells out the long name,
    # so a string prefix compared them as two different directories. The
    # directory was the same one throughout; only the spelling differed. This is
    # the same reason `paths.py` resolves both of its own paths.
    claimed = (main / ".git").resolve()
    check(
        "State lives in the shared git dir, not the worktree",
        bool(payload) and Path(report_path).resolve().is_relative_to(claimed),
        f"report {report_path.name} lives under {claimed}, not under {worker}",
    )

    subprocess.run(["git", "worktree", "remove", "--force", str(worker)], cwd=main, check=True)
    check(
        "Evidence survives retiring the checkout",
        report_path.is_file() and not worker.exists(),
        f"worktree removed={not worker.exists()} report still present={report_path.is_file()}",
    )
    if report_path.is_file():
        data = json.loads(report_path.read_text(encoding="utf-8"))
        check("  ... and is complete, not truncated",
              data["outcome"]["result"] == "PASS" and data["lifecycle"] == "terminal",
              f"outcome={data['outcome']['result']} lifecycle={data['lifecycle']}")


def row_interrupted_report_write() -> None:
    """A temp file must never be readable as a finished report."""
    repo = make_repo("interrupted")
    done = run("check", "run", "--project", str(repo), "--check", "totals-behavior", "--json")
    run_dir = Path(json.loads(done.stdout)["report_path"]).parent
    leftovers = [p.name for p in run_dir.iterdir() if ".tmp" in p.name]
    check("Interrupted final report write leaves no partial report",
          not leftovers, f"run dir holds {[p.name for p in run_dir.iterdir()]}")


def main() -> int:
    resolved = _tree_under_test()
    expected = ROOT / "src" / "vkit" / "cli.py"
    if resolved is None:
        print(
            f"cannot import vkit at all, so no row can run against {expected}",
            file=sys.stderr,
        )
        return 4
    if Path(resolved).resolve() != expected.resolve():
        print(
            f"these rows would test {resolved}, not {expected}.\n"
            f"This script names its own src tree, so a mismatch means it is not "
            f"standing beside one. Run it from the checkout it belongs to, with "
            f"{SCRIPT.name} directly under that checkout's scripts/.",
            file=sys.stderr,
        )
        return 4
    print(f"command under test: {' '.join(COMMAND)}\n  imports {resolved}\n")
    for row in (
        row_installed_package_drives_example, row_introduced_defect, row_tool_missing,
        row_zero_exit_no_artifact, row_malformed_artifact, row_unknown_check_and_schema,
        row_source_changes, row_dirty_tree, row_timeout_descendants, row_large_output,
        row_evidence_survives_checkout, row_interrupted_report_write,
    ):
        try:
            row()
        except Exception as exc:  # noqa: BLE001 - a broken row is a FAIL, not a crash
            record(row.__name__, FAIL, f"raised {type(exc).__name__}: {exc}")

    passed = sum(1 for _, v, _ in results if v == PASS)
    failed = [r for r in results if r[1] == FAIL]
    print(f"\n{passed}/{len(results)} acceptance rows pass")
    if failed:
        print("failing rows:")
        for name, _, note in failed:
            print(f"  - {name}: {note}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
