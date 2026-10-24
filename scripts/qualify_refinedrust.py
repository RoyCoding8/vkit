"""Run the scalar RefinedRust probe in the pinned Linux environment.

A successful probe is not full Q0 qualification. The report keeps the remaining
proof, statement-binding, coverage and negative-fixture gates explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "refinedrust-qualification"
QUALIFICATION_AXIOMS = {
    "Stdlib.Logic.ProofIrrelevance.proof_irrelevance",
    "Stdlib.Logic.FunctionalExtensionality.functional_extensionality_dep",
}


def nix_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False).replace("${", "\\${")


def run_command(command: list[str], cwd: Path, log_path: Path) -> dict:
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) as process:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            process.wait()
    return {"returncode": process.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3)}


def read_axioms(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    if lines == ["Closed under the global context"]:
        return {}
    if not lines or lines.pop(0) != "Axioms:":
        raise ValueError("unrecognized theorem-assumption report")
    axioms = {}
    current = None
    for line in lines:
        if not line.strip():
            continue
        if line[0].isspace():
            if current is None:
                raise ValueError("assumption type without a qualified name")
            axioms[current] += "\n" + line
        else:
            name, separator, type_display = line.partition(" :")
            if (not separator or name in axioms
                    or not re.fullmatch(r"[A-Za-z_][\w']*(?:\.[A-Za-z_][\w']*)+", name, flags=re.ASCII)):
                raise ValueError(f"unrecognized assumption entry: {line}")
            current = name
            axioms[name] = type_display.strip()
    if not axioms or any(not value.strip() for value in axioms.values()):
        raise ValueError("empty assumption declaration")
    return {name: {"display_type": value,
                   "display_type_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
            for name, value in axioms.items()}


def read_checker_safety(path: Path) -> dict:
    observed = []
    unsafe_fixpoints = []
    section = None
    unexpected_content = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("* "):
            observed.append(line)
            section = line
        elif section is not None and section != "* Axioms:" and line.strip():
            unexpected_content = True
            if section == "* Constants/Inductives relying on unsafe (co)fixpoints:":
                unsafe_fixpoints.append(line.strip())
    expected = [
        "* Theory: Set is predicative",
        "* Theory: Rewrite rules are not allowed",
        "* Axioms:",
        "* Constants/Inductives relying on type-in-type: <none>",
        "* Constants/Inductives relying on unsafe (co)fixpoints: <none>",
        "* Inductives whose positivity is assumed: <none>",
    ]
    return {"headers": observed, "unsafe_fixpoints": unsafe_fixpoints,
            "safe": observed == expected and not unexpected_content}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory for sources and logs.")
    args = parser.parse_args()
    upstream = args.upstream.resolve()
    output = args.output.resolve()
    pin = json.loads((FIXTURE / "upstream.json").read_text(encoding="utf-8"))
    report = {
        "qualification": "INCONCLUSIVE",
        "upstream": pin,
        "stages": [],
        "remaining_gates": [
            "transitive theorem assumption audit and trusted source-body binding",
            "rejection of wrong bodies, proof holes and verification bypasses",
            "collection support and complete compiler-backed source inventory",
            "two-version namespace and proof composition",
        ],
    }
    try:
        revision = subprocess.check_output(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
        ).strip()
        if revision != pin["revision"]:
            raise ValueError(f"upstream revision mismatch: {revision}")
        dirty = subprocess.check_output(
            ["git", "-C", str(upstream), "status", "--porcelain", "--untracked-files=all"],
            text=True,
        )
        if dirty:
            raise ValueError("upstream checkout must be clean")
        observed = hashlib.sha256((upstream / "flake.lock").read_bytes()).hexdigest()
        if observed != pin["flake_lock_sha256"]:
            raise ValueError(f"upstream lock digest mismatch: {observed}")
        if sys.platform != "linux":
            raise ValueError("run this probe inside Linux with Nix installed")
        if output.is_relative_to(upstream) or output.is_relative_to(FIXTURE):
            raise ValueError("output must be outside the upstream checkout and fixture")
        output.mkdir(parents=True, exist_ok=False)
        work = output / "source"
        shutil.copytree(FIXTURE, work, ignore=shutil.ignore_patterns("output", "target"))
        report["fixture_sha256"] = {
            p.relative_to(work).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(work.rglob("*"))
            if p.is_file()
        }
        nix = ["nix", "--extra-experimental-features", "nix-command flakes"]
        arguments = "{ upstreamPath = " + nix_string(upstream) + "; }"
        print("Resolve pinned library source identities", flush=True)
        with (output / "sources.log").open("w", encoding="utf-8") as log:
            sources = json.loads(subprocess.check_output(
                nix + ["eval", "--impure", "--json", "--expr",
                       "import " + nix_string(work / "sources.nix") + " " + arguments],
                text=True, stderr=log,
            ))
            report["source_inputs"] = sources
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            for source in sources:
                print(f"Capture {source['name']} at {source['revision']}", flush=True)
                parsed = urlsplit(source["url"])
                repository, separator, _ = parsed.path.partition("/-/archive/")
                if parsed.hostname != "gitlab.mpi-sws.org" or not separator:
                    raise ValueError(f"unsupported source archive: {source['url']}")
                nar_hash = subprocess.check_output(
                    nix + ["hash", "convert", "--hash-algo", "sha256", "--to", "sri", source["hash"]],
                    text=True, stderr=log,
                ).strip()
                tree = {"type": "git", "url": f"https://{parsed.hostname}{repository}.git",
                        "rev": source["revision"], "narHash": nar_hash}
                expression = "(builtins.fetchTree (builtins.fromJSON " + nix_string(json.dumps(tree)) + ")).outPath"
                captured = subprocess.check_output(
                    nix + ["eval", "--impure", "--raw", "--expr", expression], text=True, stderr=log,
                ).strip()
                if captured != source["path"]:
                    raise ValueError(f"source store path mismatch for {source['name']}: {captured}")
                print(f"Captured {source['name']} at {source['revision']}", file=log, flush=True)
                print(f"Verified source identity for {source['name']}", flush=True)
        environment = [
            *nix,
            "develop", "--impure", "--expr",
            "import " + nix_string(work / "environment.nix") + " " + arguments,
            "--inputs-from", str(upstream),
            "--print-build-logs",
            "--max-jobs", "1", "--cores", "2",
            "--command", "bash", "-euc",
        ]
        loadpath = (
            "-Q _build/default/vkit_qualification refinedrust.examples.vkit_qualification "
            "-Q ../audit VKitQualification"
        )
        contract_script = (f"cd output\ndune exec -- rocq compile {loadpath} ../audit/Contract.v\n"
                           f"dune exec -- rocq compile {loadpath} ../audit/Expected.v\n")
        stages = (
            ("body", "rustc --version\nrocq --version\ncargo refinedrust --show-config\n"
             "cargo refinedrust -- --locked\ndune build --root output\n",
             "output/**/proof_reserve.vo"),
            ("contract", contract_script,
             "audit/Expected.vo"),
            ("replay", f"cd output\ndune exec -- rocq check -silent -o {loadpath} VKitQualification.Expected\n",
             None),
        )
        for name, script, artifact in stages:
            stage = {"name": name, "command": [*environment, script], "state": "STARTED"}
            report["stages"].append(stage)
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"Run {name} stage", flush=True)
            stage.update(run_command(stage["command"], work, output / f"{name}.log"))
            stage["state"] = "FAILED"
            if stage["returncode"] == 0:
                if artifact is not None:
                    proofs = [p for p in work.glob(artifact) if p.is_file() and not p.is_symlink()]
                    if len(proofs) != 1:
                        raise ValueError(f"expected one {name} proof artifact, found {len(proofs)}")
                    stage["artifact"] = {"path": proofs[0].relative_to(work).as_posix(),
                                         "sha256": hashlib.sha256(proofs[0].read_bytes()).hexdigest()}
                if name == "body":
                    inventory = work / "output/vkit_qualification/source-inventory.json"
                    census = json.loads(inventory.read_text(encoding="utf-8"))
                    if (census["schema_version"] != 1
                            or census["scope"] != "active_cfg_local_callable_hir"
                            or census["complete"] is not True):
                        raise ValueError("incomplete or incompatible compiler census")
                    expected = {"vkit_qualification::reserve", "vkit_qualification::private_helper",
                                "vkit_qualification::inner::exposed"}
                    paths = {item["path"] for item in census["items"]}
                    if paths != expected:
                        raise ValueError(f"unexpected compiler callable census: {sorted(paths)}")
                    outcomes = {item["path"]: (item["mode"], item["translation"])
                                for item in census["items"]}
                    if outcomes != {
                        "vkit_qualification::reserve": ("prove", "body_generated"),
                        "vkit_qualification::private_helper": (None, "not_registered"),
                        "vkit_qualification::inner::exposed": (None, "not_registered"),
                    }:
                        raise ValueError(f"unexpected compiler translation outcomes: {outcomes}")
                    if not any(entry["path"] == "vkit_qualification::exported_alias"
                               and entry["target_path"] == "vkit_qualification::inner::exposed"
                               for entry in census["module_paths"]):
                        raise ValueError("compiler census missed the reexport target")
                    stage["source_inventory"] = {
                        "path": inventory.relative_to(work).as_posix(),
                        "sha256": hashlib.sha256(inventory.read_bytes()).hexdigest(),
                        "scope": census["scope"],
                        "items": census["items"],
                    }
                if name == "contract":
                    axioms = read_axioms(work / "audit/assumptions.out")
                    stage["assumption_audit"] = {
                        "scope": "QUALIFICATION_ONLY",
                        "observed": axioms,
                        "unexpected": sorted(axioms.keys() - QUALIFICATION_AXIOMS),
                        "product_policy_accepted": False,
                    }
                    if axioms.keys() != QUALIFICATION_AXIOMS:
                        raise ValueError("the scalar theorem's foundational assumptions changed")
                if name == "replay":
                    stage["safety_audit"] = read_checker_safety(output / "replay.log")
                    if not stage["safety_audit"]["safe"]:
                        raise ValueError("recursive checker did not report the required safe typing flags")
                stage["state"] = "COMPLETED"
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            if stage["state"] != "COMPLETED":
                break
        if all(stage["state"] == "COMPLETED" for stage in report["stages"]):
            report["negative_probes"] = []
            for case in ("wrong_body", "weakened_contract", "shadowed_contract", "admitted_body",
                         "unsafe_body", "trust_me", "only_spec"):
                case_root = output / "negative" / case
                case_work = case_root / "source"
                shutil.copytree(FIXTURE, case_work, ignore=shutil.ignore_patterns("output", "target"))
                source = case_work / "src/lib.rs"
                config = case_work / "RefinedRust.toml"
                replacements = []
                if case in {"wrong_body", "weakened_contract", "shadowed_contract", "unsafe_body"}:
                    replacements.append((source, "\n    used + amount\n", "\n    used\n"))
                if case == "weakened_contract":
                    replacements.append((source,
                                         '#[rr::returns("if decide (0 < a ∧ a ≤ c - u) then u + a else u")]',
                                         '#[rr::returns("u")]'))
                if case == "admitted_body":
                    replacements.append((config, "admit_proofs = false", "admit_proofs = true"))
                if case in {"trust_me", "only_spec"}:
                    replacements.append((source, "pub fn reserve(", f"#[rr::{case}]\npub fn reserve("))
                for path, old, new in replacements:
                    content = path.read_text(encoding="utf-8")
                    if content.count(old) != 1:
                        raise ValueError(f"ambiguous {case} mutation in {path.name}")
                    path.write_text(content.replace(old, new), encoding="utf-8")
                probe = {"name": case, "state": "INCONCLUSIVE", "stages": [],
                         "input_sha256": {p.relative_to(case_work).as_posix():
                                          hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in (source, config)}}
                report["negative_probes"].append(probe)
                case_stages = [("generation", "cargo refinedrust -- --locked\n")]
                if case not in {"trust_me", "only_spec"}:
                    case_stages.append(("body", "dune build --root output\n"))
                if case == "shadowed_contract":
                    case_stages.append(("vulnerable_control",
                                        f"cd output\ndune exec -- rocq compile {loadpath} ../audit/Vulnerable.v\n"))
                if case in {"weakened_contract", "shadowed_contract", "admitted_body", "unsafe_body"}:
                    case_stages.append(("contract", contract_script))
                if case == "unsafe_body":
                    case_stages.append(("replay", stages[2][1]))
                for name, script in case_stages:
                    command = ["timeout", "--kill-after=10s", "600s", *environment, script]
                    step = {"name": name, "command": command, "state": "STARTED"}
                    probe["stages"].append(step)
                    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                    print(f"Run {case}/{name}", flush=True)
                    log_path = case_root / f"{name}.log"
                    step.update(run_command(command, case_work, log_path))
                    step["state"] = "COMPLETED" if step["returncode"] == 0 else "FAILED"
                    if step["returncode"] in {124, 137}:
                        probe["state"] = "TIMED_OUT"
                        break
                    if step["returncode"] in {125, 126, 127}:
                        probe["state"] = "UNEXPECTED_FAILURE"
                        break
                    log = log_path.read_text(encoding="utf-8")
                    if name == "generation" and case in {"trust_me", "only_spec"}:
                        diagnostic = f"No assumption is allowed, but this function is annotated with `#[rr::{case}]`"
                        if step["returncode"] == 0:
                            probe["state"] = "NOT_REJECTED"
                        else:
                            errors = [line for line in log.splitlines() if line.startswith("error:")]
                            rejected = (step["returncode"] == 101 and len(errors) == 2
                                        and errors[0] == f"error: [RefinedRust] {diagnostic}"
                                        and errors[1].startswith("error: could not compile `vkit-qualification`")
                                        and "due to 1 previous error" in errors[1]
                                        and "pub fn reserve(" in log)
                            probe["state"] = "EXPECTED_REJECTION" if rejected else "UNEXPECTED_FAILURE"
                        break
                    if step["returncode"] != 0:
                        if case == "wrong_body" and name == "body":
                            rejected = (step["returncode"] == 1
                                        and 'File "./vkit_qualification/proofs/proof_reserve.v"' in log
                                        and "(in proof reserve_proof): Attempt to save an incomplete proof" in log)
                            probe["state"] = "EXPECTED_REJECTION" if rejected else "UNEXPECTED_FAILURE"
                        elif case in {"weakened_contract", "shadowed_contract"} and name == "contract":
                            probe["state"] = ("EXPECTED_REJECTION" if step["returncode"] == 1
                                              and "../audit/Expected.v" in log
                                              and "while it is expected to have type" in log
                                              and "reserve_proof" in log
                                              else "UNEXPECTED_FAILURE")
                        else:
                            probe["state"] = "UNEXPECTED_FAILURE"
                        break
                    if case == "admitted_body" and name == "generation":
                        proof = case_work / "output/vkit_qualification/proofs/proof_reserve.v"
                        if "Admitted. (* admitted due to admit_proofs config flag *)" not in proof.read_text(encoding="utf-8"):
                            raise ValueError("admitted-body mutation did not generate the intended proof hole")
                    if case == "unsafe_body" and name == "generation":
                        proof = case_work / "output/vkit_qualification/proofs/proof_reserve.v"
                        statement, separator, _ = proof.read_text(encoding="utf-8").partition("Proof.")
                        if not separator or statement.count("Lemma reserve_proof") != 1:
                            raise ValueError("unsafe probe no longer targets one generated proof")
                        statement = statement.replace("Lemma reserve_proof", (
                            "Unset Guard Checking.\n"
                            "Fixpoint false_proof (n : nat) : False := false_proof n.\n"
                            "Set Guard Checking.\n\nLemma reserve_proof"))
                        proof.write_text(statement + "Proof. exact (False_rect _ (false_proof 0)). Qed.\n"
                                         "End proof.\n", encoding="utf-8")
                        probe["mutated_proof_sha256"] = hashlib.sha256(proof.read_bytes()).hexdigest()
                    if case == "shadowed_contract" and name == "generation":
                        proof = case_work / "output/vkit_qualification/proofs/proof_reserve.v"
                        imports = proof.read_text(encoding="utf-8").split("Section proof.")[0]
                        expected = (case_work / "audit/Contract.v").read_text(encoding="utf-8")
                        definitions = expected[expected.index("Definition reserve_result"):expected.index("End contract.")]
                        if definitions.count("refinedrust.primitive.functions.typed_function") != 1:
                            raise ValueError("shadow probe no longer targets one verification judgment")
                        definitions = definitions.replace("refinedrust.primitive.functions.typed_function", "typed_function")
                        weak_proof = ('Lemma reserve_proof (π : thread_id) : expected_body π.\n'
                                      'Proof. intros panic_shim_loc Hlayout. iIntros "_". done. Qed.\n'
                                      'End contract.\n')
                        shadow = ("Definition typed_function := ltac:(\n"
                                  "  let T := type of @refinedrust.primitive.functions.typed_function in\n"
                                  "  refine (_ : T); intros; exact True%I).\n"
                                  "Arguments typed_function {Σ RRGS lfts rts} π fn fp.\n\n")
                        proof.write_text(imports + shadow + definitions + weak_proof, encoding="utf-8")
                        probe["mutated_proof_sha256"] = hashlib.sha256(proof.read_bytes()).hexdigest()
                        vulnerable = (imports
                                      + "From refinedrust.examples.vkit_qualification.proofs Require Import proof_reserve.\n"
                                      + definitions
                                      + "Theorem reserve_expected (π : thread_id) : expected_body π.\n"
                                      + "Proof. exact (reserve_proof π). Qed.\nEnd contract.\n")
                        (case_work / "audit/Vulnerable.v").write_text(vulnerable, encoding="utf-8")
                    if case == "shadowed_contract" and name == "vulnerable_control":
                        probe["vulnerable_control_accepted"] = True
                    if case == "admitted_body" and name == "contract":
                        axioms = read_axioms(case_work / "audit/assumptions.out")
                        probe["observed_axioms"] = sorted(axioms)
                        proof_hole = "refinedrust.examples.vkit_qualification.proofs.proof_reserve.reserve_proof"
                        probe["state"] = ("PROOF_HOLE_DETECTED" if proof_hole in axioms
                                          and proof_hole not in QUALIFICATION_AXIOMS else "NOT_REJECTED")
                    if case == "unsafe_body" and name == "replay":
                        audit = read_checker_safety(log_path)
                        probe["safety_audit"] = audit
                        unsafe = "refinedrust.examples.vkit_qualification.proofs.proof_reserve.false_proof"
                        probe["state"] = ("UNSAFE_PROOF_DETECTED" if not audit["safe"]
                                          and unsafe in audit["unsafe_fixpoints"] else "UNEXPECTED_FAILURE")
                if probe["state"] == "INCONCLUSIVE":
                    probe["state"] = "NOT_REJECTED"
                (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"qualification": report["qualification"],
                          "stages": [{"name": s["name"], "state": s["state"]} for s in report["stages"]],
                          "negative_probes": [{"name": p["name"], "state": p["state"]}
                                              for p in report.get("negative_probes", [])],
                          "report": str(output / "report.json")}))
        return 3
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        if "fixture_sha256" in report:
            report["error"] = str(error)
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Qualification blocked: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
