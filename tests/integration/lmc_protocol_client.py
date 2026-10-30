"""Minimal LMCache MP-protocol client for integration tests (pinned 0.5.3).

This is the REAL engine-driven CPU data path of LMCache 0.5.3 — the same
protocol the `lmcache bench server` CLI and the vLLM MP connector use:
REGISTER_KV_CACHE_ENGINE_DRIVEN_CONTEXT, LOOKUP, QUERY_PREFETCH_STATUS,
PREPARE_STORE/COMMIT_STORE, PREPARE_RETRIEVE/COMMIT_RETRIEVE over ZMQ.

Why a dedicated client instead of `lmcache bench server`: the bench CLI
hardcodes its cache identity to `_MODEL_NAME = "test-model"` (helpers.py) and
exposes no flag to override it. Per-model/per-config isolation can only be
proven at the LMCache API level by addressing the SAME tokens under DIFFERENT
`model_name` values (the field that ObjectKey and the L2 filename are derived
from), so the test needs to set that field directly.

Verified against the 0.5.3 release source (lmcache/v1/multiprocess/...,
lmcache/v1/distributed/api.py) and a live run: nothing here is invented —
every request type, payload list and response shape is the real wire format.
"""
import dataclasses
import os
import time

try:  # pragma: no cover - import guard for machines without lmcache
    import torch
    import zmq
    from lmcache.cli.commands.bench.server_bench import helpers as _H
    from lmcache.v1.kv_layer_groups import parse_kvcache_shape_spec
    from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
    from lmcache.v1.multiprocess.group_view import EngineGroupInfo
    from lmcache.v1.multiprocess.mq import MessageQueueClient
    from lmcache.v1.multiprocess.posix_shm import shm_open_pool_as_mmap
    from lmcache.v1.platform.cpu.shm import shm_unlink
    LMC_IMPORT_ERROR = None
except ImportError as _exc:  # pragma: no cover
    LMC_IMPORT_ERROR = _exc

#: Same synthetic geometry as the round-trip test: 4 layers, K/V split,
#: 64 blocks x 16 tokens x 8 heads x 128 head_dim, float16.
SHAPE_SPEC = "(2,64,16,8,128):float16:4"
BLOCK_SIZE = 16
NUM_TOKENS = 63            # + 1 leading id = 64 tokens = 4 chunks of 16
CHUNK_SIZE = 16

# Deterministic payload: the pinned helpers fill the KV tensors from a fixed
# torch seed (42), so the same tokens always carry the same bytes.
_HELLO_TOKEN_ID = 9906


def build_token_ids(seq_no: int = 7, num_tokens: int = NUM_TOKENS) -> tuple:
    """Deterministic token sequence: (seq_no, hello, hello, ...)."""
    return (seq_no,) + (_HELLO_TOKEN_ID,) * num_tokens


def make_key(token_ids, request_id, model_name, start=0, end=0,
             worker_id=None, world_size=1, cache_salt=""):
    """Build a real IPCCacheServerKey (the cache identity of the MP protocol).

    NOTE: `request_id` is excluded from key equality in LMCache 0.5.3
    (`field(compare=False)`), so the same tokens address the same objects
    across invocations. `model_name` IS part of both ObjectKey and the L2
    filename; `cache_salt` is part of ObjectKey only.
    """
    return IPCCacheServerKey(
        model_name=model_name,
        world_size=world_size,
        worker_id=worker_id,
        token_ids=tuple(token_ids),
        start=start,
        end=end if end > 0 else len(token_ids),
        request_id=request_id,
        cache_salt=cache_salt,
    )


class LmcClient:
    """One registered engine-driven KV context against an `lmcache server`."""

    def __init__(self, rpc_url: str, model_name: str, instance_id: int,
                 log=None):
        if LMC_IMPORT_ERROR is not None:  # pragma: no cover
            raise RuntimeError(
                "lmcache runtime not importable: %s" % LMC_IMPORT_ERROR)
        self.model_name = model_name
        self.instance_id = instance_id
        self._log = log
        self._ctx = zmq.Context()
        self.client = MessageQueueClient(rpc_url, self._ctx)
        self._groups = parse_kvcache_shape_spec(SHAPE_SPEC)
        self.block_size = self._groups[0].shape_desc.bs
        self.num_layers = sum(g.num_layers for g in self._groups)
        self._egis = [
            EngineGroupInfo(engine_group_id=i,
                            layer_indices=tuple(g.layer_indices),
                            tokens_per_block=self.block_size)
            for i, g in enumerate(self._groups)
        ]
        # Unique SHM prefix per client: the pinned allocator derives segment
        # names from the prefix and fails with FileExistsError on collisions.
        shm_prefix = "/lmcache_kv_kvp_%d_%d" % (os.getpid(), instance_id)
        (self.tensors, self.wrappers,
         self.shm_names) = _H._allocate_cpu_shm_kv_cache(
            groups=self._groups, shm_prefix=shm_prefix)
        hints = {"kv_layout": "NHD", "num_layers": self.num_layers,
                 "num_heads": 8, "head_size": 128,
                 "num_blocks": 64, "block_size": self.block_size,
                 "dtype": "float16", "kv_size": 2}
        self.reg = _H._send_register_kv_cache(
            self.client, instance_id=instance_id, model_name=model_name,
            world_size=1, layout_hints=hints, kv_caches=self.wrappers,
            use_gpu=False, use_handle=False, engine_group_infos=self._egis)
        self.pool = None
        if self.reg and getattr(self.reg, "shm_name", "") and \
                getattr(self.reg, "pool_size", 0):
            self.pool = shm_open_pool_as_mmap(
                self.reg.shm_name, self.reg.pool_size)

    @property
    def registered(self) -> bool:
        return bool(self.reg) and self.reg is not _H._TIMEOUT

    def store(self, token_ids, request_id, cache_salt="", block_offset=0):
        """Store the deterministic KV payload for `token_ids`. Returns status."""
        num_full = (len(token_ids) // CHUNK_SIZE) * CHUNK_SIZE
        key = make_key(token_ids, request_id, self.model_name,
                       start=0, end=num_full, worker_id=0,
                       cache_salt=cache_salt)
        status = _H._send_store(
            self.client, key, block_offset=block_offset,
            block_size=self.block_size,
            num_engine_group_infos=len(self._egis), use_gpu=False,
            use_handle=False, client_tensors=self.tensors,
            chunk_size=CHUNK_SIZE, server_pool=self.pool,
            instance_id=self.instance_id)
        _H._send_end_session(self.client, request_id)
        return status

    def lookup_hits(self, token_ids, request_id, cache_salt=""):
        """LOOKUP + poll. Returns the number of chunks found (0 == miss)."""
        num_full = (len(token_ids) // CHUNK_SIZE) * CHUNK_SIZE
        key = make_key(token_ids, request_id, self.model_name,
                       start=0, end=num_full, worker_id=None,
                       cache_salt=cache_salt)
        accepted = _H._send_lookup(self.client, key, tp_size=1)
        if not accepted:
            return None
        hits = _H._poll_prefetch_status(self.client, request_id)
        _H._send_end_session(self.client, request_id)
        return 0 if hits is None else int(hits)

    def close(self):
        try:
            self.client.close()
        finally:
            self._ctx.term()
        for name in self.shm_names:
            try:
                shm_unlink(name)
            except OSError:
                pass