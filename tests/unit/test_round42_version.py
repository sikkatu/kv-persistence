"""Round 42 regression: LMCache version-compatibility pre-flight.

The install run failed at register_kv_caches (300s timeout) because the
host served lmcache 0.5.3 while the pinned vLLM image ships 0.5.6 -- mixed
minor versions break the MP protocol silently. The launcher must extract
BOTH versions and refuse on mismatch, before docker run.
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
case "$*" in
  *entrypoint*python3*)
    # version probe from run-vllm-with-persist.sh
    echo "${STUB_IMG_LMCACHE:-0.5.6}"
    exit 0 ;;
esac
exit 0
"""

# forwards to the real python3 EXCEPT for the lmcache version probe
PYTHON_STUB = """#!/usr/bin/env bash
case "$*" in
  *importlib.metadata*lmcache*)
    echo "${STUB_HOST_LMCACHE:-0.5.3}"
    exit 0 ;;
esac
exec /usr/bin/python3 "$@"
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
    for name, body in (("docker", DOCKER_STUB), ("python3", PYTHON_STUB)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("KVP_") or k in ("API_KEY", "BIND", "IMAGE", "MODEL_DIR",
                                         "LMCACHE_HOST", "LMCACHE_PORT",
                                         "LMCACHE_L2_SERDE", "USE_LOSSLESS_SERDE",
                                         "STUB_IMG_LMCACHE", "STUB_HOST_LMCACHE"):
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
    (cache_root / "lmcache-server.pid").write_text(str(os.getpid()))
    LOCAL.write_text(VALID_PINS)
    # reachable server so the reachability pre-flight passes first
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    env["LMCACHE_HOST"] = "127.0.0.1"
    env["LMCACHE_PORT"] = str(srv.getsockname()[1])
    env["_TEST_SRV"] = str(srv.fileno())  # keep fd reference
    return env, srv


def test_version_mismatch_blocks_before_docker(tmp_path):
    """Round 42: host lmcache 0.5.3 vs image 0.5.6 must abort BEFORE
    docker run (was: register_kv_caches silently timed out after 300s)."""
    saved = _preserve()
    srv = None
    try:
        env, srv = _base_env(tmp_path)
        env["STUB_IMG_LMCACHE"] = "0.5.6"
        env["STUB_HOST_LMCACHE"] = "0.5.3"
        r = subprocess.run(["bash", str(LAUNCHER)], env=env,
                           capture_output=True, text=True, timeout=120)
        combined = r.stdout + r.stderr
        assert r.returncode != 0, (
            "a version mismatch must abort before docker run:\n" + combined[-1500:]
        )
        assert "LMCACHE VERSION MISMATCH" in combined, (
            "the refusal must name the version mismatch:\n" + combined[-1500:]
        )
        stublog = tmp_path / "stub.log"
        # the version probe itself runs `docker run --rm --entrypoint python3`
        # -- the ENGINE start (`docker run -d --name ...`) must not be reached.
        log = stublog.read_text() if stublog.exists() else ""
        assert "-d --name" not in log, (
            "the engine container must NOT be started on a version mismatch"
        )
    finally:
        if srv is not None:
            srv.close()
        _restore(saved)


def test_version_match_passes_pre_flight(tmp_path):
    """Round 42: matching versions let the launch through the pre-flight."""
    saved = _preserve()
    srv = None
    try:
        env, srv = _base_env(tmp_path)
        env["STUB_IMG_LMCACHE"] = "0.5.6"
        env["STUB_HOST_LMCACHE"] = "0.5.6"
        r = subprocess.run(["bash", str(LAUNCHER)], env=env,
                           capture_output=True, text=True, timeout=120)
        combined = r.stdout + r.stderr
        assert "pre-flight: lmcache version matches" in combined, (
            "the pre-flight must confirm matching versions:\n" + combined[-1500:]
        )
    finally:
        if srv is not None:
            srv.close()
        _restore(saved)