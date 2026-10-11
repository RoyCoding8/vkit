from __future__ import annotations

import hashlib

import pytest

from vkit.operations._data import read_json


def test_reads_json_and_hashes_the_exact_captured_bytes(tmp_path) -> None:
    raw = b'{ "value": 1 }\n'
    (tmp_path / "source.json").write_bytes(raw)

    value, digest = read_json(tmp_path, "source.json", max_bytes=len(raw))

    assert value == {"value": 1}
    assert digest == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("raw", [
    b'{"outer":{"key":1,"key":2}}',
    b'{"value":NaN}',
    b'{"value":Infinity}',
    b'{"value":1e9999}',
])
def test_rejects_duplicate_keys_and_non_finite_numbers(tmp_path, raw) -> None:
    (tmp_path / "source.json").write_bytes(raw)

    with pytest.raises(ValueError):
        read_json(tmp_path, "source.json", max_bytes=1024)


def test_rejects_oversized_invalid_utf8_and_invalid_json(tmp_path) -> None:
    path = tmp_path / "source.json"
    path.write_bytes(b"12345")
    with pytest.raises(ValueError, match="at most 4 bytes"):
        read_json(tmp_path, "source.json", max_bytes=4)

    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        read_json(tmp_path, "source.json", max_bytes=4)

    path.write_bytes(b"{")
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        read_json(tmp_path, "source.json", max_bytes=4)


@pytest.mark.parametrize("path", ("", "../outside.json", "..\\outside.json", "C:/source.json"))
def test_rejects_non_repository_relative_paths(tmp_path, path) -> None:
    with pytest.raises(ValueError, match="path"):
        read_json(tmp_path, path, max_bytes=100)


def test_internal_symlinks_are_allowed_by_default_and_can_be_rejected(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"ok":true}', encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    assert read_json(tmp_path, "link.json", max_bytes=100)[0] == {"ok": True}
    with pytest.raises(ValueError, match="symlinks"):
        read_json(tmp_path, "link.json", max_bytes=100, reject_symlinks=True)
