from __future__ import annotations

import base64
import csv
import hashlib
import io
import stat
import struct
import zipfile

import pytest

pytest.importorskip("packaging")

from vkit.operations.wheel_artifacts import compare_wheels
import vkit.operations.wheel_artifacts as wheel_artifacts


_DIST_INFO = "demo-1.0.dist-info"
_METADATA = (
    "Metadata-Version: 2.1\n"
    "Name: demo\n"
    "Version: 1.0\n"
    "Requires-Python: >=3.11\n"
    "Requires-Dist: dep>=1\n"
    "\n"
).encode()


def _wheel_bytes(files: dict[str, bytes], *, reverse_record: bool = False,
                 corrupt_record: bool = False, symlink: str | None = None,
                 executable_files: frozenset[str] = frozenset(),
                 wheel_tags: tuple[str, ...] = ("py3-none-any",),
                 wheel_build: str | None = None,
                 metadata_version: str = "2.1",
                 generator: bool = True,
                 compression: int = zipfile.ZIP_DEFLATED,
                 timestamp: tuple[int, int, int, int, int, int] = (2024, 1, 1, 0, 0, 0)) -> bytes:
    wheel_metadata = (
        "Wheel-Version: 1.0\n" + ("Generator: wheel-test\n" if generator else "")
        + "Root-Is-Purelib: true\n"
        + "".join(f"Tag: {tag}\n" for tag in wheel_tags)
        + (f"Build: {wheel_build}\n" if wheel_build is not None else "")
        + "\n"
    ).encode()
    members = {
        f"{_DIST_INFO}/METADATA": _METADATA.replace(b"Metadata-Version: 2.1", f"Metadata-Version: {metadata_version}".encode()),
        f"{_DIST_INFO}/WHEEL": wheel_metadata,
        f"{_DIST_INFO}/entry_points.txt": b"[console_scripts]\ndemo=demo:main\n",
        **files,
    }
    rows = []
    for name, data in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        size = len(data)
        if corrupt_record and name.endswith("demo.py"):
            size += 1
        rows.append((name, f"sha256={digest}", str(size)))
    rows.append((f"{_DIST_INFO}/RECORD", "", ""))
    if reverse_record:
        rows.reverse()
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    members[f"{_DIST_INFO}/RECORD"] = record.getvalue().encode()

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in members.items():
            if name == symlink:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, data)
            else:
                info = zipfile.ZipInfo(name, date_time=timestamp)
                if name in executable_files:
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o755) << 16
                info.compress_type = compression
                archive.writestr(info, data)
    return output.getvalue()


def _write(project, relative: str, data: bytes) -> None:
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def test_corrupt_deflate_member_is_rejected(tmp_path):
    raw = bytearray(_wheel_bytes({"demo.py": b"value = 1\n"}))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo("demo.py")
    name_length, extra_length = struct.unpack_from("<HH", raw, info.header_offset + 26)
    payload_start = info.header_offset + 30 + name_length + extra_length
    raw[payload_start] = (raw[payload_start] & 0xF8) | 0x07
    path = "demo-1.0-py3-none-any.whl"
    _write(tmp_path, path, bytes(raw))

    result = compare_wheels(tmp_path, path, path)

    assert result["status"] == "UNSUPPORTED"
    assert "corrupt or unsupported ZIP member" in result["detail"]


def test_malformed_metadata_headers_are_rejected(tmp_path):
    malformed = _METADATA.rstrip(b"\n") + b"\ninvalid header without colon\n\n"
    path = "demo-1.0-py3-none-any.whl"
    _write(tmp_path, path, _wheel_bytes({f"{_DIST_INFO}/METADATA": malformed}))

    result = compare_wheels(tmp_path, path, path)

    assert result["status"] == "UNSUPPORTED"
    assert "metadata is malformed" in result["detail"]


def test_equal_wheels_compare_normalized_install_targets(tmp_path):
    project = tmp_path
    _write(project, "before/demo-1.0-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"def main(): return 1\n"}))
    _write(project, "after/demo-1.0-py3-none-any.whl",
           _wheel_bytes({"demo-1.0.data/purelib/demo.py": b"def main(): return 1\n"},
                        reverse_record=True, compression=zipfile.ZIP_STORED,
                        timestamp=(2025, 1, 1, 0, 0, 0)))

    result = compare_wheels(project, "before/demo-1.0-py3-none-any.whl",
                            "after/demo-1.0-py3-none-any.whl")

    assert result["status"] == "SAME_INSTALL_MAPPING"
    assert result["difference_count"] == 0
    assert result["before"]["inferred_import_roots"] == [
        {"name": "demo", "paths": ["purelib/demo.py"],
         "path_count": 1, "paths_truncated": False},
    ]
    assert result["after"]["entry_points"] == [
        {"group": "console_scripts", "name": "demo", "target": "demo:main"},
    ]
    assert "does not install" in result["scope"]


def test_wheel_payload_is_never_imported(tmp_path):
    project = tmp_path
    wheel = _wheel_bytes({"demo.py": b"raise RuntimeError('wheel code must not run')\n"})
    _write(project, "before/demo-1.0-py3-none-any.whl", wheel)
    _write(project, "after/demo-1.0-py3-none-any.whl", wheel)

    result = compare_wheels(project, "before/demo-1.0-py3-none-any.whl",
                            "after/demo-1.0-py3-none-any.whl")

    assert result["status"] == "SAME_INSTALL_MAPPING"
    assert result["difference_count"] == 0


def test_changed_installed_file_returns_literal_mapping_difference(tmp_path):
    project = tmp_path
    _write(project, "before/demo-1.0-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"value = 1\n"}))
    _write(project, "after/demo-1.0-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"value = 2\n"}))

    result = compare_wheels(project, "before/demo-1.0-py3-none-any.whl",
                            "after/demo-1.0-py3-none-any.whl")

    assert result["status"] == "DIFFERENT_INSTALL_MAPPING"
    assert result["differences"] == [
        {
            "kind": "file_changed",
            "target": "dist-info/RECORD",
            "before_sha256": "efe9801e2e504b07944d4a1f8ac8e09a151ca1b81fb01a641d57b1d958dd1562",
            "after_sha256": "7121c89f37102b4b68dec2cdaa87e80cbda2be98f3ff3661854c1316afde6d8d",
            "before_size": 313,
            "after_size": 313,
            "before_executable": False,
            "after_executable": False,
        },
        {
            "kind": "file_changed",
            "target": "purelib/demo.py",
            "before_sha256": "585c93666fcb046b7b264d3fa73202aa2a38254ae82a4b3ba19e873c2d5a9886",
            "after_sha256": "69ef4f409b530264b0da21ad4054978ada08895c93631a0da1957e2a10b592b5",
            "before_size": 10,
            "after_size": 10,
            "before_executable": False,
            "after_executable": False,
        },
    ]


def test_executable_bit_is_part_of_the_static_mapping(tmp_path):
    project = tmp_path
    _write(project, "before/demo-1.0-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"value = 1\n"}))
    _write(project, "after/demo-1.0-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"value = 1\n"},
                        executable_files=frozenset({"demo.py"})))

    result = compare_wheels(project, "before/demo-1.0-py3-none-any.whl",
                            "after/demo-1.0-py3-none-any.whl")

    assert result["status"] == "DIFFERENT_INSTALL_MAPPING"
    assert result["difference_count"] == 1
    assert result["differences"] == [{
        "kind": "file_changed", "target": "purelib/demo.py",
        "before_sha256": "585c93666fcb046b7b264d3fa73202aa2a38254ae82a4b3ba19e873c2d5a9886",
        "after_sha256": "585c93666fcb046b7b264d3fa73202aa2a38254ae82a4b3ba19e873c2d5a9886",
        "before_size": 10, "after_size": 10,
        "before_executable": False, "after_executable": True,
    }]


def test_wheel_build_tag_is_parsed_and_compared(tmp_path):
    project = tmp_path
    _write(project, "before/demo-1.0-2beta-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"value = 1\n"}, wheel_build="2beta"))
    _write(project, "after/demo-1.0-3rc1-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"value = 1\n"}, wheel_build="3rc1"))

    result = compare_wheels(project, "before/demo-1.0-2beta-py3-none-any.whl",
                            "after/demo-1.0-3rc1-py3-none-any.whl")

    assert result["before"]["build"] == "2beta"
    assert result["after"]["build"] == "3rc1"
    assert result["status"] == "DIFFERENT_INSTALL_MAPPING"
    assert any(item.get("field") == "build" for item in result["differences"])


def test_wheel_build_metadata_must_agree_with_filename(tmp_path):
    project = tmp_path
    _write(project, "bad/demo-1.0-2beta-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"x"}, wheel_build="3beta"))
    _write(project, "ok/demo-1.0-py3-none-any.whl", _wheel_bytes({"demo.py": b"x"}))

    result = compare_wheels(project, "bad/demo-1.0-2beta-py3-none-any.whl",
                            "ok/demo-1.0-py3-none-any.whl")

    assert result["status"] == "UNSUPPORTED"
    assert "Build metadata disagrees" in result["detail"]


@pytest.mark.parametrize("version", ["", "1.0", "3.0", "2.1.0"])
def test_unsupported_or_missing_metadata_version_is_rejected(tmp_path, version):
    project = tmp_path
    _write(project, "bad/demo-1.0-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"x"}, metadata_version=version))
    _write(project, "ok/demo-1.0-py3-none-any.whl", _wheel_bytes({"demo.py": b"x"}))

    result = compare_wheels(project, "bad/demo-1.0-py3-none-any.whl",
                            "ok/demo-1.0-py3-none-any.whl")

    assert result["status"] == "UNSUPPORTED"
    assert "Metadata-Version" in result["detail"]


def test_generator_is_required_by_the_wheel_profile(tmp_path):
    project = tmp_path
    _write(project, "bad/demo-1.0-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"x"}, generator=False))
    _write(project, "ok/demo-1.0-py3-none-any.whl", _wheel_bytes({"demo.py": b"x"}))

    result = compare_wheels(project, "bad/demo-1.0-py3-none-any.whl",
                            "ok/demo-1.0-py3-none-any.whl")

    assert result["status"] == "UNSUPPORTED"
    assert "Generator" in result["detail"]


def test_filename_tags_are_bounded_before_packaging_parser(tmp_path, monkeypatch):
    project = tmp_path
    python_tags = ".".join(f"p{i}" for i in range(18))
    abi_tags = ".".join(f"a{i}" for i in range(18))
    filename = f"demo-1.0-{python_tags}-{abi_tags}-pl0.pl1.whl"
    _write(project, f"bad/{filename}", b"")
    _write(project, "ok/demo-1.0-py3-none-any.whl", _wheel_bytes({"demo.py": b"x"}))
    monkeypatch.setattr("packaging.utils.parse_wheel_filename",
                        lambda *_: (_ for _ in ()).throw(AssertionError("parser called")))

    result = compare_wheels(project, f"bad/{filename}", "ok/demo-1.0-py3-none-any.whl")

    assert result["status"] == "UNSUPPORTED"
    assert "filename tag expands beyond" in result["detail"]


def test_wheel_tags_are_bounded_before_tag_parser(tmp_path, monkeypatch):
    project = tmp_path
    python_tags = ".".join(f"p{i}" for i in range(9))
    abi_tags = ".".join(f"a{i}" for i in range(9))
    platform_tags = ".".join(f"x{i}" for i in range(7))
    compressed_tag = f"{python_tags}-{abi_tags}-{platform_tags}"
    _write(project, "bad/demo-1.0-py3-none-any.whl",
           _wheel_bytes({"demo.py": b"x"}, wheel_tags=(compressed_tag,)))
    _write(project, "ok/demo-1.0-py3-none-any.whl", _wheel_bytes({"demo.py": b"x"}))
    import packaging.tags
    original = packaging.tags.parse_tag

    def guarded_parse_tag(value):
        if value == compressed_tag:
            raise AssertionError("oversized WHEEL tag reached parser")
        return original(value)

    monkeypatch.setattr(packaging.tags, "parse_tag", guarded_parse_tag)

    result = compare_wheels(project, "bad/demo-1.0-py3-none-any.whl",
                            "ok/demo-1.0-py3-none-any.whl")

    assert result["status"] == "UNSUPPORTED"
    assert "WHEEL tag expands beyond" in result["detail"]


@pytest.mark.parametrize(
    "files",
    [
        {"a": b"file", "a/b.py": b"child"},
        {"demo.py": b"file", "demo-1.0.data/purelib/demo.py/child.py": b"child"},
        {"invalid<name>.py": b"x"},
        {"trailing./module.py": b"x"},
        {"trailing /module.py": b"x"},
    ],
)
def test_file_directory_clashes_and_nonportable_paths_are_rejected(tmp_path, files):
    project = tmp_path
    _write(project, "bad/demo-1.0-py3-none-any.whl", _wheel_bytes(files))
    _write(project, "ok/demo-1.0-py3-none-any.whl", _wheel_bytes({"demo.py": b"x"}))

    result = compare_wheels(project, "bad/demo-1.0-py3-none-any.whl",
                            "ok/demo-1.0-py3-none-any.whl")

    assert result["status"] == "UNSUPPORTED"
    assert "prefix clash" in result["detail"] or "portable" in result["detail"]


def test_packaging_version_mismatch_is_unavailable_before_analysis(tmp_path, monkeypatch):
    project = tmp_path
    wheel = _wheel_bytes({"demo.py": b"x"})
    _write(project, "before/demo-1.0-py3-none-any.whl", wheel)
    _write(project, "after/demo-1.0-py3-none-any.whl", wheel)
    monkeypatch.setattr("importlib.metadata.version", lambda _: "26.4")

    result = compare_wheels(project, "before/demo-1.0-py3-none-any.whl",
                            "after/demo-1.0-py3-none-any.whl")

    assert result["status"] == "UNAVAILABLE"
    assert "exactly packaging==26.3" in result["detail"]


def test_output_evidence_has_a_hard_serialized_size_limit(tmp_path, monkeypatch):
    project = tmp_path
    wheel = _wheel_bytes({"demo.py": b"x"})
    _write(project, "before/demo-1.0-py3-none-any.whl", wheel)
    _write(project, "after/demo-1.0-py3-none-any.whl", wheel)
    monkeypatch.setattr(wheel_artifacts, "MAX_OUTPUT_BYTES", 32)

    result = compare_wheels(project, "before/demo-1.0-py3-none-any.whl",
                            "after/demo-1.0-py3-none-any.whl")

    assert result["status"] == "UNKNOWN"
    assert result["detail"] == "wheel comparison evidence exceeds the 2 MiB output limit"
    assert "difference_count" not in result


@pytest.mark.parametrize(
    ("wheel", "detail"),
    [
        (lambda: _wheel_bytes({"../escape.py": b"x"}), "unsafe ZIP path"),
        (lambda: _wheel_bytes({"demo.py": b"x"}, corrupt_record=True), "RECORD SHA-256"),
        (lambda: _wheel_bytes({"demo.py": b"x"}, symlink="demo.py"), "symlink"),
        (lambda: _wheel_bytes({"Demo.py": b"x", "demo.py": b"y"}), "case-colliding"),
        (lambda: _wheel_bytes({"demo-1.0.data/scripts/demo": b"#!/bin/sh\n"}), "unsupported .data install scheme"),
        (lambda: _wheel_bytes({"demo.py": b"x" * 10_000}), "compression-ratio limit"),
    ],
)
def test_unsupported_wheel_semantics_do_not_become_comparison_results(tmp_path, wheel, detail):
    project = tmp_path
    _write(project, "bad/demo-1.0-py3-none-any.whl", wheel())
    _write(project, "ok/demo-1.0-py3-none-any.whl", _wheel_bytes({"demo.py": b"x"}))

    result = compare_wheels(project, "bad/demo-1.0-py3-none-any.whl",
                            "ok/demo-1.0-py3-none-any.whl")

    assert result["status"] == "UNSUPPORTED"
    assert detail.lower() in result["detail"].lower()


def test_path_must_stay_inside_repository(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    outside = tmp_path / "outside.whl"
    outside.write_bytes(_wheel_bytes({"demo.py": b"x"}))

    result = compare_wheels(project, str(outside), str(outside))

    assert result["status"] == "UNSUPPORTED"
    assert result["detail"] == "wheel path must be repository-relative"
