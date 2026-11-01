"""Unit tests: the L2 file-size patch and serde registration against a FAKE
LMCache surface.

Review point: "Нет юнит-теста на _install_size_patch и register против
реального LMCache." These tests exercise the exact call shapes the patch
intercepts -- `DynamicNixlStoreL2Adapter._execute_store_in_the_loop` /
`_execute_load_in_loop` are module-level async methods called as
`self._execute_...(keys, objects, task_id)` -- against a stub module that
mimics the real one, so the patch logic is verified WITHOUT installing
LMCache/torch. The stub is injected into sys.modules before `apply()` runs.

What is verified:
* store: objects narrowed by set_used_size get meta.phy_size == align_up(n),
  then it is RESTORED afterwards (L1 free() accounting safety).
* store: objects NOT narrowed keep their original phy_size (uncompressed path
  is byte-for-byte upstream).
* load: meta.phy_size is shrunk to the STORED file size when the file is
  smaller than the allocation (short DMA read never runs past EOF).
* idempotence: applying twice does not wrap twice.
* register(): fail-fast raises CodecError when a codec binding is missing.
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation"))


# ---------------------------------------------------------------------------
# Stub objects
# ---------------------------------------------------------------------------


class _FakeMeta:
    def __init__(self, phy_size: int):
        self.phy_size = phy_size


class _FakeObj:
    def __init__(self, phy_size: int, narrowed: int | None = None):
        self.meta = _FakeMeta(phy_size)
        self._used_size_override = narrowed

    @property
    def byte_array(self):
        return memoryview(bytearray(self.meta.phy_size))


class _FakeStoreResult:
    def __init__(self, ok: bool, nbytes: int):
        self._ok = ok
        self._n = nbytes

    def is_successful(self):
        return self._ok

    def bytes_transferred(self):
        return self._n


class _FakeAdapter:
    """Mimics DynamicNixlStoreL2Adapter's store/load methods."""

    def __init__(self):
        self._lock = types.SimpleNamespace()  # only used as a context manager
        self._lock.__enter__ = lambda s: s
        self._lock.__exit__ = lambda s, *a: False
        self._memory_objects = {}
        self.seen_store_sizes = []
        self.seen_load_sizes = []
        self.nixl_agent = types.SimpleNamespace(l1_align_bytes=4096)

    async def _orig_store(self, keys, objects, task_id):
        self.seen_store_sizes.append([o.meta.phy_size for o in objects])
        self._completed_store_tasks = {task_id: _FakeStoreResult(True, 0)}
        return None

    async def _orig_load(self, keys, objects, task_id):
        self.seen_load_sizes.append([o.meta.phy_size for o in objects])
        self._completed_load_tasks = {task_id: None}
        return None


def _install_fake_lmcache(monkeypatch):
    """Install a stub nixl_store_dynamic_l2_adapter module in sys.modules."""
    mod = types.ModuleType("lmcache.v1.distributed.l2_adapters.nixl_store_dynamic_l2_adapter")

    class DynamicNixlStoreL2Adapter:
        async def _execute_store_in_the_loop(self, keys, objects, task_id):
            self.seen_store_sizes.append([o.meta.phy_size for o in objects])
            self._completed_store_tasks = {task_id: _FakeStoreResult(True, 0)}

        async def _execute_load_in_loop(self, keys, objects, task_id):
            self.seen_load_sizes.append([o.meta.phy_size for o in objects])
            self._completed_load_tasks = {task_id: None}

    mod.DynamicNixlStoreL2Adapter = DynamicNixlStoreL2Adapter

    # Parent packages must exist as stubs too.
    pkg_l1 = types.ModuleType("lmcache")
    pkg_v1 = types.ModuleType("lmcache.v1")
    pkg_dist = types.ModuleType("lmcache.v1.distributed")
    pkg_l2a = types.ModuleType("lmcache.v1.distributed.l2_adapters")
    pkg_l2a.nixl_store_dynamic_l2_adapter = mod

    monkeypatch.setitem(sys.modules, "lmcache", pkg_l1)
    monkeypatch.setitem(sys.modules, "lmcache.v1", pkg_v1)
    monkeypatch.setitem(sys.modules, "lmcache.v1.distributed", pkg_dist)
    monkeypatch.setitem(sys.modules, "lmcache.v1.distributed.l2_adapters", pkg_l2a)
    monkeypatch.setitem(
        sys.modules, "lmcache.v1.distributed.l2_adapters.nixl_store_dynamic_l2_adapter", mod
    )
    return mod


def _new_adapter(mod):
    """Fresh adapter instance with the bookkeeping the wrappers touch."""
    import threading

    adapter = mod.DynamicNixlStoreL2Adapter()
    adapter.seen_store_sizes = []
    adapter.seen_load_sizes = []
    adapter._completed_store_tasks = {}
    adapter._completed_load_tasks = {}
    adapter._lock = threading.Lock()
    adapter._memory_objects = {}
    adapter.nixl_agent = types.SimpleNamespace(l1_align_bytes=4096)
    return adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_store_wrapper_narrows_only_narrowed_objects(monkeypatch):
    from kvpersist import compress_serde

    mod = _install_fake_lmcache(monkeypatch)
    assert compress_serde._install_size_patch_now() is True

    adapter = _new_adapter(mod)

    narrowed = _FakeObj(8192, narrowed=28 + 100)   # frame 128 -> pad to 4096
    plain = _FakeObj(8192)                          # not narrowed
    _run(adapter._execute_store_in_the_loop([], [narrowed, plain], 1))

    # During the call the adapter saw the PADDED frame size for the narrowed
    # object and the ORIGINAL size for the plain one.
    assert adapter.seen_store_sizes[0] == [4096, 8192]
    # Afterwards both are restored -- L1 free() accounting uses meta.phy_size.
    assert narrowed.meta.phy_size == 8192
    assert plain.meta.phy_size == 8192


def test_load_wrapper_shrinks_to_stored_file_size(monkeypatch):
    from kvpersist import compress_serde

    mod = _install_fake_lmcache(monkeypatch)
    compress_serde._install_size_patch_now()

    adapter = _new_adapter(mod)
    # Index says the file on disk is 4096 bytes.
    adapter._memory_objects = {"k": types.SimpleNamespace(size=4096)}

    dst = _FakeObj(8192)  # full uncompressed allocation
    _run(adapter._execute_load_in_loop(["k"], [dst], 1))

    assert adapter.seen_load_sizes[0] == [4096], (
        "load DMA length must match the stored file size, not the allocation"
    )
    # Restored after the call.
    assert dst.meta.phy_size == 8192


def test_patch_is_idempotent(monkeypatch):
    from kvpersist import compress_serde

    mod = _install_fake_lmcache(monkeypatch)
    cls = mod.DynamicNixlStoreL2Adapter
    assert compress_serde._install_size_patch_now() is True
    first = cls._execute_store_in_the_loop
    assert compress_serde._install_size_patch_now() is True
    second = cls._execute_store_in_the_loop
    assert first is second, "patch must not wrap twice"


def test_patch_not_installed_without_kvp_serde(monkeypatch):
    """Review point: with serde=none the adapter class must be left alone."""
    from kvpersist import compress_serde

    mod = _install_fake_lmcache(monkeypatch)
    cls = mod.DynamicNixlStoreL2Adapter
    # Simulate a deployment that only calls apply() (no kvp serde configured).
    # The alias _install_size_patch() must NOT install the wrappers.
    compress_serde._install_size_patch()
    assert not getattr(cls, "_kvp_size_patch_installed", False), (
        "size patch must only install when a kvp-* serde is actually built"
    )


# --- registration / fail-fast ---------------------------------------------


def test_register_without_lmcache_returns_false(monkeypatch):
    from kvpersist import compress_serde

    monkeypatch.setitem(sys.modules, "lmcache", None)  # force ImportError path
    for name in list(sys.modules):
        if name.startswith("lmcache"):
            monkeypatch.setitem(sys.modules, name, None)
    # register() imports lmcache.v1.distributed.serde.factory; absent -> False
    assert compress_serde.register() is False


def test_register_with_fake_factory_registers_all(monkeypatch):
    from kvpersist import compress_serde

    registered = {}

    class FakeFactory:
        @staticmethod
        def register_serde_factory(name, factory):
            registered[name] = factory

    # Build the module chain needed by register()'s import.
    mods = {
        "lmcache": types.ModuleType("lmcache"),
        "lmcache.v1": types.ModuleType("lmcache.v1"),
        "lmcache.v1.distributed": types.ModuleType("lmcache.v1.distributed"),
        "lmcache.v1.distributed.serde": types.ModuleType("lmcache.v1.distributed.serde"),
        "lmcache.v1.distributed.serde.factory": FakeFactory,
    }
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)

    # Fake codecs so check_codec_available passes for lz4/zstd.
    fake_lz4 = types.ModuleType("lz4")
    fake_lz4_frame = types.ModuleType("lz4.frame")
    fake_lz4.frame = fake_lz4_frame
    monkeypatch.setitem(sys.modules, "lz4", fake_lz4)
    monkeypatch.setitem(sys.modules, "lz4.frame", fake_lz4_frame)
    fake_zstd = types.ModuleType("zstandard")
    monkeypatch.setitem(sys.modules, "zstandard", fake_zstd)

    assert compress_serde.register() is True
    assert set(registered) == {"kvp-lz4", "kvp-zstd", "kvp-raw"}


def test_register_raises_when_codec_missing(monkeypatch):
    """Review point: register() must fail-fast, not reach READY."""
    from kvpersist import compress_serde

    class FakeFactory:
        @staticmethod
        def register_serde_factory(name, factory):
            pass

    mods = {
        "lmcache": types.ModuleType("lmcache"),
        "lmcache.v1": types.ModuleType("lmcache.v1"),
        "lmcache.v1.distributed": types.ModuleType("lmcache.v1.distributed"),
        "lmcache.v1.distributed.serde": types.ModuleType("lmcache.v1.distributed.serde"),
        "lmcache.v1.distributed.serde.factory": FakeFactory,
    }
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)

    # Make sure lz4/zstandard are genuinely absent.
    for name in ("lz4", "lz4.frame", "zstandard"):
        monkeypatch.setitem(sys.modules, name, None)

    with pytest.raises(compress_serde.CodecError):
        compress_serde.register()