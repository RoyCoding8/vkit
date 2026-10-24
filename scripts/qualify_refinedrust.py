"""Run the scalar RefinedRust probe in the pinned Linux environment.

A successful probe is not full Q0 qualification. The report keeps the remaining
proof, statement-binding, coverage and negative-fixture gates explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "refinedrust-qualification"


def nix_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False).replace("${", "\\${")


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
        "scalar_probe": "NOT_RUN",
        "remaining_gates": [
            "independent full proof checking and transitive assumption audit",
            "trusted expected statement and source-body binding",
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
        command = [
            *nix,
            "develop", "--impure", "--expr",
            "import " + nix_string(work / "environment.nix") + " " + arguments,
            "--max-jobs", "1", "--cores", "2",
            "--command", "bash", "-euc",
            "rustc --version\nrocq --version\ncargo refinedrust --show-config\n"
            "cargo refinedrust -- --locked\ndune build --root output\n",
        ]
        report["command"] = command
        report["scalar_probe"] = "STARTED"
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print("Build the pinned proof environment and check the scalar body", flush=True)
        started = time.monotonic()
        with (output / "scalar.log").open("w", encoding="utf-8") as log:
            with subprocess.Popen(command, cwd=work, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True) as process:
                assert process.stdout is not None
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    print(line, end="", flush=True)
                process.wait()
        report["scalar_elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["returncode"] = process.returncode
        report["scalar_probe"] = "BLOCKED"
        if process.returncode == 0:
            proofs = [p for p in (work / "output").rglob("proof_reserve.vo") if not p.is_symlink()]
            if len(proofs) != 1:
                raise ValueError(f"expected one compiled reserve body proof, found {len(proofs)}")
            report["body_proof"] = {"path": proofs[0].relative_to(work).as_posix(),
                                    "sha256": hashlib.sha256(proofs[0].read_bytes()).hexdigest()}
            report["scalar_probe"] = "COMPILED"
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"qualification": report["qualification"], "scalar_probe": report["scalar_probe"],
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
