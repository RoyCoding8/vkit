"""Check observed branch tips in one bounded CI environment."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, choices=range(1, 31), required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("run this controller in its manual GitHub Actions workflow")
    root = Path(__file__).resolve().parents[1]
    token = os.environ["GITHUB_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ["GITHUB_REF_NAME"]
    args.output.mkdir(parents=True, exist_ok=False)
    trusted_files = ["scripts/qualify_refinedrust.py",
                     "examples/refinedrust-qualification/environment.nix",
                     "examples/refinedrust-qualification/sources.nix",
                     "examples/refinedrust-qualification/upstream.json"]
    driver_hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                     for name in trusted_files}
    (args.output / "driver-hashes.json").write_text(
        json.dumps(driver_hashes, indent=2) + "\n", encoding="utf-8")
    deadline = time.monotonic() + args.minutes * 60
    seen: set[str] = set()
    child_env = {key: value for key, value in os.environ.items()
                 if "TOKEN" not in key and not key.startswith("GIT_CONFIG_")}
    fetch_env = dict(child_env)
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    fetch_env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                     GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {credential}")

    def api(method: str, suffix: str, data: dict) -> dict:
        budget = deadline + (60 if method == "PATCH" else 0) - time.monotonic()
        if budget <= 0:
            raise TimeoutError("session deadline reached")
        request = Request(
            f"https://api.github.com/repos/{repository}/check-runs{suffix}",
            data=json.dumps(data).encode(), method=method,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        with urlopen(request, timeout=min(30, budget)) as response:
            return json.load(response)

    def stop(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        while time.monotonic() < deadline:
            subprocess.run(["git", "fetch", "--quiet", "--no-tags", "origin", f"refs/heads/{branch}"],
                           cwd=root, env=fetch_env, check=True,
                           timeout=min(30, max(0.001, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            revision = subprocess.check_output(
                ["git", "rev-parse", "FETCH_HEAD"], cwd=root, text=True).strip()
            if revision in seen:
                time.sleep(min(10, max(0, deadline - time.monotonic())))
                continue
            seen.add(revision)
            subprocess.run(["git", "checkout", "--quiet", "--detach", revision],
                           cwd=root, env=child_env, check=True,
                           timeout=min(30, max(0.001, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            run = args.output / revision
            log_path = args.output / f"{revision}.log"
            check = None
            completed = False
            detail = "The session stopped before a model result was recorded."
            try:
                check = api("POST", "", {"name": "Provisional ledger model", "head_sha": revision,
                                         "status": "in_progress"})
                for name, digest in driver_hashes.items():
                    if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                        raise ValueError(f"restart the session to review changed driver input: {name}")
                command = ["python3", str(root / "scripts/qualify_refinedrust.py"), "--only", "model",
                           "--upstream", str(args.upstream), "--output", str(run)]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("session deadline reached before model launch")
                with log_path.open("w", encoding="utf-8") as log:
                    with subprocess.Popen(command, cwd=root, env=child_env, stdout=log,
                                          stderr=subprocess.STDOUT, start_new_session=True) as process:
                        try:
                            process.wait(timeout=remaining)
                        finally:
                            if process.poll() is None:
                                os.killpg(process.pid, signal.SIGTERM)
                                try:
                                    process.wait(timeout=10)
                                except subprocess.TimeoutExpired:
                                    os.killpg(process.pid, signal.SIGKILL)
                                    process.wait()
                report = json.loads((run / "report.json").read_text(encoding="utf-8"))
                stages = report.get("stages", [])
                completed = (report.get("execution_mode") == "model_only"
                             and report.get("qualification") == "INCONCLUSIVE"
                             and "error" not in report and len(stages) == 1
                             and stages[0]["name"] == "model" and stages[0]["state"] == "COMPLETED")
                detail = log_path.read_text(encoding="utf-8")[-8000:]
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                detail = f"{type(error).__name__}: {error}\n"
                if log_path.exists():
                    detail += log_path.read_text(encoding="utf-8")[-7000:]
            finally:
                result = {"revision": revision, "model_completed": completed,
                          "qualification": "INCONCLUSIVE", "product_policy_accepted": False}
                (args.output / f"{revision}.json").write_text(
                    json.dumps(result, indent=2) + "\n", encoding="utf-8")
                if check is not None:
                    api("PATCH", f"/{check['id']}", {
                        "status": "completed", "conclusion": "success" if completed else "failure",
                        "output": {"title": "Provisional model checked" if completed else "Model check failed",
                                   "summary": "Product qualification remains INCONCLUSIVE. Evidence is retained per revision.",
                                   "text": detail},
                    })
                print(json.dumps(result), flush=True)
    except KeyboardInterrupt:
        return 130
    except subprocess.TimeoutExpired:
        if time.monotonic() < deadline:
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
