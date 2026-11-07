"""Round 38 regression tests (the round-37 fixes needed follow-ups):

1. systemd wrapper: ensure_cache_root BEFORE the adapter render write
   (first start: dir missing -> file skipped -> gate exit 5).
2. LMCACHE_HTTP_HOST=0.0.0.0 refused in start-cache.sh AND the wrapper.
3. start-cache.sh: single watermark source for JSON, echo and server flag.
4. gate --kv-cache-dtype auto without MODEL_DIR -> INCONCLUSIVE (exit 5),
   not MISMATCH (exit 4).
5. select_victims: normal entries win over shared ones; shared tail is not
   evicted wholesale (nominal stop).
6. requirements-lmcache.txt: openai pinned exactly once.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
GATE = ROOT / "scripts" / "verify-engine-identity.sh"


def test_systemd_wrapper_ensure_root_before_render():
    """Round 38: ensure_cache_root must be called BEFORE the adapter render
    write -- on a first start the dir does not exist yet and the old order
    silently skipped the write (gate reported INCONCLUSIVE)."""
    src = (ROOT / "systemd" / "kvp-lmcache-server").read_text()
    idx_ensure = src.find("ensure_cache_root\n_root_for_render=")
    assert idx_ensure != -1, (
        "ensure_cache_root must run immediately before the render write"
    )
    # the render write must come after ensure_cache_root
    idx_write = src.find("l2-adapter.rendered.json")
    assert idx_write > idx_ensure, "render write must follow ensure_cache_root"


def test_http_host_loopback_enforced():
    """Round 38: LMCACHE_HTTP_HOST=0.0.0.0 must be refused in BOTH
    start-cache.sh and the systemd wrapper (was accepted silently)."""
    for f in ("scripts/start-cache.sh", "systemd/kvp-lmcache-server"):
        src = (ROOT / f).read_text()
        assert "LMCACHE_HTTP_HOST must be loopback-only" in src, (
            f"{f} must refuse a non-loopback LMCACHE_HTTP_HOST"
        )


def test_start_cache_single_watermark_source():
    """Round 38: the rendered JSON, the summary line and the server flag must
    share ONE resolved watermark variable (was: JSON 0.9 vs server 0.8)."""
    src = (ROOT / "scripts" / "start-cache.sh").read_text()
    assert "EFFECTIVE_WM=" in src, "start-cache.sh needs a single watermark var"
    # no bare ${KVP_EVICT_TRIGGER_WATERMARK:-0.9} in the sed line any more
    assert re.search(r'__KVP_EVICT_TRIGGER_WATERMARK__\|\$EFFECTIVE_WM\|', src), (
        "the sed must use the resolved EFFECTIVE_WM"
    )
    # the server flag and summary must use the same variable (4 refs: def,
    # sed, echo, 2 flags)
    assert src.count("EFFECTIVE_WM") >= 4, (
        "sed, echo and both server flags must reference EFFECTIVE_WM"
    )


def test_gate_auto_without_model_dir_is_inconclusive(tmp_path):
    """Round 38: --kv-cache-dtype auto with no MODEL_DIR cannot prove the
    dtype -- the verdict must be INCONCLUSIVE (exit 5), never a false
    MISMATCH (exit 4)."""
    import os

    cache_root = tmp_path / "cache_root"
    cache_root.mkdir()
    (cache_root / "l2-adapter.rendered.json").write_text("{}")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\n'
        'if [ "$1" = "inspect" ]; then\n'
        '  python3 -c \'import json,os; print(json.dumps(os.environ.get("STUB_CMD_ARGS","").split("|")))\'\n'
        '  exit 0\n'
        'fi\n'
        'exit 0\n'
    )
    docker.chmod(0o755)
    cfg_dir = ROOT / "configs" / "mimo-v2.6-pro"
    local = cfg_dir / "identity.env.local"
    saved = local.read_bytes() if local.exists() else None
    try:
        local.write_text(
            "KVP_KV_CACHE_DTYPE=auto\n"
            f"KVP_MODEL_REVISION={'a' * 40}\n"
            f"KVP_TOKENIZER_REVISION={'b' * 40}\n"
            f"KVP_CHAT_TEMPLATE_DIGEST={'c' * 64}\n"
        )
        args = "|".join([
            "--tensor-parallel-size", "4",
            "--kv-cache-dtype", "auto",
            "--max-model-len", "1048576",
            "--block-size", "16",
            "--kv-transfer-config",
            '{"kv_connector":"LMCacheMPConnector","kv_role":"kv_both"}',
        ])
        env = dict(
            os.environ,
            PYTHONPATH=str(IMPL),
            KVP_MODEL_CONFIG="mimo-v2.6-pro",
            PATH=f"{bindir}:{os.environ.get('PATH', '')}",
            STUB_CMD_ARGS=args,
            KVP_CACHE_ROOT=str(cache_root),
            # Round 48: explicit raw mode (matches the "{}" raw adapter)
            USE_LOSSLESS_SERDE="0",
        )
        env.pop("MODEL_DIR", None)
        env.pop("KVP_MODEL_DIR", None)
        r = subprocess.run(["bash", str(GATE), "test-container"],
                           env=env, capture_output=True, text=True, timeout=60)
        out = r.stdout + r.stderr
        assert r.returncode == 5, (
            "auto dtype without MODEL_DIR must be INCONCLUSIVE (5), got "
            f"{r.returncode}:\n{out[-1200:]}"
        )
        assert "MISMATCH" not in out.upper() or "INCONCLUSIVE" in out.upper()
    finally:
        if saved is not None:
            local.write_bytes(saved)
        elif local.exists():
            local.unlink()


def test_select_victims_prefers_unshared():
    """Round 38: a shared entry (0 effective bytes freed) must not displace a
    fitting unshared one; the shared tail is picked only up to the gap."""
    import sys

    sys.path.insert(0, str(IMPL))
    from kvpersist.retention import EntryStats, select_victims

    shared = EntryStats(key="shared", tokens=10, bytes_on_disk=1000,
                        hits=0, last_access_ts=1.0, shared_refs=5)
    normal = EntryStats(key="normal", tokens=10, bytes_on_disk=500,
                        hits=0, last_access_ts=2.0, shared_refs=1)
    victims = select_victims([shared, normal], bytes_to_free=500, now=1e9)
    keys = [v.key for v in victims]
    assert "normal" in keys, (
        f"the unshared entry frees real bytes and must be picked first: {keys}"
    )
    # the shared one must NOT be picked when the unshared one already covers
    # the nominal gap
    assert "shared" not in keys, (
        f"shared entries must not be evicted wholesale: {keys}"
    )


def test_openai_pinned_once():
    """Round 38: the openai block was copy-pasted twice in
    requirements-lmcache.txt."""
    src = (ROOT / "requirements-lmcache.txt").read_text()
    assert src.count("openai>=") == 1, (
        f"openai must be pinned exactly once, found {src.count('openai>=')}"
    )