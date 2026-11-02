"""Integration: REAL deduplication measurement against a live `lmcache server`.

Review fixes over the first revision of this test:
* the server is launched through `kvpersist.launch` (NOT the salt patch alone),
  so the tmp_guard active-write registry and the lossless serde registration
  run in the same process as the real data path;
* `_wait_quiet()` waits for the L2 directory to become STABLE (unchanged file
  set for several consecutive polls) instead of returning at the first count
  -- async stores must all finish before measuring;
* besides counting files, the test asserts the SHARED prefix files are
  BYTE-IDENTICAL and UNCHANGED (same size, same mtime) after the second
  request: a silently rewritten identical path would not change the file
  count, so content stability is checked too;
* both conversations must still LOOKUP all of their chunks afterwards.

Runs only with `KVP_LIVE_LMCACHE=1` (pinned lmcache 0.5.3 + POSIX-SHM Linux
host) -- same external prerequisite as the other live tests, see
docs/benchmarks.md. Without that flag this test SKIPS: a plain `pytest` run
does NOT verify real backend dedup (honestly stated, per review).
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
# Directory must be unchanged this many consecutive polls before measuring.
_QUIET_POLLS = 5
_QUIET_INTERVAL_S = 2


def _snapshot(l2: Path) -> dict:
    """name -> (size, mtime_ns) of every non-empty *.bin under l2."""
    out = {}
    for p in pers._l2_data_files(l2):
        st = p.stat()
        out[p.name] = (st.st_size, st.st_mtime_ns)
    return out


def _wait_quiet(l2: Path, timeout_s: int = 120) -> dict:
    """Wait until the L2 directory stops changing (all async stores done)."""
    deadline = time.time() + timeout_s
    prev, stable = None, 0
    while time.time() < deadline:
        cur = _snapshot(l2)
        if prev is not None and cur == prev:
            stable += 1
            if stable >= _QUIET_POLLS:
                return cur
        else:
            stable = 0
        prev = cur
        time.sleep(_QUIET_INTERVAL_S)
    return prev or {}


def test_shared_prefix_chunks_stored_once_real_backend(tmp_path):
    from lmc_protocol_client import LmcClient, build_token_ids

    l2 = tmp_path / "l2"
    adapter = pers._render_adapter(tmp_path, budget_gb="1")
    log = tmp_path / "dedup.log"
    zmq_port, http_port = pers._free_port(), pers._free_port()
    server = None
    try:
        # launch through kvpersist.launch: salt patch + tmp_guard + serde
        # registration all active in the real write path.
        server = pers._start_server(
            adapter, zmq_port, http_port, log, "dedup", launcher="launch"
        )
        pers._wait_ready(server, http_port, log)

        client = LmcClient(
            f"tcp://127.0.0.1:{zmq_port}", "mimo-pro/dedup", instance_id=7100
        )
        try:
            # ---- Conversation A: prefix + suffix A (4 chunks total) ----
            base = list(build_token_ids(seq_no=7, num_tokens=_PREFIX_TOKENS))
            tokens_a = base + list(build_token_ids(seq_no=8, num_tokens=_SUFFIX_TOKENS))
            status_a = client.store(tokens_a, "dedup-store-A")
            assert status_a is not None, "store of conversation A failed"

            snap_a = _wait_quiet(l2)
            assert len(snap_a) >= 4, (
                f"conversation A must write its 4 chunks to L2, got "
                f"{len(snap_a)} files: {sorted(snap_a)}"
            )

            # Identify the SHARED prefix chunk files: the first 3 chunks of A
            # come from `base`, so they are the files NOT created for the
            # suffix. We know B shares them -- they must survive unchanged.
            names_a = set(snap_a)

            # ---- Conversation B: SAME prefix + different suffix ----
            tokens_b = base + list(build_token_ids(seq_no=9, num_tokens=_SUFFIX_TOKENS))
            status_b = client.store(tokens_b, "dedup-store-B")
            assert status_b is not None, "store of conversation B failed"

            snap_b = _wait_quiet(l2)
            added = set(snap_b) - names_a
            assert len(added) == 1, (
                "the shared 3-chunk prefix must NOT be stored twice: expected "
                f"exactly 1 new L2 file for B's suffix, got {len(added)} new "
                f"({sorted(added)}); total now {len(snap_b)}"
            )

            # Every file that existed after A must be BYTE-IDENTICAL after B
            # (same size, same mtime): a rewrite to the same path would keep
            # the count unchanged but silently replace shared bytes.
            for name, meta in snap_a.items():
                assert snap_b.get(name) == meta, (
                    f"shared file {name} was modified by B's store "
                    f"({meta} -> {snap_b.get(name)}) -- shared blocks must "
                    "stay untouched"
                )

            # Both conversations must still hit all of their chunks.
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
        if server is not None:
            pers._stop_server(server, log, "dedup")