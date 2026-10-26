> **English** | [Русский](README.ru.md) | [中文](README.zh.md)

# kv-persistence — Universal Persistent KV Cache for vLLM

Persistent, disk-backed KV-cache layer for vLLM: after a restart, reusable prompt KV
blocks are recovered from NVMe instead of being recomputed. The design goal —
long chat-app conversations (100k–1M tokens) reopening in seconds instead of tens of
minutes — is measured on target hardware in `scripts/benchmark.sh`; results are
published in [docs/benchmarks.md](docs/benchmarks.md) once the acceptance run
completes (see [Status](#status)).

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

Russian readers: [README.ru.md](README.ru.md) · 中文读者: [README.zh.md](README.zh.md)

Primary target: MiMo-V2.6-Pro on 4 × NVIDIA RTX PRO 6000 (the
`sikkatu/recipes` `recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit` recipe). The cache
layer is model-agnostic; model-specific settings live under `configs/<model>/` only.

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
bash scripts/setup-identity.sh "$HOME/models/MiMo-V2.6-Pro-MOPD"   # fill AND verify identity pins
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
| `configs/mimo-v2.6-pro/` | MiMo identity pins and L2 adapter template, EN/RU/ZH |
| `scripts/` | preflight, cache start/stop, launcher, recovery test, benchmark, admin CLI |
| `implementation/` | `kvpersist` package: identity, budgets, retention ranking, admin CLI |
| `tests/` | unit (29, run anywhere), integration (needs `lmcache`), recovery (GPU host) |

## Status

Implementation complete; offline tests green. Hardware acceptance runs on the 4-GPU
host are scripted (`scripts/test-recovery.sh`, `scripts/benchmark.sh`) and executed by
the operator. Unverified features are labelled `NEEDS TESTING` / `BLOCKED` in
[docs/final-report.md](docs/final-report.md) — nothing untested is claimed as working.

## Design rules

* The production recipe (`sikkatu/recipes`) is **read-only** for this project.
* No unauthenticated management endpoints; the cache root is `0700`.
* No credentials, tokens, prompt text or cache data in the tree.
* All documented commands work as-is, without manual additions.

## License

Apache-2.0 — see [LICENSE](LICENSE). Anyone using this project or publishing
a fork must carry the [NOTICE](NOTICE) content (author: **sikkatu** / project:
**kv-persistence** / https://github.com/sikkatu/kv-persistence), per Apache-2.0
Section 4(d).