"""Unit test: content-key deduplication MODEL (simulation, not LMCache).

Review finding: the old `tests/integration/test_dedup.py` claimed to measure
LMCache deduplication but only wrote bytes with plain Python functions in a
temp dir and was skipped without the `lmcache` CLI. It proves nothing about the
real backend. This file is the renamed, honestly-labelled simulation: it tests
the content-key *model* that LMCache implements (identical chunk content ⇒
identical key ⇒ one stored object).

The REAL backend behaviour is covered by
`tests/integration/test_l2_dedup_real.py`, which drives the LMCache MP
protocol client against a live `lmcache server` and measures the actual L2
files. Nothing here talks to LMCache.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))


def _content_key(blob: bytes) -> str:
    """Stands in for LMCache's content-derived chunk key."""
    import hashlib

    return hashlib.sha256(blob).hexdigest()


class _FakeContentStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def store(self, blob: bytes) -> Path:
        p = self.root / (_content_key(blob) + ".bin")
        if not p.exists():
            p.write_bytes(blob)
        return p

    def usage(self) -> int:
        return sum(f.stat().st_size for f in self.root.iterdir())


def test_identical_content_stores_once(tmp_path):
    store = _FakeContentStore(tmp_path / "l2")
    shared = b"P" * 8192
    store.store(shared)
    first = store.usage()
    store.store(shared)  # same content => same key => no new file
    assert store.usage() == first, "identical content must not be stored twice"


def test_shared_prefix_between_two_suffixes_not_double_stored(tmp_path):
    """Two conversations sharing a stable prefix: the shared bytes land once."""
    store = _FakeContentStore(tmp_path / "l2")
    prefix = b"P" * 8192
    suffix_a = b"A" * 1024
    suffix_b = b"B" * 1024

    store.store(prefix)
    store.store(suffix_a)
    after_a = store.usage()
    store.store(prefix)   # conversation B: same prefix chunk
    store.store(suffix_b)
    after_b = store.usage()

    assert after_b - after_a == len(suffix_b), (
        "the shared prefix must NOT add bytes on the second conversation "
        f"(delta {after_b - after_a} != {len(suffix_b)})"
    )