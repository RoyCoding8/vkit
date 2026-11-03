from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import NO_WINDOW, git
from vkit import paired as paired_module
from vkit.certification import IntentRevision
from vkit.paired import run_paired_execution
from vkit.evidence import policy_identity
from vkit.paired_profiles import ProgramPlan, identify_environment
from vkit.paths import open_project


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _git_output(root, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@vkit", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        **NO_WINDOW,
    )
    return result.stdout.strip()


def _tools(language: str):
    if language == "python":
        return None, Path(sys.executable).resolve()
    if language == "rust":
        raw = os.environ.get("VKIT_TEST_RUSTC")
        if raw is None:
            rustup = shutil.which("rustup")
            if rustup is not None:
                result = subprocess.run(
                    [rustup, "which", "rustc"], capture_output=True, text=True,
                    check=False, **NO_WINDOW,
                )
                if result.returncode == 0:
                    raw = result.stdout.strip()
            raw = raw or shutil.which("rustc")
        if raw is None or not Path(raw).is_file():
            pytest.skip("Rust compiler is unavailable; set VKIT_TEST_RUSTC to its executable")
        return Path(raw).resolve(), None
    compiler = os.environ.get("VKIT_TEST_TSC")
    runtime = shutil.which("node")
    if compiler is None or not Path(compiler).is_file() or runtime is None:
        pytest.skip("native TypeScript compiler or Node is unavailable; set VKIT_TEST_TSC")
    return Path(compiler).resolve(), Path(runtime).resolve()


def _binding_source(language: str, delta: int) -> tuple[str, str]:
    if language == "rust":
        source = f"pub fn convert(value: u32) -> u32 {{ value + {delta} }}\n"
        return "candidate.rs", source
    if language == "typescript":
        source = f"export function convert(value: number): number {{ return value + {delta}; }}\n"
        return "candidate.ts", source
    return "candidate.py", f"def convert(value):\n    return value + {delta}\n"


def test_runner_identity_covers_policy_source_map_and_each_added_module(
    tmp_path, monkeypatch,
):
    expected = {
        "certification.py", "intent.py", "paired.py", "paired_profiles.py",
        "witness.py", "nowindow.py", "proc.py", "inputs.py", "schemas.py",
    }
    actual = policy_identity()
    assert expected <= {Path(name).name for name in actual}

    build_root = tmp_path / "build"
    build_root.mkdir()
    driver = build_root / "driver.py"
    driver.write_text("pass\n", encoding="utf-8")
    plan = ProgramPlan(None, ("python",), build_root, (driver,))
    plans = (("base", plan, build_root),)
    for name in sorted(expected):
        first_map = {f"src/vkit/{name}": "a" * 64}
        second_map = {f"src/vkit/{name}": "b" * 64}
        monkeypatch.setattr(paired_module, "policy_identity", lambda value=first_map: value)
        first = json.loads(paired_module._runner_identity(plans))
        monkeypatch.setattr(paired_module, "policy_identity", lambda value=second_map: value)
        second = json.loads(paired_module._runner_identity(plans))
        assert first["implementation"] == first_map
        assert first != second


def _repository(
    tmp_path, *, language: str, candidate_delta: int, outputs: dict[str, list[str]],
    semantic_changes: dict[str, object] | None = None, candidate_source: str | None = None,
    wrong_tool_pin: bool = False,
):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q")

    compiler, runtime = _tools(language)
    measured = identify_environment(language, compiler=compiler, runtime=runtime)
    environment = json.loads(json.dumps(measured))
    if wrong_tool_pin:
        role = "runtime" if language == "python" else "compiler"
        environment["tools"][role]["sha256"] = "0" * 64
    semantic = {
        "binding": {
            "schema_version": 1,
            "kind": "u32_function",
            "language": language,
            "source": {"rust": "candidate.rs", "python": "candidate.py",
                       "typescript": "candidate.ts"}[language],
            "symbol": "convert",
        },
        "codec": {"schema_version": 1, "kind": "u32_le"},
        "observation": {"schema_version": 1, "kind": "u32_return"},
        "environment": environment,
    }
    if semantic_changes:
        semantic.update(semantic_changes)
    raw_artifacts = {role: _json_bytes(value) for role, value in semantic.items()}
    hashes = {role: _sha(raw) for role, raw in raw_artifacts.items()}
    revision_raw = {
        "schema_version": 1,
        "requirement": {
            "source": "requirements.md#conversion",
            "statement": "The conversion may return any listed value for each input.",
        },
        "contract": {
            "domain": {
                "binding": "example::convert",
                "inputs": ["0", "1"],
                "observations": ["0", "1", "2", "3"],
                "codec_sha256": hashes["codec"],
                "observation_sha256": hashes["observation"],
                "environment_sha256": hashes["environment"],
            },
            "admissible_inputs": ["0", "1"],
            "allowed_outputs": [
                {"input": token, "outputs": values}
                for token, values in sorted(outputs.items())
            ],
        },
        "artifacts": {
            role: {
                "path": f"verification/semantics/{role}.json",
                "sha256": hashes[role],
            }
            for role in ("binding", "codec", "observation", "environment")
        },
    }
    revision = IntentRevision.from_json(revision_raw)
    for role, content in raw_artifacts.items():
        path = root / f"verification/semantics/{role}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    revision_path = root / f"verification/change-intents/{revision.digest}.v1.json"
    revision_path.parent.mkdir(parents=True, exist_ok=True)
    revision_path.write_bytes(_json_bytes(revision.to_json()))
    (root / "verification/change-intents/accepted.v1.json").write_bytes(
        _json_bytes({"schema_version": 1, "accepted": [revision.digest]})
    )
    source_name, base_source = _binding_source(language, 0)
    source = root / source_name
    source.write_text(base_source, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "accepted target")
    target_oid = _git_output(root, "rev-parse", "HEAD")

    _, generated_candidate = _binding_source(language, candidate_delta)
    generated_candidate += {
        "rust": "// candidate revision\n",
        "python": "# candidate revision\n",
        "typescript": "// candidate revision\n",
    }[language]
    source.write_text(candidate_source if candidate_source is not None else generated_candidate,
                       encoding="utf-8")
    git(root, "add", source_name)
    git(root, "commit", "-qm", "candidate")
    candidate_oid = _git_output(root, "rev-parse", "HEAD")
    request = {
        "schema_version": 1,
        "base_oid": target_oid,
        "candidate_oid": candidate_oid,
        "intent_sha256": revision.digest,
        "claim": {"kind": "finite_contract", "contract": revision.contract.to_json()},
    }
    return root, target_oid, request


@pytest.mark.parametrize("language", ["rust", "python", "typescript"])
@pytest.mark.parametrize("delta,expected_status,expected_different", [
    (0, "samples_within_contract", False),
    (1, "samples_within_contract", True),
    (2, "observed_violation", True),
])
def test_paired_execution_runs_native_profiles_and_records_samples(
    tmp_path, language, delta, expected_status, expected_different
):
    root, target_oid, request = _repository(
        tmp_path, language=language, candidate_delta=delta,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )

    result = run_paired_execution(
        open_project(root),
        request,
        trusted_target_oid=target_oid,
        output_dir=tmp_path / "paired",
        input_tokens=("1",),
        repetitions=2,
        max_shrink_trials=0,
    )

    assert result.status == expected_status, result.report["failures"]
    assert result.binding is not None
    sample = result.report["samples"][0]
    assert sample["stable"] is True
    assert sample["different"] is expected_different
    assert sample["candidate_within_contract"] is (expected_status != "observed_violation")
    assert sample["in_observation_domain"] is True
    assert result.report["source_proof"] is False
    assert result.report["dependency_closure_checked"] is False
    assert result.report["os_sandbox"] is False
    assert len(sample["runs"]) == 2
    assert [run["base"]["value"] for run in sample["runs"]] == [1, 1]
    assert [run["candidate"]["value"] for run in sample["runs"]] == [1 + delta] * 2
    assert result.report["input_selection"] == {
        "admissible_count": 2,
        "selected": ["1"],
        "omitted_count": 1,
    }
    assert result.report["resource_limits"] == {
        "max_process_invocations": 128,
        "max_output_bytes_per_process": 65536,
        "max_total_process_output_bytes": 8388608,
        "wall_deadline_seconds": 300,
    }
    assert result.report["resource_usage"]["process_invocations"] == 6
    assert result.report["resource_usage"]["process_output_bytes"] <= 6 * 65536
    assert result.report["resource_usage"]["elapsed_seconds"] <= 300
    retained = json.loads((tmp_path / "paired" / "paired-report.json").read_text(encoding="utf-8"))
    assert retained == result.report


@pytest.mark.parametrize("language", ["rust", "python", "typescript"])
def test_native_profiles_reject_non_u32_results(tmp_path, language):
    wrong_types = {
        "rust": "pub fn convert(value: u32) -> i64 { value as i64 }\n",
        "python": "def convert(value):\n    return True\n",
        "typescript": "export function convert(value: number): unknown { return '1'; }\n",
    }
    root, target_oid, request = _repository(
        tmp_path,
        language=language,
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
        candidate_source=wrong_types[language],
    )

    result = run_paired_execution(
        open_project(root), request, trusted_target_oid=target_oid,
        output_dir=tmp_path / "paired", input_tokens=("1",), repetitions=1,
        max_shrink_trials=0,
    )

    assert result.status == "infrastructure_failure"
    assert result.report["failures"][-1]["reason"] == "exit_code"


def test_paired_execution_shrinks_only_within_admissible_inputs(tmp_path):
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=2,
        outputs={"0": ["0"], "1": ["1"]},
    )

    result = run_paired_execution(
        open_project(root),
        request,
        trusted_target_oid=target_oid,
        output_dir=tmp_path / "paired",
        input_tokens=("1",),
        repetitions=3,
        max_shrink_trials=2,
    )

    assert result.status == "observed_violation"
    assert result.report["shrinking"] == {
        "limit": 2,
        "seed_input": "1",
        "reduced_input": "0",
        "attempted_inputs": [{"input": "0", "reused": False}],
        "complete": True,
    }
    assert [sample["input"] for sample in result.report["samples"]] == ["1", "0"]
    assert [run["candidate"]["value"] for run in result.report["samples"][0]["runs"]] == [3, 3, 3]
    assert [run["candidate"]["value"] for run in result.report["samples"][1]["runs"]] == [2, 2, 2]
    retained = json.loads((tmp_path / "paired" / "paired-report.json").read_bytes())
    assert retained["shrinking"]["reduced_input"] == "0"
    replay = run_paired_execution(
        open_project(root), request, trusted_target_oid=target_oid,
        output_dir=tmp_path / "rechecked", input_tokens=(retained["shrinking"]["reduced_input"],),
        repetitions=3, max_shrink_trials=0,
    )
    assert replay.status == "observed_violation"
    assert [row["base"]["value"] for row in replay.report["samples"][0]["runs"]] == [0, 0, 0]
    assert [row["candidate"]["value"] for row in replay.report["samples"][0]["runs"]] == [2, 2, 2]
    assert replay.binding.base_capture_sha256 == result.binding.base_capture_sha256
    assert replay.binding.candidate_capture_sha256 == result.binding.candidate_capture_sha256


def _run_python_case(tmp_path, *, candidate_source=None, semantic_changes=None,
                     wrong_tool_pin=False, timeout_seconds=3, should_cancel=lambda: False):
    root, target_oid, request = _repository(
        tmp_path,
        language="python",
        candidate_delta=0,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
        semantic_changes=semantic_changes,
        candidate_source=candidate_source,
        wrong_tool_pin=wrong_tool_pin,
    )
    return run_paired_execution(
        open_project(root), request, trusted_target_oid=target_oid,
        output_dir=tmp_path / "paired", input_tokens=("1",), repetitions=2,
        timeout_seconds=timeout_seconds, max_shrink_trials=0, should_cancel=should_cancel,
    )


def test_paired_execution_retains_build_failure(tmp_path):
    result = _run_python_case(tmp_path, candidate_source="def convert(value):\n    return (\n")

    assert result.status == "infrastructure_failure"
    assert result.report["builds"][-1]["state"] == "failed"
    assert result.report["failures"][-1]["reason"] == "exit_code"
    assert result.report["builds"][-1]["stderr"] is not None


def test_paired_execution_retains_malformed_stdout(tmp_path):
    result = _run_python_case(
        tmp_path,
        candidate_source="import sys\nsys.stdout.write('noise\\n')\ndef convert(value):\n    return value\n",
    )

    assert result.status == "infrastructure_failure"
    assert result.report["failures"][-1]["reason"] == "malformed_output"
    assert result.report["failures"][-1]["artifacts"][0]["path"].endswith("candidate-1-1.stdout")


def test_paired_execution_rejects_wrong_semantic_and_source_pins(tmp_path):
    semantic = {"observation": {"schema_version": True, "kind": "u32_return"}}
    wrong_semantic = _run_python_case(tmp_path / "semantic", semantic_changes=semantic)
    assert wrong_semantic.status == "unknown"
    assert wrong_semantic.report["failures"][-1]["reason"] == "unsupported_declaration"

    binding = {
        "schema_version": 1, "kind": "u32_function", "language": "python",
        "source": "missing.py", "symbol": "convert",
    }
    wrong_source = _run_python_case(
        tmp_path / "source", semantic_changes={"binding": binding}
    )
    assert wrong_source.status == "unknown"
    assert wrong_source.report["failures"][-1]["reason"] == "unsupported_program"


def test_paired_execution_rejects_wrong_tool_pin(tmp_path):
    result = _run_python_case(tmp_path, wrong_tool_pin=True)

    assert result.status == "infrastructure_failure"
    assert result.report["failures"][-1]["reason"] == "tool_pin_mismatch"


def test_paired_execution_reports_timeout_and_cancellation_as_unknown(tmp_path):
    infinite = "def convert(value):\n    while True:\n        pass\n"
    timed_out = _run_python_case(
        tmp_path / "timeout", candidate_source=infinite, timeout_seconds=0.25
    )
    assert timed_out.status == "unknown"
    assert timed_out.report["failures"][-1]["reason"] == "timeout"

    cancelled = _run_python_case(
        tmp_path / "cancelled", candidate_source=infinite, should_cancel=lambda: True
    )
    assert cancelled.status == "unknown"
    assert cancelled.report["failures"][-1]["reason"] == "cancelled"


def test_paired_execution_marks_unstable_repetitions_unknown(tmp_path):
    state_dir = tmp_path / "external-state"
    state_dir.mkdir()
    source = (
        "from pathlib import Path\n"
        f"state_dir = Path({str(state_dir)!r})\n"
        "state = state_dir / (Path(__file__).parents[1].name + '.txt')\n"
        "def convert(value):\n"
        "    count = int(state.read_text()) if state.exists() else 0\n"
        "    state.write_text(str(count + 1))\n"
        "    return count % 2\n"
    )
    result = _run_python_case(tmp_path, candidate_source=source)

    assert result.status == "unknown"
    assert result.report["failures"][-1]["reason"] == "unstable"
    assert result.report["samples"][0]["stable"] is False


def test_paired_execution_process_budget_counts_builds_and_refuses_more(tmp_path, monkeypatch):
    monkeypatch.setattr(paired_module, "_MAX_PROCESS_INVOCATIONS", 2)
    result = _run_python_case(tmp_path)

    assert result.status == "unknown"
    assert result.report["resource_limits"]["max_process_invocations"] == 2
    assert result.report["resource_usage"]["process_invocations"] == 2
    assert result.report["failures"][-1]["reason"] == "process_budget_exhausted"


def test_paired_execution_wall_deadline_expires_before_build(tmp_path, monkeypatch):
    root, target_oid, request = _repository(
        tmp_path, language="python", candidate_delta=1,
        outputs={"0": ["0", "1"], "1": ["1", "2"]},
    )
    clock = iter((10.0, 11.0, 11.0, 11.0))
    monkeypatch.setattr(paired_module, "_MAX_RUN_SECONDS", 1)
    monkeypatch.setattr(paired_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    result = run_paired_execution(
        open_project(root), request, trusted_target_oid=target_oid,
        output_dir=tmp_path / "paired", input_tokens=("1",), repetitions=1,
    )

    assert result.status == "unknown"
    assert result.report["resource_usage"]["process_invocations"] == 0
    assert result.report["failures"][-1]["reason"] == "wall_deadline"


def test_paired_execution_rejects_candidate_tampering_with_retained_artifacts(tmp_path):
    source = (
        "from pathlib import Path\n"
        "root = Path(__file__).parents[2]\n"
        "(root / 'inputs' / '1.u32le').write_bytes(b'\\x02\\x00\\x00\\x00')\n"
        "(root / 'commands' / 'base-1-1.stdout').write_bytes(b'999\\n')\n"
        "(root / 'semantics' / 'codec.json').write_bytes(b'{}\\n')\n"
        "def convert(value):\n"
        "    return value\n"
    )
    result = _run_python_case(tmp_path, candidate_source=source)
    output_root = tmp_path / "paired"
    mismatches = [
        ref["path"] for ref in result.report["artifacts"]
        if _sha((output_root / ref["path"]).read_bytes()) != ref["sha256"]
    ]

    assert result.status == "infrastructure_failure"
    assert "inputs/1.u32le" in mismatches
    assert "commands/base-1-1.stdout" in mismatches
    assert "semantics/codec.json" in mismatches
    assert result.report["failures"][-1]["reason"] == "retained_artifact_mismatch"
    assert result.report["samples"][0]["runs"][0]["base"]["value"] == 1
    assert result.report["samples"][0]["runs"][0]["candidate"]["value"] == 1


def test_paired_python_reads_input_before_candidate_import(tmp_path):
    source = (
        "from pathlib import Path\n"
        "root = Path(__file__).parents[2]\n"
        "input_path = root / 'inputs' / '1.u32le'\n"
        "original_input = input_path.read_bytes()\n"
        "input_path.write_bytes(b'\\x02\\x00\\x00\\x00')\n"
        "def convert(value):\n"
        "    input_path.write_bytes(original_input)\n"
        "    return value\n"
    )
    result = _run_python_case(tmp_path, candidate_source=source)

    assert result.status == "samples_within_contract"
    sample = result.report["samples"][0]
    assert sample["different"] is False
    assert [run["candidate"]["value"] for run in sample["runs"]] == [1, 1]
