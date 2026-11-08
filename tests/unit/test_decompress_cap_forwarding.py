"""Round 66: verify the codec output cap is actually FORWARDED (no real bindings needed).

The zstandard/lz4 bindings may be absent (the codec tests skip in that case),
but the guard logic must still be verifiable: these tests stub the codec
modules and assert that the decompressor passes the cap as the output bound
and stops at it.
"""
from __future__ import annotations
import sys
import types

import pytest

from kvpersist import compress_serde as cs


class _StubZstdDecompressor:
    """Simulates zstandard: honors max_output_size like the real binding
    (raises when the output would exceed it)."""

    def decompress(self, data, max_output_size=0):
        # pretend the payload inflates to 3x its size
        out = data * 3
        if max_output_size and len(out) > max_output_size:
            raise RuntimeError(f"decompression output exceeds max_output_size {max_output_size}")
        return out


def test_zstd_cap_is_forwarded_and_enforced(monkeypatch):
    stub = types.ModuleType("zstandard")
    stub.ZstdDecompressor = _StubZstdDecompressor
    monkeypatch.setitem(sys.modules, "zstandard", stub)
    # 10-byte payload inflates to 30 bytes; cap 20 -> the stub must refuse
    with pytest.raises(ValueError):
        cs._decompress_zstd(b"x" * 10, 20)
    # cap 30 -> exactly fits
    out = cs._decompress_zstd(b"x" * 10, 30)
    assert len(out) == 30


class _StubLZ4Decompressor:
    """Simulates lz4.frame.LZ4FrameDecompressor with max_length semantics:
    each call emits at most max_length bytes and eof is reached after the
    total output is produced."""

    def __init__(self):
        self._total = 0
        self.eof = False

    def decompress(self, data, max_length=0):
        if data:
            self._pending = data * 3   # same 3x "bomb"
            self._pos = 0
        if self._pos >= len(self._pending):
            self.eof = True
            return b""
        n = min(max_length or len(self._pending), len(self._pending) - self._pos)
        chunk = self._pending[self._pos : self._pos + n]
        self._pos += n
        if self._pos >= len(self._pending):
            self.eof = True
        return chunk


def test_lz4_cap_is_enforced_by_the_streaming_guard(monkeypatch):
    stub = types.ModuleType("lz4")
    frame_mod = types.ModuleType("lz4.frame")
    frame_mod.LZ4FrameDecompressor = _StubLZ4Decompressor
    stub.frame = frame_mod
    monkeypatch.setitem(sys.modules, "lz4", stub)
    monkeypatch.setitem(sys.modules, "lz4.frame", frame_mod)
    # 10-byte payload inflates to 30; the guard must raise once output > cap
    with pytest.raises(ValueError, match="decompression bomb"):
        cs._decompress_lz4(b"x" * 10, 20)
    # a generous cap passes through
    out = cs._decompress_lz4(b"x" * 10, 30)
    assert len(out) == 30