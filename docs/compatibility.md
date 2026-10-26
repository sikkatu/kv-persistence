> 🌐 Единые языковые версии: **English** | [Русский](compatibility.ru.md) | [中文](compatibility.zh.md)

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
| LMCache | **0.5.3**, pinned in `requirements-lmcache.txt` | NEEDS TESTING on host |
| MP-protocol fit (lmcache ↔ vLLM) | ZMQ request channel | NEEDS TESTING on host |
| L2 `nixl_store_dynamic` persistence | persist_enabled + lazy index rebuild | NEEDS TESTING on host |
| Restore after vLLM restart | acceptance requirement | NEEDS TESTING on host |
| Restore after both restarts | acceptance requirement | NEEDS TESTING on host |
| Model | MiMo-V2.6-Pro-MOPD + custom patches | VERIFIED (production) |
| KV dtype | brief: fp16; production runs bf16 | CONFIG CHOICE; re-measure after change |
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