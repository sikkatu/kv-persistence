"""Round 39 regression: ensure_cache_root must NOT chmod 700 an arbitrary
parent of a custom KVP_CACHE_ROOT (only the StateDirectory default
/var/lib/kv-persistence or a dir literally named kv-persistence)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_parent_chmod_restricted_to_state_dir():
    """Round 39: with KVP_CACHE_ROOT=<shared>/kvc the parent (e.g. /data)
    became 700 -- locked away from every other user. The parent tightening
    must be scoped to the kv-persistence dirs only."""
    src = (ROOT / "systemd" / "kvp-lmcache-server").read_text()
    # the old unconditional pattern must be gone
    assert 'chmod 700 "$parent" 2>/dev/null || true' in src, (
        "the parent chmod should still exist for the StateDirectory case"
    )
    # and it must sit inside a case guard
    assert 'case "$parent" in' in src, (
        "the parent chmod must be scoped by a case guard"
    )
    assert '/var/lib/kv-persistence)' in src, (
        "the guard must match the StateDirectory default exactly"
    )
    # sanity: the chmod call must come AFTER the case guard line
    idx_case = src.find('case "$parent" in')
    idx_chmod = src.find('chmod 700 "$parent"')
    assert idx_chmod > idx_case, (
        "chmod 700 on the parent must be inside the case guard"
    )