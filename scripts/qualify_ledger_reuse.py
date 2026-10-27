"""Exercise two ledger interpretations and compare identical proof work."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import statistics
import sys
import time
import uuid

from check_ledger_container import ROOT, product_component, run
from vkit import container, evidence, jobs
from vkit.formal import ledger, verus
from vkit.formal.policy import OPERATIONS, GitCommit, LedgerBindings, VerificationRequest
from vkit.formal.service import resolve_commit
from vkit.inputs import canonical_digest, capture_git_commit
from vkit.paths import open_project
from vkit.runner import execute, prepare_formal
from vkit.store import Store, now


def project_for(root, component, source):
    (root / "verification").mkdir(parents=True)
    (root / component.entry).write_text(source, encoding="utf-8")
    (root / "verification/manifest.json").write_text(json.dumps({"schema_version": 3, "checks": [],
        "formal": {"components": [component.to_json()]}}), encoding="utf-8")
    for args in (("init", "-q"), ("add", "."), ("-c", "user.name=VKit CI", "-c",
                 "user.email=ci@vkit.invalid", "commit", "-qm", "reuse candidate")):
        run(["git", "-C", str(root), *args])
    project = open_project(root)
    return project, resolve_commit(project, "HEAD")


def approve(store, component, identity):
    store.approve({component.digest: {"kind": "formal_policy", "policy_sha256": component.digest,
        "package_sha256": component.package.sha256,
        "checker_sha256": canonical_digest(identity["semantic"]), "accepted_at": now()}})


def phase_seconds(output):
    return sum(item["seconds"] for item in json.loads((output / "timings.json").read_text()))


def direct(component, snapshot, identity, directory):
    started = time.monotonic()
    current = verus.resolve_execution(component)
    if current["semantic"] != identity["semantic"]:
        raise RuntimeError("direct procedure execution identity changed")
    package = ledger.verify_package(component)
    trusted, output = directory / "trusted", directory / "output"
    trusted.mkdir(parents=True)
    output.mkdir()
    output.chmod(0o777)
    (trusted / "expected.rs").write_text(ledger.render_expected(component), encoding="utf-8")
    toolchain = identity["semantic"]["verus"]["version"]["toolchain"].split()[0]
    phases = verus._verus_phases("/trusted/package", "/output/candidate_api.rs", "/trusted/rendered/expected.rs", toolchain)
    phases[0]["marker"] = identity["semantic"]["verus"]["version"]["version"]
    plan = {"source": "/source/" + component.entry,
            "source_sha256": evidence.sha256(snapshot / component.entry), "phases": phases}
    options = [*container.sealed_options(), "--workdir", "/work",
               "--env", "RUSTUP_HOME=/assets/rustup", "--env", "CARGO_HOME=/assets/cargo",
               "--env", "PATH=/assets/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               *container.mount(snapshot, "/source", True),
               *container.mount(package.root, "/trusted/package", True),
               *container.mount(trusted, "/trusted/rendered", True), *container.mount(output, "/output", False)]
    for key, target in (("verus_root", "/assets/verus"), ("rustup_home", "/assets/rustup"), ("cargo_home", "/assets/cargo")):
        options += container.mount(Path(identity["paths"][key]), target, True)
    result = container.run_container(image=identity["semantic"]["image_id"],
        run_id=f"{directory.name}-{uuid.uuid4().hex}", options=options,
        command=["python3", "-c", verus._CONTAINER_SCRIPT, json.dumps(plan)],
        run_dir=directory, timeout_seconds=900, should_cancel=lambda: False)
    if result.exit.code != 0 or not result.removed or not verus._expected_logs(output, phases)[0]:
        raise RuntimeError(f"direct full-proof procedure failed: {result}")
    return {"wall_seconds": time.monotonic() - started, "phase_seconds": phase_seconds(output)}


def runtime_fixture(fixture, component, proof, identity, directory):
    commands, assertions = [], []
    for i, item in enumerate(fixture["commands"]):
        if "reserve" in item:
            id, owner, amount = item["reserve"]
            commands.append(f"Command::Reserve {{ id: {id}, owner: {owner}, amount: {amount} }}")
        else:
            id, owner = item["release"]
            commands.append(f"Command::Release {{ id: {id}, owner: {owner} }}")
        assertions.append(f"assert!(matches!(responses[{i}], {item['expected']}));")
    smoke = ("extern crate ledger; extern crate candidate_api;\n"
             "use ledger::ledger_api::{Command, Response, LedgerError};\n"
             f"use candidate_api::{fixture['namespace']} as api;\nfn main() {{\n"
             f"let initial = api::new([None; {component.max_records}], {fixture['capacity']});\n"
             f"let commands = [{','.join(commands)}];\n"
             f"let mut responses = [Response::Reserved(Ok(())); {len(commands)}];\n"
             "let _final_state = api::run_commands(initial, commands, &mut responses);\n"
             + "\n".join(assertions) + '\nprintln!("VKIT_REUSE_OK");\n}\n')
    directory.mkdir()
    (directory / "smoke.rs").write_text(smoke, encoding="utf-8")
    output = directory / "output"
    output.mkdir()
    output.chmod(0o777)
    command = ["/assets/verus/verus", "/trusted/smoke.rs", "--no-cheating", "--compile",
               "--import", "ledger=/proof/ledger.vir", "--extern", "ledger=/proof/libledger.rlib",
               "--import", "candidate_api=/proof/candidate_api.vir", "--extern", "candidate_api=/proof/libcandidate_api.rlib",
               "-L", "/proof", "-o", "/output/smoke"]
    options = [*container.sealed_options(), "--workdir", "/output",
               "--env", "RUSTUP_HOME=/assets/rustup", "--env", "CARGO_HOME=/assets/cargo",
               "--env", "PATH=/assets/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               *container.mount(directory, "/trusted", True), *container.mount(proof, "/proof", True),
               *container.mount(output, "/output", False)]
    for key, target in (("verus_root", "/assets/verus"), ("rustup_home", "/assets/rustup"), ("cargo_home", "/assets/cargo")):
        options += container.mount(Path(identity["paths"][key]), target, True)
    driver = "import subprocess,sys,json; subprocess.run(json.loads(sys.argv[1]),check=True); subprocess.run(['/output/smoke'],check=True)"
    result = container.run_container(image=identity["semantic"]["image_id"],
        run_id=f"{directory.name}-{uuid.uuid4().hex}", options=options, command=["python3", "-c", driver, json.dumps(command)],
        run_dir=directory, timeout_seconds=180, should_cancel=lambda: False)
    if result.exit.code != 0 or not result.removed or "VKIT_REUSE_OK" not in (directory / "container.stdout").read_text():
        raise RuntimeError(f"compiled API/data fixture failed: {fixture['id']}")
    return {"passed": True, "driver_sha256": evidence.sha256(directory / "smoke.rs"), "commands": len(commands)}


def qualify(directory, source, identity):
    if Path(verus.__file__).resolve().is_relative_to(ROOT / "src"):
        raise RuntimeError("reuse qualification requires the installed wheel, not an editable checkout")
    fixtures = json.loads((ROOT / "examples/ledger-reuse/fixtures.json").read_text())
    if len(fixtures) != 2 or {f["id"] for f in fixtures} != {"quota-admission", "worker-slot-reservations"}:
        raise RuntimeError("reuse qualification requires both named application fixtures")
    for fixture in fixtures:
        commands = fixture["commands"]
        if (len(commands) < 5 or not any("reserve" in c for c in commands) or not any("release" in c for c in commands)
                or not any("CapacityExceeded" in c["expected"] for c in commands)
                or not any("OwnerMismatch" in c["expected"] for c in commands)):
            raise RuntimeError("reuse fixture must exercise admission, capacity rejection, release and ownership rejection")
    reuse = []
    for fixture in fixtures:
        namespace = fixture["namespace"]
        component = replace(product_component(fixture["max_records"]), id=fixture["id"],
                            bindings=LedgerBindings(**{op: f"{namespace}::{op}" for op in OPERATIONS}))
        candidate = source.replace("verus! {", f"pub mod {namespace} {{\nuse super::*;\nverus! {{", 1) + "\n}\n"
        project, oid = project_for(directory / fixture["id"] / "project", component, candidate)
        store = Store.of(project)
        approve(store, component, identity)
        record = prepare_formal(project, VerificationRequest(component.id, GitCommit(oid)), component, canonical_digest(identity["semantic"]), store=store)
        record = execute(None, record["run_id"], store=store)
        run_dir = store.run_dir(record["run_id"])
        if record["outcome"]["result"] != "PASS" or not evidence.validate_record(record, run_dir):
            raise RuntimeError(f"reuse proof failed: {fixture['id']}: {record['outcome']}")
        runtime = runtime_fixture(fixture, component, run_dir / "formal-output", identity, directory / fixture["id"] / "runtime")
        reuse.append({"id": component.id, "candidate": oid, "package_sha256": component.package.sha256,
                      "receipt": record["receipt"], "runtime": runtime})

    component = product_component(3)
    project, oid = project_for(directory / "overhead/project", component, source)
    store = Store.of(project)
    approve(store, component, identity)
    captured = capture_git_commit(project, oid)
    if len(captured.files) > 100:
        raise RuntimeError("overhead fixture exceeds 100 captured files")
    pairs = []
    for i in range(5):
        pair = {}
        for mode in (("direct", "vkit") if i % 2 == 0 else ("vkit", "direct")):
            if mode == "direct":
                pair[mode] = direct(component, project.root, identity, directory / "overhead" / f"direct-{i}")
            else:
                started = time.monotonic()
                checker = store.approved()[component.digest]["checker_sha256"]
                record = prepare_formal(project, VerificationRequest(component.id, GitCommit(oid)), component, checker, store=store)
                record = execute(None, record["run_id"], store=store)
                wall = time.monotonic() - started
                run_dir = store.run_dir(record["run_id"])
                if record["outcome"]["result"] != "PASS" or not evidence.validate_record(record, run_dir):
                    raise RuntimeError("overhead job did not establish its full proof")
                pair[mode] = {"wall_seconds": wall, "phase_seconds": phase_seconds(run_dir / "formal-output"),
                              "run_id": record["run_id"], "receipt": record["receipt"]}
        pair["overhead_seconds"] = ((pair["vkit"]["wall_seconds"] - pair["vkit"]["phase_seconds"])
                                    - (pair["direct"]["wall_seconds"] - pair["direct"]["phase_seconds"]))
        pairs.append(pair)
    median = statistics.median(p["overhead_seconds"] for p in pairs)
    report = {"reuse": reuse, "package_sha256": component.package.sha256,
              "installed_module": str(Path(verus.__file__).resolve()),
              "overhead": {"pairs": pairs, "median_seconds": median, "target_seconds": 2.0, "passed": median <= 2.0}}
    from vkit.mcp import Server

    cli = json.loads(run([sys.executable, "-m", "vkit.cli", "verify", component.id,
                          "--candidate", oid, "--wait", "60", "--project", str(project.root), "--json"], timeout=900).stdout)
    mcp, error = Server(project.root).call("verify", {"component": component.id, "goal": {"kind": "implements"},
        "candidate": {"kind": "git_commit", "oid": oid}, "wait_seconds": 60})
    if error:
        raise RuntimeError(f"installed MCP verification failed: {mcp}")
    surface_records = [jobs.wait(store, r["summary"]["run_id"], 900) for r in (cli, mcp)]
    if (len({r["job_id"] for r in surface_records}) != 1
            or len({r["run_id"] for r in surface_records}) != 2
            or any(r["outcome"]["result"] != "PASS" or not evidence.validate_record(r, store.run_dir(r["run_id"])) for r in surface_records)):
        raise RuntimeError("installed CLI and MCP did not establish the same job in distinct attempts")
    report["surfaces"] = {"job_id": cli["summary"]["job_id"], "attempts": [r["run_id"] for r in surface_records],
                          "cli_and_mcp_passed": True}
    (directory / "reuse-qualification.json").write_text(json.dumps(report, indent=2) + "\n")
    if median > 2.0:
        raise RuntimeError(f"median VKit orchestration overhead exceeded two seconds: {median:.3f}")
    return report


if __name__ == "__main__":
    raise SystemExit("Run through the pinned ledger qualification workflow.")
