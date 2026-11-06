"""Round 43b regression: start-cache.sh must surface the [kvp] patch status.

The kvpersist.launch messages go to the server log (daemon stderr), not
the terminal. On a successful start the operator must SEE whether the
cache_salt filename mapping and the active-write registry are installed.
"""
from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "start-cache.sh"


def test_start_cache_surfaces_kvp_patch_status():
    """After 'cache service READY' the script must grep the fresh part of
    the server log for '[kvp]' lines and print them (with a fallback
    hint when absent)."""
    t = SCRIPT.read_text()
    ready_idx = t.index('cache service READY')
    tail = t[ready_idx:]
    assert "[kvp]" in tail, (
        "start-cache.sh must show the [kvp] patch-status lines after READY"
    )
    assert "grep" in tail and "LOG" in tail, (
        "the status must come from the server log, not be hardcoded"
    )
    assert "patch status not found" in tail, (
        "a missing status must print a hint, not stay silent"
    )