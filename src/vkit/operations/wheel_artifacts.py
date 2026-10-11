"""Compare static installed-file mappings recorded in Python wheel archives."""
from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ._data import encode_json


PROFILE = "python-wheel-install-map-v1"
MODEL_SCOPE = (
    "Validates and compares the static install-path mapping declared by two Python wheels. "
    "It does not install, import, build, or execute either package and does not establish "
    "runtime behavior or reproducible builds. Scheme names are abstract namespaces that "
    "may alias on a target host; executable bits are compared as declared archive metadata."
)
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_DIFFERENCES = 256
MAX_IMPORT_ROOTS = 128
MAX_IMPORT_PATHS_PER_ROOT = 8
MAX_METADATA_ITEMS = 512
MAX_EXPANDED_TAGS = 512
MAX_METADATA_VALUE_CHARS = 1024
MAX_WHEEL_PATH_CHARS = 512
_COMPRESSION_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_RESERVED_WINDOWS_STEMS = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_EXTENSION_MODULE = re.compile(
    r"^(?P<name>[^.]+)(?:\.(?:cpython-\d+[^.]*|cp\d+[^.]*|abi3))?\.(?:so|pyd)$",
    re.IGNORECASE,
)


class _UnsupportedWheel(ValueError):
    pass


class _PackagingUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class _CapturedArchive:
    path: str
    raw: bytes
    sha256: str


@dataclass(frozen=True)
class _Wheel:
    name: str
    version: str
    build: str | None
    tags: tuple[str, ...]
    root_is_purelib: bool
    requires_python: str | None
    requires_dist: tuple[str, ...]
    provides_extra: tuple[str, ...]
    entry_points: tuple[dict[str, str], ...]
    declared_import_roots: tuple[str, ...]
    inferred_import_roots: tuple[dict[str, Any], ...]
    inferred_import_root_count: int
    inferred_import_roots_truncated: bool
    files: dict[str, tuple[str, int, bool]]
    file_count: int


def _capture(root: Path, path: str) -> _CapturedArchive:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("wheel path must be a non-empty repository-relative path")
    if Path(path).is_absolute() or PureWindowsPath(path).drive or PureWindowsPath(path).root:
        raise ValueError("wheel path must be repository-relative")
    parts = path.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("wheel path must not contain empty, '.' or '..' segments")
    if parts[-1].lower().endswith(".whl") is False:
        raise ValueError("wheel path must identify a .whl file")

    try:
        resolved_root = Path(root).resolve(strict=True)
        current = resolved_root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("wheel paths cannot contain symlinks")
        target = current.resolve(strict=True)
        if not target.is_relative_to(resolved_root) or not target.is_file():
            raise ValueError("wheel path must identify a regular file inside the repository")
        with target.open("rb") as source:
            raw = source.read(MAX_ARCHIVE_BYTES + 1)
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot read repository-relative wheel {path!r}: {exc}") from exc

    if len(raw) > MAX_ARCHIVE_BYTES:
        raise _UnsupportedWheel("wheel archive exceeds the compressed-size limit")
    return _CapturedArchive("/".join(parts), raw, hashlib.sha256(raw).hexdigest())


def _validate_member_path(name: str, *, directory: bool) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise _UnsupportedWheel("wheel contains an invalid ZIP path")
    if directory:
        if not name.endswith("/"):
            raise _UnsupportedWheel("wheel contains a malformed directory entry")
        name = name[:-1]
    if not name or name.startswith("/"):
        raise _UnsupportedWheel("wheel contains an absolute or empty ZIP path")
    if len(name) > MAX_WHEEL_PATH_CHARS:
        raise _UnsupportedWheel("wheel member path exceeds the path-length limit")
    parts = name.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise _UnsupportedWheel("wheel contains an unsafe ZIP path")
    if any(
        part.endswith((".", " "))
        or any(ord(char) < 32 or char in '<>"|?*' for char in part)
        for part in parts
    ):
        raise _UnsupportedWheel("wheel path is not portable across supported hosts")
    if any(part.split(".", 1)[0].upper() in _RESERVED_WINDOWS_STEMS for part in parts):
        raise _UnsupportedWheel("wheel contains a Windows-reserved path")
    return "/".join(parts)


def _reject_file_prefix_clashes(paths: set[str], label: str) -> None:
    folded_paths = {path.casefold() for path in paths}
    for path in paths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index]).casefold()
            if prefix in folded_paths:
                raise _UnsupportedWheel(f"wheel {label} contains a file/directory prefix clash")


def _tag_expansion_count(value: str, label: str) -> int:
    if len(value) > MAX_METADATA_VALUE_CHARS:
        raise _UnsupportedWheel(f"wheel {label} tag exceeds the value limit")
    parts = value.strip().split("-")
    if len(parts) != 3 or any(not part for part in parts):
        raise _UnsupportedWheel(f"wheel {label} tag is malformed")
    components = [part.split(".") for part in parts]
    if any(any(not item for item in group) for group in components):
        raise _UnsupportedWheel(f"wheel {label} tag is malformed")
    count = len(components[0]) * len(components[1]) * len(components[2])
    if count > MAX_EXPANDED_TAGS:
        raise _UnsupportedWheel(f"wheel {label} tag expands beyond the 512-tag limit")
    return count


def _preflight_filename_tags(filename: str) -> None:
    parts = filename[:-4].split("-") if filename.lower().endswith(".whl") else []
    if len(parts) < 5:
        raise _UnsupportedWheel("wheel filename does not follow the supported wheel format")
    _tag_expansion_count("-".join(parts[-3:]), "filename")


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo,
                 *, keep: bool) -> tuple[str, int, bytes | None]:
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if keep else None
    size = 0
    try:
        with archive.open(info, "r") as source:
            while True:
                block = source.read(64 * 1024)
                if not block:
                    break
                size += len(block)
                if size > min(MAX_MEMBER_BYTES, info.file_size):
                    raise _UnsupportedWheel("wheel member expanded beyond its declared size or limit")
                digest.update(block)
                if chunks is not None:
                    chunks.append(block)
    except _UnsupportedWheel:
        raise
    except (OSError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error, NotImplementedError) as exc:
        raise _UnsupportedWheel("wheel contains a corrupt or unsupported ZIP member") from exc
    if size != info.file_size:
        raise _UnsupportedWheel("wheel member size does not match its ZIP directory entry")
    content = b"".join(chunks) if chunks is not None else None
    return digest.hexdigest(), size, content


def _one_header(message: Any, name: str, *, required: bool = False) -> str | None:
    values = message.get_all(name, [])
    if len(values) > 1 or (required and len(values) != 1):
        raise _UnsupportedWheel(f"wheel metadata must contain exactly one {name} field")
    value = str(values[0]).strip() if values else None
    if required and not value:
        raise _UnsupportedWheel(f"wheel metadata has an empty {name} field")
    return value


def _metadata(data: bytes, label: str) -> Any:
    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
        if message.defects or any(getattr(value, "defects", ()) for value in message.values()):
            raise _UnsupportedWheel(f"wheel {label} metadata is malformed")
        return message
    except (LookupError, UnicodeError, ValueError) as exc:
        raise _UnsupportedWheel(f"wheel {label} metadata is malformed") from exc


def _parse_entry_points(data: bytes) -> tuple[dict[str, str], ...]:
    parser = configparser.ConfigParser(interpolation=None, strict=True, delimiters=("="))
    parser.optionxform = str
    try:
        parser.read_string(data.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise _UnsupportedWheel("wheel entry_points.txt is malformed") from exc
    result = []
    for group in parser.sections():
        for name, target in parser.items(group, raw=True):
            if len(result) >= MAX_METADATA_ITEMS:
                raise _UnsupportedWheel("wheel entry_points.txt contains too many entries")
            if max(len(group), len(name), len(target)) > MAX_METADATA_VALUE_CHARS:
                raise _UnsupportedWheel("wheel entry_points.txt contains an oversized value")
            result.append({"group": group, "name": name, "target": target.strip()})
    return tuple(sorted(result, key=lambda item: (item["group"], item["name"], item["target"])))


def _record_rows(data: bytes) -> dict[str, tuple[str, int] | None]:
    try:
        text = data.decode("utf-8")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        rows = list(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise _UnsupportedWheel("wheel RECORD is malformed") from exc
    result: dict[str, tuple[str, int] | None] = {}
    for row in rows:
        if len(row) != 3 or not row[0]:
            raise _UnsupportedWheel("wheel RECORD contains a malformed row")
        name = _validate_member_path(row[0], directory=False)
        if name in result:
            raise _UnsupportedWheel("wheel RECORD contains a duplicate path")
        if name.endswith((".jws", ".p7s")):
            raise _UnsupportedWheel("wheel signatures are outside the supported static profile")
        hash_field, size_field = row[1], row[2]
        if not hash_field and not size_field:
            result[name] = None
            continue
        if not hash_field or not size_field:
            raise _UnsupportedWheel("wheel RECORD hash and size must both be present")
        algorithm, separator, encoded_digest = hash_field.partition("=")
        if not separator or algorithm not in {"sha256", "sha384", "sha512"}:
            raise _UnsupportedWheel("wheel RECORD uses an unsupported hash algorithm")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded_digest):
            raise _UnsupportedWheel("wheel RECORD contains an invalid base64 digest")
        try:
            digest = base64.urlsafe_b64decode(encoded_digest + "=" * (-len(encoded_digest) % 4))
            size = int(size_field)
        except (ValueError, base64.binascii.Error) as exc:
            raise _UnsupportedWheel("wheel RECORD contains an invalid digest or size") from exc
        if size < 0 or str(size) != size_field:
            raise _UnsupportedWheel("wheel RECORD contains an invalid size")
        result[name] = (f"{algorithm}:{digest.hex()}", size)
    return result


def _infer_import_roots(files: dict[str, tuple[str, int, bool]]) -> tuple[tuple[dict[str, Any], ...], int]:
    roots: dict[str, set[str]] = {}
    for target in files:
        parts = target.split("/")
        if len(parts) < 2 or parts[0] not in {"purelib", "platlib"}:
            continue
        relative = parts[1:]
        leaf = relative[-1]
        if leaf.endswith((".py", ".pyc")):
            module_file = leaf.rsplit(".", 1)[0]
        else:
            match = _EXTENSION_MODULE.fullmatch(leaf)
            if match is None:
                continue
            module_file = match.group("name")
        root = module_file if len(relative) == 1 else relative[0]
        if root.isidentifier():
            roots.setdefault(root, set()).add(target)
    all_roots = tuple(
        {"name": name, "paths": sorted(paths)[:MAX_IMPORT_PATHS_PER_ROOT],
         "path_count": len(paths), "paths_truncated": len(paths) > MAX_IMPORT_PATHS_PER_ROOT}
        for name, paths in sorted(roots.items())
    )
    return all_roots[:MAX_IMPORT_ROOTS], len(all_roots)


def _inspect(captured: _CapturedArchive) -> _Wheel:
    try:
        from importlib.metadata import version as installed_version
        from packaging.tags import parse_tag
        from packaging.utils import canonicalize_name, parse_wheel_filename
        from packaging.version import Version
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet
    except ImportError as exc:
        raise _PackagingUnavailable("packaging==26.3 is required for wheel filename and metadata parsing") from exc
    try:
        if installed_version("packaging") != "26.3":
            raise _PackagingUnavailable("wheel analysis requires exactly packaging==26.3")
    except _PackagingUnavailable:
        raise
    except Exception as exc:
        raise _PackagingUnavailable("could not verify the installed packaging==26.3 pin") from exc

    filename = PurePosixPath(captured.path).name
    _preflight_filename_tags(filename)
    try:
        filename_name, filename_version, build, filename_tags = parse_wheel_filename(
            filename
        )
    except (ValueError, TypeError) as exc:
        raise _UnsupportedWheel("wheel filename does not follow the supported wheel format") from exc

    try:
        archive = zipfile.ZipFile(io.BytesIO(captured.raw), "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise _UnsupportedWheel("wheel is not a valid ZIP archive") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise _UnsupportedWheel("wheel contains too many ZIP entries")
        if not infos:
            raise _UnsupportedWheel("wheel archive is empty")

        names: dict[str, zipfile.ZipInfo] = {}
        casefolded: set[str] = set()
        declared_total = 0
        for info in infos:
            name = _validate_member_path(info.filename, directory=info.is_dir())
            folded = name.casefold()
            if name in names or folded in casefolded:
                raise _UnsupportedWheel("wheel contains duplicate or case-colliding paths")
            casefolded.add(folded)
            if info.flag_bits & 0x1:
                raise _UnsupportedWheel("encrypted wheel members are unsupported")
            if info.compress_type not in _COMPRESSION_METHODS:
                raise _UnsupportedWheel("wheel uses an unsupported ZIP compression method")
            mode = (info.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if kind == stat.S_IFLNK:
                raise _UnsupportedWheel("wheel contains a symlink")
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise _UnsupportedWheel("wheel contains a non-regular ZIP member")
            if info.is_dir():
                if kind == stat.S_IFREG:
                    raise _UnsupportedWheel("wheel directory entry has regular-file mode")
                continue
            if kind == stat.S_IFDIR:
                raise _UnsupportedWheel("wheel file entry has directory mode")
            if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                raise _UnsupportedWheel("wheel member exceeds the uncompressed-size limit")
            if info.compress_size < 0:
                raise _UnsupportedWheel("wheel member has an invalid compressed size")
            if info.file_size and (info.compress_size == 0
                                   or info.file_size > info.compress_size * MAX_COMPRESSION_RATIO):
                raise _UnsupportedWheel("wheel member exceeds the compression-ratio limit")
            declared_total += info.file_size
            if declared_total > MAX_EXPANDED_BYTES:
                raise _UnsupportedWheel("wheel exceeds the total uncompressed-size limit")
            names[name] = info

        _reject_file_prefix_clashes(set(names), "archive")

        dist_info_dirs = {
            name.split("/", 1)[0]
            for name in names
            if ".dist-info/" in name
        }
        if len(dist_info_dirs) != 1:
            raise _UnsupportedWheel("wheel must contain exactly one root .dist-info directory")
        dist_info = next(iter(dist_info_dirs))
        if "/" in dist_info or not dist_info.endswith(".dist-info"):
            raise _UnsupportedWheel("wheel .dist-info directory must be at the archive root")
        stem = dist_info[: -len(".dist-info")]
        expected_stem = f"{str(filename_name).replace('-', '_')}-{filename_version}"
        if stem != expected_stem:
            raise _UnsupportedWheel("wheel .dist-info directory disagrees with the filename")
        data_dir = f"{stem}.data"

        record_path = f"{dist_info}/RECORD"
        metadata_path = f"{dist_info}/METADATA"
        wheel_path = f"{dist_info}/WHEEL"
        for required_path in (record_path, metadata_path, wheel_path):
            if required_path not in names:
                raise _UnsupportedWheel(f"wheel is missing {required_path}")

        contents: dict[str, bytes] = {}
        digests: dict[str, tuple[str, int]] = {}
        actual_total = 0
        for name, info in names.items():
            is_metadata = name.startswith(dist_info + "/")
            digest, size, content = _read_member(archive, info, keep=is_metadata)
            actual_total += size
            if actual_total > MAX_EXPANDED_BYTES:
                raise _UnsupportedWheel("wheel exceeds the total uncompressed-size limit")
            digests[name] = (digest, size)
            if content is not None:
                if size > MAX_METADATA_BYTES:
                    raise _UnsupportedWheel("wheel distribution metadata exceeds the size limit")
                contents[name] = content

        rows = _record_rows(contents[record_path])
        if record_path not in rows or rows[record_path] is not None:
            raise _UnsupportedWheel("wheel RECORD must contain its own unhashed row")
        if set(rows) != set(names):
            raise _UnsupportedWheel("wheel RECORD does not list every archive file exactly once")
        for name, recorded in rows.items():
            if name == record_path:
                continue
            if recorded is None:
                raise _UnsupportedWheel("wheel RECORD omits a file hash or size")
            algorithm_digest, expected_size = recorded
            algorithm, expected_hash = algorithm_digest.split(":", 1)
            actual_digest, actual_size = digests[name]
            if (algorithm != "sha256" or actual_digest != expected_hash
                    or actual_size != expected_size):
                if algorithm != "sha256":
                    raw = contents.get(name)
                    if raw is None:
                        raw = archive.read(names[name])
                    computed = hashlib.new(algorithm, raw).hexdigest()
                    if computed != expected_hash or actual_size != expected_size:
                        raise _UnsupportedWheel("wheel RECORD hash or size does not match its member")
                else:
                    raise _UnsupportedWheel("wheel RECORD SHA-256 hash or size does not match its member")

        metadata = _metadata(contents[metadata_path], "METADATA")
        wheel_metadata = _metadata(contents[wheel_path], "WHEEL")
        metadata_format_version = _one_header(metadata, "Metadata-Version", required=True)
        if metadata_format_version not in {"1.1", "1.2", "2.1", "2.2", "2.3", "2.4", "2.5", "2.6"}:
            raise _UnsupportedWheel("wheel Metadata-Version is outside the supported profile")
        metadata_name = _one_header(metadata, "Name", required=True)
        metadata_version = _one_header(metadata, "Version", required=True)
        try:
            if canonicalize_name(metadata_name) != canonicalize_name(str(filename_name)):
                raise _UnsupportedWheel("wheel METADATA name disagrees with its filename")
            parsed_version = Version(metadata_version)
            if parsed_version != filename_version:
                raise _UnsupportedWheel("wheel METADATA version disagrees with its filename")
        except (_UnsupportedWheel, ValueError) as exc:
            if isinstance(exc, _UnsupportedWheel):
                raise
            raise _UnsupportedWheel("wheel METADATA name or version is invalid") from exc

        wheel_version = _one_header(wheel_metadata, "Wheel-Version", required=True)
        if wheel_version != "1.0":
            raise _UnsupportedWheel("only Wheel-Version 1.0 is supported")
        generator = _one_header(wheel_metadata, "Generator", required=True)
        if len(generator) > MAX_METADATA_VALUE_CHARS:
            raise _UnsupportedWheel("wheel Generator metadata exceeds the value limit")
        wheel_build = _one_header(wheel_metadata, "Build")
        expected_build = f"{build[0]}{build[1]}" if build else None
        if wheel_build is not None and wheel_build != expected_build:
            raise _UnsupportedWheel("wheel Build metadata disagrees with its filename")
        root_flag = _one_header(wheel_metadata, "Root-Is-Purelib", required=True)
        if root_flag not in {"true", "false"}:
            raise _UnsupportedWheel("Root-Is-Purelib must be true or false")
        raw_tags = wheel_metadata.get_all("Tag", [])
        if not raw_tags or len(raw_tags) > MAX_METADATA_ITEMS:
            raise _UnsupportedWheel("wheel WHEEL metadata must declare at least one Tag")
        expansion_count = sum(_tag_expansion_count(str(tag), "WHEEL") for tag in raw_tags)
        if expansion_count > MAX_EXPANDED_TAGS:
            raise _UnsupportedWheel("wheel WHEEL tags exceed the 512-tag aggregate limit")
        try:
            wheel_tags = set().union(*(parse_tag(str(tag).strip()) for tag in raw_tags))
        except (ValueError, TypeError) as exc:
            raise _UnsupportedWheel("wheel WHEEL metadata contains an invalid Tag") from exc
        if wheel_tags != set(filename_tags):
            raise _UnsupportedWheel("wheel WHEEL tags disagree with its filename")
        if len(wheel_tags) > MAX_METADATA_ITEMS:
            raise _UnsupportedWheel("wheel expands to too many compatibility tags")

        requires_python = _one_header(metadata, "Requires-Python")
        if requires_python is not None:
            if len(requires_python) > MAX_METADATA_VALUE_CHARS:
                raise _UnsupportedWheel("wheel Requires-Python metadata exceeds the value limit")
            try:
                SpecifierSet(requires_python)
            except ValueError as exc:
                raise _UnsupportedWheel("wheel Requires-Python is malformed") from exc
        requires_dist = []
        for value in metadata.get_all("Requires-Dist", []):
            if len(str(value)) > MAX_METADATA_VALUE_CHARS or len(requires_dist) >= MAX_METADATA_ITEMS:
                raise _UnsupportedWheel("wheel Requires-Dist metadata exceeds the list limit")
            try:
                requirement = Requirement(str(value))
            except (ValueError, TypeError) as exc:
                raise _UnsupportedWheel("wheel Requires-Dist metadata is malformed") from exc
            requires_dist.append(str(requirement))
        provides_extra = []
        for value in metadata.get_all("Provides-Extra", []):
            if len(str(value)) > MAX_METADATA_VALUE_CHARS or len(provides_extra) >= MAX_METADATA_ITEMS:
                raise _UnsupportedWheel("wheel Provides-Extra metadata exceeds the list limit")
            try:
                provides_extra.append(canonicalize_name(str(value).strip()))
            except (ValueError, TypeError) as exc:
                raise _UnsupportedWheel("wheel Provides-Extra metadata is malformed") from exc

        entry_points_path = f"{dist_info}/entry_points.txt"
        entry_points = _parse_entry_points(contents[entry_points_path]) if entry_points_path in contents else ()
        top_level_path = f"{dist_info}/top_level.txt"
        if top_level_path in contents:
            try:
                declared_roots = tuple(sorted({
                    line.strip() for line in contents[top_level_path].decode("utf-8").splitlines()
                    if line.strip()
                }))
            except UnicodeDecodeError as exc:
                raise _UnsupportedWheel("wheel top_level.txt is not UTF-8") from exc
            if len(declared_roots) > MAX_METADATA_ITEMS or any(
                len(name) > MAX_METADATA_VALUE_CHARS for name in declared_roots
            ):
                raise _UnsupportedWheel("wheel top_level.txt exceeds the list limit")
            if any(not name.isidentifier() for name in declared_roots):
                raise _UnsupportedWheel("wheel top_level.txt contains an invalid import name")
        else:
            declared_roots = ()

        info_base = dist_info + "/"
        payload: dict[str, tuple[str, int, bool]] = {}
        install_targets: set[str] = set()
        for name, (digest, size) in digests.items():
            if name == record_path:
                continue
            mode = (names[name].external_attr >> 16) & 0xFFFF
            executable = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
            if name.startswith(info_base):
                relative = name[len(info_base):]
                if not relative or "/" in relative and any(
                    part in {"", ".", ".."} for part in relative.split("/")
                ):
                    raise _UnsupportedWheel("wheel .dist-info member path is invalid")
                target = "dist-info/" + relative
            elif name.startswith(data_dir + "/"):
                relative = name[len(data_dir) + 1:]
                scheme, separator, suffix = relative.partition("/")
                if not separator or not suffix or scheme not in {"purelib", "platlib", "headers", "data"}:
                    raise _UnsupportedWheel("wheel uses an unsupported .data install scheme")
                target = f"{scheme}/{suffix}"
            else:
                scheme = "purelib" if root_flag == "true" else "platlib"
                target = f"{scheme}/{name}"
            folded_target = target.casefold()
            if folded_target in install_targets:
                raise _UnsupportedWheel("wheel members collide at an install target")
            install_targets.add(folded_target)
            payload[target] = (digest, size, executable)

        if any(name.startswith(data_dir + "/scripts/") for name in names):
            raise _UnsupportedWheel("wheel .data/scripts requires installer-specific script rewriting")
        unexpected_data_roots = [
            name for name in names if ".data/" in name and not name.startswith(data_dir + "/")
        ]
        if unexpected_data_roots:
            raise _UnsupportedWheel("wheel contains an unexpected .data directory")

        canonical_rows = []
        for target, (digest, size, _) in sorted(payload.items()):
            encoded = base64.urlsafe_b64encode(bytes.fromhex(digest)).decode("ascii").rstrip("=")
            canonical_rows.append((target, f"sha256={encoded}", str(size)))
        canonical_rows.append(("dist-info/RECORD", "", ""))
        record_output = io.StringIO(newline="")
        writer = csv.writer(record_output, lineterminator="\n")
        writer.writerows(sorted(canonical_rows))
        normalized_record = record_output.getvalue().encode("utf-8")
        payload["dist-info/RECORD"] = (
            hashlib.sha256(normalized_record).hexdigest(), len(normalized_record), False
        )
        _reject_file_prefix_clashes(set(payload), "install mapping")
        if len(payload) > MAX_ARCHIVE_FILES:
            raise _UnsupportedWheel("wheel normalized install mapping contains too many files")

        all_import_roots, total_import_roots = _infer_import_roots(payload)
        return _Wheel(
            name=canonicalize_name(str(filename_name)),
            version=str(filename_version),
            build=f"{build[0]}{build[1]}" if build else None,
            tags=tuple(sorted(str(tag) for tag in filename_tags)),
            root_is_purelib=root_flag == "true",
            requires_python=requires_python,
            requires_dist=tuple(sorted(requires_dist)),
            provides_extra=tuple(sorted(provides_extra)),
            entry_points=entry_points,
            declared_import_roots=declared_roots,
            inferred_import_roots=all_import_roots,
            inferred_import_root_count=total_import_roots,
            inferred_import_roots_truncated=total_import_roots > MAX_IMPORT_ROOTS,
            files=payload,
            file_count=len(names),
        )


def _public(wheel: _Wheel, captured: _CapturedArchive) -> dict[str, Any]:
    return {
        "path": captured.path,
        "archive_sha256": captured.sha256,
        "name": wheel.name,
        "version": wheel.version,
        "build": wheel.build,
        "tags": list(wheel.tags),
        "root_is_purelib": wheel.root_is_purelib,
        "requires_python": wheel.requires_python,
        "requires_dist": list(wheel.requires_dist),
        "provides_extra": list(wheel.provides_extra),
        "entry_points": list(wheel.entry_points),
        "declared_import_roots": list(wheel.declared_import_roots),
        "inferred_import_roots": list(wheel.inferred_import_roots),
        "inferred_import_root_count": wheel.inferred_import_root_count,
        "inferred_import_roots_truncated": wheel.inferred_import_roots_truncated,
        "file_count": wheel.file_count,
        "install_file_count": len(wheel.files),
    }


def compare_wheels(root: Path, before: str, after: str) -> dict[str, Any]:
    """Compare two wheel archives without installing or importing their contents."""
    base: dict[str, Any] = {
        "operation": "compare_wheels",
        "profile": PROFILE,
        "scope": MODEL_SCOPE,
        "status": "UNKNOWN",
        "before": {"path": before},
        "after": {"path": after},
        "differences": [],
    }
    try:
        captured_before = _capture(root, before)
        captured_after = _capture(root, after)
        wheel_before = _inspect(captured_before)
        wheel_after = _inspect(captured_after)
        base["before"] = _public(wheel_before, captured_before)
        base["after"] = _public(wheel_after, captured_after)
        differences: list[dict[str, Any]] = []

        def add_difference(item: dict[str, Any]) -> None:
            if len(differences) < MAX_DIFFERENCES:
                differences.append(item)

        old_files = wheel_before.files
        new_files = wheel_after.files
        for target in sorted(old_files.keys() | new_files.keys()):
            old = old_files.get(target)
            new = new_files.get(target)
            if old == new:
                continue
            if old is None:
                add_difference({"kind": "file_added", "target": target,
                                "after_sha256": new[0], "after_size": new[1],
                                "after_executable": new[2]})
            elif new is None:
                add_difference({"kind": "file_removed", "target": target,
                                "before_sha256": old[0], "before_size": old[1],
                                "before_executable": old[2]})
            else:
                add_difference({"kind": "file_changed", "target": target,
                                "before_sha256": old[0], "after_sha256": new[0],
                                "before_size": old[1], "after_size": new[1],
                                "before_executable": old[2], "after_executable": new[2]})

        metadata_fields = (
            ("name", wheel_before.name, wheel_after.name),
            ("version", wheel_before.version, wheel_after.version),
            ("build", wheel_before.build, wheel_after.build),
            ("tags", wheel_before.tags, wheel_after.tags),
            ("root_is_purelib", wheel_before.root_is_purelib, wheel_after.root_is_purelib),
            ("requires_python", wheel_before.requires_python, wheel_after.requires_python),
            ("requires_dist", wheel_before.requires_dist, wheel_after.requires_dist),
            ("provides_extra", wheel_before.provides_extra, wheel_after.provides_extra),
            ("entry_points", wheel_before.entry_points, wheel_after.entry_points),
            ("declared_import_roots", wheel_before.declared_import_roots,
             wheel_after.declared_import_roots),
            ("inferred_import_roots", wheel_before.inferred_import_roots,
             wheel_after.inferred_import_roots),
        )
        for field, old, new in metadata_fields:
            if old != new:
                add_difference({"kind": "metadata_changed", "field": field,
                                "before": old, "after": new})

        total_differences = sum(1 for target in old_files.keys() | new_files.keys()
                                if old_files.get(target) != new_files.get(target))
        total_differences += sum(1 for _, old, new in metadata_fields if old != new)
        base["differences"] = differences
        base["difference_count"] = total_differences
        base["differences_truncated"] = total_differences > MAX_DIFFERENCES
        base["status"] = "DIFFERENT_INSTALL_MAPPING" if total_differences else "SAME_INSTALL_MAPPING"
        try:
            encode_json(base, max_bytes=MAX_OUTPUT_BYTES)
        except ValueError:
            return {
                "operation": "compare_wheels",
                "profile": PROFILE,
                "scope": MODEL_SCOPE,
                "status": "UNKNOWN",
                "detail": "wheel comparison evidence exceeds the 2 MiB output limit",
                "differences": [],
            }
        return base
    except _PackagingUnavailable as exc:
        return {**base, "status": "UNAVAILABLE", "detail": str(exc)}
    except _UnsupportedWheel as exc:
        return {**base, "status": "UNSUPPORTED", "detail": str(exc)}
    except ValueError as exc:
        return {**base, "status": "UNSUPPORTED", "detail": str(exc)}
    except (OSError, zipfile.BadZipFile, csv.Error) as exc:
        return {**base, "status": "UNKNOWN", "detail": f"wheel comparison could not be completed: {exc}"}

