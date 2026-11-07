"""Round 37 regression tests (review batch): the fixed bugs must stay fixed.

1. Bare gate must NOT clobber identity pins with synthesized defaults
   (KVP_TP_SIZE=2 in identity.env.local was replaced by 4 -> false MISMATCH).
2. tmp_guard.register_write must survive _proc_start_time == "unknown".
3. salt_filename_patch.apply() must return False when upstream renamed the
   target function (was: True, isolation silently lost).
4. retention.select_victims must not evict ALL shared entries.
5. purge --tmp must not delete valid entries whose name merely contains .tmp.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
GATE = ROOT / "scripts" / "verify-engine-identity.sh"
CONFIG_DIR = ROOT / "configs" / "mimo-v2.6-pro"


def _preserve_local():
    p = CONFIG_DIR / "identity.env.local"
    return p.read_bytes() if p.exists() else None


def _restore_local(saved):
    p = CONFIG_DIR / "identity.env.local"
    if saved is None:
        if p.exists():
            p.unlink()
    else:
        p.write_bytes(saved)


DOCKER_STUB = """#!/usr/bin/env bash
if [ "$1" = "inspect" ]; then
  python3 -c 'import json,os; print(json.dumps(os.environ.get("STUB_CMD_ARGS","").split("|")))'
  exit 0
fi
exit 0
"""


def test_bare_gate_keeps_identity_pins(tmp_path):
    """Round 37 HIGH: a pin in identity.env.local must WIN over the resolver's
    synthesized defaults -- a manual gate run used to export KVP_TP_SIZE=4
    (the resolver default) over the KVP_TP_SIZE=2 pin via the merge's env-wins
    pass, then MISMATCHed the container's real --tensor-parallel-size 2
    (expect=4). Fix: no source name set -> no export -> the pin survives."""
    saved = _preserve_local()
    try:
        (CONFIG_DIR / "identity.env.local").write_text(
            "KVP_TP_SIZE=2\n"
            "KVP_KV_CACHE_DTYPE=bfloat16\n"
            "KVP_MAX_MODEL_LEN=1048576\n"
            "KVP_BLOCK_SIZE=16\n"
            f"KVP_MODEL_REVISION={'a' * 40}\n"
            f"KVP_TOKENIZER_REVISION={'b' * 40}\n"
            f"KVP_CHAT_TEMPLATE_DIGEST={'c' * 64}\n"
        )
        # stub docker + cache root with a rendered adapter (raw codec)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(DOCKER_STUB)
        docker.chmod(0o755)
        cache_root = tmp_path / "cache_root"
        cache_root.mkdir()
        (cache_root / "l2-adapter.rendered.json").write_text("{}")
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text(
            '{"torch_dtype":"bfloat16","num_key_value_heads":8,"head_dim":128}'
        )
        args = "|".join([
            "--tensor-parallel-size", "2",
            "--kv-cache-dtype", "bfloat16",
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
            MODEL_DIR=str(model_dir),
        )
        for k in ("KVP_TP_SIZE", "TP_SIZE", "KVP_KV_CACHE_DTYPE", "KV_CACHE_DTYPE",
                  "KVP_MAX_MODEL_LEN", "MAX_MODEL_LEN", "KVP_SERVED_MODEL_NAME",
                  "SERVED_MODEL_NAME", "KVP_BLOCK_SIZE", "KV_PERSIST_BLOCK_SIZE"):
            env.pop(k, None)
        r = subprocess.run(["bash", str(GATE), "test-container"],
                           env=env, capture_output=True, text=True, timeout=60)
        out = r.stdout + r.stderr
        assert "tp_size: identity=2" in out, (
            "the pin KVP_TP_SIZE=2 must survive the resolver defaults:\n" + out[-1500:]
        )
        assert r.returncode == 0, (
            "a manual gate run must PASS against the pinned values (no false "
            f"MISMATCH from synthesized defaults):\n{out[-1500:]}"
        )
    finally:
        _restore_local(saved)


def test_tmp_guard_survives_unknown_start_time(tmp_path):
    """Round 37 HIGH: register_write crashed with TypeError when
    _proc_start_time returned the string 'unknown' -- the write was never
    registered and purge could delete its .tmp file."""
    sys.path.insert(0, str(IMPL))
    from kvpersist import tmp_guard

    orig = tmp_guard._proc_start_time
    tmp_guard._proc_start_time = lambda pid: "unknown"
    try:
        reg = tmp_path / "cache"
        reg.mkdir()
        p = tmp_guard.register_write(str(reg), "x.bin")
        assert p, "register_write must survive an 'unknown' start time"
        assert os.path.isfile(p)
    finally:
        tmp_guard._proc_start_time = orig


def test_salt_patch_reports_failure_on_rename(tmp_path):
    """Round 37: apply() must return False when upstream renamed
    _object_key_to_filename -- a dead attribute must not count as success."""
    sys.path.insert(0, str(IMPL))
    from kvpersist import salt_filename_patch

    # Fake lmcache adapter module WITHOUT the target attribute (renamed).
    import types

    fake_mod = types.ModuleType(
        "lmcache.v1.distributed.l2_adapters.nixl_store_dynamic_l2_adapter")
    fake_mod.some_other_name = lambda key: "x"  # upstream renamed the function
    pkg = types.ModuleType("lmcache")
    v1 = types.ModuleType("lmcache.v1")
    dist = types.ModuleType("lmcache.v1.distributed")
    adapters = types.ModuleType("lmcache.v1.distributed.l2_adapters")
    adapters.nixl_store_dynamic_l2_adapter = fake_mod
    sys.modules.update({
        "lmcache": pkg, "lmcache.v1": v1,
        "lmcache.v1.distributed": dist,
        "lmcache.v1.distributed.l2_adapters": adapters,
        "lmcache.v1.distributed.l2_adapters.nixl_store_dynamic_l2_adapter": fake_mod,
    })
    try:
        assert salt_filename_patch.apply() is False, (
            "apply() must report failure when the upstream function is missing"
        )
        # and with the attribute present it must succeed and install the patch
        fake_mod._object_key_to_filename = lambda key: "orig"
        assert salt_filename_patch.apply() is True
        assert fake_mod._object_key_to_filename is salt_filename_patch.patched_object_key_to_filename
    finally:
        for m in ("lmcache", "lmcache.v1", "lmcache.v1.distributed",
                  "lmcache.v1.distributed.l2_adapters",
                  "lmcache.v1.distributed.l2_adapters.nixl_store_dynamic_l2_adapter"):
            sys.modules.pop(m, None)


def test_retention_shared_entries_not_all_evicted():
    """Round 37: a shared entry freed 0 effective bytes, so the stop condition
    never triggered and EVERY shared entry was evicted. The loop must stop on
    nominal bytes."""
    sys.path.insert(0, str(IMPL))
    from kvpersist.retention import EntryStats, select_victims

    entries = [
        EntryStats(key=f"k{i}", tokens=10, hits=0, bytes_on_disk=100,
                   last_access_ts=float(i), shared_refs=3)
        for i in range(10)
    ]
    victims = select_victims(entries, bytes_to_free=150, now=1e9)
    assert len(victims) < 10, (
        f"shared entries must stop the loop on nominal bytes, evicted {len(victims)}/10"
    )
    assert len(victims) >= 2, "the requested nominal bytes must still be covered"


def test_purge_tmp_pattern_matches_only_crash_leftovers():
    """Round 37: the crash-leftover matcher must be <final>.bin.tmp.<hex>,
    not a bare '.tmp.' substring (valid names could contain it)."""
    src = (IMPL / "kvpersist" / "cli.py").read_text()
    assert 're.search(r".*\\.bin\\.tmp\\.[0-9a-f' in src or \
           re_search_present(src), \
           "cli.py must match crash leftovers by the adapter pattern, not a bare .tmp. substring"


def re_search_present(src: str) -> bool:
    import re
    return bool(re.search(r're\.search\(r".*?bin\\?\.tmp', src))


def test_budget_admit_rejects_negative():
    """Round 37: admit() accepted negative sizes (usage went down)."""
    sys.path.insert(0, str(IMPL))
    from kvpersist.budget import BudgetLedger

    ledger = BudgetLedger(budget_bytes=1000)
    try:
        ledger.admit("ns", -5)
    except ValueError:
        return
    raise AssertionError("admit(-5) must raise ValueError")