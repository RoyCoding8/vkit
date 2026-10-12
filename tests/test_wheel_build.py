from __future__ import annotations

import base64
import csv
import io
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from vkit.manifest import parse_manifest
from vkit.paths import open_project
from vkit.runner import run_check
from vkit.store import Store
from helpers import git, vkit


def _uv() -> tuple[Path, str, str] | None:
    executable = os.environ.get("VKIT_TEST_UV") or shutil.which("uv")
    if not executable:
        return None
    path = Path(executable).resolve()
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    pieces = result.stdout.strip().split()
    if result.returncode or len(pieces) < 2 or pieces[0] != "uv":
        return None
    return path, pieces[1], hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_check(
    uv: tuple[Path, str, str],
    *,
    backend: str = "uv_build",
    expected_stdout: str | None = None,
) -> dict:
    executable, version, digest = uv
    backend_decl = 'requires = ["uv_build>=0.9,<1"]\nbuild-backend = "uv_build"'
    if backend == "hatchling":
        backend_decl = 'requires = ["hatchling"]\nbuild-backend = "hatchling.build"'
    elif backend == "fixture":
        backend_decl = 'requires = ["fixture-backend==1.0"]\nbuild-backend = "fixture_backend"'
    return {
        "id": "release-wheel",
        "kind": "build",
        "timeout_seconds": 90,
        "artifact": "build.json",
        "inputs": ["pyproject.toml", "src", "build-constraints.txt", "runtime.lock", "wheelhouse"],
        "subject": {"paths": ["src"], "digest": None},
        "claim_id": "release-wheel",
        "build": {
            "uv_executable": str(executable),
            "uv_version": version,
            "uv_sha256": digest,
            "python_version": platform.python_version(),
            "python_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
            "build_constraints": "build-constraints.txt",
            "wheelhouse": "wheelhouse",
            "runtime_requirements": "runtime.lock",
            "imports": ["demo"],
            "entry_points": [{
                "name": "hello", "args": [], "expected_exit_code": 0,
                "expected_stdout": expected_stdout if expected_stdout is not None else f"hello{os.linesep}",
                "expected_stderr": "",
            }],
        },
    }


def _make_project(
    make_project,
    uv: tuple[Path, str, str],
    *,
    backend: str = "uv_build",
    accept: bool = True,
    expected_stdout: str | None = None,
) -> Path:
    check = _manifest_check(uv, backend=backend, expected_stdout=expected_stdout)
    return make_project({
        "pyproject.toml": (
            "[build-system]\n" + (
                'requires = ["uv_build>=0.9,<1"]\nbuild-backend = "uv_build"\n'
                if backend == "uv_build" else 'requires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
            ) + "\n[project]\nname = \"demo\"\nversion = \"1.0.0\"\nrequires-python = \">=3.11\"\n"
            "[project.scripts]\nhello = \"demo:main\"\n"
        ),
        "src/demo/__init__.py": "def main():\n    print('hello')\n",
        "build-constraints.txt": "# uv_build is bundled with the pinned uv executable\n",
        "runtime.lock": "# no external runtime requirements\n",
        "wheelhouse/README.txt": "All third-party wheels for this profile belong here.\n",
    }, [check], accept=accept)


def _wheel_bytes(name: str, version: str, files: dict[str, bytes], requires: tuple[str, ...] = ()) -> bytes:
    normalized = name.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    entries = dict(files)
    entries[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {normalized}\nVersion: {version}\n"
        + "".join(f"Requires-Dist: {item}\n" for item in requires)
        + "\n"
    ).encode()
    entries[f"{dist_info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: vkit-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
    record_path = f"{dist_info}/RECORD"
    rows = []
    for path, content in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        rows.append((path, f"sha256={digest}", str(len(content))))
    rows.append((record_path, "", ""))
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    entries[record_path] = stream.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return output.getvalue()


def _fixture_wheelhouse(*, include_dependency: bool = True, noisy: bool = False) -> tuple[dict[str, bytes], str]:
    dependency = _wheel_bytes("fixture-dep", "1.0", {"fixture_dep.py": b"VALUE = 'ready'\n"})
    backend_source = '''from pathlib import Path
import zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    import fixture_dep
    assert fixture_dep.VALUE == "ready"
    files = {
        "demo/__init__.py": b"def main():\\n    print('hello')\\n",
        "demo-1.0.0.dist-info/METADATA": b"Metadata-Version: 2.1\\nName: demo\\nVersion: 1.0.0\\n\\n",
        "demo-1.0.0.dist-info/WHEEL": b"Wheel-Version: 1.0\\nGenerator: fixture-backend\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n\\n",
        "demo-1.0.0.dist-info/entry_points.txt": b"[console_scripts]\\nhello = demo:main\\n",
    }
    import base64, csv, hashlib, io
    rows = []
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        rows.append((path, f"sha256={digest}", str(len(content))))
    record = "demo-1.0.0.dist-info/RECORD"
    rows.append((record, "", ""))
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\\n").writerows(rows)
    files[record] = stream.getvalue().encode()
    destination = Path(wheel_directory) / "demo-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return destination.name
'''.encode()
    if noisy:
        backend_source = backend_source.replace(
            b"    import fixture_dep\n",
            b"    import sys, time\n    for _ in range(512):\n        sys.stdout.write('x' * 1024)\n        sys.stdout.flush()\n        time.sleep(0.02)\n    import fixture_dep\n",
        )
    backend = _wheel_bytes(
        "fixture-backend", "1.0", {"fixture_backend.py": backend_source}, ("fixture-dep==1.0",)
    )
    wheels = {"fixture_backend-1.0-py3-none-any.whl": backend}
    if include_dependency:
        wheels["fixture_dep-1.0-py3-none-any.whl"] = dependency
    lock = "\n".join(
        f"{name}==1.0 --hash=sha256:{hashlib.sha256(content).hexdigest()}"
        for name, content in (("fixture-backend", backend), ("fixture-dep", dependency))
    ) + "\n"
    return wheels, lock


def _fixture_backend_project(make_project, uv: tuple[Path, str, str], *, include_dependency: bool = True, bad_hash: bool = False, noisy: bool = False) -> Path:
    wheels, lock = _fixture_wheelhouse(include_dependency=include_dependency, noisy=noisy)
    if bad_hash:
        lock = lock.replace(hashlib.sha256(wheels["fixture_backend-1.0-py3-none-any.whl"]).hexdigest(), "0" * 64)
    project = _make_project(make_project, uv, backend="fixture", accept=False)
    (project / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["fixture-backend==1.0"]\nbuild-backend = "fixture_backend"\n\n'
        '[project]\nname = "demo"\nversion = "1.0.0"\nrequires-python = ">=3.11"\n'
    )
    (project / "build-constraints.txt").write_text(lock)
    wheelhouse = project / "wheelhouse"
    for name, content in wheels.items():
        (wheelhouse / name).write_bytes(content)
    git(project, "add", ".")
    git(project, "commit", "-qm", "add local build backend wheelhouse")
    code, body = vkit(project, "accept", "--yes")
    assert code == 0, body
    return project


def _record(project: Path) -> dict:
    opened = open_project(project)
    return run_check(opened, parse_manifest(opened), "release-wheel")


def _report(project: Path, record: dict) -> dict:
    return json.loads((Store.of(open_project(project)).run_dir(record["run_id"]) / "build.json").read_text())


def test_buildcheck_uses_existing_explicit_approval_boundary(make_project):
    uv = _uv()
    if uv is None:
        pytest.skip("a local uv executable is needed to bind an explicit BuildCheck profile")
    project = _make_project(make_project, uv, accept=False)

    record = _record(project)

    assert record["outcome"]["result"] == "BLOCKED"
    assert record["outcome"]["reason"] == "not_approved"
    assert record["argv"] is None


def test_buildcheck_runs_two_clean_builds_and_literal_install_smokes(make_project):
    uv = _uv()
    if uv is None:
        pytest.skip("a local uv executable is needed for the offline build integration test")
    project = _make_project(make_project, uv)

    record = _record(project)
    report = _report(project, record)

    assert record["outcome"]["result"] == "PASS"
    assert report["profile"] == "offline-wheel-build-install-v1"
    assert report["same_wheel_bytes"] is True
    assert [build["index"] for build in report["builds"]] == [1, 2]
    assert report["builds"][0]["wheel"]["sha256"] == report["builds"][1]["wheel"]["sha256"]
    assert [item["id"] for item in report["assertions"]] == [
        "builds:wheel-bytes-identical", "builds:wheel-filename-identical",
        "install-1:imports", "install-1:entrypoint:hello",
        "install-2:imports", "install-2:entrypoint:hello",
    ]
    assert [item["result"] for item in report["assertions"]] == ["PASS"] * 6
    for assertion in report["assertions"]:
        if assertion["id"].endswith(":entrypoint:hello"):
            assert base64.b64decode(assertion["observed"]["stdout_base64"]) == f"hello{os.linesep}".encode()
    run_dir = Store.of(open_project(project)).run_dir(record["run_id"])
    assert len(list((run_dir / "artifacts").glob("build-*/*.whl"))) == 2
    assert all((run_dir / f"build-{index}.stdout.log").is_file() for index in (1, 2))


def test_entrypoint_output_mismatch_is_a_completed_failure(make_project):
    uv = _uv()
    if uv is None:
        pytest.skip("a local uv executable is needed for the offline build integration test")
    project = _make_project(make_project, uv, expected_stdout="wrong output\n")

    record = _record(project)
    report = _report(project, record)

    assert record["outcome"]["result"] == "FAIL"
    assert report["status"] == "FAIL"
    assert [item["result"] for item in report["assertions"] if item["id"].endswith(":entrypoint:hello")] == ["FAIL", "FAIL"]


def test_build_backend_missing_transitive_dependency_is_incomplete_setup(make_project):
    uv = _uv()
    if uv is None:
        pytest.skip("a local uv executable is needed to exercise the offline backend closure")
    project = _fixture_backend_project(make_project, uv, include_dependency=False)

    record = _record(project)
    report = _report(project, record)
    run_dir = Store.of(open_project(project)).run_dir(record["run_id"])

    assert record["outcome"]["result"] == "BLOCKED"
    assert record["outcome"]["reason"] == "internal_error"
    assert report["builds"][0]["exit_code"] != 0
    assert "fixture-dep" in (run_dir / "build-1.stderr.log").read_text(errors="replace").lower()


def test_build_backend_hash_mismatch_is_incomplete_setup(make_project):
    uv = _uv()
    if uv is None:
        pytest.skip("a local uv executable is needed to exercise offline hash enforcement")
    project = _fixture_backend_project(make_project, uv, bad_hash=True)

    record = _record(project)
    report = _report(project, record)
    run_dir = Store.of(open_project(project)).run_dir(record["run_id"])

    assert record["outcome"]["result"] == "BLOCKED"
    assert record["outcome"]["reason"] == "internal_error"
    assert report["builds"][0]["exit_code"] != 0
    assert "hash mismatch" in (run_dir / "build-1.stderr.log").read_text(errors="replace").lower()


def test_backend_output_is_capped_while_the_build_runs(make_project):
    uv = _uv()
    if uv is None:
        pytest.skip("a local uv executable is needed to exercise the bounded build worker")
    project = _fixture_backend_project(make_project, uv, noisy=True)

    record = _record(project)
    run_dir = Store.of(open_project(project)).run_dir(record["run_id"])

    assert record["outcome"]["result"] == "BLOCKED"
    assert record["outcome"]["reason"] == "artifact_malformed"
    assert (run_dir / "build-1.stdout.log").stat().st_size + (run_dir / "build-1.stderr.log").stat().st_size <= 256 * 1024 + 8192


def test_changed_build_definition_requires_new_approval(make_project):
    uv = _uv()
    if uv is None:
        pytest.skip("a local uv executable is needed to bind an explicit BuildCheck profile")
    project = _make_project(make_project, uv)
    manifest_path = project / "verification" / "manifest.json"
    document = json.loads(manifest_path.read_text())
    document["checks"][0]["build"]["entry_points"][0]["expected_stdout"] = "changed\n"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    record = _record(project)

    assert record["outcome"]["result"] == "BLOCKED"
    assert record["outcome"]["reason"] == "not_approved"
    assert record["argv"] is None


def test_unresolved_backend_build_is_blocked_without_calling_the_package_defective(make_project):
    uv = _uv()
    if uv is None:
        pytest.skip("a local uv executable is needed to exercise the build backend")
    project = _make_project(make_project, uv, backend="hatchling")

    record = _record(project)
    report = _report(project, record)

    assert record["outcome"]["result"] == "BLOCKED"
    assert record["outcome"]["reason"] == "internal_error"
    assert report["status"] == "BLOCKED"
    assert "execution observation" in report["problem"]["detail"]
    assert report["builds"][0]["exit_code"] != 0
