"""Lossless compression serde for the LMCache L2 tier (zstd / lz4).

WHY THIS MODULE EXISTS
Upstream LMCache 0.5.3 registers exactly three L2 serdes -- fp8 and
turboquant (both LOSSY quantization) and aesgcm (encryption). There is
NO lossless compression serde upstream, and `lmcache server` has no
--l2-serde flag at all: serde is configured per L2 adapter through the
"serde" sub-dict of the --l2-adapter JSON
(lmcache/v1/distributed/l2_adapters/config.py::_parse_serde_config).
This module fills the gap WITHOUT forking LMCache: it registers two extra
serde types, kvp-lz4 and kvp-zstd, in LMCache's own serde registry and
launches the unchanged `lmcache` CLI afterwards -- the same launcher pattern
as kvpersist.salt_filename_patch.

LOSSLESS CONTRACT
The transform is byte-exact: serialize -> deserialize returns the identical
KV bytes. When the codec cannot shrink a block (incompressible data) the
frame stores the payload RAW, so the on-disk result is never larger than the
raw KV plus one small header (padded up to the L1 page size). No
quantization, no precision loss: unlike fp8/turboquant the decoded KV is
bit-identical to the engine's cache.

WIRE FRAME (per chunk), all integers little-endian:

    offset  size  field
    0       4     magic "KVCZ"
    4       2     format version (1)
    6       2     codec id (1 = raw passthrough, 2 = lz4, 3 = zstd)
    8       8     uncompressed length in bytes
    16      8     stored payload length in bytes
    24      N     payload (compressed, or raw when the codec did not shrink)
    ...     pad   zero padding up to the L1 page size (align_bytes)

WHY THE FRAME IS PADDED TO THE PAGE SIZE
The pinned nixl_store_dynamic L2 adapter DMA-transfers the object in whole
L1 pages (get_memory_indices requires mem_size % l1_align_bytes == 0) and
writes obj.meta.phy_size bytes -- i.e. the whole allocated buffer, not the
serializer's return value. Without care a compressed chunk would still be
written to NVMe at its FULL uncompressed allocation size, which would make
the whole feature pointless. Two surgical patches close that gap (see
apply()):

1. TensorMemoryObj.meta is narrowed to the padded frame size right before
   the inner adapter runs, so the file on disk is exactly the padded frame.
   Only objects narrowed by set_used_size (the serde temp buffers) are
   touched -- ordinary KV objects keep their allocator metadata untouched,
   so L1 free() accounting is never corrupted.
2. On load the adapter is told the stored file size, so the DMA read covers
   exactly the bytes on disk.

SAFETY
Everything is opt-in: nothing here runs unless the adapter JSON asks for
"serde": {"type": "kvp-lz4"|"kvp-zstd"}. apply() returns False (never
raises) when the LMCache modules it patches are unavailable, and the
launcher prints a loud warning in that case.

Usage (host, systemd and docker -- same as the salt patch launcher):

    PYTHONPATH=<repo>/implementation \
        python3 -m kvpersist.compress_serde server --l2-adapter '{...}'
"""

from __future__ import annotations

import struct
import sys

MAGIC = b"KVCZ"
FORMAT_VERSION = 1
HEADER_LEN = 24
_HEADER = struct.Struct("<4sHHqq")

CODEC_RAW = 1
CODEC_LZ4 = 2
CODEC_ZSTD = 3

_CODEC_NAME = {CODEC_RAW: "raw", CODEC_LZ4: "lz4", CODEC_ZSTD: "zstd"}
_CODEC_ID = {"raw": CODEC_RAW, "lz4": CODEC_LZ4, "zstd": CODEC_ZSTD}


def _element_size(dtype) -> int:
    """Bytes per element of a torch dtype, without requiring torch at import.

    torch dtypes expose ``itemsize`` on recent PyTorch builds; older builds and
    test fakes are handled via the empty-tensor probe (or a plain ``itemsize``
    attribute on the fake). This keeps the module importable -- and unit
    testable -- on hosts without torch installed.
    """
    size = getattr(dtype, "itemsize", None)
    if isinstance(size, int) and size > 0:
        return size
    import torch  # local import: only needed for the legacy probe

    return torch.empty((), dtype=dtype).element_size()


def plaintext_bytes(layout_desc) -> int:
    """Total byte size of the KV data described by layout_desc."""
    total = 0
    for shape, dtype in zip(layout_desc.shapes, layout_desc.dtypes, strict=True):
        n = 1
        for dim in shape:
            n *= int(dim)
        total += n * _element_size(dtype)
    return total


def align_up(n: int, align: int) -> int:
    if align <= 1:
        return n
    return (n + align - 1) // align * align


class CodecError(RuntimeError):
    """Compression backend missing or produced an unusable frame."""


def _compress_lz4(data: bytes) -> bytes:
    try:
        import lz4.frame  # type: ignore
    except ImportError as e:  # pragma: no cover - env dependent
        raise CodecError(
            "lz4 python bindings not installed (pip install lz4) "
            "-- cannot use serde type 'kvp-lz4'"
        ) from e
    return lz4.frame.compress(data)


def _decompress_lz4(data: bytes) -> bytes:
    try:
        import lz4.frame  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise CodecError("lz4 python bindings not installed") from e
    return lz4.frame.decompress(data)


def _compress_zstd(data: bytes, level: int) -> bytes:
    try:
        import zstandard  # type: ignore
    except ImportError as e:  # pragma: no cover - env dependent
        raise CodecError(
            "zstandard python bindings not installed (pip install zstandard) "
            "-- cannot use serde type 'kvp-zstd'"
        ) from e
    return zstandard.ZstdCompressor(level=level).compress(data)


def _decompress_zstd(data: bytes) -> bytes:
    try:
        import zstandard  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise CodecError("zstandard python bindings not installed") from e
    return zstandard.ZstdDecompressor().decompress(data)


# ---------------------------------------------------------------------------
# Serializer / Deserializer
# ---------------------------------------------------------------------------


class LosslessCompressSerializer:
    """Compress KV bytes losslessly into a page-aligned frame.

    Mirrors lmcache.v1.distributed.serde.base.Serializer (duck-typed so the
    module imports without LMCache installed).
    """

    def __init__(self, codec: str, level: int = 3, align_bytes: int = 4096):
        if codec not in _CODEC_ID:
            raise ValueError(f"unknown codec {codec!r} (want lz4/zstd/raw)")
        self._codec = _CODEC_ID[codec]
        self._level = int(level)
        self._align = int(align_bytes) if int(align_bytes) > 0 else 1

    def _compress(self, data: bytes) -> tuple[int, bytes]:
        """Return (codec_id, payload): compressed when it actually shrinks."""
        if self._codec == CODEC_RAW:
            return CODEC_RAW, data
        if self._codec == CODEC_LZ4:
            out = _compress_lz4(data)
        else:
            out = _compress_zstd(data, self._level)
        if len(out) >= len(data):
            return CODEC_RAW, data
        return self._codec, out

    def serialize(self, src, dst, key) -> int:
        """Compress src's KV bytes into dst as [header][payload][pad]."""
        payload_in = bytes(src.byte_array)
        codec_id, payload = self._compress(payload_in)
        frame = _HEADER.pack(
            MAGIC, FORMAT_VERSION, codec_id, len(payload_in), len(payload)
        ) + payload
        out = memoryview(dst.byte_array).cast("B")
        out[: len(frame)] = frame
        return align_up(len(frame), self._align)

    def estimate_serialized_size(self, layout_desc) -> int:
        """Upper bound: raw payload + header, padded to the page size.

        This is also the accounting upper bound: when the codec does shrink a
        block the file on disk is smaller, but the L2 budget must be able to
        hold the WORST case (a block that does not compress). The gap between
        this bound and the real file size is what the caller over-provisions
        with --l2-adapter max_capacity_gb.
        """
        raw = plaintext_bytes(layout_desc)
        return align_up(raw + HEADER_LEN, self._align)


class LosslessCompressDeserializer:
    """Reverse of LosslessCompressSerializer: frame -> exact KV bytes."""

    def __init__(self, codec: str = "any"):
        # codec is informational: the frame header is authoritative.
        self._codec = codec

    def deserialize(self, src, dst, key) -> None:
        blob = bytes(src.byte_array)
        if len(blob) < HEADER_LEN:
            raise ValueError("kvp compress serde: frame shorter than header")
        magic, version, codec_id, plain_len, payload_len = _HEADER.unpack(
            blob[:HEADER_LEN]
        )
        if magic != MAGIC:
            raise ValueError("kvp compress serde: bad magic (not a KVCZ frame)")
        if version != FORMAT_VERSION:
            raise ValueError(
                f"kvp compress serde: unsupported frame version {version}"
            )
        if codec_id not in _CODEC_NAME:
            raise ValueError(f"kvp compress serde: unknown codec id {codec_id}")
        if payload_len < 0 or plain_len < 0:
            raise ValueError("kvp compress serde: negative length in header")
        if len(blob) < HEADER_LEN + payload_len:
            raise ValueError("kvp compress serde: truncated payload")
        payload = blob[HEADER_LEN : HEADER_LEN + payload_len]
        if codec_id == CODEC_RAW:
            data = payload
        elif codec_id == CODEC_LZ4:
            data = _decompress_lz4(payload)
        else:
            data = _decompress_zstd(payload)
        if len(data) != plain_len:
            raise ValueError(
                "kvp compress serde: decompressed size mismatch "
                f"(got {len(data)}, header says {plain_len})"
            )
        out = memoryview(dst.byte_array).cast("B")
        if len(out) < plain_len:
            raise ValueError("kvp compress serde: destination buffer too small")
        out[:plain_len] = data


# ---------------------------------------------------------------------------
# LMCache serde factory registration
# ---------------------------------------------------------------------------


def _make_factory(codec: str):
    def factory(kwargs: dict) -> "object":
        # Lazy import of LMCache's async wrapper; only reached when the
        # server actually asks for one of our serde types.
        from lmcache.v1.distributed.serde.async_processor import AsyncSerdeProcessor

        level = int(kwargs.get("level", 3))  # type: ignore[call-overload]
        align_bytes = int(kwargs.get("align_bytes", 4096))  # type: ignore[call-overload]
        max_workers = int(kwargs.get("max_workers", 1))  # type: ignore[call-overload]
        return AsyncSerdeProcessor(
            LosslessCompressSerializer(codec, level, align_bytes),
            LosslessCompressDeserializer(codec),
            max_workers=max_workers,
        )

    return factory


def register() -> bool:
    """Register kvp-lz4 / kvp-zstd / kvp-raw in LMCache's serde registry."""
    try:
        from lmcache.v1.distributed.serde.factory import register_serde_factory
    except Exception:  # noqa: BLE001 - LMCache not installed
        return False
    ok = True
    for name, codec in (("kvp-lz4", "lz4"), ("kvp-zstd", "zstd"), ("kvp-raw", "raw")):
        try:
            register_serde_factory(name, _make_factory(codec))
        except ValueError:
            pass  # already registered (e.g. apply() called twice)
        except Exception:  # noqa: BLE001
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Surgical LMCache patches: make the L2 file size follow the serialized frame
# ---------------------------------------------------------------------------


def _install_size_patch() -> bool:
    """Make the nixl_store_dynamic DMA length follow the serialized frame.

    Upstream `_execute_store_in_the_loop` / `_execute_load_in_loop` derive the
    DMA length from `obj.meta.phy_size` -- the FULL aligned allocation, not the
    serializer's return value. Without care a compressed chunk is still written
    to NVMe at its uncompressed allocation size, and the load-side DMA read
    would run past the end of a smaller file.

    Two wrappers close the gap, both restoring the metadata afterwards so L1
    free() accounting (which uses `meta.phy_size`) is never corrupted:

    * store: objects narrowed by `set_used_size` (the serde temp buffers) get
      `meta.phy_size` set to the frame size rounded UP to the adapter's page
      size, so the file is exactly the padded frame. Objects that were never
      narrowed keep their original `phy_size` byte for byte -- the
      uncompressed path is identical to upstream behaviour.
    * load: `meta.phy_size` is set to the STORED size (the adapter index
      records the real file size in `_memory_objects[key].size`), so the DMA
      read covers exactly the bytes on disk and never runs past EOF.
    """
    try:
        from lmcache.v1.distributed.l2_adapters import (
            nixl_store_dynamic_l2_adapter as mod,
        )
        cls = mod.DynamicNixlStoreL2Adapter
    except Exception:  # noqa: BLE001 - slim install / missing deps
        return False
    # `_execute_store_in_the_loop` / `_execute_load_in_loop` are METHODS of
    # DynamicNixlStoreL2Adapter (called as `self._execute_..._in_the_loop(...)`
    # from submit_store_task / submit_load_task), so the patch must land on the
    # CLASS attribute -- setting the module attribute would be silently ignored.
    orig_store = cls._execute_store_in_the_loop
    orig_load = cls._execute_load_in_loop

    def _page(adapter) -> int:
        return getattr(getattr(adapter, "nixl_agent", None), "l1_align_bytes", 0) or 4096

    async def store_wrapper(self, keys, objects, task_id):
        page = _page(self)
        saved = []
        for obj in objects:
            override = getattr(obj, "_used_size_override", None)
            if override is not None:
                saved.append((obj, obj.meta.phy_size))
                obj.meta.phy_size = align_up(override, page)
            else:
                saved.append((obj, None))
        try:
            return await orig_store(self, keys, objects, task_id)
        finally:
            for obj, phy in saved:
                if phy is not None:
                    obj.meta.phy_size = phy

    async def load_wrapper(self, keys, objects, task_id):
        with self._lock:
            stored_sizes = [
                getattr(self._memory_objects.get(key), "size", 0) for key in keys
            ]
        saved = []
        for obj, stored in zip(objects, stored_sizes, strict=False):
            if 0 < stored < obj.meta.phy_size:
                saved.append((obj, obj.meta.phy_size))
                obj.meta.phy_size = stored
            else:
                saved.append((obj, None))
        try:
            return await orig_load(self, keys, objects, task_id)
        finally:
            for obj, phy in saved:
                if phy is not None:
                    obj.meta.phy_size = phy

    cls._execute_store_in_the_loop = store_wrapper
    cls._execute_load_in_loop = load_wrapper
    return True


# ---------------------------------------------------------------------------
# Entry point: apply everything, then delegate to the lmcache CLI
# ---------------------------------------------------------------------------


def apply() -> bool:
    """Register the lossless serdes and install the size patches.

    Returns True when at least the serde registration succeeded. Never raises.
    """
    reg_ok = register()
    patch_ok = _install_size_patch()
    return reg_ok or patch_ok


def main() -> None:
    """Apply patches, then delegate to the real LMCache CLI entry point."""
    reg_ok = register()
    patch_ok = _install_size_patch()
    if reg_ok:
        print(
            "[kvp] lossless compress serde registered "
            "(kvp-lz4 / kvp-zstd / kvp-raw)",
            file=sys.stderr,
        )
    else:
        print(
            "[kvp] WARNING: lossless serde NOT registered (LMCache serde "
            "factory unavailable) -- 'kvp-lz4'/'kvp-zstd' adapter configs "
            "will fail to load",
            file=sys.stderr,
        )
    if patch_ok:
        print(
            "[kvp] L2 file-size patch active (compressed frames are written "
            "padded to the page size, not the full allocation)",
            file=sys.stderr,
        )
    else:
        print(
            "[kvp] WARNING: L2 file-size patch NOT applied (adapter module "
            "unavailable) -- compression may not reduce on-disk size",
            file=sys.stderr,
        )
    from lmcache.cli.main import main as lmc_main

    lmc_main()


if __name__ == "__main__":
    main()