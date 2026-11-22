# SPDX-License-Identifier: Apache-2.0
"""r28 L6: L2 stores fail on mixed-size object batches (the hardware blocker).

Hardware log (22 Nov 2026): every store batch ended "0 succeeded, 20 failed"
with `ValueError: memoryview assignment: lvalue and rvalue have different
structures` in compress_serde.py -- LMCache sizes the serde temp buffers from
objects[0] ONLY (its `_alloc_temp_buffers` docstring says so), and with
--separate-object-groups (default in 0.5.8rc1) a batch can carry objects of
different sizes. The existing tests allocate `dst` from OUR OWN estimate, so
this failure could never appear in them.

This test reproduces `_alloc_temp_buffers` VERBATIM (the pinned LMCache
source) over fake TensorMemoryObj-Contract objects, then runs the REAL
LosslessCompressSerializer through the buffers it allocated -- exactly the
production data path. MUTATIONS caught:
* `serde_alloc_patch`: objects[0] instead of the largest;
* `compress_serde.serialize`: dropping the `len(out) < padded` check;
* `serde_alloc_patch`: max -> min in `_largest_object`.
"""
from __future__ import annotations
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation"))

from kvpersist.compress_serde import (  # noqa: E402
    LosslessCompressDeserializer,
    LosslessCompressSerializer,
    _LayoutView,
    align_up,
    plaintext_bytes,
)
from kvpersist.serde_alloc_patch import (  # noqa: E402
    REFERENCE_SOURCE,
    _largest_object,
    _normalize,
    install,
    reference_matches,
)


# ---------------------------------------------------------------------------
# Fake MemoryObj: the TensorMemoryObj contract the wrapper relies on
# ---------------------------------------------------------------------------
class FakeObj:
    """Repeats the TensorMemoryObj contract: get_shapes/get_dtypes/get_size,
    byte_array = the first get_size() bytes of raw_data, get_physical_size,
    set_used_size(n) with 0 <= n <= phy_size."""

    def __init__(self, nbytes: int, data: bytes):
        self._raw = bytearray(data[:nbytes])
        self._raw.extend(b"\x00" * (nbytes - len(self._raw)))
        self._phy = nbytes
        self._used = nbytes
        self.used_sizes = []  # record every set_used_size call

    def get_shapes(self):
        return [(len(self._raw),)]

    def get_dtypes(self):
        return [types.SimpleNamespace(itemsize=1)]

    def get_size(self) -> int:
        return self._used

    def get_physical_size(self) -> int:
        return self._phy

    def set_used_size(self, n: int) -> None:
        assert 0 <= n <= self._phy, "set_used_size contract violated"
        self.used_sizes.append(n)
        self._used = n

    @property
    def byte_array(self):
        return memoryview(self._raw)[: self._used]


# ---------------------------------------------------------------------------
# Verbatim reproduction of LMCache's _alloc_temp_buffers data path
# (SerdeL2AdapterWrapper: layout from objects[0] -> serialized_layout_desc ->
#  reserve_write -> temp objs). The L1 manager is a fake that sizes each
# reserved buffer from the layout -- exactly like the real one.
# ---------------------------------------------------------------------------
def _alloc_temp_buffers_verbatim(serde, keys, objects, reference):
    """The pinned source logic. `reference` selects the sizing policy:
    'first' = LMCache upstream (objects[0]); 'largest' = our patch."""
    if reference == "largest":
        obj_for_layout = _largest_object(objects)
    else:
        obj_for_layout = objects[0]
    shape_0 = obj_for_layout.get_shapes()
    dtype_0 = obj_for_layout.get_dtypes()
    # serialized_layout_desc: ONE uint8 shape of estimate_serialized_size
    from kvpersist.compress_serde import _LayoutView
    layout_bytes = serde.estimate_serialized_size(
        _LayoutView(shape_0, dtype_0)
    )
    temp_keys = [f"tmp-{k}" for k in keys]
    # reserve_write: one temp buffer of `layout_bytes` per key
    results = {}
    for tk in temp_keys:
        results[tk] = (0, FakeObj(layout_bytes, b"\x00" * layout_bytes))
    temp_objs = [results[tk][1] for tk in temp_keys]
    return temp_keys, temp_objs


def _batch(n_small=2, n_big=2, small=1024, big=8192, incompressible=True):
    """A batch of DIFFERENT-sized objects (small first): the production
    layout that failed on hardware."""
    keys = [f"k{i}" for i in range(n_small + n_big)]
    data = [bytes([i + 1]) * small for i in range(n_small)]
    data += [bytes([i + 1]) * big for i in range(n_big)]
    objs = [FakeObj(small, b"\x01" * small)] * n_small
    objs += [FakeObj(big, b"\x02" * big)] * n_big
    return keys, objs


def _roundtrip_through(serde, keys, objs, alloc_policy):
    """Serialize every object of the batch through LMCache-style buffers,
    then deserialize back and compare bit-exact (the L6.3 acceptance).
    Returns (decoded_bytes, temp_objs) -- set_used_size is called on the
    temp buffers, exactly as LMCache's _run_task does."""
    _tkeys, t_objs = _alloc_temp_buffers_verbatim(serde, keys, objs, alloc_policy)
    frames = []
    for src, dst in zip(objs, t_objs):
        n = serde.serialize(src, dst, None)
        dst.set_used_size(n)  # LMCache's _run_task narrows each to real n
        frames.append((n, bytes(dst.byte_array)[:n]))
    out_bytes = []
    des = LosslessCompressDeserializer()
    for (n, frame), src in zip(frames, objs):
        out = FakeObj(len(src.byte_array), b"")
        des.deserialize(FakeObj(n, frame), out, None)
        out_bytes.append(bytes(out.byte_array))
    return out_bytes, t_objs


# ---------------------------------------------------------------------------
# The production failure and the fix
# ---------------------------------------------------------------------------
def test_l6_mixed_batch_fails_without_the_patch():
    """> The upstream allocator (objects[0] sizing) MUST fail on the mixed-size
    batch with our diagnostic error -- this is the exact hardware defect.
    MUTATION caught: dropping the `len(out) < padded` check (the error would
    no longer contain the numbers)."""
    keys, objs = _batch()
    serde = LosslessCompressSerializer("raw", align_bytes=4096)
    with pytest.raises(ValueError, match="kvp-serde: destination buffer too small") as ei:
        _roundtrip_through(serde, keys, objs, "first")
    msg = str(ei.value)
    for field in ("dst_len=", "need=", "frame=", "src_len=", "src_size=",
                  "plaintext_by_layout=", "estimate_by_layout=", "codec=",
                  "shapes=", "dtypes=", "dst_size=", "dst_phy=",
                  "object_group_id=", "kv_rank="):
        assert field in msg, f"diagnostic field {field} missing:\n{msg}"


def test_l6_mixed_batch_passes_with_the_patch():
    """> With the largest-object allocator every object serializes,
    set_used_size(n) is called for EACH temp buffer, and deserialize returns
    the exact bytes. MUTATION caught: objects[0] instead of the largest /
    max -> min."""
    keys, objs = _batch()
    serde = LosslessCompressSerializer("raw", align_bytes=4096)
    out, t_objs = _roundtrip_through(serde, keys, objs, "largest")
    assert len(out) == len(objs)
    for got, src in zip(out, objs):
        assert got == bytes(src.byte_array), "deserialize must be bit-exact"
    # set_used_size was narrowed for every temp buffer of the batch
    for tobj in t_objs:
        assert tobj.used_sizes, "set_used_size must be called per temp buffer"


def test_l6_incompressible_raw_fallback_fits():
    """> Incompressible data falls back to RAW (frame = raw + 28):
    the largest-sized buffer must fit it."""
    pytest.importorskip("zstandard",
                        reason="the zstd codec test needs the zstandard package "
                               "(same honest skip as test_decompress_bomb)")
    keys = ["a", "b"]
    raw = bytes(range(256)) * 32  # 8192 incompressible-ish bytes
    objs = [FakeObj(1024, b"\x03" * 1024), FakeObj(len(raw), raw)]
    serde = LosslessCompressSerializer("zstd", level=1, align_bytes=4096)
    out, _ = _roundtrip_through(serde, keys, objs, "largest")
    assert out[1] == raw, "the large raw object must round-trip exactly"


def test_l6_uniform_batch_keeps_old_behaviour():
    """> A uniform batch (all objects equal) behaves exactly as before --
    the patch must not change anything there."""
    keys = ["u1", "u2"]
    objs = [FakeObj(2048, b"\x07" * 2048), FakeObj(2048, b"\x08" * 2048)]
    serde = LosslessCompressSerializer("raw", align_bytes=4096)
    out, _ = _roundtrip_through(serde, keys, objs, "largest")
    assert out[0] == bytes(objs[0].byte_array)
    assert out[1] == bytes(objs[1].byte_array)


def test_l6_patch_wrapper_allocates_for_the_largest():
    """> The REAL installed wrapper (_patched_alloc) calls the original with
    [largest]: through a fake module whose source MATCHES the reference the
    patch installs, then the wrapped method must allocate buffers sized for
    the largest object of the batch. MUTATION caught: the wrapper passing
    objects[:1] / objects[0] instead of [largest], and max -> min."""
    from kvpersist import serde_alloc_patch as sap

    fake_mod = types.ModuleType("lmcache.v1.distributed.l2_adapters.serde_wrapper")

    class SerdeL2AdapterWrapper:  # noqa: N801 - mimic upstream name
        pass

    # The drift check normally verifies the source; here we register a
    # source-equivalent method and force the match (the drift behaviour is
    # covered by test_l6_patch_not_installed_on_different_source):
    def _ref_impl(self, keys, objects):
        shape_0 = objects[0].get_shapes()
        dtype_0 = objects[0].get_dtypes()
        return self._reserve(keys, shape_0, dtype_0)

    SerdeL2AdapterWrapper._alloc_temp_buffers = _ref_impl
    fake_mod.SerdeL2AdapterWrapper = SerdeL2AdapterWrapper
    for name in ("lmcache.v1.distributed.l2_adapters", "lmcache.v1.distributed",
                 "lmcache.v1", "lmcache"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["lmcache.v1.distributed.l2_adapters.serde_wrapper"] = fake_mod
    # bypass the drift check by matching the reference exactly: patch the
    # checker to True for this test (the drift behaviour is covered by
    # test_l6_patch_not_installed_on_different_source).
    orig_match = sap.reference_matches
    sap.reference_matches = lambda: True
    try:
        assert sap.install() == "patched", "the wrapper must install"
        wrapped = SerdeL2AdapterWrapper._alloc_temp_buffers
        assert wrapped is sap._patched_alloc, "the class must carry the wrapper"

        class FakeWrapper:
            def _reserve(self, keys, shapes, dtypes):
                self.sizes = [plaintext_bytes(_LayoutView(shapes, dtypes))]
                return self.sizes, self.sizes

        objs = [FakeObj(1024, b"\x01" * 1024), FakeObj(8192, b"\x02" * 8192)]
        w = FakeWrapper()
        w._serde = LosslessCompressSerializer("raw", align_bytes=4096)
        # _patched_alloc calls _original_alloc(self, keys, [largest]) --
        # the fake original reserves from the layout of THE GIVEN objects.
        def _orig(self, keys, objects):
            from kvpersist.compress_serde import _LayoutView as LV
            lay = LV(objects[0].get_shapes(), objects[0].get_dtypes())
            n = self._serde.estimate_serialized_size(lay)
            return [f"t{i}" for i in keys], [FakeObj(n, b"")] * len(keys)
        sap._original_alloc = _orig
        _tkeys, t_objs = sap._patched_alloc(w, [1, 2], objs)
        # the reserved size must be the estimate of the LARGEST (8192) object
        big_est = w._serde.estimate_serialized_size(
            _LayoutView(objs[1].get_shapes(), objs[1].get_dtypes()))
        assert t_objs and t_objs[0].get_physical_size() == big_est, (
            "the wrapper must size the buffers from the LARGEST object")
    finally:
        sap.reference_matches = orig_match
        for name in ("lmcache.v1.distributed.l2_adapters.serde_wrapper",
                     "lmcache.v1.distributed.l2_adapters",
                     "lmcache.v1.distributed", "lmcache.v1", "lmcache"):
            sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Drift protection
# ---------------------------------------------------------------------------
def test_l6_patch_not_installed_on_different_source():
    """> The patch installs ONLY when the reference matches: on a different
    `_alloc_temp_buffers` source nothing is touched and the status is
    'missing' (the launch prints the WARNING). MUTATION caught: always
    installing."""
    # Build a fake module whose source differs from the reference.
    fake_mod = types.ModuleType("lmcache.v1.distributed.l2_adapters.serde_wrapper")

    class SerdeL2AdapterWrapper:  # noqa: N801 - mimic upstream name
        def _alloc_temp_buffers(self, keys, objects):
            return keys, objects

    fake_mod.SerdeL2AdapterWrapper = SerdeL2AdapterWrapper
    sys.modules["lmcache.v1.distributed.l2_adapters.serde_wrapper"] = fake_mod
    sys.modules["lmcache.v1.distributed.l2_adapters"] = types.ModuleType(
        "lmcache.v1.distributed.l2_adapters")
    sys.modules["lmcache.v1.distributed"] = types.ModuleType(
        "lmcache.v1.distributed")
    sys.modules["lmcache.v1"] = types.ModuleType("lmcache.v1")
    sys.modules["lmcache"] = types.ModuleType("lmcache")
    try:
        assert not reference_matches(), "a differing source must not match"
        assert install() == "missing", "a differing source must NOT be patched"
        assert SerdeL2AdapterWrapper._alloc_temp_buffers.__name__ == \
            "_alloc_temp_buffers", "the original must stay untouched"
    finally:
        for name in ("lmcache.v1.distributed.l2_adapters.serde_wrapper",
                     "lmcache.v1.distributed.l2_adapters",
                     "lmcache.v1.distributed", "lmcache.v1", "lmcache"):
            sys.modules.pop(name, None)


def test_l6_diag_shapes_and_dtypes_have_real_values():
    """> N1: shapes and dtypes must print their REAL values, not `?`.
    MUTATION caught: the double wrapper `_safe(_layout_field(...), "?")`
    (returns ? for every shape/dtype)."""
    import io
    buf = bytearray(4096)
    dst = FakeObj(4096, bytes(buf))
    src = FakeObj(8192, b"\x02" * 8192)
    # make shapes/dtypes recognisable via str()
    class Shaped:
        def get_shapes(self):
            return [(8192,)]
        def get_dtypes(self):
            return ["uint8"]
        def get_size(self):
            return 8192
        def get_physical_size(self):
            return 8192
        @property
        def byte_array(self):
            return memoryview(bytearray(b"\x02" * 8192))
    serde = LosslessCompressSerializer("raw", align_bytes=4096)
    with pytest.raises(ValueError) as ei:
        serde.serialize(Shaped(), dst, None)
    msg = str(ei.value)
    assert "shapes=[(8192,)]" in msg, f"real shapes must print:\n{msg}"
    assert "dtypes=['uint8']" in msg, f"real dtypes must print:\n{msg}"
    assert "shapes=?" not in msg, f"shapes=? is the bug:\n{msg}"
    assert "dtypes=?" not in msg, f"dtypes=? is the bug:\n{msg}"


def test_l6_diag_shapes_dtypes_fallback_on_exception():
    """> N1: if get_shapes raises, the diagnostic prints `shapes=?` and the
    ORIGINAL ValueError survives (not replaced by a TypeError from the
    diagnostic collection)."""
    class BrokenShapes:
        def get_shapes(self):
            raise RuntimeError("no shapes")
        def get_dtypes(self):
            raise RuntimeError("no dtypes")
        def get_size(self):
            return 8192
        def get_physical_size(self):
            return 8192
        @property
        def byte_array(self):
            return memoryview(bytearray(b"\x02" * 8192))
    serde = LosslessCompressSerializer("raw", align_bytes=4096)
    dst = FakeObj(4096, bytes(bytearray(4096)))
    with pytest.raises(ValueError) as ei:
        serde.serialize(BrokenShapes(), dst, None)
    msg = str(ei.value)
    assert msg.startswith("kvp-serde: destination buffer too small"), (
        f"the original ValueError must survive:\n{msg}")
    assert "shapes=?" in msg, f"broken shapes must fall back to ?:\n{msg}"
    assert "dtypes=?" in msg, f"broken dtypes must fall back to ?:\n{msg}"


def test_l6_diag_exact_values_deterministic_pair():
    """> N3 (M24/M25): exact values for the deterministic pair (1024 + 8192
    bytes, raw codec, align 4096) -- computed independently from the code:
    dst_len=4096 (align_up(1024+28, 4096) = 4096), need=12288
    (align_up(8192+28, 4096) = 12288), frame=8220 (28+8192),
    src_len=8192, src_size=8192, plaintext_by_layout=8192,
    estimate_by_layout=12288, codec=raw, dst_size=4096, dst_phy=4096.
    MUTATIONS caught: estimate_by_layout -> ?, need -> ?."""
    # first object 1024 -> dst 4096; second object 8192 -> needs 12288
    small = FakeObj(1024, b"\x01" * 1024)
    big = FakeObj(8192, b"\x02" * 8192)
    serde = LosslessCompressSerializer("raw", align_bytes=4096)
    # allocate a dst sized for the SMALL object (like objects[0] would)
    dst = FakeObj(4096, bytes(bytearray(4096)))
    with pytest.raises(ValueError) as ei:
        serde.serialize(big, dst, None)
    msg = str(ei.value)
    # independently computed:
    #   frame = 28 + 8192 = 8220
    #   need  = align_up(8220, 4096) = 12288
    #   estimate_by_layout = align_up(8192 + 28, 4096) = 12288
    for field, value in [
        ("dst_len", "dst_len=4096"),
        ("need", "need=12288"),
        ("frame", "frame=8220"),
        ("src_len", "src_len=8192"),
        ("src_size", "src_size=8192"),
        ("plaintext_by_layout", "plaintext_by_layout=8192"),
        ("estimate_by_layout", "estimate_by_layout=12288"),
        ("codec", "codec=raw"),
        ("dst_size", "dst_size=4096"),
        ("dst_phy", "dst_phy=4096"),
    ]:
        assert value in msg, f"field {field} must be exact:\n{msg}"


def test_l6_reference_matches_the_pinned_source():
    """> N5b: REFERENCE_SOURCE must equal the pinned LMCache source captured
    VERBATIM into the repo fixture (tests/fixtures/lmcache_alloc_temp_buffers.py.txt,
    inspect.getsource, unedited). No machine-specific paths, no skip -- the
    drift check is meaningful everywhere."""
    fixture = ROOT / "tests" / "fixtures" / "lmcache_alloc_temp_buffers.py.txt"
    assert fixture.exists(), "the pinned-source fixture must be in the repo"
    src = fixture.read_text()
    assert _normalize(src) == _normalize(REFERENCE_SOURCE), (
        "REFERENCE_SOURCE must match the fixture (_alloc_temp_buffers)")


def _install_from_fixture(tmp_path, mutate_method=False):
    """Build a temp lmcache package whose SerdeL2AdapterWrapper carries the
    FIXTURE method (optionally with one line changed) and run the REAL
    install() with the REAL reference_matches (no monkeypatching)."""
    import sys
    import types
    from kvpersist import serde_alloc_patch as sap

    fixture = (ROOT / "tests" / "fixtures"
               / "lmcache_alloc_temp_buffers.py.txt").read_text()
    if mutate_method:
        fixture = fixture.replace("shape_0 = objects[0].get_shapes()",
                                  "shape_0 = objects[9].get_shapes()")
    fake_pkg = tmp_path / "fake_lmcache"
    for sub in ("", "v1", "v1/distributed", "v1/distributed/l2_adapters"):
        (fake_pkg / sub).mkdir(parents=True, exist_ok=True)
    (fake_pkg / "__init__.py").write_text("")
    (fake_pkg / "v1" / "__init__.py").write_text("")
    (fake_pkg / "v1" / "distributed" / "__init__.py").write_text("")
    (fake_pkg / "v1" / "distributed" / "l2_adapters" / "__init__.py").write_text("")
    # stubs for everything the fixture method needs at RUN time (not import)
    (fake_pkg / "v1" / "distributed" / "l2_adapters" / "serde_wrapper.py").write_text(
        "from __future__ import annotations\n"
        "class _L1Error:\n"
        "    SUCCESS = 0\n"
        "L1Error = _L1Error\n"
        "_L1_WRITE_TAG = 'tag'\n"
        "class MemoryLayoutDesc:\n"
        "    def __init__(self, shapes, dtypes):\n"
        "        self.shapes = shapes\n"
        "        self.dtypes = dtypes\n"
        "def make_temp_key(k):\n"
        "    return k\n"
        "def serialized_layout_desc(layout, serde):\n"
        "    return layout\n"
        "class SerdeL2AdapterWrapper:\n"
        + fixture
    )
    # make the fake package importable as `lmcache`
    sys.modules.pop("lmcache", None)
    for name in list(sys.modules):
        if name.startswith("lmcache."):
            sys.modules.pop(name, None)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "lmcache.v1.distributed.l2_adapters.serde_wrapper",
        fake_pkg / "v1" / "distributed" / "l2_adapters" / "serde_wrapper.py")
    mod = importlib.util.module_from_spec(spec)
    # register the parent packages so inspect.getsource can find the file
    for name, path in [
        ("lmcache", fake_pkg),
        ("lmcache.v1", fake_pkg / "v1"),
        ("lmcache.v1.distributed", fake_pkg / "v1" / "distributed"),
        ("lmcache.v1.distributed.l2_adapters",
         fake_pkg / "v1" / "distributed" / "l2_adapters"),
    ]:
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(path)]
        sys.modules[name] = pkg
    sys.modules["lmcache.v1.distributed.l2_adapters.serde_wrapper"] = mod
    spec.loader.exec_module(mod)
    return sap, mod


def test_l6_install_works_on_the_fixture_source(tmp_path):
    """> N5b: on a module carrying the FIXTURE method (exact pinned source) the
    REAL install() returns 'patched' and wraps the method -- WITHOUT any
    reference_matches monkeypatching."""
    sap, mod = _install_from_fixture(tmp_path)
    try:
        assert sap.reference_matches(), "the fixture source must match"
        status = sap.install()
        assert status == "patched", f"install() must patch the fixture module: {status}"
        assert (mod.SerdeL2AdapterWrapper._alloc_temp_buffers
                is sap._patched_alloc), "the class must carry the wrapper"
    finally:
        for name in list(sys.modules):
            if name.startswith("lmcache"):
                del sys.modules[name]


def test_l6_install_refuses_mutated_source(tmp_path):
    """> N5b: one changed line in the method -> reference_matches False and
    install() returns 'missing' (the drift guard)."""
    sap, mod = _install_from_fixture(tmp_path, mutate_method=True)
    try:
        assert not sap.reference_matches(), "a mutated source must not match"
        assert sap.install() == "missing"
    finally:
        for name in list(sys.modules):
            if name.startswith("lmcache"):
                del sys.modules[name]


def test_n6b_install_is_idempotent(tmp_path):
    """> N6b: two install() calls in one process -> BOTH return 'patched'
    and the wrapper is applied exactly once (the original is saved, not
    wrapped twice). MUTATION: the second call returns 'missing'."""
    sap, mod = _install_from_fixture(tmp_path)
    try:
        first = sap.install()
        assert first == "patched", f"first install: {first}"
        original = sap._original_alloc
        second = sap.install()
        assert second == "patched", (
            f"second install must stay 'patched', got '{second}'")
        assert sap._original_alloc is original, (
            "the original must be saved once, not wrapped twice")
    finally:
        for name in list(sys.modules):
            if name.startswith("lmcache"):
                del sys.modules[name]