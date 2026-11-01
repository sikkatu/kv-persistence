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

from kvpersist.compress_serde import (  # noqa: E402
    CodecError,
    require_codec,
    resolve_required_codec,
)


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


def test_store_wrapper_multiple_objects_mixed(monkeypatch):
    """Review point: several blocks in ONE call, some narrowed, some not.

    The wrapper must narrow each narrowed object independently and leave the
    rest untouched -- and restore EVERY one of them afterwards.
    """
    from kvpersist import compress_serde

    mod = _install_fake_lmcache(monkeypatch)
    compress_serde._install_size_patch_now()
    adapter = _new_adapter(mod)

    objs = [
        _FakeObj(8192, narrowed=100),   # -> 4096
        _FakeObj(8192),                 # -> 8192 (untouched)
        _FakeObj(12288, narrowed=5000), # -> 8192 (5000 -> pad to 8192)
        _FakeObj(12288),                # -> 12288 (untouched)
    ]
    _run(adapter._execute_store_in_the_loop([], objs, 1))

    assert adapter.seen_store_sizes[0] == [4096, 8192, 8192, 12288], (
        "each object must be narrowed independently"
    )
    assert [o.meta.phy_size for o in objs] == [8192, 8192, 12288, 12288], (
        "every phy_size must be restored, even after a mixed batch"
    )


def test_store_wrapper_restores_when_inner_raises(monkeypatch):
    """A failing inner store/load must not leak narrowed metadata."""
    import asyncio as _aio

    from kvpersist import compress_serde

    mod = _install_fake_lmcache(monkeypatch)
    compress_serde._install_size_patch_now()

    # Fresh class whose original store RAISES.
    class Boom:
        async def _execute_store_in_the_loop(self, keys, objects, task_id):
            raise RuntimeError("disk full")

        async def _execute_load_in_loop(self, keys, objects, task_id):
            raise RuntimeError("disk gone")

    import types as _t

    cls = mod.DynamicNixlStoreL2Adapter
    # Install the patch on a class whose originals raise.
    orig_store, orig_load = cls._execute_store_in_the_loop, cls._execute_load_in_loop

    async def raising_store(self, keys, objects, task_id):
        raise RuntimeError("disk full")

    async def raising_load(self, keys, objects, task_id):
        raise RuntimeError("disk gone")

    cls._execute_store_in_the_loop = raising_store
    cls._execute_load_in_loop = raising_load
    cls._kvp_size_patch_installed = False
    compress_serde._install_size_patch_now()

    adapter = _new_adapter(mod)
    narrowed = _FakeObj(8192, narrowed=100)
    try:
        _aio.run(adapter._execute_store_in_the_loop([], [narrowed], 1))
    except RuntimeError:
        pass
    assert narrowed.meta.phy_size == 8192, (
        "phy_size must be restored even when the inner adapter raises"
    )

    dst = _FakeObj(8192, narrowed=100)
    adapter._memory_objects = {"k": types.SimpleNamespace(size=4096)}
    try:
        _aio.run(adapter._execute_load_in_loop(["k"], [dst], 1))
    except RuntimeError:
        pass
    assert dst.meta.phy_size == 8192, (
        "phy_size must be restored even when the inner load raises"
    )


def test_load_wrapper_handles_missing_index_entry(monkeypatch):
    """Review point: a key NOT in the in-memory index must not crash the load;
    its destination keeps the original size (upstream falls back to the file)."""
    from kvpersist import compress_serde

    mod = _install_fake_lmcache(monkeypatch)
    compress_serde._install_size_patch_now()
    adapter = _new_adapter(mod)
    adapter._memory_objects = {}  # key not indexed (cold start / after restart)

    dst = _FakeObj(8192)
    _run(adapter._execute_load_in_loop(["missing"], [dst], 1))
    # No stored size -> no narrowing; the ORIGINAL phy_size is passed through.
    assert adapter.seen_load_sizes[0] == [8192]
    assert dst.meta.phy_size == 8192


def test_load_wrapper_size_mismatch_stored_bigger_than_alloc(monkeypatch):
    """Review point: file size LARGER than the destination allocation must not
    be applied (would extend past the buffer); the wrapper keeps the original."""
    from kvpersist import compress_serde

    mod = _install_fake_lmcache(monkeypatch)
    compress_serde._install_size_patch_now()
    adapter = _new_adapter(mod)
    adapter._memory_objects = {"k": types.SimpleNamespace(size=16384)}

    dst = _FakeObj(8192)  # allocation smaller than the stored file
    _run(adapter._execute_load_in_loop(["k"], [dst], 1))
    # Only sizes strictly SMALLER than the allocation are applied.
    assert adapter.seen_load_sizes[0] == [8192], (
        "a stored size larger than the allocation must not shrink the DMA length"
    )


# --- registration / require_codec / resolve_required_codec ----------------


def test_register_without_lmcache_returns_false(monkeypatch):
    from kvpersist import compress_serde

    for name in list(sys.modules):
        if name.startswith("lmcache"):
            monkeypatch.setitem(sys.modules, name, None)
    assert compress_serde.register() is False


def test_register_with_fake_factory_registers_all(monkeypatch):
    from kvpersist import compress_serde

    registered = {}

    class FakeFactory:
        @staticmethod
        def register_serde_factory(name, factory):
            registered[name] = factory

    mods = {
        "lmcache": types.ModuleType("lmcache"),
        "lmcache.v1": types.ModuleType("lmcache.v1"),
        "lmcache.v1.distributed": types.ModuleType("lmcache.v1.distributed"),
        "lmcache.v1.distributed.serde": types.ModuleType("lmcache.v1.distributed.serde"),
        "lmcache.v1.distributed.serde.factory": FakeFactory,
    }
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)

    assert compress_serde.register() is True
    assert set(registered) == {"kvp-lz4", "kvp-zstd", "kvp-raw"}


def test_register_does_not_require_any_codec_package(monkeypatch):
    """Review fix: register() itself must succeed with NO codec packages.

    The old code called check_codec_available() for BOTH codecs inside
    register(), so a raw-mode launch failed when either package was missing.
    Registration is now unconditional; availability is checked per SELECTED
    codec by require_codec().
    """
    from kvpersist import compress_serde

    registered = {}

    class FakeFactory:
        @staticmethod
        def register_serde_factory(name, factory):
            registered[name] = factory

    mods = {
        "lmcache": types.ModuleType("lmcache"),
        "lmcache.v1": types.ModuleType("lmcache.v1"),
        "lmcache.v1.distributed": types.ModuleType("lmcache.v1.distributed"),
        "lmcache.v1.distributed.serde": types.ModuleType("lmcache.v1.distributed.serde"),
        "lmcache.v1.distributed.serde.factory": FakeFactory,
    }
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)

    # BOTH codec packages genuinely absent.
    for name in ("lz4", "lz4.frame", "zstandard"):
        monkeypatch.setitem(sys.modules, name, None)

    assert compress_serde.register() is True, (
        "register() must succeed without any codec package installed"
    )
    assert set(registered) == {"kvp-lz4", "kvp-zstd", "kvp-raw"}


# --- require_codec: only the SELECTED codec is required (review fix) -------


def test_require_codec_none_mode_needs_no_packages(monkeypatch):
    """LMCACHE_L2_SERDE=none must start WITHOUT lz4 AND WITHOUT zstandard."""
    for name in ("lz4", "lz4.frame", "zstandard"):
        monkeypatch.setitem(sys.modules, name, None)
    for value in (None, "", "none", "raw", "NONE", "Raw"):
        require_codec(value)  # must NOT raise


def test_require_codec_zstd_needs_only_zstandard(monkeypatch):
    """zstd mode must not require lz4 (review: 'may additionally demand lz4')."""
    # lz4 absent, zstandard present -> must pass.
    monkeypatch.setitem(sys.modules, "lz4", None)
    monkeypatch.setitem(sys.modules, "lz4.frame", None)
    fake_zstd = types.ModuleType("zstandard")
    monkeypatch.setitem(sys.modules, "zstandard", fake_zstd)
    require_codec("zstd")  # must NOT raise despite lz4 being missing

    # zstandard absent -> must fail.
    monkeypatch.setitem(sys.modules, "zstandard", None)
    with pytest.raises(CodecError, match="zstandard"):
        require_codec("zstd")


def test_require_codec_lz4_needs_only_lz4(monkeypatch):
    """lz4 mode must not require zstandard."""
    # zstandard absent, lz4 present -> must pass.
    monkeypatch.setitem(sys.modules, "zstandard", None)
    fake_lz4 = types.ModuleType("lz4")
    fake_lz4_frame = types.ModuleType("lz4.frame")
    fake_lz4.frame = fake_lz4_frame
    monkeypatch.setitem(sys.modules, "lz4", fake_lz4)
    monkeypatch.setitem(sys.modules, "lz4.frame", fake_lz4_frame)
    require_codec("lz4")  # must NOT raise despite zstandard being missing

    # lz4 absent -> must fail.
    monkeypatch.setitem(sys.modules, "lz4", None)
    monkeypatch.setitem(sys.modules, "lz4.frame", None)
    with pytest.raises(CodecError, match="lz4"):
        require_codec("lz4")


# --- resolve_required_codec: what THIS launch needs ------------------------


def test_resolve_required_codec_from_adapter_json(monkeypatch):
    """The --l2-adapter JSON is the authority: it names the real serde."""
    for name in ("LMCACHE_L2_SERDE", "KVP_L2_SERDE"):
        monkeypatch.delenv(name, raising=False)
    argv = [
        "server",
        "--l2-adapter",
        '{"type": "nixl_store_dynamic", "serde": {"type": "kvp-zstd"}}',
    ]
    assert resolve_required_codec(argv) == "zstd"

    argv_lz4 = [
        "server",
        "--l2-adapter=" '{"type": "x", "serde": {"type": "kvp-lz4"}}',
    ]
    assert resolve_required_codec(argv_lz4) == "lz4"


def test_resolve_required_codec_raw_adapter_needs_nothing(monkeypatch):
    for name in ("LMCACHE_L2_SERDE", "KVP_L2_SERDE"):
        monkeypatch.delenv(name, raising=False)
    argv = ["server", "--l2-adapter", '{"type": "nixl_store_dynamic"}']
    assert resolve_required_codec(argv) is None, (
        "an adapter without a serde block must require no codec package"
    )


def test_resolve_required_codec_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("LMCACHE_L2_SERDE", "zstd")
    assert resolve_required_codec(["server"]) == "zstd"
    monkeypatch.delenv("LMCACHE_L2_SERDE")
    monkeypatch.setenv("KVP_L2_SERDE", "lz4")
    assert resolve_required_codec(["server"]) == "lz4"
    monkeypatch.delenv("KVP_L2_SERDE")
    assert resolve_required_codec(["server"]) is None


def test_resolve_required_codec_json_wins_over_env(monkeypatch):
    monkeypatch.setenv("LMCACHE_L2_SERDE", "zstd")
    argv = [
        "server",
        "--l2-adapter",
        '{"serde": {"type": "kvp-lz4"}}',
    ]
    assert resolve_required_codec(argv) == "lz4", (
        "the adapter JSON names the REAL serde and must win over the env hint"
    )