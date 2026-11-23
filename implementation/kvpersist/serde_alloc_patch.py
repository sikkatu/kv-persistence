"""Patch: size the serde temp buffers from the LARGEST object of the batch.

WHY (r28 L6, hardware log "0 succeeded, 20 failed"): LMCache's
``SerdeL2AdapterWrapper._alloc_temp_buffers`` derives the serialized layout
from ``objects[0]`` ONLY (its docstring: "All entries must share a single
(shape, dtype)"). With ``--separate-object-groups`` (default in 0.5.8rc1) a
batch can carry objects of DIFFERENT sizes: every temp buffer gets the FIRST
object's size, ``LosslessCompressSerializer.serialize`` then refuses the
larger ones ("kvp-serde: destination buffer too small", r28 L6.1) and the
ALL-OR-NOTHING wrapper fails the whole batch. The adapter then reports
``L2 store completed: adapter 0, 0 succeeded, 20 failed`` -- nothing ever
reaches NVMe while the service looks healthy.

THE FIX: wrap ``_alloc_temp_buffers(self, keys, objects)`` and call the
original with ``[largest]``, where ``largest`` is the object with the maximum
``plaintext_bytes(MemoryLayoutDesc(o.get_shapes(), o.get_dtypes()))``. Every
temp buffer of the batch is then sized for the largest object;
``set_used_size(n)`` afterwards narrows each to its real ``n`` -- the on-disk
frame stays exact (the KVCZ format and ``estimate_serialized_size`` are NOT
touched). Nothing else in LMCache is changed.

DRIFT PROTECTION: the patch installs ONLY when the normalized source of
``_alloc_temp_buffers`` matches the reference captured from the pinned LMCache
build. Otherwise a loud ``[kvp] WARNING: serde temp-buffer patch NOT
installed: LMCache source differs`` is printed and the launch continues --
store failures are then caught loudly by ``kvp_scan_l2_failures`` (r28 L7).

Usage (inside the image / host venv):
    PYTHONPATH=<repo>/implementation python3 -m kvpersist.serde_alloc_patch --selfcheck
``--selfcheck`` prints ``serde_alloc_patch selfcheck: OK`` and exits 0 when
the reference matched and the patch is in place (wired into
scripts/verify-image-custom.sh).
"""
from __future__ import annotations
import inspect
import re
import sys

# Normalized reference of SerdeL2AdapterWrapper._alloc_temp_buffers as found
# in the pinned image (lmcache 0.5.8rc1.post48). Every whitespace run collapses
# to one space -- the drift check compares the normalized source, not the
# exact indentation.
REFERENCE_SOURCE = """def _alloc_temp_buffers( self, keys: list[ObjectKey], objects: list[MemoryObj], ) -> tuple[list[ObjectKey], list[MemoryObj] | None]: \"\"\"Reserve one temp byte buffer per input key. All-or-nothing: any single failure releases the partial successes and returns ``(temp_keys, None)``. Args: keys: Original logical keys; used only to derive temp keys. objects: Source (store) or destination (load) MemoryObjs. All entries must share a single ``(shape, dtype)`` — the caller (store/prefetch controller) is responsible for shape-grouping before submission. \"\"\" shape_0 = objects[0].get_shapes() dtype_0 = objects[0].get_dtypes() temp_keys = [make_temp_key(k) for k in keys] layout = serialized_layout_desc( MemoryLayoutDesc(shapes=shape_0, dtypes=dtype_0), self._serde ) results = self._l1_manager.reserve_write( keys=temp_keys, is_temporary=[True] * len(temp_keys), layout_desc=layout, tag=_L1_WRITE_TAG, ) # First pass: collect every key whose reserve_write succeeded. # We must scan the full list (not bail on the first failure) # so a mixed-success result still releases all reserved keys. successful_temp_keys: list[ObjectKey] = [] for temp_key in temp_keys: r = results.get(temp_key) if r is not None and r[0] == L1Error.SUCCESS: successful_temp_keys.append(temp_key) if len(successful_temp_keys) != len(temp_keys): self._release_write_temps(successful_temp_keys) return temp_keys, None temp_objs: list[MemoryObj] = [] for temp_key in temp_keys: temp_obj = results[temp_key][1] # Every key succeeded above, and a successful reservation carries # its object. assert temp_obj is not None temp_objs.append(temp_obj) return temp_keys, temp_objs"""

def _normalize(src: str) -> str:
    """Collapse every whitespace run to one space (drift-proof normalization)."""
    return re.sub(r"\s+", " ", src).strip()

def _wrapper_module():
    """Yield the module defining _alloc_temp_buffers (the serde wrapper)."""
    import importlib
    for name in (
        "lmcache.v1.distributed.l2_adapters.serde_wrapper",
    ):
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - slim install / missing deps
            continue
        if hasattr(mod, "SerdeL2AdapterWrapper"):
            yield mod

def reference_matches() -> bool:
    """True when the installed _alloc_temp_buffers source matches the
    reference (whitespace-normalized)."""
    for mod in _wrapper_module():
        fn = getattr(mod.SerdeL2AdapterWrapper, "_alloc_temp_buffers", None)
        if fn is None:
            continue
        try:
            src = inspect.getsource(fn)
        except Exception:  # noqa: BLE001
            continue
        return _normalize(src) == _normalize(REFERENCE_SOURCE)
    return False

def _largest_object(objects):
    """The object with the maximum plaintext_bytes(layout) of the batch."""
    from .compress_serde import plaintext_bytes, _LayoutView

    def _plain(o):
        return plaintext_bytes(_LayoutView(o.get_shapes(), o.get_dtypes()))
    return max(objects, key=_plain)

def _patched_alloc(self, keys, objects):
    """Drift-protected wrapper: allocate every temp buffer for the LARGEST
    object of the batch (the original sizes them from objects[0] only)."""
    largest = _largest_object(objects)
    return _original_alloc(self, keys, [largest])

_original_alloc = None

def install() -> str:
    """Install the wrapper. Returns 'patched' or 'missing' (drift / no module).
    The reference check runs FIRST: on drift nothing is touched. A second
    call in the same process is idempotent -- the wrapper is already in
    place and must NOT be reported as 'missing' (r29 N6b)."""
    global _original_alloc
    for mod in _wrapper_module():
        cls = mod.SerdeL2AdapterWrapper
        if getattr(cls, "_alloc_temp_buffers", None) is _patched_alloc:
            return "patched"   # already installed in this process
    if not reference_matches():
        return "missing"
    for mod in _wrapper_module():
        cls = mod.SerdeL2AdapterWrapper
        _original_alloc = cls._alloc_temp_buffers
        cls._alloc_temp_buffers = _patched_alloc
        if getattr(cls, "_alloc_temp_buffers", None) is _patched_alloc:
            return "patched"
    return "missing"

def kvp_serde_configured(argv: list[str] | None = None) -> bool:
    """True when the --l2-adapter JSON configures a `kvp-*` serde (the patch
    only matters when OUR serializer is active)."""
    import json as _json
    import re as _re
    args = list(argv if argv is not None else sys.argv[1:])
    for i, a in enumerate(args):
        raw = None
        if a == "--l2-adapter" and i + 1 < len(args):
            raw = args[i + 1]
        elif a.startswith("--l2-adapter="):
            raw = a.split("=", 1)[1]
        if not raw:
            continue
        try:
            spec = _json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(spec, dict):
            continue
        serde = spec.get("serde")
        if isinstance(serde, dict) and _re.match(
            r"^kvp-([a-z0-9]+)$", str(serde.get("type", ""))
        ):
            return True
    return False

def selfcheck() -> int:
    """`--selfcheck` entry: OK + exit 0 only when the reference matched and
    the patch is in place. The line is SIGNED (r29 N6c) -- a bare OK in the
    middle of a log is impossible to attribute."""
    if install() == "patched":
        print("serde_alloc_patch selfcheck: OK")
        return 0
    print("serde_alloc_patch selfcheck: NOT INSTALLED (LMCache source differs or module unavailable)")
    return 1

def main() -> None:
    if "--selfcheck" in sys.argv:
        raise SystemExit(selfcheck())
    status = install()
    if status == "patched":
        print(
            "[kvp] serde temp-buffer patch installed "
            "(buffers sized from the largest object of the batch)",
            file=sys.stderr,
        )
    else:
        print(
            "[kvp] WARNING: serde temp-buffer patch NOT installed: "
            "LMCache source differs -- store batches may fail on mixed-size "
            "objects (kvp_scan_l2_failures will flag it loudly)",
            file=sys.stderr,
        )

if __name__ == "__main__":
    main()