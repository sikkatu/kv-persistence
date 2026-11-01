"""Integration: REAL deduplication measurement against a live `lmcache server`.

Review finding: the previous `test_dedup.py` only wrote bytes with plain Python
functions and never touched LMCache -- it could not prove backend dedup. This
test closes that gap: it drives the REAL MP protocol (the same client as
`test_l2_persistence.py`) through a real `lmcache server` subprocess and
measures the ACTUAL L2 files written by the `nixl_store_dynamic` adapter.

Scenario (the review's requested one):
  * two requests share a stable tokenized prefix and differ in the suffix;
  * the shared prefix chunks are stored once (identical content => identical
    content-derived chunk key => ONE file);
  * the second request adds only its suffix chunk files on disk;
  * the first request still LOOKUPs all of its chunks afterwards (no silent
    eviction of the shared blocks by the second store).

Runs only with `KVP_LIVE_LMCACHE=1` (pinned lmcache 0.5.3 + POSIX-SHM Linux
host) -- same external prerequisite as the other live tests, see
docs/benchmarks.md.
"""

import os
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "implementation"))

import test_l2_persistence as pers  # noqa: E402  (shared live-server helpers)

pytestmark = pytest.mark.skipif(
    os.environ.get("KVP_LIVE_LMCACHE") != "1",
    reason="real LMCache dedup measurement needs the pinned lmcache package "
           "(0.5.3) and a POSIX-SHM Linux host: set KVP_LIVE_LMCACHE=1 "
           "(documented external prerequisite, see docs/benchmarks.md)",
)

# Shared stable prefix: 3 chunks of 16 tokens (48 tokens). The helpers fill
# KV tensors from a fixed seed, so equal token ids always carry equal bytes.
_PREFIX_TOKENS = 48
_SUFFIX_TOKENS = 16  # exactly one chunk


def _wait_new_files(l2: Path, at_least: int, timeout_s: int = 60) -> list:
    """Store is asynchronous -- poll the adapter's own output files."""
    deadline = time.time() + timeout_s
    files: list = []
    while time.time() < deadline:
        files = pers._l2_data_files(l2)
        if len(files) >= at_least:
            return files
        time.sleep(1)
    return files


def test_shared_prefix_chunks_stored_once_real_backend(tmp_path):
    from lmc_protocol_client import LmcClient, build_token_ids

    l2 = tmp_path / "l2"
    adapter = pers._render_adapter(tmp_path, budget_gb="1")
    log = tmp_path / "dedup.log"
    zmq_port, http_port = pers._free_port(), pers._free_port()
    server = None
    try:
        server = pers._start_server(adapter, zmq_port, http_port, log, "dedup")
        pers._wait_ready(server, http_port, log)

        client = LmcClient(
            f"tcp://127.0.0.1:{zmq_port}", "mimo-pro/dedup", instance_id=7100
        )
        try:
            # Conversation A: prefix + suffix A (4 chunks total).
            base = list(build_token_ids(seq_no=7, num_tokens=_PREFIX_TOKENS))
            tokens_a = base + list(build_token_ids(seq_no=8, num_tokens=_SUFFIX_TOKENS))
            status_a = client.store(tokens_a, "dedup-store-A")
            assert status_a is not None, "store of conversation A failed"

            files_a = _wait_new_files(l2, at_least=4)
            assert len(files_a) >= 4, (
                f"conversation A must write its 4 chunks to L2, got "
                f"{len(files_a)} files: {[p.name for p in files_a]}"
            )
            count_a = len(files_a)

            # Conversation B: the SAME prefix + a different suffix.
            tokens_b = base + list(build_token_ids(seq_no=9, num_tokens=_SUFFIX_TOKENS))
            status_b = client.store(tokens_b, "dedup-store-B")
            assert status_b is not None, "store of conversation B failed"

            files_b = _wait_new_files(l2, at_least=count_a + 1)
            added = len(files_b) - count_a
            assert added == 1, (
                "the shared 3-chunk prefix must NOT be stored twice: expected "
                f"exactly 1 new L2 file for B's suffix, got {added} "
                f"({[p.name for p in files_b]})"
            )

            # The shared blocks must still serve the FIRST conversation.
            hits_a = client.lookup_hits(tokens_a, "dedup-lookup-A")
            assert hits_a == 4, (
                f"conversation A must still hit all 4 chunks after B was "
                f"stored, got {hits_a}/4 -- shared blocks were not reused"
            )
            hits_b = client.lookup_hits(tokens_b, "dedup-lookup-B")
            assert hits_b == 4, (
                f"conversation B must hit all 4 chunks (3 shared + 1 own), "
                f"got {hits_b}/4"
            )
        finally:
            client.close()
    finally:
        pers._stop_server(server, log, "dedup") if server is not None else None