# kv-persistence — Universal Persistent KV Cache for vLLM

Persistent, disk-backed KV-cache layer for vLLM. After a vLLM restart, reusable prompt KV
blocks are recovered from NVMe instead of being recomputed, so long Operit conversations
(100k–1M tokens) reopen in seconds instead of tens of minutes.

Primary target: **MiMo-V2.6-Pro on 4 × NVIDIA RTX PRO 6000** (the `sikkatu/recipes`
`recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit` deployment). The generic cache layer is
model-agnostic; model-specific settings live under `configs/<model>/` only.

> **Status:** implementation and offline tests are complete. Hardware validation phases
> (restart-recovery measurements on the 4-GPU host) are scripted in `scripts/` and
> tracked in [docs/benchmarks.md](docs/benchmarks.md). See
> [docs/final-report.md](docs/final-report.md) for the honest per-requirement status.

## What it does

```
Operit → vLLM (TP4) → KV connector (LMCacheMPConnector)
       → lmcache server (standalone process)
       → L2 persistent store (nixl_store_dynamic, POSIX) → NVMe
```

* The cache service is a **separate process** from vLLM; restarting vLLM does not erase
  persistent KV data (`persist_enabled=true`, files kept on disk on shutdown).
* On startup the L2 index is rebuilt lazily; lookups fall through to disk, so cached
  prefixes are found after both vLLM *and* the cache service restart.
* Storage is **bounded** (`max_capacity_gb`, default 750 GB) with soft-threshold
  (90 %) LRU eviction; the cache never grows without limit and never wipes itself on
  startup.
* Compatibility identity (model revision, tokenizer, KV dtype, block layout, TP size)
  is baked into the cache namespace: incompatible configurations can never reuse each
  other's KV data; any uncertain compatibility check yields a cache miss.

## Layout

| Path | Content |
|---|---|
| `docs/` | architecture, compatibility matrix, deployment, storage management, eviction & dedup, security, troubleshooting, benchmarks, final report |
| `configs/generic/` | model-agnostic LMCache + budget configuration |
| `configs/mimo-v2.6-pro/` | MiMo-V2.6-Pro identity pins and overrides |
| `scripts/` | preflight, cache start/stop, vLLM-with-persistence launcher, recovery test, benchmark, budget report, purge |
| `implementation/` | `kvpersist` Python package: identity, budget accounting, admin CLI |
| `tests/` | unit (no GPU), integration (CPU/LMCache), recovery (target hardware) |
| `benchmarks/` | benchmark harness and result templates |

## Quick start (host with the 4 GPUs)

Read `docs/deployment.md` first — it contains the full linear path. Short version:

```bash
git clone https://github.com/sikkatu/kv-persistence ~/kv-persistence
cd ~/kv-persistence
bash scripts/preflight.sh
bash scripts/start-cache.sh
bash scripts/run-vllm-with-persist.sh
```

Rollback to the untouched production configuration is one command and never touches
the cache data (see *Rollback* in `docs/deployment.md`).

## Design rules honored by this repository

* The production recipe (`sikkatu/recipes`) and its default branch are **read-only**;
  nothing here modifies the working vLLM image, model files, or patches.
* No unauthenticated management endpoint: the lmcache HTTP frontend binds loopback by
  default; cache data lives under `0700` directories.
* No credentials, tokens, prompt text, or cache data in the repository.
* Honest reporting: untested features are marked `NEEDS TESTING` or `BLOCKED`, never
  claimed as working.

## License

MIT — see [LICENSE](LICENSE).
