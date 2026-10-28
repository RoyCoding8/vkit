from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from vkit import container, evidence, replay_assets
from vkit.formal.verus import _tree_identity
from vkit.inputs import canonical_digest


def _tree(path: Path, marker: str) -> list[dict]:
    path.mkdir()
    nested = path / "nested"
    nested.mkdir()
    (nested / "tool").write_text(marker, encoding="utf-8")
    (nested / "tool").chmod(0o755)
    return _tree_identity(path)


def _record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict[str, Path]]:
    roots = {name: tmp_path / name for name in ("verus_root", "rustup_home", "cargo_home")}
    trees = {name: _tree(path, name) for name, path in roots.items()}
    archive = tmp_path / "verus.zip"
    archive.write_bytes(b"pinned Verus archive")
    monkeypatch.setenv("VKIT_VERUS_ARCHIVE", str(archive))
    monkeypatch.setenv("VKIT_VERUS_ROOT", str(roots["verus_root"]))
    monkeypatch.setenv("VKIT_RUSTUP_HOME", str(roots["rustup_home"]))
    monkeypatch.setenv("VKIT_CARGO_HOME", str(roots["cargo_home"]))
    semantic = {
        "target": "x86_64-unknown-linux-gnu",
        "image_id": "sha256:" + "a" * 64,
        "verus": {"archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                  "files": trees["verus_root"]},
        "rustup_home": trees["rustup_home"],
        "cargo_home": trees["cargo_home"],
    }
    checker = canonical_digest(semantic)
    record = {
        "job_id": "b" * 64,
        "checker_sha256": checker,
        "receipt": {"sha256": "c" * 64},
        "verification": {"execution_identity": {"semantic": semantic, "digest": checker}},
    }
    return record, roots


def _export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict, dict[str, Path]]:
    record, roots = _record(tmp_path, monkeypatch)
    source_run = tmp_path / "run"
    source_run.mkdir()
    (source_run / "receipt.json").write_bytes(b"original checked receipt")
    record["receipt"]["sha256"] = hashlib.sha256(
        (source_run / "receipt.json").read_bytes()
    ).hexdigest()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "record.json").write_text(json.dumps(record), encoding="utf-8")

    def fake_run(argv: list[str], **_kwargs):
        if argv[1:3] == ["save", "--output"]:
            Path(argv[3]).write_bytes(b"docker image archive")

    monkeypatch.setattr(replay_assets, "_run", fake_run)
    monkeypatch.setattr(container, "image_id", lambda image: image)
    replay_assets.capture(record, source_run, bundle)
    monkeypatch.setattr(evidence, "validate_record", lambda _record, _bundle: True)
    monkeypatch.setattr(replay_assets.platform, "system", lambda: "Linux")
    monkeypatch.setattr(replay_assets.platform, "machine", lambda: "x86_64")
    return bundle, record, roots


def test_capture_restore_preserves_identity_and_returns_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, record, roots = _export(tmp_path, monkeypatch)
    restored = replay_assets.restore(bundle, tmp_path / "restored")

    assert restored["VKIT_VERUS_IMAGE"] == record["verification"]["execution_identity"]["semantic"]["image_id"]
    assert restored["VKIT_VERUS_ARCHIVE"].endswith("verus.zip")
    for name, env_name in replay_assets._TREE_ENV.items():
        assert _tree_identity(Path(restored[env_name])) == _tree_identity(roots[name])
    assert "VKIT_VERUS_IMAGE" not in os.environ


def test_restore_rejects_tampered_asset_bytes_before_loading_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _, _ = _export(tmp_path, monkeypatch)
    archive = bundle / replay_assets.ASSET_REL / "rustup_home.tar"
    archive.write_bytes(archive.read_bytes() + b"tamper")
    loaded = []
    monkeypatch.setattr(replay_assets, "_run", lambda argv, **_kwargs: loaded.append(argv))

    with pytest.raises(ValueError, match="byte digest"):
        replay_assets.restore(bundle, tmp_path / "restored")
    assert loaded == []


def test_restore_rejects_unsafe_tar_member_even_when_manifest_hash_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _, _ = _export(tmp_path, monkeypatch)
    capsule = bundle / replay_assets.ASSET_REL
    archive_path = capsule / "rustup_home.tar"
    malicious = tmp_path / "bad.tar"
    with tarfile.open(malicious, "w") as archive:
        member = tarfile.TarInfo("../escaped")
        member.size = 3
        archive.addfile(member, io.BytesIO(b"bad"))
    manifest_path = capsule / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"]["rustup_home"].update(
        path="rustup_home.tar", sha256=hashlib.sha256(malicious.read_bytes()).hexdigest()
    )
    archive_path.write_bytes(malicious.read_bytes())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    loaded = []
    monkeypatch.setattr(replay_assets, "_run", lambda argv, **_kwargs: loaded.append(argv))

    with pytest.raises(ValueError, match="unsafe path"):
        replay_assets.restore(bundle, tmp_path / "restored")
    assert loaded == []
    assert not (tmp_path / "escaped").exists()


def test_restore_rejects_docker_image_id_mismatch_after_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, record, _ = _export(tmp_path, monkeypatch)
    monkeypatch.setattr(replay_assets, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(container, "image_id", lambda _image: "sha256:" + "d" * 64)

    with pytest.raises(ValueError, match="image ID differs"):
        replay_assets.restore(bundle, tmp_path / "restored")
    assert record["job_id"] == "b" * 64


def test_receipt_walk_prunes_only_capsule_and_rejects_linked_capsule_root(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    (run / "replay/assets").mkdir(parents=True)
    (run / "replay/assets/large.tar").write_bytes(b"portable payload")
    (run / "replay/other.txt").write_text("ordinary replay artifact", encoding="utf-8")

    retained = {path.relative_to(run).as_posix()
                for path in evidence.retained_files(run, exclude_replay_assets=True)}
    assert "replay/assets/large.tar" not in retained
    assert "replay/other.txt" in retained

    linked_run = tmp_path / "linked"
    (linked_run / "replay").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (linked_run / "replay/assets").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    with pytest.raises(ValueError, match="filesystem link"):
        list(evidence.retained_files(linked_run, exclude_replay_assets=True))


def test_evidence_export_calls_optional_asset_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "stored-run"
    run_dir.mkdir()
    (run_dir / "receipt.json").write_text("receipt", encoding="utf-8")
    record = {"run_id": "run-1", "state": "done", "job_id": "d" * 64,
              "receipt": {"sha256": "e" * 64}}

    class Store:
        def record(self, _run_id: str) -> dict:
            return record

        def run_dir(self, _run_id: str) -> Path:
            return run_dir

    monkeypatch.setattr(evidence, "validate_record", lambda *_args: True)
    monkeypatch.setattr(replay_assets, "capture", lambda *_args: {
        "path": "replay/assets", "manifest_sha256": "f" * 64,
        "checker_sha256": "a" * 64,
    })
    result = evidence.export(Store(), "run-1", tmp_path / "export", include_assets=True)

    assert result["portable_assets"]["path"] == "replay/assets"
    assert (tmp_path / "export/receipt.json").is_file()
