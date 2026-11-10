"""Round 41/80b regression: pre-flight checks (the ones preserved from the
legacy kv launcher).

The install run lost 24 minutes: the identity gate passed and only at
register_kv_caches (300s timeout) vLLM died because the LMCache cache server
did not answer. Two checks now run BEFORE the model load and they LIVE in the
shared _kvp_pre_flights() (env.sh) -- the engine path is the recipe launcher
now, but the checks must not vanish with the old launcher (round 76/77
defect class). This test drives _kvp_pre_flights directly:
  * the cache server must actually ANSWER on the request port (TCP probe);
  * L2 codec compatibility vs the RUNNING server's l2-adapter.rendered.json.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T
import os
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _listen():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run_pre_flights(cache_root: Path, host: str, port, image="img@sha256:x"):
    return subprocess.run(
        ["bash", "-c",
         f'. scripts/env.sh && _kvp_pre_flights "{ROOT}" {host} {port} '
         f'"{cache_root}" "{image}" 1000 1000 0 host'],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "KVP_SKIP_IPC_TEST": "1",
             "LMCACHE_HOST": host, "LMCACHE_PORT": str(port),
             "LMCACHE_L2_SERDE": "zstd"},
        cwd=str(ROOT))


def test_preflight_server_unreachable_blocks(tmp_path):
    """An unreachable cache server must abort BEFORE the model load --
    otherwise a ~15-min load dies at register_kv_caches (300s)."""
    port = _free_port()
    r = _run_pre_flights(tmp_path / "cache", "127.0.0.1", port)
    combined = r.stdout + r.stderr
    assert r.returncode != 0, (
        "an unreachable cache server must abort:\n" + combined[-1500:])
    assert "NOT reachable" in combined, (
        "the refusal must say the cache server is unreachable:\n" + combined[-1500:])
    assert "start-cache.sh" in combined, (
        "the refusal must name the fix (start the cache service)")


def test_preflight_codec_mismatch_blocks(tmp_path):
    """A codec mismatch between the launch and the RUNNING server must abort
    BEFORE the load (the codec is part of the cache identity)."""
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    # the RUNNING server stores lz4; this launch expects zstd
    (cache_root / "l2-adapter.rendered.json").write_text(
        '{"serde": {"type": "kvp-lz4", "level": "3", "align_bytes": "4096"}}')
    srv, port = _listen()
    try:
        r = _run_pre_flights(cache_root, "127.0.0.1", port)
    finally:
        srv.close()
    combined = r.stdout + r.stderr
    assert r.returncode != 0, (
        "a codec mismatch must abort:\n" + combined[-1500:])
    assert "CODEC MISMATCH" in combined.upper(), (
        "the refusal must name the codec mismatch:\n" + combined[-1500:])
    assert "zstd" in combined and "lz4" in combined, (
        "the refusal must name BOTH codecs:\n" + combined[-1500:])


def test_preflight_passes_when_server_listens(tmp_path):
    """A reachable server + matching codec lets the launch through the
    pre-flight (it stops later at the model load)."""
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    # expected zstd (the launch default) and the server stores zstd too
    (cache_root / "l2-adapter.rendered.json").write_text(
        '{"serde": {"type": "kvp-zstd", "level": "3"}}')
    srv, port = _listen()
    try:
        r = _run_pre_flights(cache_root, "127.0.0.1", port)
    finally:
        srv.close()
    combined = r.stdout + r.stderr
    assert "pre-flight: cache server is listening" in combined, (
        "the pre-flight must confirm the reachable server:\n" + combined[-1500:])
    assert "L2 codec matches" in combined, (
        "the pre-flight must confirm the matching codec:\n" + combined[-1500:])