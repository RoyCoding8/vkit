from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

from vkit.formal import ledger, verus
from vkit.formal.policy import FormalComponent, LedgerBindings, PackageIdentity
from vkit.proc import Exit
from vkit.inputs import canonical_digest


def _fixture(tmp_path: Path, monkeypatch):
    archive = tmp_path / "verus-0.2026.10.18.7d82994-x86-linux.zip"
    verus_root = tmp_path / "verus"
    verus_root.mkdir()
    version = {
        "verus": {
            "version": "0.2026.10.18.7d82994",
            "commit": "7d829946231d6413a398f6e4160cdfe951261dad",
            "toolchain": "1.99.1-x86_64-unknown-linux-gnu",
        }
    }
    files = {
        "verus": b"verus binary",
        "z3": b"z3 binary",
        "version.json": json.dumps(version).encode(),
    }
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, contents in files.items():
            bundle.writestr(f"verus-release/{name}", contents)
            (verus_root / name).write_bytes(contents)
    (verus_root / "verus").chmod(0o755)
    (verus_root / "z3").chmod(0o755)

    rustup = tmp_path / "rustup"
    rustc = rustup / "toolchains/1.99.1-x86_64-unknown-linux-gnu/bin/rustc"
    rustc.parent.mkdir(parents=True)
    rustc.write_bytes(b"rustc")
    cargo = tmp_path / "cargo"
    rustup_bin = cargo / "bin/rustup"
    rustup_bin.parent.mkdir(parents=True)
    rustup_bin.write_bytes(b"rustup")

    package_root = tmp_path / "package"
    package_root.mkdir()
    for filename, content in {
        "ledger.rs": "pub struct Ledger;\n",
        "expected.rs.in": "{{bindings}}\n",
        "upstream.json": "{}\n",
    }.items():
        (package_root / filename).write_text(content, encoding="utf-8")
    pins = {
        "version": version["verus"]["version"],
        "revision": version["verus"]["commit"],
        "archives": {archive.name: hashlib.sha256(archive.read_bytes()).hexdigest()},
        "expanded_files": {archive.name: canonical_digest({name: hashlib.sha256(contents).hexdigest() for name, contents in files.items()})},
    }
    package = SimpleNamespace(
        root=package_root,
        identity=SimpleNamespace(
            id="vkit.resource-ledger", version="0.1.0", sha256="a" * 64
        ),
        pins=pins,
    )
    component = FormalComponent(
        id="ledger",
        package=PackageIdentity("vkit.resource-ledger", "0.1.0", "a" * 64),
        contract="ledger.source.v1",
        entry="src/ledger.rs",
        profile=verus.PROFILE,
        max_records=7,
        bindings=LedgerBindings(
            "api::new",
            "api::reserve",
            "api::release",
            "api::reserve_batch",
            "api::apply_command",
            "api::run_commands",
        ),
        required=True,
    )
    monkeypatch.setattr(ledger, "verify_package", lambda _component: package)
    monkeypatch.setattr(ledger, "render_expected", lambda _component: "verus! {}\n")
    monkeypatch.setattr(verus.platform, "system", lambda: "Linux")
    monkeypatch.setattr(verus.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(verus.container, "image_id", lambda image: image)
    monkeypatch.setenv("VKIT_VERUS_IMAGE", "sha256:" + "1" * 64)
    monkeypatch.setenv("VKIT_VERUS_ARCHIVE", str(archive))
    monkeypatch.setenv("VKIT_VERUS_ROOT", str(verus_root))
    monkeypatch.setenv("VKIT_RUSTUP_HOME", str(rustup))
    monkeypatch.setenv("VKIT_CARGO_HOME", str(cargo))
    snapshot = tmp_path / "snapshot"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src/ledger.rs").write_text("pub struct Candidate;\n", encoding="utf-8")
    return component, package, snapshot


def test_resolved_identity_binds_artifacts_but_not_host_paths(tmp_path, monkeypatch):
    component, _, _ = _fixture(tmp_path, monkeypatch)

    identity = verus.resolve_execution(component)

    assert identity["semantic"]["profile"] == verus.PROFILE
    assert identity["semantic"]["image_id"] == os.environ["VKIT_VERUS_IMAGE"]
    assert identity["semantic"]["verus"]["archive_sha256"]
    assert identity["semantic"]["rustup_home"]
    assert identity["semantic"]["cargo_home"]
    assert "archive" not in identity["semantic"]
    assert Path(identity["paths"]["archive"]).name.endswith("x86-linux.zip")


def test_executor_runs_all_required_phases_in_one_sealed_container(
    tmp_path, monkeypatch
):
    component, _, snapshot = _fixture(tmp_path, monkeypatch)
    execution_identity = verus.resolve_execution(component)
    run_dir = tmp_path / "run"
    observed = {}

    def run_container(**kwargs):
        observed.update(kwargs)
        output = run_dir / "formal-output"
        logs = {
            "verus_version": "Verus 0.2026.10.18.7d82994\n",
            "z3_version": "Z3 version 4.16.0\n",
            "rust_version": "rustc 1.99.1 (test)\n",
            "verified_library": "verification results:: 1 verified, 0 errors\n",
            "verified_candidate": "verification results:: 1 verified, 0 errors\n",
            "verified_expected": "verification results:: 6 verified, 0 errors\n",
        }
        for name, content in logs.items():
            (output / f"{name}.log").write_text(content, encoding="utf-8")
        return verus.container.ContainerRun(Exit(0), "a" * 64, True)

    monkeypatch.setattr(verus.container, "run_container", run_container)
    outcome, metadata = verus.execute_formal(
        component, snapshot, run_dir, execution_identity, should_cancel=lambda: False
    )

    assert type(outcome).__name__ == "Passed", outcome
    assert len(outcome.scenarios) == 7
    assert metadata["scope"] == "required_api"
    assert metadata["obligations"] == ["ledger:ledger_map_accounting", "ledger:new", "ledger:reserve",
                                       "ledger:release", "ledger:reserve_batch", "ledger:apply_command",
                                       "ledger:run_commands"]
    assert metadata["container"]["removed"] is True
    options = observed["options"]
    assert "--network=none" in options and "--read-only" in options
    assert "--cap-drop=ALL" in options
    assert all(
        "readonly" in options[i + 1]
        for i, value in enumerate(options[:-1])
        if value == "--mount" and "/output" not in options[i + 1]
    )
    assert observed["image"] == execution_identity["semantic"]["image_id"]
    plan = json.loads(observed["command"][-1])
    assert (
        plan["source_sha256"]
        == hashlib.sha256((snapshot / component.entry).read_bytes()).hexdigest()
    )
    candidate_phase = next(
        phase for phase in plan["phases"] if phase["id"] == "verified_candidate"
    )
    assert "/output/candidate_api.rs" in candidate_phase["argv"]
