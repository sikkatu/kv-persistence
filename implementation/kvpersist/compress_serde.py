"""Lossless compression serde for the LMCache L2 tier (zstd / lz4).

WHY THIS MODULE EXISTS
Upstream LMCache 0.5.3 registers exactly three L2 serdes -- fp8 and
turboquant (both LOSSY quantization) and aesgcm (encryption). There is
NO lossless compression serde upstream, and `lmcache server` has no
--l2-serde flag at all: serde is configured per L2 adapter through the
"serde" sub-dict of the --l2-adapter JSON
(lmcache/v1/distributed/l2_adapters/config.py::_parse_serde_config).
This module fills the gap WITHOUT forking LMCache: it registers three extra
serde types, kvp-lz4, kvp-zstd and kvp-raw, in LMCache's own serde registry and
launches the unchanged `lmcache` CLI afterwards -- the same launcher pattern
as kvpersist.salt_filename_patch.

LOSSLESS CONTRACT
The transform is byte-exact: serialize -> deserialize returns the identical
KV bytes. When the codec cannot shrink a block (incompressible data) the
frame stores the payload RAW, so the on-disk result is never larger than the
raw KV plus the frame header, rounded up to the L1 page size -- i.e. a chunk
is at most ONE page larger than the raw payload, never unboundedly larger.
No quantization, no precision loss: unlike fp8/turboquant the decoded KV is
bit-identical to the engine's cache.

WIRE FRAME (per chunk), all integers little-endian:

    offset  size  field
    0       4     magic "KVCZ"
    4       2     format version (2)
    6       2     codec id (1 = raw passthrough, 2 = lz4, 3 = zstd)
    8       8     uncompressed length in bytes
    16      8     stored payload length in bytes
    24      4     CRC32 (zlib) of the uncompressed payload
    28      N     payload (compressed, or raw when the codec did not shrink)
    ...     pad   ZERO padding up to the L1 page size (align_bytes)

The CRC32 covers the UNCOMPRESSED bytes, so a truncated DMA read, a bit
flip, or a frame written by a different codec is detected at deserialize
time and reported as a cache MISS -- never as silently corrupt KV.

WHY THE FRAME IS PADDED TO THE PAGE SIZE
The pinned nixl_store_dynamic L2 adapter DMA-transfers the object in whole
L1 pages (get_memory_indices requires mem_size % l1_align_bytes == 0) and
writes obj.meta.phy_size bytes -- i.e. the whole allocated buffer, not the
serializer's return value. Without care a compressed chunk would still be
written to NVMe at its FULL uncompressed allocation size, which would make
the whole feature pointless. Two surgical patches close that gap (see
_install_size_patch):

1. TensorMemoryObj.meta.phy_size is narrowed to the padded frame size right
   before the inner adapter runs, so the file on disk is exactly the padded
   frame. Only objects narrowed by set_used_size (the serde temp buffers) are
   touched -- ordinary KV objects keep their allocator metadata untouched, so
   L1 free() accounting is never corrupted.
2. On load the adapter is told the stored file size, so the DMA read covers
   exactly the bytes on disk and never runs past EOF.

THE PATCHES ARE INSTALLED ONLY WHEN A kvp-* SERDE IS CONFIGURED
If the adapter JSON has no "serde" block (or a non-kvp one) the adapter class
is left COMPLETELY untouched: the load/store path is byte-for-byte upstream
behaviour. This matters because a short DMA read followed by an unvalidated
copy would otherwise leave a garbage tail in the destination KV object; by
installing only when this repo actually owns the frame format we never
introduce that window on the uncompressed path. (This directly answers the
review point that load_wrapper used to run even for serde=none.)

FAIL-FAST CODEC AVAILABILITY
`register()` verifies the codec bindings UP FRONT and raises CodecError if a
requested codec cannot be imported. The server therefore refuses to start
instead of reaching READY and failing later inside the serde thread pool.
In docker mode the same check runs inside the container via
scripts/start-cache.sh's probe path.

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
import zlib

MAGIC = b"KVCZ"
FORMAT_VERSION = 2
HEADER_LEN = 28
_HEADER = struct.Struct("<4sHHqqI")

CODEC_RAW = 1
CODEC_LZ4 = 2
CODEC_ZSTD = 3

_CODEC_NAME = {CODEC_RAW: "raw", CODEC_LZ4: "lz4", CODEC_ZSTD: "zstd"}
_CODEC_ID = {"raw": CODEC_RAW, "lz4": CODEC_LZ4, "zstd": CODEC_ZSTD}


class CodecError(RuntimeError):
    """Compression backend missing or produced an unusable frame."""


def _element_size(dtype) -> int:
    """Bytes per element of a torch dtype, without requiring torch at import."""
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


# ---------------------------------------------------------------------------
# Codecs -- each import is a HARD requirement when the codec is selected.
# ---------------------------------------------------------------------------


def _compress_lz4(data: bytes) -> bytes:
    import lz4.frame  # type: ignore

    return lz4.frame.compress(data)


def _decompress_lz4(data: bytes) -> bytes:
    import lz4.frame  # type: ignore

    return lz4.frame.decompress(data)


def _compress_zstd(data: bytes, level: int) -> bytes:
    import zstandard  # type: ignore

    return zstandard.ZstdCompressor(level=level).compress(data)


def _decompress_zstd(data: bytes) -> bytes:
    import zstandard  # type: ignore

    return zstandard.ZstdDecompressor().decompress(data)


# ---------------------------------------------------------------------------
# Serializer / Deserializer
# ---------------------------------------------------------------------------


class LosslessCompressSerializer:
    """Compress KV bytes losslessly into a page-aligned, zero-padded frame."""

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
        """Compress src's KV bytes into dst as [header][payload][zero pad].

        The padding is EXPLICITLY zeroed: the destination is an L1 slab block
        that may have been recycled from another tenant's buffer, and the DMA
        write covers the whole padded frame. Leaving stale bytes there would
        put another tenant's data into OUR cache file on disk.
        """
        payload_in = bytes(src.byte_array)
        codec_id, payload = self._compress(payload_in)
        crc = zlib.crc32(payload_in) & 0xFFFFFFFF
        frame = _HEADER.pack(
            MAGIC, FORMAT_VERSION, codec_id, len(payload_in), len(payload), crc
        ) + payload
        padded = align_up(len(frame), self._align)
        out = memoryview(dst.byte_array).cast("B")
        out[: len(frame)] = frame
        if padded > len(frame):
            out[len(frame):padded] = b"\x00" * (padded - len(frame))
        return padded

    def estimate_serialized_size(self, layout_desc) -> int:
        """Upper bound: raw payload + header, padded to the page size.

        This is the TEMP-BUFFER allocation size (LMCache sizes the L1 temp
        block from it), not the on-disk size: after the size patch the file
        holds only the padded frame, which for compressible data is smaller.
        For the L2 budget the worst case (no compression) is raw + header
        padded to one page -- i.e. at most ONE page over the raw payload.
        """
        raw = plaintext_bytes(layout_desc)
        return align_up(raw + HEADER_LEN, self._align)


class LosslessCompressDeserializer:
    """Reverse of LosslessCompressSerializer: frame -> exact KV bytes.

    Every field is validated (magic, version, lengths, CRC32) BEFORE any byte
    is written to the destination: a truncated DMA read, a frame from another
    codec, or a bit flip raises ValueError, which LMCache reports as a load
    failure (cache MISS) -- never as silently corrupt KV.
    """

    def __init__(self, codec: str = "any"):
        self._codec = codec  # informational; the frame header is authoritative.

    def deserialize(self, src, dst, key) -> None:
        blob = bytes(src.byte_array)
        if len(blob) < HEADER_LEN:
            raise ValueError("kvp compress serde: frame shorter than header")
        magic, version, codec_id, plain_len, payload_len, crc = _HEADER.unpack(
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
        if zlib.crc32(data) & 0xFFFFFFFF != crc:
            raise ValueError(
                "kvp compress serde: CRC32 mismatch -- frame is corrupt "
                "(reported as a cache miss, never returned as KV)"
            )
        out = memoryview(dst.byte_array).cast("B")
        if len(out) < plain_len:
            raise ValueError("kvp compress serde: destination buffer too small")
        out[:plain_len] = data


# ---------------------------------------------------------------------------
# LMCache serde factory registration (fail-fast on missing codecs)
# ---------------------------------------------------------------------------


def _make_factory(codec: str):
    def factory(kwargs: dict):
        from lmcache.v1.distributed.serde.async_processor import AsyncSerdeProcessor

        # Fail-fast at serde creation (adapter config parse): if the codec's
        # bindings are missing the server dies here instead of reaching READY
        # and failing later inside the serde thread pool.
        check_codec_available(codec)
        # Deterministic patch trigger: the size patch must be installed BEFORE
        # the first store/load task, which is exactly now (the serde is built
        # while the L2 adapter is being constructed, long before any I/O).
        _install_size_patch_now()
        level = int(kwargs.get("level", 3))  # type: ignore[call-overload]
        align_bytes = int(kwargs.get("align_bytes", 4096))  # type: ignore[call-overload]
        max_workers = int(kwargs.get("max_workers", 1))  # type: ignore[call-overload]
        return AsyncSerdeProcessor(
            LosslessCompressSerializer(codec, level, align_bytes),
            LosslessCompressDeserializer(codec),
            max_workers=max_workers,
        )

    return factory


def check_codec_available(codec: str) -> None:
    """Raise CodecError immediately when the codec's bindings are missing.

    Called at registration time (fail-fast): the server must refuse to start
    rather than reach READY and fail later inside the serde thread pool.
    """
    if codec == "lz4":
        try:
            import lz4.frame  # noqa: F401
        except ImportError as e:
            raise CodecError(
                "kvp-lz4 serde requested but the 'lz4' python package is not "
                "installed. Install it (pip install lz4) or use "
                "LMCACHE_L2_SERDE=none / zstd."
            ) from e
    elif codec == "zstd":
        try:
            import zstandard  # noqa: F401
        except ImportError as e:
            raise CodecError(
                "kvp-zstd serde requested but the 'zstandard' python package "
                "is not installed. Install it (pip install zstandard) or use "
                "LMCACHE_L2_SERDE=none / lz4."
            ) from e
    elif codec != "raw":
        raise CodecError(f"unknown codec {codec!r}")


def register() -> bool:
    """Register kvp-lz4 / kvp-zstd / kvp-raw in LMCache's serde registry.

    Returns True when registration succeeded. Raises CodecError (fail-fast)
    when a requested codec's bindings are unavailable.
    """
    try:
        from lmcache.v1.distributed.serde.factory import register_serde_factory
    except Exception:  # noqa: BLE001 - LMCache not installed
        return False
    for name, codec in (("kvp-lz4", "lz4"), ("kvp-zstd", "zstd"), ("kvp-raw", "raw")):
        check_codec_available(codec)  # fail-fast: raises CodecError
    for name, codec in (("kvp-lz4", "lz4"), ("kvp-zstd", "zstd"), ("kvp-raw", "raw")):
        try:
            register_serde_factory(name, _make_factory(codec))
        except ValueError:
            pass  # already registered (e.g. apply() called twice)
    return True


# ---------------------------------------------------------------------------
# Surgical LMCache patches: make the L2 file size follow the serialized frame
# ---------------------------------------------------------------------------


def _install_size_patch_now() -> bool:
    """Install the store/load wrappers on DynamicNixlStoreL2Adapter (idempotent).

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

    WHEN THIS RUNS: only from the kvp-* serde factory, i.e. only when the
    deployment actually configured a lossless codec. With `LMCACHE_L2_SERDE=none`
    the adapter class is left COMPLETELY untouched (byte-for-byte upstream
    load/store), so a short DMA read can never be masked by our wrapper -- that
    window is closed on the raw path by NOT having a wrapper at all.
    """
    try:
        from lmcache.v1.distributed.l2_adapters import (
            nixl_store_dynamic_l2_adapter as mod,
        )
        cls = mod.DynamicNixlStoreL2Adapter
    except Exception:  # noqa: BLE001 - slim install / missing deps
        return False

    if getattr(cls, "_kvp_size_patch_installed", False):
        return True

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
    cls._kvp_size_patch_installed = True
    return True


# Backwards-compatible alias (tests and launch.py may call the old name).
def _install_size_patch() -> bool:
    """Register-only hook: the real patch installs when a kvp-* serde is built."""
    try:
        from lmcache.v1.distributed.l2_adapters import (  # noqa: F401
            nixl_store_dynamic_l2_adapter,
        )
    except Exception:  # noqa: BLE001
        return False
    return True


# ---------------------------------------------------------------------------
# Entry point: apply everything, then delegate to the lmcache CLI
# ---------------------------------------------------------------------------


def apply() -> bool:
    """Register the lossless serdes (fail-fast on missing codecs).

    Returns True when registration succeeded. Raises CodecError when a
    selected codec is unavailable -- the caller should abort the launch.
    The size patch installs itself when the first kvp-* serde is built.
    """
    return register()


def main() -> None:
    """Apply patches, then delegate to the real LMCache CLI entry point."""
    try:
        reg_ok = register()
    except CodecError as e:
        print(f"[kvp] FATAL: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    if reg_ok:
        print(
            "[kvp] lossless compress serde registered "
            "(kvp-lz4 / kvp-zstd / kvp-raw); the L2 file-size patch installs "
            "when a kvp-* serde is configured",
            file=sys.stderr,
        )
    else:
        print(
            "[kvp] WARNING: lossless serde NOT registered (LMCache serde "
            "factory unavailable) -- a 'kvp-lz4'/'kvp-zstd' adapter config "
            "will fail to load",
            file=sys.stderr,
        )
    from lmcache.cli.main import main as lmc_main

    lmc_main()


if __name__ == "__main__":
    main()