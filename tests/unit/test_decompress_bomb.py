"""Round 66: decompression-bomb guard tests (review deferred item 1).

A KVCZ frame may LIE about its uncompressed size ("decompression bomb"):
a few payload bytes claiming an enormous plain_len. The deserializer must
reject it cleanly (ValueError -> cache MISS) BEFORE any big allocation.

Covered:
  * plain_len above the destination buffer -> clean error (pre-existing)
  * plain_len above KVP_DECOMPRESS_MAX_BYTES -> clean error, no allocation
  * a real zstd bomb (tiny payload, huge output) -> capped by max_output_size
  * a lying plain_len on a real zstd frame -> clean error
"""
from __future__ import annotations
import os
import struct
import sys
import types
import zlib

import pytest

from kvpersist.compress_serde import (
    HEADER_LEN,
    MAGIC,
    FORMAT_VERSION,
    CODEC_RAW,
    CODEC_LZ4,
    CODEC_ZSTD,
    LosslessCompressDeserializer,
)


class _FakeMem:
    def __init__(self, size, data=b""):
        self._buf = bytearray(size)
        self._buf[: len(data)] = data

    @property
    def byte_array(self):
        return self._buf

    def set_used_size(self, n):
        pass


def _frame(plain_len, payload, codec=CODEC_RAW, crc=None):
    if crc is None:
        crc = zlib.crc32(payload) & 0xFFFFFFFF
    f = bytearray(HEADER_LEN + len(payload))
    struct.pack_into(
        "<4sHHqqI", f, 0, MAGIC, FORMAT_VERSION, codec, plain_len, len(payload), crc
    )
    f[HEADER_LEN:] = payload
    return bytes(f)


def test_lying_plain_len_rejected_before_decompression(monkeypatch):
    """A frame claiming 100 GiB uncompressed with 4 payload bytes must fail
    BEFORE the codec runs -- the error is about the declared size."""
    monkeypatch.delenv("KVP_DECOMPRESS_MAX_BYTES", raising=False)
    des = LosslessCompressDeserializer()
    frame = _frame(100 << 30, b"abcd")
    with pytest.raises(ValueError, match="exceeds the allowed"):
        des.deserialize(_FakeMem(len(frame), frame), _FakeMem(1 << 30), None)


def test_configurable_ceiling_is_enforced(monkeypatch):
    monkeypatch.setenv("KVP_DECOMPRESS_MAX_BYTES", "1024")
    des = LosslessCompressDeserializer()
    frame = _frame(2048, b"abcd")
    with pytest.raises(ValueError, match="KVP_DECOMPRESS_MAX_BYTES"):
        des.deserialize(_FakeMem(len(frame), frame), _FakeMem(1 << 20), None)


def test_plain_len_larger_than_destination_rejected():
    """Even below the ceiling, a plain_len bigger than the destination buffer
    must be refused (checked BEFORE decompression)."""
    des = LosslessCompressDeserializer()
    frame = _frame(4096, b"abcd")
    with pytest.raises(ValueError, match="destination buffer too small"):
        des.deserialize(_FakeMem(len(frame), frame), _FakeMem(1024), None)


def test_zstd_decompression_bomb_is_capped(monkeypatch):
    """A tiny payload that inflates to 100 MiB must NOT allocate it when the
    ceiling is 1 MiB -- zstd's max_output_size stops it."""
    zstandard = pytest.importorskip("zstandard")
    monkeypatch.setenv("KVP_DECOMPRESS_MAX_BYTES", str(1 << 20))
    bomb_plain = b"\x00" * (100 << 20)          # 100 MiB of zeros
    payload = zstandard.ZstdCompressor(level=10).compress(bomb_plain)
    assert len(payload) < (1 << 20), "test setup: the bomb must compress small"
    des = LosslessCompressDeserializer()
    frame = _frame(len(bomb_plain), payload, codec=CODEC_ZSTD,
                   crc=zlib.crc32(bomb_plain) & 0xFFFFFFFF)
    with pytest.raises(ValueError, match="exceeds the allowed"):
        des.deserialize(_FakeMem(len(frame), frame), _FakeMem(1 << 30), None)


def test_zstd_lie_about_size_after_the_cap(monkeypatch):
    """A frame lying DOWNWARD (plain_len=2 KiB, real output 4 KiB) must be
    refused by the codec cap at plain_len bytes -- it must NOT be allowed to
    inflate up to the ceiling first (round 67: the bound is plain_len)."""
    zstandard = pytest.importorskip("zstandard")
    monkeypatch.delenv("KVP_DECOMPRESS_MAX_BYTES", raising=False)
    real = b"\x01" * 4096
    payload = zstandard.ZstdCompressor().compress(real)
    des = LosslessCompressDeserializer()
    # claim 2048 while the payload really decompresses to 4096
    frame = _frame(2048, payload, codec=CODEC_ZSTD,
                   crc=zlib.crc32(real) & 0xFFFFFFFF)
    with pytest.raises(ValueError):
        des.deserialize(_FakeMem(len(frame), frame), _FakeMem(1 << 20), None)


def test_codec_cap_is_plain_len_not_the_ceiling(monkeypatch):
    """Round 67 (review): the OUTPUT BOUND forwarded to the codec must be the
    frame's plain_len -- the round-66 code passed KVP_DECOMPRESS_MAX_BYTES,
    so a downward-lying frame could inflate to the ceiling first."""
    calls = []

    class _SpyDecompressor:
        def decompress(self, data, max_output_size=0):
            calls.append(max_output_size)
            return b"\x00" * max_output_size

    stub = types.ModuleType("zstandard")
    stub.ZstdDecompressor = _SpyDecompressor
    monkeypatch.setitem(sys.modules, "zstandard", stub)
    monkeypatch.setenv("KVP_DECOMPRESS_MAX_BYTES", str(1 << 30))  # huge ceiling
    des = LosslessCompressDeserializer()
    frame = _frame(2048, b"payload", codec=CODEC_ZSTD, crc=zlib.crc32(b"\x00" * 2048) & 0xFFFFFFFF)
    des.deserialize(_FakeMem(len(frame), frame), _FakeMem(1 << 20), None)
    assert calls == [2048], f"codec got {calls}, want [2048] (plain_len, not the ceiling)"


def test_empty_frame_never_asks_for_unbounded_output(monkeypatch):
    """zstandard reads max_output_size=0 as 'not set' -- plain_len == 0 must
    be capped to 1 byte instead (an empty frame yields b"", anything bigger is
    refused by the codec before it allocates)."""
    calls = []

    class _SpyDecompressor:
        def decompress(self, data, max_output_size=0):
            calls.append(max_output_size)
            return b""

    stub = types.ModuleType("zstandard")
    stub.ZstdDecompressor = _SpyDecompressor
    monkeypatch.setitem(sys.modules, "zstandard", stub)
    monkeypatch.delenv("KVP_DECOMPRESS_MAX_BYTES", raising=False)
    des = LosslessCompressDeserializer()
    frame = _frame(0, b"", codec=CODEC_ZSTD, crc=zlib.crc32(b"") & 0xFFFFFFFF)
    des.deserialize(_FakeMem(len(frame), frame), _FakeMem(64), None)
    assert calls == [1], f"empty frame must cap at 1 byte, got {calls}"


def test_honest_frame_still_roundtrips(monkeypatch):
    """The guard must not break honest frames."""
    zstandard = pytest.importorskip("zstandard")
    monkeypatch.delenv("KVP_DECOMPRESS_MAX_BYTES", raising=False)
    from kvpersist.compress_serde import LosslessCompressSerializer
    data = b"kvp-honest-frame" * 64
    ser = LosslessCompressSerializer("zstd")
    dst = _FakeMem(1 << 16)
    n = ser.serialize(data, dst, None)
    frame = bytes(dst.byte_array)[:n]
    out = _FakeMem(1 << 16)
    LosslessCompressDeserializer().deserialize(_FakeMem(len(frame), frame), out, None)
    assert bytes(out.byte_array)[: len(data)] == data