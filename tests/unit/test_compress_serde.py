"""Unit tests: lossless compression serde frame (round-trip, fallback, sizes).

These run WITHOUT torch / lz4 / zstandard: the frame logic and the raw
passthrough codec are stdlib-only, and the missing-codec paths must fail
loudly (CodecError), never silently corrupt data. When lz4/zstandard ARE
installed the same round-trip assertions run against them as well.
"""

import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))

from kvpersist.compress_serde import (  # noqa: E402
    CODEC_LZ4,
    CODEC_RAW,
    CODEC_ZSTD,
    FORMAT_VERSION,
    HEADER_LEN,
    MAGIC,
    CodecError,
    LosslessCompressDeserializer,
    LosslessCompressSerializer,
    align_up,
    plaintext_bytes,
)


class _FakeMem:
    """Minimal MemoryObj stand-in: byte_array + set_used_size semantics."""

    def __init__(self, size: int, data: bytes = b""):
        self._buf = bytearray(size)
        self._buf[: len(data)] = data
        self._used_size_override = None

    @property
    def byte_array(self):
        return memoryview(self._buf)

    def set_used_size(self, n: int) -> None:
        self._used_size_override = n


class _FakeLayout:
    def __init__(self, nbytes: int, itemsize: int = 2):
        self.shapes = [_FakeShape(nbytes // itemsize)]
        self.dtypes = [_FakeDtype(itemsize)]


class _FakeShape:
    def __init__(self, n):
        self._n = n

    def __iter__(self):
        return iter([self._n])


class _FakeDtype:
    def __init__(self, itemsize):
        self.itemsize = itemsize


def test_align_up():
    assert align_up(0, 4096) == 0
    assert align_up(1, 4096) == 4096
    assert align_up(4096, 4096) == 4096
    assert align_up(4097, 4096) == 8192
    assert align_up(123, 1) == 123


def test_plaintext_bytes_without_torch():
    # 1000 elements x 2 bytes = 2000, plus a second group of 4 x 4 = 16
    lay = _FakeLayout(2000, itemsize=2)
    lay.shapes.append(_FakeShape(4))
    lay.dtypes.append(_FakeDtype(4))
    assert plaintext_bytes(lay) == 2016


def _roundtrip(codec: str, payload: bytes, align: int = 4096):
    ser = LosslessCompressSerializer(codec, level=3, align_bytes=align)
    src = _FakeMem(len(payload), payload)
    dst = _FakeMem(align_up(len(payload) + HEADER_LEN, align))
    n = ser.serialize(src, dst, None)
    assert n % align == 0, "serialized size must be page-aligned"
    frame = bytes(dst.byte_array)[:n]
    des = LosslessCompressDeserializer()
    out = _FakeMem(len(payload))
    des.deserialize(_FakeMem(n, frame), out, None)
    assert bytes(out.byte_array)[: len(payload)] == payload
    return frame, n


def test_roundtrip_raw_codec_exact():
    payload = os.urandom(5000)
    frame, n = _roundtrip("raw", payload)
    magic, version, codec_id, plain_len, payload_len = struct.unpack(
        "<4sHHqq", frame[:HEADER_LEN]
    )
    assert magic == MAGIC
    assert version == FORMAT_VERSION
    assert codec_id == CODEC_RAW
    assert plain_len == 5000 and payload_len == 5000
    assert n == align_up(5000 + HEADER_LEN, 4096)


def test_roundtrip_raw_incompressible_never_grows():
    """Random data: raw codec stores it verbatim, frame fits the estimate."""
    payload = os.urandom(10000)
    frame, n = _roundtrip("raw", payload)
    est = LosslessCompressSerializer("raw", align_bytes=4096).estimate_serialized_size(
        _FakeLayout(10000, itemsize=1)
    )
    assert n <= est
    _, _, codec_id, _, payload_len = struct.unpack("<4sHHqq", frame[:HEADER_LEN])
    assert codec_id == CODEC_RAW
    assert payload_len == 10000


@pytest.mark.parametrize("codec", ["lz4", "zstd"])
def test_roundtrip_and_shrink_with_real_codec(codec):
    try:
        if codec == "lz4":
            import lz4.frame  # noqa: F401
        else:
            import zstandard  # noqa: F401
    except ImportError:
        pytest.skip(f"{codec} python bindings not installed")
    payload = b"kv-cache-block" * 5000  # highly compressible
    ser = LosslessCompressSerializer(codec, level=3, align_bytes=4096)
    src = _FakeMem(len(payload), payload)
    dst = _FakeMem(align_up(len(payload) + HEADER_LEN, 4096))
    n = ser.serialize(src, dst, None)
    frame = bytes(dst.byte_array)[:n]
    _, _, codec_id, plain_len, payload_len = struct.unpack("<4sHHqq", frame[:HEADER_LEN])
    assert plain_len == len(payload)
    assert codec_id in (CODEC_LZ4, CODEC_ZSTD), "compressible data must stay compressed"
    assert payload_len < plain_len, "compressed payload must be smaller than raw"
    des = LosslessCompressDeserializer()
    out = _FakeMem(len(payload))
    des.deserialize(_FakeMem(n, frame), out, None)
    assert bytes(out.byte_array)[: len(payload)] == payload


def test_missing_codec_raises_codec_error():
    try:
        import lz4.frame  # noqa: F401

        pytest.skip("lz4 installed; missing-codec path not reachable")
    except ImportError:
        pass
    ser = LosslessCompressSerializer("lz4", align_bytes=4096)
    src = _FakeMem(10, b"0123456789")
    dst = _FakeMem(4096)
    with pytest.raises(CodecError):
        ser.serialize(src, dst, None)


def test_deserializer_rejects_bad_magic():
    des = LosslessCompressDeserializer()
    frame = bytearray(_FakeMem(HEADER_LEN, b"\x00" * HEADER_LEN).byte_array)
    struct.pack_into("<4sHHqq", frame, 0, b"XXXX", FORMAT_VERSION, CODEC_RAW, 1, 1)
    with pytest.raises(ValueError, match="bad magic"):
        des.deserialize(_FakeMem(len(frame), bytes(frame)), _FakeMem(1), None)


def test_deserializer_rejects_truncated_payload():
    des = LosslessCompressDeserializer()
    frame = bytearray(HEADER_LEN + 4)
    struct.pack_into("<4sHHqq", frame, 0, MAGIC, FORMAT_VERSION, CODEC_RAW, 100, 100)
    with pytest.raises(ValueError, match="truncated"):
        des.deserialize(_FakeMem(len(frame), bytes(frame)), _FakeMem(100), None)


def test_deserializer_rejects_size_mismatch():
    des = LosslessCompressDeserializer()
    payload = b"abcd"
    frame = bytearray(HEADER_LEN + len(payload))
    struct.pack_into("<4sHHqq", frame, 0, MAGIC, FORMAT_VERSION, CODEC_RAW, 99, len(payload))
    frame[HEADER_LEN:] = payload
    with pytest.raises(ValueError, match="mismatch"):
        des.deserialize(_FakeMem(len(frame), bytes(frame)), _FakeMem(99), None)


def test_estimate_is_upper_bound_padded():
    ser = LosslessCompressSerializer("raw", align_bytes=4096)
    est_small = ser.estimate_serialized_size(_FakeLayout(4000, itemsize=1))
    assert est_small == 4096, "4000 + 24 header = 4024 -> one 4 KiB page"
    est_big = ser.estimate_serialized_size(_FakeLayout(4100, itemsize=1))
    assert est_big == 8192, "4100 + 24 header = 4124 -> two 4 KiB pages"