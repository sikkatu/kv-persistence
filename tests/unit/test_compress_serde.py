"""Unit tests: lossless compression serde frame (round-trip, padding, CRC, sizes).

Frame format v2 (28-byte header + payload + zero pad):

    offset  size  field
    0       4     magic "KVCZ"
    4       2     format version (2)
    6       2     codec id (1=raw, 2=lz4, 3=zstd)
    8       8     uncompressed length
    16      8     stored payload length
    24      4     CRC32 of the UNCOMPRESSED payload
    28      N     payload
    ...     pad   ZERO padding up to the page size

These run WITHOUT torch / lz4 / zstandard: the frame logic and the raw
passthrough codec are stdlib-only. When lz4/zstandard ARE installed the same
round-trip assertions run against them as well.
"""

import os
import struct
import sys
import zlib
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
    check_codec_available,
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


class _FakeShape:
    def __init__(self, n):
        self._n = n

    def __iter__(self):
        return iter([self._n])


class _FakeDtype:
    def __init__(self, itemsize):
        self.itemsize = itemsize


class _FakeLayout:
    def __init__(self, nbytes: int, itemsize: int = 2):
        self.shapes = [_FakeShape(nbytes // itemsize)]
        self.dtypes = [_FakeDtype(itemsize)]


# --- helpers ---------------------------------------------------------------


def _parse_header(frame: bytes):
    return struct.unpack("<4sHHqqI", frame[:HEADER_LEN])


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


# --- frame format v2 -------------------------------------------------------


def test_header_fields_v2():
    payload = b"hello world"
    frame, n = _roundtrip("raw", payload)
    magic, version, codec_id, plain_len, payload_len, crc = _parse_header(frame)
    assert magic == MAGIC
    assert version == FORMAT_VERSION
    assert codec_id == CODEC_RAW
    assert plain_len == len(payload) and payload_len == len(payload)
    assert crc == zlib.crc32(payload) & 0xFFFFFFFF


def test_roundtrip_raw_codec_exact():
    payload = os.urandom(5000)
    frame, n = _roundtrip("raw", payload)
    assert n == align_up(5000 + HEADER_LEN, 4096)


def test_padding_is_zeroed():
    """The padding after the payload MUST be zeroed (review point: a recycled
    L1 slab block could leak another tenant's bytes into our cache file)."""
    payload = b"A" * 100
    frame, n = _roundtrip("raw", payload)
    _, _, _, plain_len, payload_len, _ = _parse_header(frame)
    pad_start = HEADER_LEN + payload_len
    pad = frame[pad_start:]
    assert pad, "frame must have padding to reach the page boundary"
    assert all(b == 0 for b in pad), (
        f"padding must be zeroed, found {sum(1 for b in pad if b)} non-zero bytes"
    )


def test_chunk_never_grows_by_more_than_one_page():
    """A chunk is at most ONE page larger than the raw payload (review point:
    'chunk never grows' was imprecise -- it can grow by up to one page)."""
    for raw_len in (1, 100, 4000, 4096, 8192):
        payload = os.urandom(raw_len)
        _, n = _roundtrip("raw", payload)
        assert n <= align_up(raw_len + HEADER_LEN, 4096)
        assert n <= raw_len + 4096, (
            f"raw_len={raw_len}: frame {n} exceeds raw + one page"
        )


def test_roundtrip_raw_incompressible_never_grows():
    payload = os.urandom(10000)
    frame, n = _roundtrip("raw", payload)
    est = LosslessCompressSerializer("raw", align_bytes=4096).estimate_serialized_size(
        _FakeLayout(10000, itemsize=1)
    )
    assert n <= est
    _, _, codec_id, _, payload_len, _ = _parse_header(frame)
    assert codec_id == CODEC_RAW
    assert payload_len == 10000


# --- CRC32 integrity -------------------------------------------------------


def test_crc_detects_bit_flip():
    """A single flipped byte in the payload must raise, never return corrupt KV."""
    payload = b"stable-kv-cache-block" * 100
    ser = LosslessCompressSerializer("raw", align_bytes=4096)
    src = _FakeMem(len(payload), payload)
    dst = _FakeMem(align_up(len(payload) + HEADER_LEN, 4096))
    n = ser.serialize(src, dst, None)
    frame = bytearray(dst.byte_array)[:n]
    # Flip one byte inside the payload.
    frame[HEADER_LEN + 5] ^= 0x01
    des = LosslessCompressDeserializer()
    with pytest.raises(ValueError, match="CRC32"):
        des.deserialize(_FakeMem(n, bytes(frame)), _FakeMem(len(payload)), None)


def test_crc_detects_truncated_payload():
    des = LosslessCompressDeserializer()
    payload = b"abcd"
    frame = bytearray(HEADER_LEN + len(payload))
    struct.pack_into(
        "<4sHHqqI", frame, 0, MAGIC, FORMAT_VERSION, CODEC_RAW, 99, len(payload),
        zlib.crc32(b"WRONG") & 0xFFFFFFFF,
    )
    frame[HEADER_LEN:] = payload
    with pytest.raises(ValueError):
        des.deserialize(_FakeMem(len(frame), bytes(frame)), _FakeMem(99), None)


def test_deserializer_rejects_bad_magic():
    des = LosslessCompressDeserializer()
    frame = bytearray(HEADER_LEN + 4)
    struct.pack_into(
        "<4sHHqqI", frame, 0, b"XXXX", FORMAT_VERSION, CODEC_RAW, 1, 1, 0
    )
    with pytest.raises(ValueError, match="bad magic"):
        des.deserialize(_FakeMem(len(frame), bytes(frame)), _FakeMem(1), None)


def test_deserializer_rejects_truncated_payload():
    des = LosslessCompressDeserializer()
    frame = bytearray(HEADER_LEN + 4)
    struct.pack_into(
        "<4sHHqqI", frame, 0, MAGIC, FORMAT_VERSION, CODEC_RAW, 100, 100, 0
    )
    with pytest.raises(ValueError, match="truncated"):
        des.deserialize(_FakeMem(len(frame), bytes(frame)), _FakeMem(100), None)


def test_deserializer_rejects_size_mismatch():
    des = LosslessCompressDeserializer()
    payload = b"abcd"
    frame = bytearray(HEADER_LEN + len(payload))
    struct.pack_into(
        "<4sHHqqI", frame, 0, MAGIC, FORMAT_VERSION, CODEC_RAW, 99, len(payload),
        zlib.crc32(payload) & 0xFFFFFFFF,
    )
    frame[HEADER_LEN:] = payload
    with pytest.raises(ValueError, match="mismatch"):
        des.deserialize(_FakeMem(len(frame), bytes(frame)), _FakeMem(99), None)


# --- fail-fast codec availability (review point) ---------------------------


def test_check_codec_available_raises_for_missing_codec():
    try:
        import lz4.frame  # noqa: F401

        pytest.skip("lz4 installed; missing-codec path not reachable")
    except ImportError:
        pass
    with pytest.raises(CodecError, match="lz4"):
        check_codec_available("lz4")


def test_check_codec_available_passes_for_raw():
    check_codec_available("raw")  # raw never needs a third-party binding


def test_register_fails_fast_without_codec_bindings():
    """register() must raise CodecError BEFORE the server reaches READY."""
    try:
        import zstandard  # noqa: F401

        pytest.skip("zstandard installed; missing-codec path not reachable")
    except ImportError:
        pass
    # Force a register() attempt with a codec whose binding is missing.
    # register() only imports LMCache lazily; when LMCache is absent it
    # returns False without raising. When it IS present the codec check
    # runs first and raises.
    try:
        import lmcache  # noqa: F401

        lmcache_present = True
    except ImportError:
        lmcache_present = False
    if not lmcache_present:
        pytest.skip("lmcache not installed; register() returns False silently")
    from kvpersist.compress_serde import register

    with pytest.raises(CodecError):
        register()


# --- real codecs (skipped when bindings are absent) ------------------------


@pytest.mark.parametrize("codec", ["lz4", "zstd"])
def test_roundtrip_and_shrink_with_real_codec(codec):
    try:
        if codec == "lz4":
            import lz4.frame  # noqa: F401
        else:
            import zstandard  # noqa: F401
    except ImportError:
        pytest.skip(f"{codec} python bindings not installed")
    payload = b"kv-cache-block" * 5000
    ser = LosslessCompressSerializer(codec, level=3, align_bytes=4096)
    src = _FakeMem(len(payload), payload)
    dst = _FakeMem(align_up(len(payload) + HEADER_LEN, 4096))
    n = ser.serialize(src, dst, None)
    frame = bytes(dst.byte_array)[:n]
    _, _, codec_id, plain_len, payload_len, crc = _parse_header(frame)
    assert plain_len == len(payload)
    assert codec_id in (CODEC_LZ4, CODEC_ZSTD), "compressible data must stay compressed"
    assert payload_len < plain_len, "compressed payload must be smaller than raw"
    assert crc == zlib.crc32(payload) & 0xFFFFFFFF
    des = LosslessCompressDeserializer()
    out = _FakeMem(len(payload))
    des.deserialize(_FakeMem(n, frame), out, None)
    assert bytes(out.byte_array)[: len(payload)] == payload
    # Padding must still be zeroed after compression.
    pad = frame[HEADER_LEN + payload_len:]
    assert all(b == 0 for b in pad)


def test_missing_codec_raises_codec_error_at_serialize():
    try:
        import lz4.frame  # noqa: F401

        pytest.skip("lz4 installed; missing-codec path not reachable")
    except ImportError:
        pass
    ser = LosslessCompressSerializer("lz4", align_bytes=4096)
    src = _FakeMem(10, b"0123456789")
    dst = _FakeMem(4096)
    with pytest.raises((CodecError, ModuleNotFoundError)):
        ser.serialize(src, dst, None)


# --- sizes -----------------------------------------------------------------


def test_align_up():
    assert align_up(0, 4096) == 0
    assert align_up(1, 4096) == 4096
    assert align_up(4096, 4096) == 4096
    assert align_up(4097, 4096) == 8192
    assert align_up(123, 1) == 123


def test_plaintext_bytes_without_torch():
    lay = _FakeLayout(2000, itemsize=2)
    lay.shapes.append(_FakeShape(4))
    lay.dtypes.append(_FakeDtype(4))
    assert plaintext_bytes(lay) == 2016


def test_estimate_is_upper_bound_padded():
    ser = LosslessCompressSerializer("raw", align_bytes=4096)
    est_small = ser.estimate_serialized_size(_FakeLayout(4000, itemsize=1))
    assert est_small == 4096, "4000 + 28 header = 4028 -> one 4 KiB page"
    est_big = ser.estimate_serialized_size(_FakeLayout(4100, itemsize=1))
    assert est_big == 8192, "4100 + 28 header = 4128 -> two 4 KiB pages"