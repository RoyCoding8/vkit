"""Run the pinned Verus ledger qualification in a sealed Docker container."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "verus-linux-x86_64-v1"
LABEL = "vkit.ledger.run_id"
FORMAL_LABEL = "vkit.formal.run_id"
SOURCE_REL = Path("src/vkit/formal/packages/resource_ledger/ledger.rs")
TRUSTED_FILES = (
    Path("scripts/check_verus_ledger.py"),
    Path("src/vkit/formal/packages/resource_ledger/upstream.json"),
    Path("examples/verus-qualification/smoke.rs"),
    Path("examples/verus-qualification/candidate_api.rs"),
    Path("src/vkit/formal/packages/resource_ledger/expected.rs.in"),
)


def run(command: list[str], *, timeout: float = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def docker_image_id(image: str) -> str:
    image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).stdout.strip()
    if not image_id.startswith("sha256:"):
        raise ValueError("Docker did not return an image content digest")
    return image_id


def docker_base(run_id: str, cidfile: Path) -> list[str]:
    return [
        "docker", "run", "--cidfile", str(cidfile), "--label", f"{LABEL}={run_id}",
        "--label", f"vkit.ledger.profile={PROFILE}", "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=256",
        "--memory=4g", "--cpus=2", "--ulimit", "nofile=1024:1024",
        "--ulimit", "fsize=2147483648:2147483648",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m", "--user", "65532:65532",
        "--workdir", "/work", "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp",
    ]


def inspect_label(container_id: str) -> str:
    return run(["docker", "inspect", "--format", f'{{{{ index .Config.Labels "{LABEL}" }}}}', container_id]).stdout.strip()


def remove_owned(run_id: str) -> list[str]:
    result = run(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={run_id}"], check=False)
    container_ids = [item for item in result.stdout.splitlines() if item]
    removed = []
    for container_id in container_ids:
        if inspect_label(container_id) != run_id:
            raise RuntimeError(f"refusing to remove container with a different run label: {container_id}")
        run(["docker", "rm", "-f", container_id], check=False)
        removed.append(container_id)
    remaining = run(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={run_id}"], check=False)
    if remaining.stdout.strip():
        raise RuntimeError(f"owned containers remain for run {run_id}")
    return removed


def cleanup_run(run_id: str, output: Path | None = None) -> list[str]:
    removed = []
    for suffix in ("", "-probe", "-network", "-timeout", "-crash"):
        removed.extend(remove_owned(run_id + suffix))
    removed.extend(remove_formal_owned(f"{run_id}-product"))
    if output is not None and output.is_dir():
        from vkit.container import recover_attempt

        descriptors = list((output / "product/durable/project/.git/vkit/runs").glob("*/container-control/descriptor.json"))
        descriptors += list((output / "product/recovery/runs").glob("*/container-control/descriptor.json"))
        descriptors += list((output / "product/gate").glob("*/store/runs/*/container-control/descriptor.json"))
        descriptors += list((output / "product/reuse").glob("*/project/.git/vkit/runs/*/container-control/descriptor.json"))
        descriptors += list((output / "product/reuse").glob("*/runtime/container-control/descriptor.json"))
        descriptors += list((output / "product/reuse/overhead").glob("direct-*/container-control/descriptor.json"))
        for descriptor in descriptors:
            recovery = recover_attempt(descriptor.parent.parent)
            if recovery["status"] == "blocked":
                raise RuntimeError(f"durable container cleanup failed: {recovery}")
    return removed


def formal_label(container_id: str) -> str:
    result = run(["docker", "inspect", "--format", f'{{{{ index .Config.Labels "{FORMAL_LABEL}" }}}}',
                  container_id], check=False)
    return result.stdout.strip()


def remove_formal_owned(prefix: str) -> list[str]:
    listing = run(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={FORMAL_LABEL}"],
                  check=False)
    removed = []
    for container_id in listing.stdout.splitlines():
        if container_id and formal_label(container_id).startswith(prefix + "-"):
            run(["docker", "rm", "-f", container_id], check=False)
            removed.append(container_id)
    remaining = run(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label={FORMAL_LABEL}"],
                    check=False)
    for container_id in remaining.stdout.splitlines():
        if container_id and formal_label(container_id).startswith(prefix + "-"):
            raise RuntimeError(f"product verifier container remains for {prefix}")
    return removed


def mount(source: Path, destination: str, readonly: bool = True) -> list[str]:
    spec = f"type=bind,src={source.resolve()},dst={destination}"
    return ["--mount", spec + (",readonly" if readonly else "")]


def stage_trusted(directory: Path, source: Path) -> tuple[Path, Path, dict[str, str]]:
    trusted = directory / "trusted"
    for relative in TRUSTED_FILES:
        target = trusted / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    from vkit.formal.ledger import render_expected

    expected = trusted / "examples/verus-qualification/expected.rs"
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_text(render_expected(product_component(3)), encoding="utf-8", newline="\n")
    source_target = trusted / SOURCE_REL
    source_target.parent.mkdir(parents=True, exist_ok=True)
    source_target.touch()
    captured_source = directory / "source" / "ledger.rs"
    captured_source.parent.mkdir(parents=True)
    shutil.copyfile(source, captured_source)
    hashes = {relative.as_posix(): sha256(trusted / relative) for relative in TRUSTED_FILES}
    hashes["examples/verus-qualification/expected.rs"] = sha256(expected)
    hashes[SOURCE_REL.as_posix()] = sha256(captured_source)
    return trusted, captured_source, hashes


def product_component(max_records: int):
    from vkit.formal import ledger
    from vkit.formal.policy import FormalComponent, LedgerBindings

    return FormalComponent(
        id="resource-ledger", package=ledger.load_package().identity,
        contract=ledger.CONTRACT, entry="candidate_api.rs", profile=ledger.PROFILE,
        max_records=max_records,
        bindings=LedgerBindings("new", "reserve", "release", "reserve_batch",
                                "apply_command", "run_commands"),
        required=True,
    )


def weaken_reserve_contract(source: str) -> str:
    start = source.index("pub fn reserve<")
    body = source.index("\n    {", start)
    signature = source[start:body]
    ensures = signature.index("        ensures\n")
    return source[:start + ensures] + "        ensures true,\n" + source[body:]


def run_product_probe(image_id: str, run_id: str, archive: Path, verus_root: Path,
                      rustup_home: Path, cargo_home: Path, directory: Path) -> dict:
    from vkit.formal import ledger, verus
    from vkit.outcome import Blocked, BlockedReason, Passed

    os.environ.update({
        "VKIT_VERUS_IMAGE": image_id,
        "VKIT_VERUS_ARCHIVE": str(archive),
        "VKIT_VERUS_ROOT": str(verus_root),
        "VKIT_RUSTUP_HOME": str(rustup_home),
        "VKIT_CARGO_HOME": str(cargo_home),
    })
    component = product_component(3)
    execution_identity = verus.resolve_execution(component)
    source = (ROOT / "examples/verus-qualification/candidate_api.rs").read_text(encoding="utf-8")
    cases = {}
    directory.mkdir(parents=True, exist_ok=False)

    def execute_case(name: str, records: int, candidate_source: str):
        snapshot = directory / "snapshots" / name
        snapshot.mkdir(parents=True)
        (snapshot / "candidate_api.rs").write_text(candidate_source, encoding="utf-8", newline="\n")
        policy = product_component(records)
        outcome, metadata = verus.execute_formal(
            policy, snapshot, directory / f"{run_id}-product-{name}",
            execution_identity, should_cancel=lambda: False
        )
        cases[name] = {"outcome": outcome.to_json(), "metadata": metadata}
        return outcome, metadata

    for name, records in (("positive_n3", 3), ("positive_n5", 5)):
        outcome, metadata = execute_case(name, records, source)
        expected = list(ledger.obligation_ids(product_component(records)))
        outcome_ids = [scenario.scenario_id for scenario in outcome.scenarios] if isinstance(outcome, Passed) else []
        if (not isinstance(outcome, Passed) or len(outcome.scenarios) != 7
                or outcome_ids != expected or any(not scenario.passed for scenario in outcome.scenarios)
                or metadata.get("obligations") != expected
                or metadata.get("method") != "verus_source"
                or metadata.get("assurance") != "trusted_engine"
                or metadata.get("category") != "source_verification"
                or metadata.get("scope") != "required_api"
                or len(metadata.get("evidence", [])) != 6
                or metadata.get("container", {}).get("removed") is not True):
            raise RuntimeError(f"product Verus probe failed for {name}: {outcome.to_json()}")

    weakened, _ = execute_case("weakened_reserve", 3, weaken_reserve_contract(source))
    if not isinstance(weakened, Blocked) or weakened.reason != BlockedReason.FORMAL_OBLIGATION:
        raise RuntimeError(f"product Verus negative probe did not block on the obligation: {weakened.to_json()}")
    durable = run_durable_probe(directory / "durable", source, execution_identity)
    recovery = run_recovery_probe(directory / "recovery", image_id, run_id)
    gate = run_gate_probe(directory / "gate", source, image_id)
    from qualify_ledger_reuse import qualify

    reuse = qualify(directory / "reuse", source, execution_identity)
    return {"passed": True, "image_id": image_id, "cases": cases,
            "durable": durable, "recovery": recovery, "gate": gate, "reuse": reuse}


def run_gate_probe(directory: Path, source: str, image_id: str) -> dict:
    from vkit.formal.service import resolve_commit
    from vkit.paths import open_project

    root = directory / "project"
    (root / "verification").mkdir(parents=True)
    source = source.replace("verus! {", "verus! {\n    const ADJUST: bool = false;", 1)
    source = source.rsplit("}", 1)[0] + "    const DELTA: u32 = 0;\n}\n"
    source = source.replace("        ledger::ledger_api::reserve(input, id, owner, amount)",
                            "        let amount = if ADJUST && DELTA > 0 && amount > 0 { amount - 1 } else { amount };\n"
                            "        ledger::ledger_api::reserve(input, id, owner, amount)", 1)
    (root / "candidate_api.rs").write_text(source, encoding="utf-8")
    driver = '''import json, os, pathlib, socket, sys
assert "VKIT_QUALIFICATION_SECRET" not in os.environ
assert not pathlib.Path("/output/../job.json").exists()
try:
    pathlib.Path("candidate_api.rs").write_text("must not be writable")
except OSError:
    pass
else:
    raise AssertionError("candidate mount is writable")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=0.2)
except OSError:
    pass
else:
    raise AssertionError("network is available")
pathlib.Path(sys.argv[1]).write_text(json.dumps({"schema_version":1,"scenarios":[
    {"id":"isolated","result":"PASS","observation":"candidate read-only; network and host secrets absent"}]}))
'''
    check = {"id": "isolation", "kind": "scenario", "timeout_seconds": 30,
             "artifact": "result.json", "command": ["{{python}}", "-I", "-c", driver, "{{run_dir}}/result.json"],
             "required_scenarios": ["isolated"], "inputs": ["candidate_api.rs"],
             "subject": {"paths": ["candidate_api.rs"], "digest": None}, "claim_id": "isolation"}
    (root / "verification/manifest.json").write_text(json.dumps({
        "schema_version": 3, "checks": [check],
        "formal": {"components": [product_component(3).to_json()]}}), encoding="utf-8")

    def git(*args):
        return run(["git", "-C", str(root), "-c", "user.name=VKit CI", "-c", "user.email=ci@vkit.invalid", *args])

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "target policy and feature interaction")
    project = open_project(root)
    target = resolve_commit(project, "HEAD")
    git("checkout", "-qb", "branch-a")
    (root / "candidate_api.rs").write_text(source.replace("ADJUST: bool = false", "ADJUST: bool = true"), encoding="utf-8")
    (root / "verification/manifest.json").unlink()
    git("add", "-u")
    git("commit", "-qm", "enable adjustment and delete candidate policy")
    branch_a = resolve_commit(project, "HEAD")
    git("checkout", "-qb", "branch-b", target)
    (root / "candidate_api.rs").write_text(source.replace("DELTA: u32 = 0", "DELTA: u32 = 1"), encoding="utf-8")
    git("add", "-u")
    git("commit", "-qm", "change adjustment amount")
    branch_b = resolve_commit(project, "HEAD")
    git("checkout", "branch-a")
    git("merge", "--no-ff", "-qm", "combined candidate", "branch-b")
    combined = resolve_commit(project, "HEAD")
    os.environ["VKIT_CI_IMAGE"] = image_id
    os.environ["VKIT_QUALIFICATION_SECRET"] = "host-only-control"
    reports = {}
    try:
        for name, oid in (("branch_a", branch_a), ("branch_b", branch_b), ("combined", combined)):
            invocation = run([sys.executable, "-m", "vkit.cli", "gate", "--project", str(project.root),
                              "--candidate", oid, "--target", target, "--output", str(directory / name), "--json"],
                             timeout=900, check=False)
            report = json.loads(invocation.stdout)
            expected = "BLOCKED" if name == "combined" else "READY"
            if invocation.returncode != (3 if name == "combined" else 0) or report["verdict"] != expected or len(report["checks"]) != 2:
                raise RuntimeError(f"gate control {name} failed: {report['checks']}")
            if any(not c["receipt_valid"] for c in report["checks"]):
                raise RuntimeError(f"gate control {name} has invalid receipts")
            reports[name] = {"candidate": oid, "verdict": report["verdict"], "checks": report["checks"]}
        formal = next(c for c in reports["combined"]["checks"] if c["kind"] == "formal_goal")
        if formal["outcome"].get("reason") != "formal_obligation":
            raise RuntimeError("combined candidate did not fail on its formal obligation")
    finally:
        os.environ.pop("VKIT_QUALIFICATION_SECRET", None)
    return {"passed": True, "target": target, "candidate_policy_deleted": True, "cases": reports}


def run_recovery_probe(directory: Path, image_id: str, qualification_id: str) -> dict:
    from vkit.container import control_path, recover_attempt
    from vkit.store import Store

    directory.mkdir()
    attempt = qualification_id + "-recovery"
    store = Store(directory)
    run_dir = store.run_dir(attempt)
    program = """import sys
from pathlib import Path
from vkit.container import run_container
from vkit.store import Store
store, attempt, image = Store(Path(sys.argv[1])), sys.argv[2], sys.argv[3]
with store.hold(attempt):
    store.save({'run_id': attempt, 'state': 'running'})
    run_container(image=image, run_id=attempt+'-owned', options=['--network=none', '--read-only'],
                  command=['sleep', '60'], run_dir=store.run_dir(attempt),
                  timeout_seconds=90, should_cancel=lambda: False)
"""
    with (directory / "worker.log").open("wb") as log:
        worker = subprocess.Popen([sys.executable, "-c", program, str(directory), attempt, image_id],
                                  stdout=log, stderr=subprocess.STDOUT)
        try:
            cid_path = control_path(run_dir).parent / "container.cid"
            deadline = time.monotonic() + 30
            cid = None
            while time.monotonic() < deadline and worker.poll() is None:
                if cid_path.is_file():
                    cid = cid_path.read_text(encoding="ascii").strip()
                    running = run(["docker", "inspect", "--format", "{{.State.Running}}", cid], check=False)
                    if running.returncode == 0 and running.stdout.strip() == "true":
                        break
                time.sleep(0.1)
            else:
                raise RuntimeError("recovery probe did not reach a running container")
            worker.kill()
            worker.wait(timeout=10)
            record = store.record(attempt)
            if (record["state"] != "interrupted" or record["container_cleanup"]["status"] != "removed"
                    or run(["docker", "inspect", cid], check=False).returncode == 0):
                raise RuntimeError(f"killed-worker container recovery failed: {record}")
            return {"passed": True, "container_id": cid, "state": record["state"],
                    "cleanup": record["container_cleanup"]}
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=10)
            cleanup = recover_attempt(run_dir)
            if cleanup["status"] == "blocked":
                raise RuntimeError(f"recovery probe cleanup failed: {cleanup}")


def run_durable_probe(directory: Path, source: str, identity: dict) -> dict:
    from vkit import evidence, jobs
    from vkit.formal.policy import GitCommit, VerificationRequest
    from vkit.formal.service import resolve_commit
    from vkit.inputs import canonical_digest
    from vkit.manifest import parse_manifest
    from vkit.paths import open_project
    from vkit.store import Store, now

    root = directory / "project"
    (root / "verification").mkdir(parents=True)
    (root / "candidate_api.rs").write_text(source, encoding="utf-8", newline="\n")
    component = product_component(3)
    (root / "verification/manifest.json").write_text(json.dumps({
        "schema_version": 3, "checks": [], "formal": {"components": [component.to_json()]}}), encoding="utf-8")
    for arguments in (["init", "-q"], ["add", "."],
                      ["-c", "user.name=VKit qualification", "-c", "user.email=ci@vkit.invalid", "commit", "-qm", "candidate"]):
        run(["git", "-C", str(root), *arguments])
    project = open_project(root)
    manifest, store = parse_manifest(project), Store.of(project)
    oid = resolve_commit(project, "HEAD")
    store.approve({component.digest: {"kind": "formal_policy", "policy_sha256": component.digest,
                                     "package_sha256": component.package.sha256,
                                     "checker_sha256": canonical_digest(identity["semantic"]), "accepted_at": now()}})
    request = VerificationRequest(component.id, GitCommit(oid))
    submitted = jobs.submit_formal(project, request, component, identity, store=store)
    first = jobs.wait(store, submitted["run_id"], 900)
    bundle = directory / "bundle"
    evidence.export(store, first["run_id"], bundle)
    (root / "candidate_api.rs").write_text("deliberately broken live checkout", encoding="utf-8")
    retried = jobs.wait(store, jobs.retry(project, first["run_id"], store=store)["run_id"], 900)
    replayed = jobs.wait(store, jobs.replay(project, manifest, bundle, store=store)["run_id"], 900)
    for record in (first, retried, replayed):
        if (record["state"] != "done" or record["outcome"]["result"] != "PASS"
                or record["job_id"] != first["job_id"]
                or record["assurance"] != "trusted_engine" or record["method"] != "verus_source"
                or not evidence.validate_record(record, store.run_dir(record["run_id"]))):
            raise RuntimeError(f"durable product verification failed: {record}")
    if len({record["run_id"] for record in (first, retried, replayed)}) != 3:
        raise RuntimeError("retry and replay did not create distinct attempts")
    vir = bundle / "formal-output/candidate_api.vir"
    vir.write_bytes(vir.read_bytes() + b"tampered")
    try:
        jobs.replay(project, manifest, bundle, store=store)
    except ValueError:
        tamper_rejected = True
    else:
        raise RuntimeError("replay accepted changed verifier artifacts")
    return {"job_id": first["job_id"], "candidate": oid, "tamper_rejected": tamper_rejected,
            "attempts": [{"run_id": record["run_id"], "receipt": record["receipt"]}
                         for record in (first, retried, replayed)]}


def container_command(image_id: str, base: list[str], trusted: Path, source: Path, archive: Path,
                      verus_root: Path, rustup_home: Path, cargo_home: Path,
                      output: Path | None, command: list[str]) -> list[str]:
    return [
        *base,
        *mount(trusted, "/trusted"),
        *mount(source, f"/trusted/{SOURCE_REL.as_posix()}"),
        *mount(archive, f"/assets/{archive.name}"),
        *mount(verus_root, "/assets/verus"),
        *mount(rustup_home, "/assets/rustup"),
        *mount(cargo_home, "/assets/cargo"),
        *([] if output is None else mount(output, "/output", readonly=False)),
        "--env", "RUSTUP_HOME=/assets/rustup", "--env", "CARGO_HOME=/assets/cargo",
        "--env", "PATH=/assets/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        image_id, *command,
    ]


def execute(image_id: str, run_id: str, cidfile: Path, command: list[str],
            log_path: Path, timeout: float, output: Path | None = None) -> dict:
    process = None
    container_id = None
    start = time.monotonic()
    timeout_started = None
    owned_label = False
    removed = False
    output_retained = None
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def terminate(signum, frame):
        if cidfile.is_file():
            cid = cidfile.read_text(encoding="utf-8").strip()
            if cid and inspect_label(cid) == run_id:
                run(["docker", "stop", "--time", "2", cid], check=False)
                run(["docker", "kill", cid], check=False)
        raise KeyboardInterrupt(f"received signal {signum}")

    for signum in previous_handlers:
        signal.signal(signum, terminate)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timeout_started = time.monotonic()
                if cidfile.is_file():
                    container_id = cidfile.read_text(encoding="utf-8").strip()
                    if container_id:
                        owned_label = inspect_label(container_id) == run_id
                    if container_id and owned_label:
                        run(["docker", "stop", "--time", "2", container_id], check=False)
                        run(["docker", "kill", container_id], check=False)
                process.kill()
                process.wait()
                code = None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if cidfile.is_file():
            container_id = cidfile.read_text(encoding="utf-8").strip()
            if container_id and inspect_label(container_id) == run_id:
                owned_label = True
                if output is not None:
                    output_retained = output.is_dir() and any(output.iterdir())
                run(["docker", "rm", "-f", container_id], check=False)
                removed = run(["docker", "inspect", container_id], check=False).returncode != 0
    return {"returncode": code, "container_id": container_id, "label_owned": owned_label,
            "seconds": round(time.monotonic() - start, 3), "log": log_path.name,
            "timed_out": code is None, "container_removed": removed,
            "output_retained": output_retained,
            "cancel_seconds": round(time.monotonic() - timeout_started, 3) if timeout_started else None}


def run_controls(image_id: str, run_id: str, cid_dir: Path, trusted: Path, source: Path,
                 archive: Path, verus_root: Path, rustup_home: Path, cargo_home: Path,
                 output: Path) -> dict:
    controls = {}
    probe_base = docker_base(f"{run_id}-probe", cid_dir / "probe.cid")
    ro_cmd = container_command(image_id, probe_base, trusted, source, archive, verus_root,
                               rustup_home, cargo_home, None,
                               ["python3", "-c", f"from pathlib import Path; Path('/trusted/{SOURCE_REL.as_posix()}').write_text('changed')"])
    ro_result = run(ro_cmd, check=False)
    controls["source_read_only"] = ro_result.returncode != 0 and "Read-only file system" in ro_result.stderr + ro_result.stdout

    net_cmd = container_command(image_id, docker_base(f"{run_id}-network", cid_dir / "network.cid"),
                                trusted, source, archive, verus_root, rustup_home, cargo_home, None,
                                ["python3", "-c", "import errno,socket,sys\ntry:\n    socket.create_connection(('1.1.1.1', 53), timeout=2)\nexcept OSError as error:\n    if error.errno != errno.ENETUNREACH:\n        raise\n    print('network_denied:ENETUNREACH')\n    sys.exit(0)\nsys.exit(2)"])
    net_result = run(net_cmd, timeout=10, check=False)
    network_cidfile = cid_dir / "network.cid"
    network_cid = network_cidfile.read_text(encoding="utf-8").strip() if network_cidfile.is_file() else ""
    network_mode = run(["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", network_cid],
                       check=False).stdout.strip() if network_cid else ""
    controls["network_disabled"] = (net_result.returncode == 0
                                    and "network_denied:ENETUNREACH" in net_result.stdout
                                    and network_mode == "none"
                                    and inspect_label(network_cid) == f"{run_id}-network")

    timeout_id = f"{run_id}-timeout"
    timeout_cidfile = cid_dir / "timeout.cid"
    timeout_execution = execute(image_id, timeout_id, timeout_cidfile,
                                [*docker_base(timeout_id, timeout_cidfile), image_id, "sleep", "60"],
                                cid_dir / "timeout.log", 1)
    controls["timeout_cid_external"] = timeout_cidfile.parent != output and bool(timeout_execution["container_id"])
    controls["timeout_label_owned"] = timeout_execution["label_owned"]
    controls["timeout_observed"] = timeout_execution["timed_out"]
    controls["timeout_removed"] = timeout_execution["container_removed"]
    controls["cancel_seconds"] = timeout_execution["cancel_seconds"]
    controls["cancel_under_five_seconds"] = bool(controls["cancel_seconds"] is not None
                                                  and controls["cancel_seconds"] <= 5)

    crash_id = f"{run_id}-crash"
    crash_cidfile = cid_dir / "crash.cid"
    crash_cmd = [*docker_base(crash_id, crash_cidfile), "--detach", image_id,
                 "python3", "-c", "raise SystemExit(23)"]
    run(crash_cmd)
    crash_cid = crash_cidfile.read_text(encoding="utf-8").strip()
    crash_exit = run(["docker", "wait", crash_cid]).stdout.strip()
    controls["crash_exit_seen"] = crash_exit == "23"
    controls["crash_label_owned"] = inspect_label(crash_cid) == crash_id
    controls["crash_removed"] = crash_cid in remove_owned(crash_id)
    controls["all_passed"] = all(value for key, value in controls.items()
                                  if key not in {"all_passed", "cancel_seconds"})
    return controls


def cleanup(args: argparse.Namespace) -> int:
    removed = cleanup_run(args.cleanup_run_id, args.output)
    print(json.dumps({"run_id": args.cleanup_run_id, "removed": removed}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--verus-root", type=Path)
    parser.add_argument("--rustup-home", type=Path)
    parser.add_argument("--cargo-home", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=780)
    parser.add_argument("--run-id", default=f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{uuid.uuid4().hex[:12]}")
    parser.add_argument("--cleanup-run-id")
    args = parser.parse_args()
    if args.cleanup_run_id:
        return cleanup(args)
    required = (args.archive, args.verus_root, args.rustup_home, args.cargo_home, args.output)
    if any(path is None for path in required):
        parser.error("archive, verus-root, rustup-home, cargo-home and output are required")
    if os.name != "posix" or os.geteuid() == 0:
        raise RuntimeError("sealed ledger qualification requires a non-root Linux Docker host")
    if not (args.cargo_home / "bin/rustup").is_file():
        raise FileNotFoundError("the isolated Rust toolchain must include cargo/bin/rustup")
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    assets = (args.archive, args.verus_root, args.rustup_home, args.cargo_home)
    if any(runner_temp not in path.resolve().parents for path in assets):
        raise RuntimeError("container tool assets must be staged under RUNNER_TEMP, outside the host home")
    args.output.mkdir(parents=True, exist_ok=False)
    os.chmod(args.output, 0o700)
    candidate_output = args.output / "candidate-output"
    candidate_output.mkdir()
    os.chmod(candidate_output, 0o777)
    image_id = docker_image_id(args.image)
    run_id = args.run_id
    cid_dir = args.output.parent / f"{args.output.name}-control"
    cid_dir.mkdir(parents=True, exist_ok=True)
    trusted, source, input_hashes = stage_trusted(cid_dir, ROOT / SOURCE_REL)
    report = {
        "profile": PROFILE,
        "image": {"reference": args.image, "id": image_id},
        "tool_assets_root": str(runner_temp),
        "dockerfile_sha256": sha256(ROOT / "examples/verus-qualification/container/Dockerfile"),
        "orchestrator_sha256": sha256(Path(__file__).resolve()),
        "run_id": run_id,
        "input_sha256": input_hashes,
        "mounts": [
            {"target": "/trusted", "read_only": True},
            {"target": f"/trusted/{SOURCE_REL.as_posix()}", "read_only": True},
            {"target": f"/assets/{args.archive.name}", "read_only": True},
            {"target": "/assets/verus", "read_only": True},
            {"target": "/assets/rustup", "read_only": True},
            {"target": "/assets/cargo", "read_only": True},
            {"target": "/tmp", "kind": "tmpfs", "size": "128 MiB", "exec": False},
            {"target": "/output", "host_source": str(candidate_output),
             "kind": "private runner-temp bind", "read_only": False, "exec": True},
        ],
        "container_policy": {"network": "none", "user": "65532:65532", "cap_drop": "ALL",
                             "no_new_privileges": True, "rootfs_read_only": True,
                             "tmpfs": {"/tmp": {"size": "128 MiB", "exec": False}},
                             "output_file_size_limit_bytes": 2147483648,
                             "memory": "4 GiB", "cpus": 2, "pids": 256,
                             "home_token_socket_mounts": []},
        "checks": {},
    }
    try:
        controls = run_controls(image_id, run_id, cid_dir, trusted, source, args.archive,
                                args.verus_root, args.rustup_home, args.cargo_home, args.output)
        report["checks"].update(controls)
        if not controls["all_passed"]:
            raise RuntimeError("container isolation negative probe failed")
        cidfile = cid_dir / "qualification.cid"
        command = container_command(image_id, docker_base(run_id, cidfile), trusted, source,
                                    args.archive, args.verus_root, args.rustup_home,
                                    args.cargo_home, candidate_output,
                                    ["python3", "/trusted/scripts/check_verus_ledger.py",
                                     "--verus-root", "/assets/verus",
                                     "--archive", f"/assets/{args.archive.name}",
                                     "--output", "/output/evidence"])
        execution = execute(image_id, run_id, cidfile, command,
                            args.output / "container.log", args.timeout, candidate_output)
        report["execution"] = execution
        evidence = candidate_output / "evidence/report.json"
        report["checks"]["comparison_passed"] = False
        evidence_report = json.loads(evidence.read_text(encoding="utf-8")) if evidence.is_file() else {}
        report["checks"]["comparison_passed"] = evidence_report.get("comparison_passed") is True
        report["driver_scope"] = evidence_report.get("scope")
        if (execution["returncode"] != 0 or execution["timed_out"]
                or not execution["output_retained"] or not evidence.is_file()):
            raise RuntimeError("sealed Verus qualification failed")
        checks = evidence_report["checks"]
        rust_log = next(item["log"] for item in checks if item["name"] == "rust_version")
        z3_log = next(item["log"] for item in checks if item["name"] == "solver_version")
        rust_text = (candidate_output / "evidence" / rust_log).read_text(encoding="utf-8", errors="replace")
        z3_text = (candidate_output / "evidence" / z3_log).read_text(encoding="utf-8", errors="replace")
        report["checks"]["rust_1_99_1"] = "rustc 1.99.1" in rust_text
        report["checks"]["z3_4_16_0"] = "Z3 version 4.16.0" in z3_text
        report["product"] = run_product_probe(image_id, run_id, args.archive, args.verus_root,
                                               args.rustup_home, args.cargo_home, args.output / "product")
        report["checks"]["product_passed"] = report["product"]["passed"]
        report["qualification_passed"] = all(report["checks"][name] for name in
                                              ("comparison_passed", "rust_1_99_1", "z3_4_16_0", "product_passed"))
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration,
            subprocess.SubprocessError, KeyboardInterrupt) as error:
        report["error"] = str(error)
        report["qualification_passed"] = False
    finally:
        try:
            report["cleanup"] = {"removed": cleanup_run(run_id, args.output), "remaining": False}
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            report["cleanup"] = {"remaining": True, "error": str(error)}
            report["qualification_passed"] = False
        (args.output / "container-qualification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"qualification_passed": report["qualification_passed"],
                      "image_id": image_id, "run_id": run_id}, indent=2))
    return 0 if report["qualification_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
