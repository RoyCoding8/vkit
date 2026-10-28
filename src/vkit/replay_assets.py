"""Capture and restore digest-bound assets for portable formal evidence replay."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import container
from .formal.verus import _tree_identity
from .inputs import canonical_digest
from .nowindow import hidden_window

ASSET_REL = Path("replay/assets")
_MANIFEST = "manifest.json"
_MAX_ARCHIVE_BYTES = 16 * 1024**3
_MAX_CAPSULE_BYTES = 16 * 1024**3
_MAX_TREE_ENTRIES = 100000
_MAX_MANIFEST_BYTES = 16 * 1024**2
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_TREE_ENV = {
    "verus_root": "VKIT_VERUS_ROOT",
    "rustup_home": "VKIT_RUSTUP_HOME",
    "cargo_home": "VKIT_CARGO_HOME",
}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(argv: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           check=False, **hidden_window())
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or
                         f"asset command failed: {Path(argv[0]).name}")
    return result


def _identity(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        execution = record["verification"]["execution_identity"]
        semantic = execution["semantic"]
        digest = execution["digest"]
    except (KeyError, TypeError) as exc:
        raise ValueError("run has no formal execution identity") from exc
    if (not isinstance(semantic, dict) or not isinstance(digest, str)
            or canonical_digest(semantic) != digest
            or record.get("checker_sha256") != digest):
        raise ValueError("formal execution identity does not match the accepted checker")
    return semantic, digest


def _safe_link(root: Path, path: Path) -> None:
    target = os.readlink(path)
    if Path(target).is_absolute():
        raise ValueError(f"tool asset link is absolute: {path.relative_to(root)}")
    resolved = (path.parent / target).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError(f"tool asset link escapes its root: {path.relative_to(root)}")


def _tree_archive(source: Path, destination: Path, expected: list[dict[str, Any]]) -> dict[str, str]:
    source = source.resolve(strict=True)
    if source.is_symlink() or not source.is_dir() or _tree_identity(source) != expected:
        raise ValueError(f"tool asset tree differs from its recorded identity: {source.name}")
    for path in source.rglob("*"):
        if path.is_symlink():
            _safe_link(source, path)
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(source.rglob("*")):
            archive.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
    if destination.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("tool asset archive exceeds the portable asset size limit")
    return {"sha256": _hash(destination), "tree_digest": canonical_digest(expected)}


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or path == PurePosixPath(".") or path.is_absolute()
            or ".." in path.parts or "\\" in name
            or path.as_posix() != name or ":" in name):
        raise ValueError("asset archive contains an unsafe path")
    return path


def _safe_link_target(path: PurePosixPath, target: str) -> None:
    link = PurePosixPath(target)
    if not target or link.is_absolute() or "\\" in target or ":" in target:
        raise ValueError("asset archive contains an unsafe symbolic link")
    parts = list(path.parent.parts)
    for part in link.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError("asset archive link escapes its root")
            parts.pop()
        else:
            parts.append(part)


def _extract_tree(archive_path: Path, destination: Path,
                  expected: list[dict[str, Any]], expected_hash: str) -> Path:
    if _hash(archive_path) != expected_hash:
        raise ValueError("tool asset archive digest does not match")
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve(strict=True)
    with tarfile.open(archive_path, "r") as archive:
        members, size = [], 0
        for member in archive:
            size += member.size
            if len(members) >= _MAX_TREE_ENTRIES or size > _MAX_ARCHIVE_BYTES:
                raise ValueError("expanded tool asset tree exceeds the portable asset limits")
            members.append(member)
        names: set[str] = set()
        links = []
        files = []
        directories = []
        for member in members:
            relative = _safe_member(member.name)
            if member.name in names:
                raise ValueError("asset archive contains duplicate paths")
            names.add(member.name)
            target = root.joinpath(*relative.parts)
            if not target.resolve(strict=False).is_relative_to(root):
                raise ValueError("asset archive path escapes its destination")
            if member.isdir():
                directories.append((member, target))
            elif member.isfile():
                files.append((member, target))
            elif member.issym():
                _safe_link_target(relative, member.linkname)
                links.append((member, target))
            else:
                raise ValueError("asset archive contains a hard link or special file")
        directory_names = {PurePosixPath(member.name) for member, _ in directories}
        file_names = {PurePosixPath(member.name) for member, _ in files}
        link_names = {PurePosixPath(member.name) for member, _ in links}
        all_names = directory_names | file_names | link_names
        for name in all_names:
            for parent in name.parents:
                if parent != PurePosixPath(".") and parent in (file_names | link_names):
                    raise ValueError("asset archive has a non-directory parent")
        for _, target in sorted(directories, key=lambda row: len(row[0].name)):
            target.mkdir(parents=True, exist_ok=True)
        for member, target in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("asset archive file has no content")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)
        for member, target in links:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(member.linkname)
        for member, target in sorted(directories, key=lambda row: len(row[0].name), reverse=True):
            target.chmod(member.mode & 0o777)
    if _tree_identity(root) != expected:
        raise ValueError("restored tool tree differs from its recorded identity")
    return root


def capture(record: dict[str, Any], directory: Path, destination: Path) -> dict[str, Any]:
    """Add exact image and tool assets beneath replay/ so the original receipt stays intact."""
    from .evidence import is_link, sha256

    semantic, checker_sha256 = _identity(record)
    if (is_link(directory) or not directory.is_dir()
            or sha256(directory / "receipt.json") != record.get("receipt", {}).get("sha256")):
        raise ValueError("portable assets are not bound to the intact source receipt")
    if semantic.get("target") != "x86_64-unknown-linux-gnu":
        raise ValueError("portable Verus assets require the Linux x86-64 profile")
    base = destination / ASSET_REL
    if base.exists():
        raise ValueError("portable asset capsule already exists")
    base.mkdir(parents=True)
    try:
        image_id = semantic["image_id"]
        if not isinstance(image_id, str) or _IMAGE_ID.fullmatch(image_id) is None:
            raise ValueError("portable assets require a full Docker image ID")
        image_archive = base / "image.tar"
        _run(["docker", "save", "--output", str(image_archive), image_id])
        if image_archive.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("container image archive exceeds the portable asset size limit")
        if container.image_id(image_id) != image_id:
            raise ValueError("Docker image ID changed while exporting portable assets")
        assets: dict[str, Any] = {"image": {"path": "image.tar", "sha256": _hash(image_archive),
                                             "image_id": image_id}}
        archive_path = Path(os.environ.get("VKIT_VERUS_ARCHIVE", "")).resolve(strict=True)
        if _hash(archive_path) != semantic["verus"]["archive_sha256"]:
            raise ValueError("Verus archive differs from the recorded execution identity")
        archive_name = archive_path.name
        shutil.copyfile(archive_path, base / archive_name)
        if (base / archive_name).stat().st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("Verus archive exceeds the portable asset size limit")
        assets["verus_archive"] = {"path": archive_name, "sha256": _hash(base / archive_name)}
        for name, env_name in _TREE_ENV.items():
            source = Path(os.environ.get(env_name, "")).resolve(strict=True)
            expected = semantic[name] if name != "verus_root" else semantic["verus"]["files"]
            asset = _tree_archive(source, base / f"{name}.tar", expected)
            asset.update(path=f"{name}.tar", tree=expected)
            assets[name] = asset
        manifest = {
            "schema_version": 1,
            "job_id": record["job_id"],
            "receipt_sha256": record["receipt"]["sha256"],
            "checker_sha256": checker_sha256,
            "semantic_sha256": canonical_digest(semantic),
            "semantic": semantic,
            "assets": assets,
        }
        (base / _MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n",
                                      encoding="utf-8", newline="\n")
        if sum(path.stat().st_size for path in base.iterdir()) > _MAX_CAPSULE_BYTES:
            raise ValueError("portable asset capsule exceeds the size limit")
        return {"path": ASSET_REL.as_posix(), "manifest_sha256": _hash(base / _MANIFEST),
                "checker_sha256": checker_sha256}
    except BaseException:
        shutil.rmtree(base, ignore_errors=True)
        raise


def restore(
    bundle: Path, destination: Path, *, record: dict[str, Any] | None = None,
    accepted_checker_sha256: str | None = None,
) -> dict[str, str]:
    """Validate and materialize a capsule; returned values can be passed as a child environment."""
    bundle = bundle.resolve(strict=True)
    from .evidence import validate_record
    from .store import read_json

    bundled_record = read_json(bundle / "record.json")
    if not isinstance(bundled_record, dict) or not validate_record(bundled_record, bundle):
        raise ValueError("bundle is incomplete or its evidence digests do not match")
    if record is not None and record != bundled_record:
        raise ValueError("portable assets do not match the selected evidence record")
    record = bundled_record
    semantic, checker_sha256 = _identity(record)
    if accepted_checker_sha256 is not None and accepted_checker_sha256 != checker_sha256:
        raise ValueError("portable assets do not match the currently accepted checker")
    base = bundle / ASSET_REL
    if base.is_symlink() or not base.is_dir():
        raise ValueError("portable asset directory is missing or linked")
    manifest_path = base / _MANIFEST
    if (manifest_path.is_symlink() or not manifest_path.is_file()
            or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES):
        raise ValueError("portable asset manifest is missing or linked")
    manifest = read_json(manifest_path)
    if (not isinstance(manifest, dict) or set(manifest) != {
            "schema_version", "job_id", "receipt_sha256", "checker_sha256",
            "semantic_sha256", "semantic", "assets"}
            or manifest.get("schema_version") != 1
            or manifest.get("job_id") != record.get("job_id")
            or manifest.get("receipt_sha256") != record.get("receipt", {}).get("sha256")
            or manifest.get("checker_sha256") != checker_sha256
            or manifest.get("semantic_sha256") != canonical_digest(semantic)
            or manifest.get("semantic") != semantic):
        raise ValueError("portable asset manifest is not bound to this evidence receipt")
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError("portable Verus assets require a Linux x86-64 replay host")
    assets = manifest.get("assets")
    if not isinstance(assets, dict) or set(assets) != {"image", "verus_archive", *_TREE_ENV}:
        raise ValueError("portable asset manifest is malformed")
    expected_names = {_MANIFEST}
    required_fields = {
        "image": {"path", "sha256", "image_id"},
        "verus_archive": {"path", "sha256"},
        "verus_root": {"path", "sha256", "tree_digest", "tree"},
        "rustup_home": {"path", "sha256", "tree_digest", "tree"},
        "cargo_home": {"path", "sha256", "tree_digest", "tree"},
    }
    fixed_paths = {"image": "image.tar", **{name: f"{name}.tar" for name in _TREE_ENV}}
    referenced_paths = set()
    for name, item in assets.items():
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise TypeError("portable asset manifest is malformed")
        if set(item) != required_fields[name]:
            raise ValueError("portable asset manifest has missing or unexpected fields")
        path = Path(item["path"])
        if (path.is_absolute() or ".." in path.parts or "\\" in item["path"]
                or len(path.parts) != 1 or path.as_posix() != item["path"]):
            raise ValueError("portable asset manifest contains an unsafe path")
        if name in fixed_paths and item["path"] != fixed_paths[name]:
            raise ValueError("portable asset manifest has an unexpected asset path")
        if name == "verus_archive" and path.suffix != ".zip":
            raise ValueError("portable Verus archive must retain its zip filename")
        if item["path"] in referenced_paths:
            raise ValueError("portable asset manifest reuses an asset path")
        referenced_paths.add(item["path"])
        asset_path = base / path
        if asset_path.is_symlink() or not asset_path.is_file():
            raise ValueError("portable asset is missing or linked")
        if asset_path.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("portable asset exceeds the size limit")
        expected_names.add(path.name)
        if _hash(asset_path) != item.get("sha256"):
            raise ValueError("portable asset byte digest does not match")
    seen, size = set(), 0
    for path in base.iterdir():
        if path.name not in expected_names:
            raise ValueError("portable asset capsule has unexpected files")
        seen.add(path.name)
        size += path.stat().st_size
        if size > _MAX_CAPSULE_BYTES:
            raise ValueError("portable asset capsule exceeds the size limit")
    if seen != expected_names:
        raise ValueError("portable asset capsule has missing files")
    image = assets["image"]
    if image.get("image_id") != semantic.get("image_id"):
        raise ValueError("portable image identity differs from the accepted checker")
    verus_archive = assets["verus_archive"]
    if verus_archive.get("sha256") != semantic.get("verus", {}).get("archive_sha256"):
        raise ValueError("portable Verus archive differs from the accepted checker")
    for name in _TREE_ENV:
        expected = semantic[name] if name != "verus_root" else semantic["verus"]["files"]
        if (assets[name].get("tree") != expected
                or assets[name].get("tree_digest") != canonical_digest(expected)):
            raise ValueError(f"portable {name} tree differs from the accepted checker")
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("portable asset destination already exists")
    destination.mkdir(parents=True)
    try:
        restored: dict[str, str] = {}
        for name, env_name in _TREE_ENV.items():
            expected = assets[name]["tree"]
            root = _extract_tree(base / assets[name]["path"], destination / name,
                                 expected, assets[name]["sha256"])
            if canonical_digest(expected) != assets[name]["tree_digest"]:
                raise ValueError("portable tool tree digest does not match")
            restored[env_name] = str(root)
        verus_archive = assets["verus_archive"]
        restored["VKIT_VERUS_ARCHIVE"] = str((base / verus_archive["path"]).resolve(strict=True))
        _run(["docker", "load", "--input", str((base / image["path"]).resolve(strict=True))])
        if container.image_id(image["image_id"]) != image["image_id"]:
            raise ValueError("restored Docker image ID differs from the recorded image")
        restored["VKIT_VERUS_IMAGE"] = image["image_id"]
        return restored
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
