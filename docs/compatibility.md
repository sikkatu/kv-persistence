> 🌐 Language versions: **English** | [Русский](compatibility.ru.md) | [中文](compatibility.zh.md)

# Compatibility matrix

Status labels: **VERIFIED** (checked on the actual pinned runtime), **NEEDS
TESTING** (documented but not yet executed on the target host), **BLOCKED**.
Nothing here is claimed working without its marker.

## Runtime tuple

| Item | Value | Status |
|---|---|---|
| vLLM build | `vllm/vllm-openai:nightly-8e2beaa3bff…` pinned `@sha256:167ccd86…` | VERIFIED |
| vLLM commit | `8e2beaa3bff595577a069d49d96bde9074294ec2` (2026-10-21) | VERIFIED |
| Python | 3.12 | VERIFIED |
| `LMCacheMPConnector` in image | `kv_connector_module_path` supported | VERIFIED (source) |
| LMCache | **0.5.6** (matches the pinned vLLM image; pinned in `requirements-lmcache.txt`) | VERIFIED (hardware 2026-11-07) |
| MP-protocol fit (lmcache ↔ vLLM) | ZMQ request channel | VERIFIED (hardware 2026-11-07: register_kv_caches + API served) |
| L2 `cache_salt` isolation | **native in 0.5.6+** (`@`-suffix in the file name); our patch is a compat shim for pre-0.5.6 files | VERIFIED (source) |
| L2 `nixl_store_dynamic` persistence | persist_enabled + lazy index rebuild | VERIFIED (hardware 2026-11-07: 90 MiB stored, read back after restart) |
| Restore after vLLM restart | acceptance requirement | VERIFIED (hardware 2026-11-07, scenario A: cached_tokens=2560, ttft 0s) |
| Restore after both restarts | acceptance requirement | VERIFIED (hardware 2026-11-07, scenario B: cached_tokens=2560, L2 read events=1) |
| Model | MiMo-V2.6-Pro-MOPD + custom patches | VERIFIED (production) |
| KV dtype | bfloat16 (effective project dtype; part of cache identity) | CONFIG CHOICE; re-measure after change |
| NVMe backend | POSIX (GDS deliberately deferred) | NEEDS TESTING |

## Version pinning policy

* **vLLM** — pinned by image digest (`@sha256:`), enforced by the launchers.
* **LMCache** — pinned in `requirements-lmcache.txt`; the table above names the
  ONLY tested tuple. A bump requires replaying `scripts/test-recovery.sh`
  (disk-backed recovery is the pass criterion, not "it imports").
* On ABI mismatch run `lmcache server` from the production image instead
  (docs/troubleshooting.md).

## Why LMCache

| Candidate | Verdict |
|---|---|
| `nixl_store_dynamic` (LMCache L2) | chosen: persist/recover + bounded capacity + L2 eviction |
| `nixl_store` | rejected: pre-allocated registry, no restart recovery documented |
| In-process `LMCacheConnectorV1` | rejected: cache dies with the vLLM process |
| vLLM native prefix cache only | insufficient: lost on restart |
| Custom connector adapter | not needed — pinned vLLM ships the MP connector |