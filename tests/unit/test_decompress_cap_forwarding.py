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
    total output is produced. Like the REAL API, max_length=0 means
    UNLIMITED -- and every call is recorded for the boundary assertions."""

    calls = []          # recorded (max_length, returned-chunk-size) pairs

    def __init__(self):
        self._total = 0
        self.eof = False
        self._pending = b""
        self._pos = 0

    def decompress(self, data, max_length=0):
        if data:
            self._pending = data * 10240   # 10-byte input -> 100 KiB "bomb"
            self._pos = 0
        limit = max_length if max_length > 0 else len(self._pending)  # 0 = unlimited
        n = min(limit, len(self._pending) - self._pos)
        chunk = self._pending[self._pos : self._pos + n]
        self._pos += n
        if self._pos >= len(self._pending):
            self.eof = True
        _StubLZ4Decompressor.calls.append((max_length, len(chunk)))
        return chunk


def test_lz4_cap_is_enforced_by_the_streaming_guard(monkeypatch):
    stub = types.ModuleType("lz4")
    frame_mod = types.ModuleType("lz4.frame")
    frame_mod.LZ4FrameDecompressor = _StubLZ4Decompressor
    stub.frame = frame_mod
    monkeypatch.setitem(sys.modules, "lz4", stub)
    monkeypatch.setitem(sys.modules, "lz4.frame", frame_mod)

    # --- the BOMB path: a 100 KiB output with a 20-byte cap
    _StubLZ4Decompressor.calls = []
    with pytest.raises(ValueError, match="decompression bomb"):
        cs._decompress_lz4(b"x" * 10, 20)
    # Round 68 (review): NO call may receive max_length <= 0 (0 == UNLIMITED
    # in the real lz4 API -- that is exactly how the 99 MiB slipped through),
    # and no chunk may exceed cap + 1.
    for ml, chunk in _StubLZ4Decompressor.calls:
        assert ml > 0, f"max_length={ml} is UNLIMITED in the lz4 API (boundary bug)"
        assert chunk <= 21, f"chunk of {chunk} bytes exceeds cap+1=21"

    # --- the boundary path: output fills the cap EXACTLY, then one byte more
    _StubLZ4Decompressor.calls = []
    with pytest.raises(ValueError, match="decompression bomb"):
        cs._decompress_lz4(b"y" * 10, 20)   # same 100 KiB, cap 20 -> trips at 21
    for ml, chunk in _StubLZ4Decompressor.calls:
        assert ml > 0, f"max_length={ml} at the boundary is UNLIMITED"
        assert chunk <= 21

    # --- an honest frame (output == cap exactly) passes
    _StubLZ4Decompressor.calls = []
    out = cs._decompress_lz4(b"z" * 2, 20480)   # 2-byte input -> 20480 == cap
    assert len(out) == 20480
    for ml, chunk in _StubLZ4Decompressor.calls:
        assert ml > 0, f"honest frame got max_length={ml} (would be UNLIMITED)"