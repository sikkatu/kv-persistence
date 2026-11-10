"""Round 45 regression: CUDA IPC root cause -- diagnostics, fail-fast, ipc test.

The server log proved the real failure: 'Error handling operation
register_kv_cache' -> torch.AcceleratorError CUDA_ERROR_MAP_FAILED (the
host cache server could not open GPU memory created in the container).
The round-44 RPC ping passed because ordinary RPC works -- the failure is
specific to CUDA IPC memory mapping. Round 45:
* diagnostics must show the 'Error handling operation' exception (the old
  "last 30 lines" showed eviction noise and hid it);
* the wait loop must fail FAST on a fresh server-side handler error
  instead of burning the 300s register timeout;
* a CUDA IPC boundary test (scripts/ipc-test.sh) is wired into the
  pre-flight so the failing step is reproduced BEFORE the model load.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = _T.recipe_launcher()  # Round 80b: recipe owns the engine launch
IPC_TEST = ROOT / "scripts" / "ipc-test.sh"


def test_ipc_test_script_exists_and_is_ours():
    t = IPC_TEST.read_text()
    assert IPC_TEST.stat().st_mode & 0o111, "ipc-test.sh must be executable"
    for needle in ("producer.py", "consumer.py", "handle.pkl",
                   "CONSUMER", "host-root", "container"):
        assert needle in t, f"ipc-test.sh must cover {needle}"
    assert "PASS:" in t and "FAIL" in t, "the verdict must be explicit"
    assert "KVP_LAUNCH_MODE=docker" in t, (
        "the failure verdict must name the recommended fix"
    )


def test_pre_flight_wires_ipc_test_before_engine_start():
    t = _T.contract_text()
    assert "ipc-test.sh" in t, "the pre-flight must run the IPC boundary test"
    idx = t.index("ipc-test.sh")
    run_idx = t.index('docker run -d --name "$CONTAINER"')
    assert idx < run_idx, "the IPC test must run BEFORE the engine starts"
    assert "KVP_SKIP_IPC_TEST" in t, (
        "the operator must be able to skip the test explicitly"
    )
    assert "CUDA IPC BOUNDARY FAILED" in t, (
        "the refusal must name the CUDA IPC boundary failure"
    )


def test_diagnostics_show_the_server_error():
    """The diagnostics must surface 'Error handling operation' (the real
    exception) and filter the eviction noise."""
    t = _T.contract_text()
    assert "Error handling operation" in t
    assert "server ERROR (root cause)" in t
    assert "skipping eviction" in t, (
        "the eviction noise must be filtered out of the tail"
    )


def test_wait_loop_fails_fast_on_server_error():
    """A fresh 'Error handling operation' in the server log must abort the
    wait immediately -- no point burning the 300s register timeout. Round 80b:
    the fail-fast lives in the recipe launcher's API-wait loop (the engine
    owner); the order check reads that file (not the cross-file union)."""
    rl = _T.recipe_launcher()
    assert rl is not None, "the recipe launcher must exist"
    t = rl.read_text()
    wait_idx = t.index("for _ in $(seq 1 \"$WAIT_TRIES\")")
    fail_idx = t.index("fail-fast (round 79)")
    assert fail_idx > wait_idx, (
        "the fail-fast must live in the wait loop"
    )
    seg = t[wait_idx:]
    assert "Error handling operation register_kv_cache" in seg, (
        "the wait loop must scan the server log for handler errors")
    assert "waiting 300s cannot fix it" in seg, (
        "the abort must explain that waiting cannot fix a dead handshake")