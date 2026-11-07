"""Round 41 regression: pre-docker-run checks in run-vllm-with-persist.sh.

The install run lost 24 minutes: the identity gate passed and only at
register_kv_caches (300s timeout) vLLM died because the LMCache cache
server did not answer. Two checks now run BEFORE docker run:
  * L2 codec compatibility vs the RUNNING server's l2-adapter.rendered.json;
  * the cache server must actually ANSWER on the request port (TCP probe --
    a live PID is not a working server, which is exactly the install run).
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "run-vllm-with-persist.sh"
KEY_FILE = ROOT / "live" / "api_key"
CONFIG_DIR = ROOT / "configs" / "mimo-v2.6-pro"
LOCAL = CONFIG_DIR / "identity.env.local"

DOCKER_STUB = """#!/usr/bin/env bash
echo "docker $*" >> "${STUB_LOG:-/dev/null}"
exit 0
"""

VALID_PINS = (
    "KVP_MODEL_REVISION=" + "a" * 40 + "\n"
    "KVP_TOKENIZER_REVISION=" + "b" * 40 + "\n"
    "KVP_CHAT_TEMPLATE_DIGEST=" + "c" * 64 + "\n"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _preserve():
    return (KEY_FILE.read_bytes() if KEY_FILE.exists() else None,
            LOCAL.read_bytes() if LOCAL.exists() else None)


def _restore(saved):
    key, local = saved
    for p, data in ((KEY_FILE, key), (LOCAL, local)):
        if data is None:
            if p.exists():
                p.unlink()
        else:
            p.write_bytes(data)


def _base_env(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("KVP_") or k in ("API_KEY", "BIND", "IMAGE", "MODEL_DIR",
                                         "LMCACHE_HOST", "LMCACHE_PORT",
                                         "LMCACHE_L2_SERDE", "USE_LOSSLESS_SERDE"):
            del env[k]
    env["PATH"] = f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}"
    env["STUB_LOG"] = str(tmp_path / "stub.log")
    recipe = tmp_path / "recipe"
    recipe.mkdir(exist_ok=True)
    env["RECIPE_DIR"] = str(recipe)
    env["IMAGE"] = "x@sha256:" + "a" * 64
    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    (model / "config.json").write_text(
        '{"torch_dtype":"bfloat16","num_key_value_heads":8,"head_dim":128}'
    )
    env["MODEL_DIR"] = str(model)
    cache_root = tmp_path / "cache_root"
    cache_root.mkdir(exist_ok=True)
    env["KVP_CACHE_ROOT"] = str(cache_root)
    # a LIVE pid file (the launcher requires it before the TCP pre-flight)
    (cache_root / "lmcache-server.pid").write_text(str(os.getpid()))
    # valid identity pins so the pre-launch gate passes (round-31 pattern)
    LOCAL.write_text(VALID_PINS)
    return env


def test_preflight_codec_mismatch_blocks_before_docker(tmp_path):
    """Round 41: a codec mismatch between the launch and the RUNNING server
    must abort BEFORE docker run (was: surfaced only after the full load)."""
    saved = _preserve()
    try:
        env = _base_env(tmp_path)
        # Round 50: the launch default codec is now zstd, so the adapter must
        # use a DIFFERENT codec (lz4) to reproduce the mismatch.
        (tmp_path / "cache_root" / "l2-adapter.rendered.json").write_text(
            '{"serde": {"type": "kvp-lz4", "level": "3", "align_bytes": "4096"}}'
        )
        r = subprocess.run(["bash", str(LAUNCHER)], env=env,
                           capture_output=True, text=True, timeout=120)
        combined = r.stdout + r.stderr
        assert r.returncode != 0, (
            "a codec mismatch must abort before docker run:\n" + combined[-1500:]
        )
        assert "CODEC MISMATCH" in combined.upper(), (
            "the refusal must name the codec mismatch:\n" + combined[-1500:]
        )
        stublog = tmp_path / "stub.log"
        assert not stublog.exists() or "docker run" not in stublog.read_text(), (
            "docker run must NOT be reached on a codec mismatch"
        )
    finally:
        _restore(saved)


def test_preflight_server_unreachable_blocks_before_docker(tmp_path):
    """Round 41: an unreachable cache server must abort BEFORE docker run --
    otherwise a 13-minute model load dies at register_kv_caches (300s)."""
    saved = _preserve()
    try:
        env = _base_env(tmp_path)
        env["LMCACHE_HOST"] = "127.0.0.1"
        env["LMCACHE_PORT"] = str(_free_port())  # nothing listens here
        r = subprocess.run(["bash", str(LAUNCHER)], env=env,
                           capture_output=True, text=True, timeout=120)
        combined = r.stdout + r.stderr
        assert r.returncode != 0, (
            "an unreachable cache server must abort before docker run:\n"
            + combined[-1500:]
        )
        assert "NOT reachable" in combined, (
            "the refusal must say the cache server is unreachable:\n"
            + combined[-1500:]
        )
        stublog = tmp_path / "stub.log"
        assert not stublog.exists() or "docker run" not in stublog.read_text(), (
            "docker run must NOT be reached when the cache server is down"
        )
    finally:
        _restore(saved)


def test_preflight_passes_when_server_listens(tmp_path):
    """Round 41: a reachable server + matching codec lets the launch through
    the pre-flight (it stops later at the stub docker / post-launch gate)."""
    saved = _preserve()
    try:
        env = _base_env(tmp_path)
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            env["LMCACHE_HOST"] = "127.0.0.1"
            env["LMCACHE_PORT"] = str(port)
            r = subprocess.run(["bash", str(LAUNCHER)], env=env,
                               capture_output=True, text=True, timeout=120)
            combined = r.stdout + r.stderr
            assert "pre-flight: cache server is listening" in combined, (
                "the pre-flight must confirm the reachable server:\n"
                + combined[-1500:]
            )
        finally:
            srv.close()
    finally:
        _restore(saved)