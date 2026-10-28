from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
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
    "PublishedAttemptComplete",
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
    # Pytest may have imported an editable install from another checkout before this
    # driver is loaded. Runtime observations must exercise the source tree under test.
    for name in tuple(sys.modules):
        if name == "vkit" or name.startswith("vkit."):
            del sys.modules[name]
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


def _manifest(config: str) -> dict[str, Any]:
    raw = json.loads((ROOT / "verification/manifest.json").read_text(encoding="utf-8"))
    check = next(item for item in raw["checks"] if item["id"] == CHECK_ID)
    check["config"] = config
    check["inputs"] = [MODEL_REL.as_posix(), config]
    check["subject"]["paths"] = [MODEL_REL.as_posix(), config]
    check["properties"] = PROPERTIES
    raw["checks"] = [check]
    return raw


def _new_project(directory: Path, env: dict[str, str], *, wrong_pass: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=False)
    model = (ROOT / MODEL_REL).read_text(encoding="utf-8")
    config = (ROOT / CONFIG_REL).read_text(encoding="utf-8")
    if wrong_pass:
        config = config.replace("InjectFalsePass = FALSE", "InjectFalsePass = TRUE")
    model_path = directory / MODEL_REL
    config_path = directory / CONFIG_REL
    model_path.parent.mkdir(parents=True)
    model_path.write_text(model, encoding="utf-8", newline="\n")
    config_path.write_text(config, encoding="utf-8", newline="\n")
    (directory / "verification").mkdir(exist_ok=True)
    (directory / "verification/manifest.json").write_text(
        json.dumps(_manifest(CONFIG_REL.as_posix()), indent=2) + "\n",
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
    from vkit.store import _process_alive, read_json

    worker = read_json(store.run_dir(run_id) / "worker.json")
    if not worker or not isinstance(worker.get("pid"), int):
        raise RuntimeError(f"attempt {run_id} has no observed worker process")
    pid = worker["pid"]
    if os.name == "nt":
        done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                              capture_output=True, text=True, check=False, env=env,
                              timeout=15, **_hidden_window())
        if done.returncode != 0 and _process_alive(pid):
            raise RuntimeError(f"could not stop worker {pid}: {done.stdout} {done.stderr}")
    else:
        os.killpg(pid, signal.SIGKILL)


def _wait_started(store: Any, run_id: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    path = store.run_dir(run_id) / "started"
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    log = store.run_dir(run_id) / "worker.log"
    detail = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else "no worker log"
    raise RuntimeError(f"job command did not reach its start barrier: {run_id}: {detail[-2000:]}")


def _runtime_observations(directory: Path, env: dict[str, str]) -> dict[str, Any]:
    from vkit.jobs import retry, submit, wait
    from vkit.manifest import parse_manifest
    from vkit.paths import open_project
    from vkit.store import Store

    directory.mkdir(parents=True, exist_ok=False)
    (directory / "verification").mkdir()
    (directory / "slow.py").write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "output = Path(sys.argv[1])\n"
        "root = output.parent\n"
        "(root / 'started').touch()\n"
        "while not (root / 'release').exists():\n"
        "    time.sleep(0.01)\n"
        "output.write_text(json.dumps({'schema_version': 1, 'scenarios': "
        "[{'id': 'ok', 'result': 'PASS', 'observation': 'ok'}]}), encoding='utf-8')\n",
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
    first = submit(project, manifest, "lifecycle-runtime", store=store)
    _wait_state(store, first["run_id"], "running")
    _wait_started(store, first["run_id"])

    second = submit(project, manifest, "lifecycle-runtime", store=store)
    _wait_state(store, second["run_id"], "running")
    _wait_started(store, second["run_id"])
    if first["job_id"] != second["job_id"] or first["run_id"] == second["run_id"]:
        raise RuntimeError("concurrent submissions did not create distinct attempts for one job")

    # Let cancellation and command completion contend at the same release barrier.
    barrier = threading.Barrier(2)

    def cancel_at_release() -> None:
        barrier.wait()
        store.request_cancel(first["run_id"])

    cancel_thread = threading.Thread(target=cancel_at_release)
    cancel_thread.start()
    barrier.wait()
    (store.run_dir(first["run_id"]) / "release").touch()
    cancel_thread.join(timeout=5)
    if cancel_thread.is_alive():
        raise RuntimeError("the cancellation race did not finish")
    if not store.cancel_requested(first["run_id"]):
        raise RuntimeError("the concurrent cancel request was not recorded")
    raced = wait(store, first["run_id"], 30)
    if raced["state"] != "done" or raced["outcome"]["result"] not in {"PASS", "BLOCKED"}:
        raise RuntimeError(f"cancel/completion contention produced an invalid result: {raced!r}")
    if raced["outcome"]["result"] == "BLOCKED" and raced["outcome"].get("reason") != "cancelled":
        raise RuntimeError(f"the blocked cancellation race had an unrelated reason: {raced!r}")

    pointer_before_loss = store.evidence_for("registered_check", "lifecycle-runtime", first["job_id"])
    if pointer_before_loss is None or pointer_before_loss["run_id"] != raced["run_id"]:
        raise RuntimeError("completed attempt did not publish an intact evidence pointer")

    # Keep the sibling worker active while recovery observes its death. It must not publish.
    _stop_worker(second["run_id"], store, env)
    interrupted = _wait_state(store, second["run_id"], "interrupted")
    if interrupted.get("outcome") is not None:
        raise RuntimeError(f"a lost worker exposed a terminal outcome: {interrupted!r}")
    interrupted_identity = {name: interrupted.get(name) for name in
                            ("run_id", "job_id", "inputs", "source", "state", "outcome")}
    pointer_after_loss = store.evidence_for("registered_check", "lifecycle-runtime", second["job_id"])
    if pointer_after_loss != pointer_before_loss:
        raise RuntimeError("worker loss replaced previously published evidence")

    third = retry(project, second["run_id"], store=store)
    _wait_state(store, third["run_id"], "running")
    _wait_started(store, third["run_id"])
    (store.run_dir(third["run_id"]) / "release").touch()
    passed = wait(store, third["run_id"], 30)
    if passed["state"] != "done" or passed["outcome"]["result"] != "PASS":
        raise RuntimeError(f"the retry did not finish with a recorded pass: {passed!r}")

    raced_final = store.record(first["run_id"])
    interrupted_final = store.record(second["run_id"])
    passed_final = store.record(third["run_id"])
    if (raced_final["state"] != raced["state"]
            or raced_final["outcome"] != raced["outcome"]
            or any(raced_final.get(name) != raced.get(name)
                   for name in ("run_id", "job_id", "inputs", "source"))
            or interrupted_final["state"] != "interrupted"
            or interrupted_final.get("outcome") is not None
            or any(interrupted_final.get(name) != value
                   for name, value in interrupted_identity.items())
            or passed_final["outcome"].get("result") != "PASS"):
        raise RuntimeError("later retries changed an earlier attempt or exposed a false terminal result")
    published = store.evidence_for("registered_check", "lifecycle-runtime", third["job_id"])
    if published is None or published["run_id"] != third["run_id"] or published["result"] != "PASS":
        raise RuntimeError("the final pass was not bound to the latest completed retry")

    records = [raced_final, interrupted_final, passed_final]
    job_ids = {record["job_id"] for record in records}
    input_digests = {record["inputs"]["digest"] for record in records}
    run_ids = {record["run_id"] for record in records}
    if len(job_ids) != 1 or len(input_digests) != 1 or len(run_ids) != 3:
        raise RuntimeError("retries changed the immutable job/input identity or reused an attempt ID")
    return {
        "job_id": next(iter(job_ids)),
        "input_digest": next(iter(input_digests)),
        "attempts": [{"run_id": record["run_id"], "job_id": record["job_id"],
                      "state": record["state"],
                      "outcome": record.get("outcome")} for record in records],
        "concurrent_same_job": {"run_ids": [first["run_id"], second["run_id"]],
                                "shared_job_id": first["job_id"]},
        "cancel_completion_race": {"run_id": raced["run_id"],
                                   "outcome": raced["outcome"]},
        "published_before_worker_loss": pointer_before_loss,
        "published_after_worker_loss": pointer_after_loss,
        "final_published": published,
    }


def capture_runtime_observations(directory: Path) -> dict[str, Any]:
    return _runtime_observations(directory, _environment())


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

    observations = _runtime_observations(run_root / "runtime", env)
    bad_project = _new_project(run_root / "negative", env, wrong_pass=True)
    bad_record = _run_model_case(bad_project, env, "FAIL")
    counterexamples = (bad_record.get("obligations") or {}).get("counterexamples", [])
    if not any(item.get("obligation", {}).get("obligation") == "NoFalsePass"
               for item in counterexamples):
        raise RuntimeError(f"the injected false pass did not fail NoFalsePass: {counterexamples!r}")

    report = {
        "schema_version": 1,
        "scope": "bounded multi-attempt lifecycle model plus observed production job API behavior",
        "model": {"run_id": static_record["run_id"], "result": "PASS",
                  "attempt_slots": 3, "max_attempt_number": 3,
                  "publication_model": "The pointer selects any completed attempt for this job, including an older attempt that publishes later; publication is atomic in the model and hash bytes are abstracted.",
                  "obligations": static_record.get("obligations")},
        "observed_execution": observations,
        "negative_control": {"result": "FAIL",
                             "counterexamples": counterexamples},
        "limit": "TLC checks three indexed attempts with finite state domains. No parameterized induction or implementation-refinement proof is claimed. The production observations cover these OS/process schedules only; hash bytes and crashes during pointer publication are abstracted.",
    }
    (run_root / "lifecycle-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"result": "PASS", "report": str(run_root / "lifecycle-report.json"),
                      "attempts": len(observations["attempts"]),
                      "concurrent_same_job": True,
                      "cancel_completion_race": observations["cancel_completion_race"]["outcome"]["result"],
                      "negative_control": "NoFalsePass rejected"}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        print(f"lifecycle check failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
