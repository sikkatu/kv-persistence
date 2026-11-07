"""Round 45h regression: same-UID engine launch with DYNAMIC uid/gid.

The same-UID ipc-test passed (1000:1000 -> PASS). The engine launcher must
support that path with a DYNAMIC uid/gid (id -u / id -g -- any user may
have any UID, no hardcoded 1000):
* KVP_SAME_UID=1 adds --user $(id -u):$(id -g) to docker run;
* the HF cache and torch caches move to world-writable /tmp paths (the
  non-root user cannot write /root);
* the pre-flight IPC test uses PRODUCER_USER=$(id -u):$(id -g) to
  reproduce EXACTLY the production boundary;
* without KVP_SAME_UID the container runs as root (the proven fallback).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "run-vllm-with-persist.sh"


def test_same_uid_flag_and_dynamic_uid():
    t = LAUNCHER.read_text()
    # Round 48 (user): same-UID is the DEFAULT -- no flag to type in a
    # fresh terminal; KVP_SAME_UID=0 opts into the root fallback.
    assert 'KVP_SAME_UID="${KVP_SAME_UID:-1}"' in t
    assert '_uid="$(id -u)"' in t and '_gid="$(id -g)"' in t, (
        "the UID/GID must be resolved dynamically (any user, any UID)"
    )
    assert 'ENGINE_USER_ARGS=(--user "$_uid:$_gid")' in t, (
        "the engine container must get --user with the dynamic uid:gid"
    )
    assert "--user 1000" not in t and ":1000" not in t, (
        "no hardcoded UID anywhere in the launcher"
    )


def test_cache_paths_move_to_writable_tmp():
    """Non-root cannot write /root -- the caches must live under /tmp."""
    t = LAUNCHER.read_text()
    assert "HF_HOME=/tmp/hf-cache" in t
    assert "XDG_CACHE_HOME=/tmp/xdg-cache" in t
    assert "TRITON_CACHE_DIR=/tmp/triton-cache" in t
    assert "-v \"$HF_CACHE:/tmp/hf-cache\"" in t, (
        "the HF cache must mount at the writable path in same-UID mode"
    )


def test_pre_flight_ipc_uses_producer_user():
    """The pre-flight must reproduce the production boundary: the ipc-test
    producer gets the same UID as the engine container will have."""
    t = LAUNCHER.read_text()
    idx = t.index("PRODUCER_USER=")
    assert idx > t.index("ipc-test.sh"), (
        "PRODUCER_USER must be passed to the ipc-test call"
    )
    assert '_ipc_pu="$_uid:$_gid"' in t, (
        "the producer UID must match the dynamic uid:gid"
    )


def test_same_uid_defined_before_preflight():
    """KVP_SAME_UID must be defined BEFORE the pre-flight uses it
    (set -u would die otherwise)."""
    t = LAUNCHER.read_text()
    def_idx = t.index('KVP_SAME_UID="${KVP_SAME_UID:-1}"')
    use_idx = t.index('if [ "$KVP_SAME_UID" = "1" ]; then\n      _ipc_pu=')
    assert def_idx < use_idx, (
        "KVP_SAME_UID must be defined before the pre-flight IPC block"
    )


def test_default_is_same_uid_not_root():
    """Round 48 (user request): the DEFAULT launch is same-UID (no root).
    The root path stays available with KVP_SAME_UID=0 and is kept proven by
    CONSUMER=host-root in ipc-test."""
    t = LAUNCHER.read_text()
    # the root cache-path line must still exist (root fallback), but the
    # same-UID branch must come first and be the default
    assert 'ENGINE_CACHE_ARGS=(-e HF_HOME=/root/.cache/huggingface)' in t
    assert 'KVP_SAME_UID="${KVP_SAME_UID:-1}"' in t, (
        "same-UID must be the default so a fresh terminal needs no flags")
    assert 'MOUNTS=(-v "$HF_CACHE:/root/.cache/huggingface")' in t