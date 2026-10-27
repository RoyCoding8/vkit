from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHECK_ID = "lifecycle-model"
MODEL_REL = Path("verification/lifecycle/Attempts.tla")
CONFIG_REL = Path("verification/lifecycle/Attempts.cfg")
PROPERTIES = [
    "TypeOK",
    "NoFalsePass",
    "TerminalHasResult",
    "InterruptedHasNoResult",
    "IdentityPinned",
]


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    java_home = Path(env.get("JAVA_HOME", ""))
    java_bin = java_home / "bin"
    if (java_bin / ("java.exe" if os.name == "nt" else "java")).is_file():
        env["PATH"] = str(java_bin) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), *filter(None, env.get("PYTHONPATH", "").split(os.pathsep))]
    )
    os.environ["PYTHONPATH"] = env["PYTHONPATH"]
    os.environ["PATH"] = env["PATH"]
    sys.path.insert(0, str(ROOT / "src"))
    return env


def _hidden_window() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    from vkit.nowindow import hidden_window

    return hidden_window()


def _run(argv: list[str], cwd: Path, env: dict[str, str], *, expected: frozenset[int] = frozenset({0})) -> dict[str, Any]:
    done = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=False,
        timeout=240, **_hidden_window(),
    )
    if done.returncode not in expected:
        raise RuntimeError(
            f"command exited {done.returncode}, expected {sorted(expected)}: {argv!r}\n"
            f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
        )
    try:
        body = json.loads(done.stdout) if done.stdout.strip().startswith("{") else None
    except json.JSONDecodeError:
        body = None
    return {"returncode": done.returncode, "stdout": done.stdout,
            "stderr": done.stderr, "body": body}


def _cli(project: Path, env: dict[str, str], *args: str,
         expected: frozenset[int] = frozenset({0})) -> dict[str, Any]:
    return _run(
        [sys.executable, "-m", "vkit.cli", *args, "--project", str(project), "--json"],
        project, env, expected=expected,
    )


def _manifest(config: str, *, trace: bool = False) -> dict[str, Any]:
    raw = json.loads((ROOT / "verification/manifest.json").read_text(encoding="utf-8"))
    check = next(item for item in raw["checks"] if item["id"] == CHECK_ID)
    check["config"] = config
    check["inputs"] = [MODEL_REL.as_posix(), config]
    check["subject"]["paths"] = [MODEL_REL.as_posix(), config]
    if trace:
        check["properties"] = [*PROPERTIES, "TraceComplete"]
    raw["checks"] = [check]
    return raw


def _new_project(directory: Path, env: dict[str, str], *, events: list[str] | None = None,
                 identity: tuple[str, str] | None = None, wrong_pass: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=False)
    model = (ROOT / MODEL_REL).read_text(encoding="utf-8")
    config = (ROOT / CONFIG_REL).read_text(encoding="utf-8")
    if events is not None:
        encoded = ", ".join(json.dumps(event) for event in events)
        model = re.sub(
            r'ObservedEvents == <<"unused">>',
            f"ObservedEvents == <<{encoded}>>",
            model,
            count=1,
        )
        if 'ObservedEvents == <<"unused">>' in model:
            raise RuntimeError("observed event sequence was not bound into the lifecycle model")
        config = config.replace("SPECIFICATION Spec", "SPECIFICATION TraceSpec")
        config += "\nPROPERTY TraceComplete\n"
    if identity is not None:
        job, source = identity
        config = config.replace('ExpectedJob = "job-v1"', f"ExpectedJob = {json.dumps(job)}")
        config = config.replace('ExpectedSource = "source-v1"', f"ExpectedSource = {json.dumps(source)}")
    if wrong_pass:
        config = config.replace("InjectFalsePass = FALSE", "InjectFalsePass = TRUE")
    model_path = directory / MODEL_REL
    config_path = directory / CONFIG_REL
    model_path.parent.mkdir(parents=True)
    model_path.write_text(model, encoding="utf-8", newline="\n")
    config_path.write_text(config, encoding="utf-8", newline="\n")
    (directory / "verification").mkdir(exist_ok=True)
    (directory / "verification/manifest.json").write_text(
        json.dumps(_manifest(CONFIG_REL.as_posix(), trace=events is not None), indent=2) + "\n",
        encoding="utf-8",
    )
    _run(["git", "init", "-q"], directory, env)
    _run(["git", "-c", "user.name=VKit Lifecycle", "-c", "user.email=lifecycle@vkit.invalid",
          "add", "."], directory, env)
    _run(["git", "-c", "user.name=VKit Lifecycle", "-c", "user.email=lifecycle@vkit.invalid",
          "commit", "-qm", "lifecycle fixture"], directory, env)
    accepted = _cli(directory, env, "accept", "--yes")
    if accepted["returncode"] != 0:
        raise RuntimeError(f"lifecycle fixture could not accept its check: {accepted['stdout']} {accepted['stderr']}")
    return directory


def _run_model_case(directory: Path, env: dict[str, str], expected: str) -> dict[str, Any]:
    result = _cli(directory, env, "check", "run", "--check", CHECK_ID,
                  expected=frozenset({0, 1, 3}))
    body = result["body"]
    if not isinstance(body, dict):
        raise RuntimeError(f"lifecycle checker returned no JSON record: {result['stdout']} {result['stderr']}")
    records = body.get("runs") or ([body] if body.get("run_id") else [])
    record = records[0] if records else body
    actual = (record.get("outcome") or {}).get("result")
    if actual != expected:
        raise RuntimeError(f"lifecycle model result was {actual!r}, expected {expected!r}: {record!r}")
    return record


def _wait_state(store: Any, run_id: str, state: str, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = store.record(run_id)
        if latest and latest["state"] == state:
            return latest
        if latest and latest["state"] in {"done", "interrupted"} and latest["state"] != state:
            break
        time.sleep(0.02)
    raise RuntimeError(f"attempt {run_id} did not reach {state!r}: {latest!r}")


def _stop_worker(run_id: str, store: Any, env: dict[str, str]) -> None:
    from vkit.store import read_json

    worker = read_json(store.run_dir(run_id) / "worker.json")
    if not worker or not isinstance(worker.get("pid"), int):
        raise RuntimeError(f"attempt {run_id} has no observed worker process")
    pid = worker["pid"]
    if os.name == "nt":
        done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                              capture_output=True, text=True, check=False, env=env,
                              **_hidden_window())
        if done.returncode != 0:
            raise RuntimeError(f"could not stop worker {pid}: {done.stdout} {done.stderr}")
    else:
        os.killpg(pid, signal.SIGKILL)


def _runtime_trace(directory: Path, env: dict[str, str]) -> tuple[list[str], tuple[str, str], list[dict[str, Any]]]:
    from vkit.jobs import retry, submit, wait
    from vkit.manifest import parse_manifest
    from vkit.paths import open_project
    from vkit.store import Store

    directory.mkdir(parents=True, exist_ok=False)
    (directory / "verification").mkdir()
    (directory / "slow.py").write_text(
        "import json, sys, time\n"
        "time.sleep(3)\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as output:\n"
        "    json.dump({'schema_version': 1, 'scenarios': [{'id': 'ok', 'result': 'PASS', 'observation': 'ok'}]}, output)\n",
        encoding="utf-8",
    )
    check = {
        "id": "lifecycle-runtime", "kind": "scenario", "cwd": ".",
        "timeout_seconds": 60, "artifact": "result.json",
        "command": ["{{python}}", "slow.py", "{{run_dir}}/result.json"],
        "required_scenarios": ["ok"], "inputs": ["slow.py"],
        "subject": {"paths": ["slow.py"], "digest": None},
        "claim_id": "lifecycle-runtime",
    }
    (directory / "verification/manifest.json").write_text(
        json.dumps({"schema_version": 3, "description": "Lifecycle runtime trace.",
                    "checks": [check]}, indent=2) + "\n",
        encoding="utf-8",
    )
    _run(["git", "init", "-q"], directory, env)
    _run(["git", "-c", "user.name=VKit Lifecycle", "-c", "user.email=lifecycle@vkit.invalid",
          "add", "."], directory, env)
    _run(["git", "-c", "user.name=VKit Lifecycle", "-c", "user.email=lifecycle@vkit.invalid",
          "commit", "-qm", "runtime fixture"], directory, env)
    accepted = _cli(directory, env, "accept", "--yes")
    if accepted["returncode"] != 0:
        raise RuntimeError(f"runtime fixture could not accept its check: {accepted['stdout']} {accepted['stderr']}")

    project = open_project(directory)
    manifest = parse_manifest(project)
    store = Store.of(project)
    events: list[str] = []
    records: list[dict[str, Any]] = []

    first = submit(project, manifest, "lifecycle-runtime", store=store)
    events.append("submit")
    records.append(first)
    _wait_state(store, first["run_id"], "running")
    events.append("start")
    store.request_cancel(first["run_id"])
    events.append("cancel")
    cancelled = wait(store, first["run_id"], 30)
    if cancelled["state"] != "done" or cancelled["outcome"].get("reason") != "cancelled":
        raise RuntimeError(f"the real cancellation path did not publish a cancelled result: {cancelled!r}")
    events.append("complete_blocked")
    records.append(cancelled)

    second = retry(project, first["run_id"], store=store)
    events.append("retry")
    records.append(second)
    _wait_state(store, second["run_id"], "running")
    events.append("start")
    _stop_worker(second["run_id"], store, env)
    events.append("worker_lost")
    interrupted = _wait_state(store, second["run_id"], "interrupted")
    if interrupted.get("outcome") is not None:
        raise RuntimeError(f"a lost worker exposed a terminal outcome: {interrupted!r}")
    events.append("recover")
    records.append(interrupted)
    published = store.evidence_for("registered_check", "lifecycle-runtime", second["job_id"])
    if published is None or published["run_id"] != first["run_id"] or published["result"] != "BLOCKED":
        raise RuntimeError("worker loss replaced the prior cancelled evidence or published new evidence")

    third = retry(project, second["run_id"], store=store)
    events.append("retry")
    records.append(third)
    _wait_state(store, third["run_id"], "running")
    events.append("start")
    passed = wait(store, third["run_id"], 30)
    if passed["state"] != "done" or passed["outcome"]["result"] != "PASS":
        raise RuntimeError(f"the retry did not finish with a recorded pass: {passed!r}")
    events.append("complete_pass")
    records.append(passed)

    cancelled_final = store.record(first["run_id"])
    interrupted_final = store.record(second["run_id"])
    passed_final = store.record(third["run_id"])
    if (cancelled_final["outcome"].get("reason") != "cancelled"
            or interrupted_final["state"] != "interrupted"
            or interrupted_final.get("outcome") is not None
            or passed_final["outcome"].get("result") != "PASS"):
        raise RuntimeError("later retries changed an earlier attempt or exposed a false terminal result")
    published = store.evidence_for("registered_check", "lifecycle-runtime", third["job_id"])
    if published is None or published["run_id"] != third["run_id"] or published["result"] != "PASS":
        raise RuntimeError("the final pass was not bound to the latest completed retry")

    job_ids = {record["job_id"] for record in records}
    input_digests = {record["inputs"]["digest"] for record in records}
    run_ids = {record["run_id"] for record in records}
    if len(job_ids) != 1 or len(input_digests) != 1 or len(run_ids) != 3:
        raise RuntimeError("retries changed the immutable job/input identity or reused an attempt ID")
    records = [cancelled_final, interrupted_final, passed_final]
    return events, (next(iter(job_ids)), next(iter(input_digests))), records


def capture_runtime_trace(directory: Path) -> tuple[list[str], tuple[str, str], list[dict[str, Any]]]:
    return _runtime_trace(directory, _environment())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the registered lifecycle TLC self-check.")
    parser.add_argument("--output", type=Path, default=ROOT / ".audit/lifecycle-model")
    args = parser.parse_args()
    jar_raw = os.environ.get("VKIT_TLA2TOOLS_JAR", "").strip()
    if not jar_raw or not Path(jar_raw).is_file():
        raise RuntimeError("VKIT_TLA2TOOLS_JAR must name the pinned tla2tools.jar")
    expected_jar = (ROOT / "examples/formal-ci/tla2tools.sha256").read_text(encoding="ascii").strip()
    observed_jar = hashlib.sha256(Path(jar_raw).read_bytes()).hexdigest()
    if observed_jar != expected_jar:
        raise RuntimeError(f"TLC jar digest mismatch: expected {expected_jar}, got {observed_jar}")
    env = _environment()
    java = shutil.which("java", path=env.get("PATH"))
    if not java:
        raise RuntimeError("the pinned TLC run requires Java on PATH or under JAVA_HOME")
    args.output.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=args.output))

    static = _new_project(run_root / "model", env)
    static_record = _run_model_case(static, env, "PASS")

    events, identity, records = _runtime_trace(run_root / "runtime", env)
    trace_project = _new_project(run_root / "trace", env, events=events, identity=identity)
    trace_record = _run_model_case(trace_project, env, "PASS")

    bad_events = ["submit", "start", "wrong_identity_pass"]
    bad_project = _new_project(run_root / "negative", env, events=bad_events,
                               identity=identity, wrong_pass=True)
    bad_record = _run_model_case(bad_project, env, "FAIL")
    counterexamples = (bad_record.get("obligations") or {}).get("counterexamples", [])
    if not any(item.get("obligation", {}).get("obligation") == "NoFalsePass"
               for item in counterexamples):
        raise RuntimeError(f"the injected false pass did not fail NoFalsePass: {counterexamples!r}")

    report = {
        "schema_version": 1,
        "scope": "finite attempt lifecycle model plus captured production job API trace",
        "model": {"run_id": static_record["run_id"], "result": "PASS",
                  "obligations": static_record.get("obligations")},
        "observed_trace": {"events": events, "job_id": identity[0], "input_digest": identity[1],
                           "attempts": [{"run_id": item["run_id"], "state": item["state"],
                                         "outcome": item.get("outcome")} for item in records],
                           "model_run_id": trace_record["run_id"], "result": "PASS"},
        "negative_control": {"events": bad_events, "result": "FAIL",
                             "counterexamples": counterexamples},
        "limit": "The finite model checks the declared lifecycle abstraction; one observed trace checks only the captured execution, not all scheduler interleavings.",
    }
    (run_root / "lifecycle-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"result": "PASS", "report": str(run_root / "lifecycle-report.json"),
                      "events": events, "negative_control": "NoFalsePass rejected"}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(f"lifecycle check failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
