> Multilingual docs: [English](https://github.com/sikkatu/kv-persistence/wiki/Architecture) | [Русский](https://github.com/sikkatu/kv-persistence/wiki/Architecture.ru) | [中文](https://github.com/sikkatu/kv-persistence/wiki/Architecture.zh)

# Architecture

## Goal

After a vLLM restart, reusable prompt KV blocks are recovered from NVMe and used
instead of being recomputed. Long Operit histories (100k–1M tokens) must reopen
fast. Session state must be bounded to a configurable storage budget and keep the
most valuable prefixes.

## Logical topology

```
Operit (chat client)
   │  OpenAI-compatible /v1/chat/completions (long message history)
   ▼
vLLM  (TP4, MiMo-V2.6-Pro, production recipe unchanged)
   │  KV connector: LMCacheMPConnector  (--kv-transfer-config)
   ▼
LMCache server  (standalone process, MP mode; ZMQ request port 5555)
   │  L1: host RAM buffer (configurable, ~64 GB)
   ▼
L2 persistent tier:  nixl_store_dynamic / POSIX  →  NVMe
   eviction controller (LRU, soft-watermark 0.9, reclaim ratio 0.1)
```

### Data flow

1. vLLM computes KV chunks and STORES them through the connector into L1 (host RAM).
2. The StoreController asynchronously pushes each object to L2 (NVMe, one file per
   chunk key, atomic temp-file rename).
3. On lookup the server first checks L1; on miss it falls through to L2 on disk
   (deterministic file name → lazy index rebuild). On hit the data is prefetched
   into L1 and returned to vLLM.
4. On graceful shutdown `persist_enabled=true` keeps the L2 data files; the
   in-memory index is rebuilt lazily at next startup.

### Why `nixl_store_dynamic` and not `nixl_store`

`nixl_store_dynamic` opens and registers files per operation instead of
pre-allocating at init. This is exactly what is needed for persistence: cached
KV metadata (deterministic file names on disk) survives restarts of either
process; there is no fd-count limit at 750 GB scale. `nixl_store`'s pre-allocated
registry does not survive restarts. This behavior is documented upstream and
re-verified in docs/compatibility.md.

### Component independence

The cache service is a separate process. Restarting vLLM never erases persistent
KV data; restarting the cache service alone keeps the data files on disk
(`persist_enabled=true`). A storage mode that only holds data in one process's
memory does NOT satisfy the requirement and is not used.

## Model-agnostic boundary

| Layer | Location | Model-specific? |
|---|---|---|
| Cache identity / namespaces | `implementation/kvpersist/identity.py` | input data (identity fields) |
| Budget / quota policy | `implementation/kvpersist/budget.py` | no |
| Retention semantics | `implementation/kvpersist/retention.py` | no |
| LMCache server + L2 adapter | `configs/generic/*` | no |
| Model pins (identity values, geometry) | `configs/<model>/identity.env` | yes |
| vLLM launcher (model weights/patches) | `scripts/run-vllm-with-persist.sh` | yes |

Adding a new compatible model means adding `configs/<model>/identity.env` and a
launcher section; the cache policy code does not change. KV blocks between
different models are NEVER shared: identity namespaces keep them isolated.
"Model-agnostic" means the mechanism supports multiple deployments — not that
their KV data is interchangeable.

## Cache identity (compatibility namespace)

Namespace = `kvp-` + first 32 hex of SHA-256 over canonical JSON of:

model id, model revision (40-hex SHA), tokenizer name+revision, effective KV
cache dtype, block size, num KV heads, head dim, TP size, connector name+version,
max model len, chat template digest (sha256 of the applied template).

* Any empty/uncertain field ⇒ `IdentityError` ⇒ cache MISS (recompute), never a
  wrong hit (`tests/unit/test_identity.py`).
* A revision, template, dtype or block-layout change hashes to a different
  namespace ⇒ old entries are simply not addressed (isolation, not deletion).
  Old namespaces are NOT auto-deleted (see docs/storage-management.md).

## Failure model

| Failure | Behavior |
|---|---|
| cache miss / partial hit | recompute only the missing prefix |
| incompatible entry | unreachable by namespace; treated as miss |
| corrupt/incomplete file | lookup falls through (miss) → recompute; no wrong output |
| interrupted write | temp-file + atomic rename ⇒ readers see old or new file only |
| interrupted eviction | same atomicity; index/lazy lookup tolerates missing files |
| disk full | adapter rejects stores above `max_capacity_gb`; serving continues without cache writes |
| cache service down | vLLM falls back to no persistent cache; request correctness unchanged |
| crash of either process | restart → lazy index rebuild from disk |

A cache failure must never corrupt model output: the worst case is recomputation
(already true by construction — KV from a miss is computed fresh).

## Security boundary

* LMCache HTTP frontend (management/metrics) binds `127.0.0.1` only.
* Cache root is `0700`, owned by the service user.
* No credentials in the repo; no prompt text in logs or reports (keys and counts
  only). KV data itself is derived from prompts — treat the NVMe directory as
  sensitive (docs/security.md).
