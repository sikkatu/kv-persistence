# Compatibility

Status labels: **VERIFIED** (checked on the actual pinned runtime), **NEEDS
TESTING** (documented/reasoned but not yet executed on the target host),
**BLOCKED** (cannot be used as-is). Nothing here is called working without its
marker. Hardware validation requires access to the 4× RTX PRO 6000 host;
phases not yet run there are marked below.

## Runtime tuple under test

| Item | Value | Status |
|---|---|---|
| vLLM build | `vllm/vllm-openai:nightly-8e2beaa3bff…` pinned `@sha256:167ccd86…` | VERIFIED (analysis) |
| vLLM commit | `8e2beaa3bff595577a069d49d96bde9074294ec2` (2026-10-21) | VERIFIED |
| Python | 3.12 (image) | VERIFIED |
| Connector API | `vllm/distributed/kv_transfer/kv_connector/v1/` includes `lmcache_mp_connector.py` + `lmcache_integration/` | VERIFIED (source inspection at pinned commit) |
| `kv_connector_module_path` support | present in `vllm/config/kv_transfer_config.py` at pinned commit → external LMCache connector is loadable | VERIFIED (source inspection) |
| LMCache | MP mode server + `LMCacheMPConnector` | NEEDS TESTING on target host |
| Model | MiMo-V2.6-Pro-MOPD, 69 layers × 384 experts, top-8, custom patches (`mimo_v2_hotsplit.py`, `mimo_v2_omni.py`) | VERIFIED (production recipe runs) |
| TP4 + Hotsplit + UVA offload | `MOE_BACKEND=humming`, `CPU_OFFLOAD_GB=128`, `GPU_UTIL=0.85` | VERIFIED (production) |
| KV cache dtype | brief requests `fp16`; production recipe effectively runs `auto` = **bf16** today | NEEDS TESTING — set `KVP_KV_CACHE_DTYPE` to the dtype ACTUALLY in effect (identity requires resolved dtype, not `auto`) |
| MAX_NUM_SEQS | brief: 2; production launcher default in file: 1 → `run-vllm-with-persist.sh` sets 2 explicitly | VERIFIED (launcher sets it; engine log to confirm) |
| MAX_MODEL_LEN | 1048576 (1M) | VERIFIED (production default) |
| NVMe L2 backend | `nixl_store_dynamic` + POSIX, `persist_enabled=true`, `max_capacity_gb` | NEEDS TESTING on target host (documented behavior verified in upstream docs) |
| L2 recovery after vLLM restart | primary phase 3 requirement | NEEDS TESTING on target host |
| L2 recovery after both restarts | documented lazy index rebuild from disk | NEEDS TESTING on target host |
| Eviction (LRU, watermark, ratio) | adapter-native | NEEDS TESTING with small limit |
| Dedup / shared blocks | LMCache chunk-key reuse (same content ⇒ same key ⇒ single stored object, counted) | NEEDS TESTING (docs/benchmarks.md, Test 7) |
| GDS / GDS_MT / HF3FS / OBJ backends | not used (POSIX chosen deliberately) | N/A — deliberately out of scope |

## Why LMCache and not alternatives

| Candidate | Verdict |
|---|---|
| `nixl_store_dynamic` (LMCache L2) | chosen: documented persist/recover, `max_capacity_gb`, per-adapter eviction, POSIX works on local NVMe without GDS |
| `nixl_store` | rejected: pre-allocated registry; restart recovery not the supported path |
| LMCache in-process (`LMCacheConnectorV1`) | rejected for production: cache dies with vLLM process; kept only as a CPU-only dev fallback |
| vLLM native prefix cache | stays enabled (in-memory tier), but is lost on restart → not sufficient alone |
| SGLang-style radix cache / other engines | out of scope: production stack is vLLM (user decision recorded) |
| Custom connector adapter | would be required only if LMCache were incompatible with the pinned build; the pinned build ships `lmcache_mp_connector.py`, so no custom adapter is needed |

## vLLM < / ≥ 0.20.0 note

Docs state that `LMCacheMPConnector` resolves to vLLM's bundled connector unless
`kv_connector_module_path` explicitly points at `lmcache.integration.vllm.lmcache_mp_connector`.
`run-vllm-with-persist.sh` ALWAYS sets `kv_connector_module_path` so the
LMCache-shipped connector (which tracks the server protocol) is used and the
vendored one cannot shadow it.

## Items intentionally not claimed

* Do NOT assume configured env vars are honored without engine-log proof.
* 1M context capacity ≠ every 1M-token request will run efficiently on this
  hardware; per-request KV footprint and prefill time must be measured.
* Compatibility with upstream vLLM does not imply compatibility with the custom
  production build (custom patches `mimo_v2_hotsplit.py` / `mimo_v2_omni.py`);
  final compatibility is judged from the target-host test runs in
  docs/benchmarks.md.