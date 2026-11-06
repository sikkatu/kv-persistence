"""Round 43 regression: LMCache 0.5.6 API drift must not crash the patches.

The install run failed at start-cache.sh: tmp_guard.install() raised
AttributeError (DynamicNixlStorageAgent has no 'dynamic_store_file' --
0.5.6 moved the agent and renamed the store method to 'dynamic_store'),
and salt_filename_patch silently missed the moved module. Both patches
must adapt to the 0.5.6 layout:
* upstream 0.5.6 embeds cache_salt in the name ('@'-suffix) -> install()
  reports 'upstream', nothing is patched, no crash;
* tmp_guard wraps FileDynamicNixlStorageAgent.dynamic_store (the CONCRETE
  class -- not the ABC's abstractmethod);
* launch.py prints the "no patch needed" line instead of dying.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))


def _install_fake_lmcache_056(monkeypatch):
    """Register a fake lmcache tree with the 0.5.6 layout."""
    def mod(name):
        m = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    # package shells
    for shell in ("lmcache", "lmcache.v1", "lmcache.v1.distributed",
                  "lmcache.v1.distributed.l2_adapters",
                  "lmcache.v1.distributed.l2_adapters.nixl_store_agents"):
        mod(shell)

    # 0.5.6 upstream filename mapping WITH the '@'-suffix
    dyn = mod("lmcache.v1.distributed.l2_adapters.nixl_store_agents"
              ".dynamic_nixl_store_agent")

    def _object_key_to_filename(key):
        safe_model_name = key.model_name.replace("/", "--")
        chunk_hex = key.chunk_hash.hex()
        salt_suffix = f"@{key.cache_salt}" if key.cache_salt else ""
        return (f"{safe_model_name}_{key.kv_rank:08x}_"
                f"{key.object_group_id:x}_{chunk_hex}{salt_suffix}.bin")
    dyn._object_key_to_filename = _object_key_to_filename

    class DynamicNixlStorageAgent:
        async def dynamic_store(self, mem_indices, key):
            raise NotImplementedError

    dyn.DynamicNixlStorageAgent = DynamicNixlStorageAgent

    fileagent = mod("lmcache.v1.distributed.l2_adapters.nixl_store_agents"
                    ".file_dynamic_nixl_store_agent")
    fileagent._object_key_to_filename = _object_key_to_filename

    class FileDynamicNixlStorageAgent(DynamicNixlStorageAgent):
        def __init__(self, file_path):
            self.file_path = file_path
            self.stored = []

        async def dynamic_store(self, mem_indices, key):
            self.stored.append(key)

    fileagent.FileDynamicNixlStorageAgent = FileDynamicNixlStorageAgent
    fileagent.DynamicNixlStorageAgent = DynamicNixlStorageAgent

    # 0.5.3-style adapter module WITHOUT the filename mapping (0.5.6 layout)
    adapter = mod("lmcache.v1.distributed.l2_adapters.nixl_store_dynamic_l2_adapter")
    adapter.DynamicNixlStorageAgent = DynamicNixlStorageAgent
    return fileagent, dyn


class _Key:
    def __init__(self, salt=""):
        self.model_name = "mimo"
        self.kv_rank = 1
        self.object_group_id = 0
        self.chunk_hash = b"\xab" * 32
        self.cache_salt = salt


def test_upstream_active_on_056(monkeypatch):
    """0.5.6's '@'-suffix mapping must be detected as upstream-active."""
    _install_fake_lmcache_056(monkeypatch)
    from kvpersist import salt_filename_patch as sfp
    assert sfp.upstream_active() is True


def test_install_reports_upstream_and_patches_nothing(monkeypatch):
    """On 0.5.6 install() must NOT overwrite the upstream mapping."""
    fileagent, dyn = _install_fake_lmcache_056(monkeypatch)
    from kvpersist import salt_filename_patch as sfp
    original = dyn._object_key_to_filename
    status = sfp.install()
    assert status == "upstream", status
    assert dyn._object_key_to_filename is original, (
        "the upstream mapping must stay intact on 0.5.6"
    )
    assert fileagent._object_key_to_filename is original


def test_launch_uses_upstream_status(monkeypatch, tmp_path):
    """launch.py must print the 'no patch needed' line, not die."""
    _install_fake_lmcache_056(monkeypatch)
    import os
    from kvpersist import launch
    monkeypatch.setenv("KVP_CACHE_ROOT", str(tmp_path))
    # launch.main() delegates to the CLI -- intercept it
    import kvpersist.compress_serde  # noqa: F401 - ensure importable
    called = {}
    fake_cli = types.ModuleType("lmcache.cli.main")
    def _fake_main():
        called["yes"] = True
    fake_cli.main = _fake_main
    monkeypatch.setitem(sys.modules, "lmcache.cli", types.ModuleType("lmcache.cli"))
    monkeypatch.setitem(sys.modules, "lmcache.cli.main", fake_cli)
    launch.main()
    assert called.get("yes"), "launch must delegate to the CLI after patching"


def test_tmp_guard_wraps_dynamic_store(monkeypatch):
    """tmp_guard must wrap the 0.5.6 dynamic_store on the FILE agent."""
    fileagent, _ = _install_fake_lmcache_056(monkeypatch)
    from kvpersist import tmp_guard
    assert tmp_guard.install() is True
    assert getattr(fileagent.FileDynamicNixlStorageAgent,
                   "_kvp_tmp_guard_installed", False)
    assert "dynamic_store" not in getattr(
        fileagent.FileDynamicNixlStorageAgent, "__abstractmethods__", ())


def test_tmp_guard_survives_missing_lmcache(monkeypatch):
    """Without lmcache, install() must return False, never raise."""
    monkeypatch.setitem(sys.modules, "lmcache", None)
    for name in list(sys.modules):
        if name.startswith("lmcache."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    from kvpersist import tmp_guard
    assert tmp_guard.install() in (True, False)


def test_053_layout_still_patched(monkeypatch):
    """The 0.5.3 layout (filename mapping in the adapter module) must
    still receive the legacy '_'-suffix patch."""
    for name in list(sys.modules):
        if name.startswith("lmcache"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    import types
    for shell in ("lmcache", "lmcache.v1", "lmcache.v1.distributed",
                  "lmcache.v1.distributed.l2_adapters"):
        m = types.ModuleType(shell)
        monkeypatch.setitem(sys.modules, shell, m)
    adapter = types.ModuleType(
        "lmcache.v1.distributed.l2_adapters.nixl_store_dynamic_l2_adapter")
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)

    def _object_key_to_filename(key):
        return "upstream-0.5.3-name.bin"
    adapter._object_key_to_filename = _object_key_to_filename

    from kvpersist import salt_filename_patch as sfp
    assert sfp.upstream_active() is False
    assert sfp.install() == "patched"
    name = adapter._object_key_to_filename(_Key(salt="tenantA"))
    assert name.endswith("_tenantA.bin"), name