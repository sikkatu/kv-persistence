> **English** | [Русский](README.ru.md) | [中文](README.zh.md)

# kv-persistence — Persistent KV Cache for vLLM

<p align="center">
  <img src="docs/assets/og-template.jpg" alt="kv-persistence — persistent KV cache for vLLM on NVMe" width="820">
</p>

Persistent, disk-backed KV-cache layer for vLLM: after a restart, reusable prompt KV
blocks are recovered from NVMe instead of being recomputed. The design goal — long
chat-app conversations (100k-1M tokens) reopening in seconds instead of tens of
minutes — is a *target to be proven*; the benchmark suite
(`scripts/benchmark.sh`) measures TTFT/prefill at 32K/128K/256K and against
`max_model_len` acceptance criteria listed in [docs/benchmarks.md](docs/benchmarks.md)
once the hardware acceptance run completes on a target GPU host (see [Status](#status)).
Until that run lands, claims of "seconds" are UNKNOWN — no numbers are published
as verified yet.

**📖 Documentation (EN / RU / ZH) lives in the Wiki:**
[Home](https://github.com/sikkatu/kv-persistence/wiki) ·
[Quick Start](https://github.com/sikkatu/kv-persistence/wiki/Quick-Start) ·
[Architecture](https://github.com/sikkatu/kv-persistence/wiki/Architecture) ·
[Configuration](https://github.com/sikkatu/kv-persistence/wiki/Configuration) ·
[Persistence & Recovery](https://github.com/sikkatu/kv-persistence/wiki/Persistence-and-Recovery) ·
[Storage & Eviction](https://github.com/sikkatu/kv-persistence/wiki/Storage-and-Eviction) ·
[Recipes Integration](https://github.com/sikkatu/kv-persistence/wiki/Recipes-Integration) ·
[Security](https://github.com/sikkatu/kv-persistence/wiki/Security) ·
[Troubleshooting](https://github.com/sikkatu/kv-persistence/wiki/Troubleshooting) ·
[FAQ](https://github.com/sikkatu/kv-persistence/wiki/FAQ)

Works with **any vLLM-served model on any NVIDIA GPU host** (tested reference:
MiMo-V2.6-Pro on 4× NVIDIA RTX PRO 6000 via the `sikkatu/recipes` recipe).
The cache layer is model-agnostic; model-specific settings live under
`configs/<model>/` — copy that directory for a new model and fill in its pins.

## What it does

```
Chat client → vLLM (TP4) → KV connector (LMCacheMPConnector)
       → lmcache server (standalone process) → L2 (nixl_store_dynamic, POSIX) → NVMe
```

* **Persistence across restarts** — data survives a vLLM restart AND a full cache-service
  restart (`persist_enabled=true`; on-disk index is rebuilt lazily from file names).
* **Bounded storage** — hard cap (1.5 TB default, configurable to whatever the NVMe
  device holds), soft-threshold (90 %) automatic LRU eviction; the cache never wipes
  itself on startup.
* **Compatibility namespaces** — model revision, tokenizer, KV dtype, block layout and
  chat template are part of the cache key: incompatible deployments never read each
  other's KV data; anything uncertain is a cache miss, never a wrong hit.
* **Model-agnostic core** — policy code is separate from model integration; a new model
  adds configuration, not code. KV data of different models is never shared.

## Documentation map

| Topic | Wiki (EN/RU/ZH) | In-repo (EN) |
|---|---|---|
| Install → configure → launch → verify | [Quick Start](https://github.com/sikkatu/kv-persistence/wiki/Quick-Start) | [docs/deployment.md](docs/deployment.md) |
| Topology, data flow, failure model | [Architecture](https://github.com/sikkatu/kv-persistence/wiki/Architecture) | [docs/architecture.md](docs/architecture.md) |
| Every knob, limits, quotas | [Configuration](https://github.com/sikkatu/kv-persistence/wiki/Configuration) | [docs/storage-management.md](docs/storage-management.md) |
| Acceptance scenarios A/B | [Persistence & Recovery](https://github.com/sikkatu/kv-persistence/wiki/Persistence-and-Recovery) | [docs/benchmarks.md](docs/benchmarks.md) |
| `KV_PERSIST=1` in the recipe launchers | [Recipes Integration](https://github.com/sikkatu/kv-persistence/wiki/Recipes-Integration) | [docs/compatibility.md](docs/compatibility.md) |
| Security rules | [Security](https://github.com/sikkatu/kv-persistence/wiki/Security) | [docs/security.md](docs/security.md) |
| Honest status & gaps | — | [docs/final-report.md](docs/final-report.md) |

## Quick start

```bash
git clone https://github.com/sikkatu/kv-persistence ~/kv-persistence
cd ~/kv-persistence
bash scripts/setup-identity.sh "$HOME/models/<your-model-dir>"   # fill AND verify identity pins
bash scripts/preflight.sh               # verify host, storage and identity
bash scripts/start-cache.sh             # persistent cache service (NVMe)
bash scripts/run-vllm-with-persist.sh   # vLLM + KV connector (needs IMAGE, MODEL_DIR)
bash scripts/test-recovery.sh           # acceptance: restart → cache reuse proof (exit 0 = PASS)
```

Each command refuses loudly instead of producing a half-working setup: identity
values are written and verified by `setup-identity.sh`, `preflight.sh` checks the
physical NVMe device and `test-recovery.sh` ends with a machine verdict.

Permanent switch in the production recipe is `KV_PERSIST=1` in `tp4.env`
(see wiki *Recipes Integration*); rollback is removing that one line.

## Repository layout

| Path | Content |
|---|---|
| `docs/` | English technical documentation (wiki: all three languages) |
| `configs/generic/` | cache + lmcache-server settings, EN/RU/ZH comment translations |
| `configs/<model>/` | per-model identity pins and L2 adapter template (example: `mimo-v2.6-pro`), EN/RU/ZH |
| `scripts/` | preflight, cache start/stop, launcher, recovery test, benchmark, admin CLI |
| `implementation/` | `kvpersist` package: identity, budgets, retention ranking, admin CLI |
| `tests/` | unit (59, run anywhere), integration (needs `lmcache`), recovery (GPU host) |

## Status

Implementation complete; offline tests green. Hardware acceptance runs on a GPU
host are scripted (`scripts/test-recovery.sh`, `scripts/benchmark.sh`) and executed by
the operator. Unverified features are labelled `NEEDS TESTING` / `BLOCKED` in
[docs/final-report.md](docs/final-report.md) — nothing untested is claimed as working.

## Design rules

* The production recipe (`sikkatu/recipes`) is **read-only** for this project.
* Management endpoints (lmcache HTTP + ZMQ) are **loopback-only** and the cache
  root is `0700`. Loopback-only is NOT authentication: any other UID on the same
  host can reach loopback ports. Trust local users as trusted admins; see
  docs/security.md.
* No credentials, tokens, prompt text or cache data in the tree.
* All documented commands work as-is once precondition variables (`IMAGE`,
  `MODEL_DIR`, `API_KEY`) are exported — the Quick Start sequences it linearly.

## Add your own model

The cache layer is model-agnostic — adding a model does NOT require code changes:

1. Copy the reference example and rename it to your model id:
   ```bash
   cp -r "$REPO/configs/mimo-v2.6-pro" "$REPO/configs/<your-model>"
   ```
2. Edit `configs/<your-model>/identity.env`:
   * `KVP_MODEL_ID` = your model's HF id (or any unique label).
   * `KVP_TP_SIZE`, `KVP_BLOCK_SIZE`, `KVP_NUM_KV_HEADS`, `KVP_HEAD_DIM`,
     `KVP_MAX_MODEL_LEN`, `KVP_KV_CACHE_DTYPE` = the KV geometry vLLM reports
     for YOUR model (see `docs/architecture.md`, "identity fields").
   * `KVP_CONNECTOR_VERSION` = a descriptive tag for your vLLM+LMCache combo.
3. Run `bash scripts/setup-identity.sh "$HOME/models/<your-model-dir>"` to fill
   and verify the revision/digest fields automatically.
4. Point `scripts/preflight.sh`, `start-cache.sh` and the vLLM launcher at
   `configs/<your-model>/` (the scripts read the path from `CONFIG_FILE`/env;
   see `docs/deployment.md`).

Each model gets its own cache namespace — KV data of different models is never
mixed, and a change to any identity field safely falls back to a cache miss.

**Note:** support for non-MiMo models is untested until exercised on hardware;
treat the `configs/mimo-v2.6-pro/` directory as the single VERIFIED reference
configuration and expect to adapt the launcher (patches / model loader) for
architecturally different models.

## License

Apache-2.0 — see [LICENSE](LICENSE). If you **distribute** a modified version,
fork, bundle, or SaaS built on this code, you must carry the [NOTICE](NOTICE)
content (author: **sikkatu** / project: **kv-persistence** /
https://github.com/sikkatu/kv-persistence), per Apache-2.0 Section 4(d).
Running it unmodified in a private backend does not require credit — §4(d)
binds distributors, not every internal user.