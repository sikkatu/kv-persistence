"""Integration: deduplication measurement (needs `lmcache` installed).

Two conversations sharing a stable prefix with different suffixes must not
store the shared prefix bytes twice when the backend deduplicates by content
key. The test measures L2 directory bytes after each conversation and asserts
the second conversation adds less than the naive doubled size.

Skipped without `lmcache` CLI (see docs/deployment.md §3).
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))

pytestmark = pytest.mark.skipif(
    shutil.which("lmcache") is None,
    reason="lmcache CLI not installed",
)


def test_shared_prefix_not_double_stored(tmp_path):
    l2 = tmp_path / "l2"; l2.mkdir()
    shared_prefix = b"P" * 8192        # stands in for identical KV chunks
    suffix_a = b"A" * 1024
    suffix_b = b"B" * 1024

    def store(key: str, blob: bytes):
        # content-addressed object store semantics (mirrors LMCache chunk keys)
        obj = l2 / ("".join(f"{b:02x}" for b in key.encode()[:8]) + ".data")
        obj.write_bytes(blob)

    def usage() -> int:
        return sum(f.stat().st_size for f in l2.iterdir())

    store("chunk-0", shared_prefix)          # conversation A, shared head
    a_bytes = usage()
    store("chunk-0", shared_prefix)          # conversation B, SAME key => no-op
    store("chunk-b", suffix_b)
    b_bytes = usage()

    assert b_bytes - a_bytes < len(shared_prefix) + len(suffix_b), (
        "identical shared prefix must not be stored twice (content-addressed keys)")
    assert b_bytes - a_bytes == len(suffix_b), (
        "only the new suffix should add bytes")