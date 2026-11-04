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

Designed for reuse with **any vLLM-served model on any NVIDIA GPU host** — the
model-agnostic core keeps policy code separate from model integration.
The only hardware-confirmed stack is the reference one: MiMo-V2.6-Pro on
4× NVIDIA RTX PRO 6000 via the `sikkatu/recipes` recipe. Every other model or
host is **experimental compatibility** until it passes the acceptance suite
(`scripts/test-recovery.sh`); expect to adapt the launcher (patches / model
loader) for architecturally different models.

## What it does

```
Chat client → vLLM (TP4) → KV connector (LMCacheMPConnector)
       → lmcache server (standalone process) → L2 (nixl_store_dynamic, POSIX) → NVMe
```

* **Persistence across restarts** — data survives a vLLM restart AND a full cache-service
  restart (`persist_enabled=true`; on-disk index is rebuilt lazily from file names).
* **Bounded storage** — hard cap (1.5 TB default, configurable to whatever the NVMe
  device holds), soft-threshold (90 %) automatic LRU eviction; the cache never wipes
  itself on startup. **Restart caveat (NEEDS TESTING):** LMCache's index is rebuilt
  lazily from on-disk file sizes as they are touched, so the *accounted* usage starts
  from zero at startup while the real files are still there. Until a hardware run
  confirms the pinned build feeds restored sizes back into `get_usage()`, do NOT
  assume pre-restart files count against the cap (see
  [docs/storage-management.md](docs/storage-management.md) "Restart accounting").
* **Compatibility namespaces** — model revision, tokenizer, KV dtype, block layout and
  chat template are part of the cache key: incompatible deployments never read each
  other's KV data; anything uncertain is a cache miss, never a wrong hit. The namespace
  is enforced at the LMCache storage level via `lmcache_model_name()` (embedded in the
  `model_name` that forms the ObjectKey and L2 file name). For per-user/tenant
  isolation use `cache_salt` (fixed at the disk-key level in review round 15 by
  `kvpersist/salt_filename_patch.py`, which ALL launchers use) or
  `lmcache_model_name(extra_discriminator=...)` / a separate cache root.
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

## Lossless persistence

The L2 tier is **lossless only**. KV blocks on NVMe are always decoded back to
the exact bytes the engine produced — there is no fp8 / turboquant / lbq
quantization anywhere in this project, and any such codec is refused loudly at
startup. Two storage modes are available:

* **Byte-exact raw (default)** — `LMCACHE_L2_SERDE=none`. Chunks are stored
  verbatim, identical to the uncompressed upstream path. Nothing is
  transformed; a cache hit returns the engine's exact KV bytes.
* **Lossless compression (opt-in)** — `LMCACHE_L2_SERDE=zstd` or `lz4`
  (or the `USE_LOSSLESS_SERDE` switch, see below). Chunks are compressed with
  the lossless `kvp-zstd` / `kvp-lz4` serdes registered by
  `implementation/kvpersist/compress_serde.py`. A block that does not shrink
  is stored raw, so a chunk never grows by more than one 4 KiB page (the
  28-byte frame header plus padding up to the page boundary). Expected size
  reduction on real KV payloads is modest — the commonly cited range is
  roughly **1.1–1.4x**, but that number is an *estimate*, not a measurement
  from this repository: no benchmark of the compression ratio has been run
  here yet (see `docs/final-report.md`). Treat it as a rough planning figure,
  not a guarantee.

Every frame carries a **CRC32 of the uncompressed payload** (`KVCZ` format v2):
a truncated DMA read, a bit flip, or a frame written under a different codec is
detected at deserialize time and reported as a cache **miss** — never returned
as silently corrupt KV.

**Dependencies:** the uncompressed default (`none`) needs **no** compression
packages at all. `zstd` needs only `zstandard`, `lz4` needs only `lz4` — a
startup check (`require_codec`) verifies exactly the selected codec before the
server starts, and never requires an unused one.

Enable lossless compression for one run:

```bash
USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh     # zstd
USE_LOSSLESS_SERDE=lz4 bash scripts/start-cache.sh   # lz4
```

or set it permanently in `configs/generic/lmcache-server.env(.local)`:

```bash
LMCACHE_L2_SERDE=zstd
```

The codec is part of the cache-identity namespace: switching between `none`,
`zstd` and `lz4` makes the previously stored chunks a safe cache **miss**
(never a wrong read), and every launcher (`start-cache.sh`,
`run-vllm-with-persist.sh`, `verify-engine-identity.sh`) resolves the switch to
the same effective value.

## Monitoring (Prometheus + Grafana)
The full observability stack from the official LMCache example ships in
`observability/` (OTel Collector + Tempo + Prometheus + Grafana) with the
**official LMCache dashboards already provisioned**. One command brings it up:

```bash
bash scripts/start-observability.sh
# or, equivalently:
cd observability && docker compose up -d
```
* **Grafana** → <http://localhost:3000> (anonymous Admin access, no login).
  Dashboards → *LMCache* and *LMCache Coordinator* are imported automatically.
* **Prometheus** → <http://localhost:9091>
* **Tempo** → <http://localhost:3200> (traces)
* **OTLP gRPC** → `localhost:4320` — where the cache server pushes metrics.

**Dashboard/version honesty note (review round 18):** the dashboards are the
*official* LMCache assets, but they were authored against a NEWER LMCache than
the pinned **0.5.3** this repo runs. Static comparison against the 0.5.3 source
(`lmcache/v1/mp_observability/`) shows most panels map to real 0.5.3 metrics
(L1/L2 usage gauges, store/prefetch pipelines, eviction loop, event-bus,
throughput histograms), but **4 of 28 panels** referenced metrics 0.5.3 never
emits (lookup prompt/request-hit token counters, L0↔L1 inflight store/retrieve
counters, transfer-phase DMA counters). Those panels are now **remapped to the
real 0.5.3 metrics** (`lookup_requested`/`lookup_hit` for the hit rate,
`l1_read`/`l1_write` chunk counters for L0↔L1 activity, `l0_l1_store_throughput`
for DMA throughput). The remap is a best-effort semantic match, not an upstream
guarantee — labels/units may differ slightly from the original intent.
The *LMCache Coordinator* dashboard expects coordinator telemetry that 0.5.3's
`mp_coordinator` does not export (48 series with no 0.5.3 analogue); this repo
does not run a coordinator, so that dashboard will legitimately stay empty.

**Security:** every published port is bound to `127.0.0.1` only (the
`docs/security.md` loopback contract). Grafana runs with anonymous Admin access
and the login form disabled — exposing it on `0.0.0.0` would hand out admin on
the whole network. To reach the stack from another host, put an authenticated
reverse proxy in front of it. All images are pinned to exact versions
(no `:latest`), matching the rest of the project.

**Docker mode:** when the cache server runs in a sidecar container
(`KVP_LAUNCH_MODE=docker`), `start-cache.sh` automatically rewrites
`localhost`/`127.0.0.1` in the OTLP endpoint to `host.docker.internal` — inside
the container `localhost` is the container itself, so the metrics would
otherwise never reach the collector.

Point the cache server at the collector and enable telemetry (off by default,
the cache works fine without it):

```bash
USE_OBSERVABILITY=1 bash scripts/start-cache.sh
# or explicitly:
KVP_OTLP_ENDPOINT=http://localhost:4320 bash scripts/start-cache.sh
```

Panels worth watching on the LMCache dashboard:

| Panel | What it tells you |
|---|---|
| cache hit rate | hit / miss ratio per request — the primary health signal |
| L1 / L2 usage | tier occupancy vs capacity (L1 = host RAM, L2 = NVMe) |
| store / retrieve throughput | bytes per second written to and read from NVMe |
| evictions | LRU evictions per tier — a rising rate means the budget is too small |
| restore (prefetch) time | how fast a restarted server refills L1 from NVMe |

Stop the stack with `bash scripts/start-observability.sh --down` (data volumes
are kept).

## Sizing for a ~200 GB KV working set

A conversation with up to ~200 GB of KV needs room for that plus growth
headroom:

| Knob | Recommendation |
|---|---|
| `KVP_L1_SIZE_GB` (host RAM buffer) | 30–40 GB |
| `KVP_CACHE_BUDGET_GB` (L2 hard cap) | ≥ 250–300 GB on a **dedicated local NVMe** |
| `KVP_CACHE_ROOT` | path on that NVMe, never a network share |
| `KVP_CACHE_DEVICE` | pin the block device so `preflight.sh` can prove the cache sits on the right disk |

The shipped default budget is 1500 GB because the reference host shares one
volume between the OS, the model and the cache; on a dedicated NVMe lower it to
300 GB. `KVP_CACHE_BUDGET_GB`, `KVP_CACHE_ROOT` and `KVP_CACHE_DEVICE` all live
in `configs/generic/cache.env(.local)`.

## Quick start: lossless + Grafana

```bash
# 1. Monitoring (optional but recommended)
bash scripts/start-observability.sh

# 2. LMCache + vLLM in lossless mode
USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh
USE_LOSSLESS_SERDE=1 bash scripts/run-vllm-with-persist.sh
```

`USE_LOSSLESS_SERDE` must be identical for both commands — it is part of the
cache-identity namespace. Pin the same value as `KVP_L2_SERDE` in
`identity.env.local` so the identity gate can verify the server's codec
(it reads the rendered L2 adapter the server was started with). Omit it
everywhere to stay on the byte-exact raw mode.

## Repository layout

| Path | Content |
|---|---|
| `docs/` | English technical documentation (wiki: all three languages) |
| `configs/generic/` | cache + lmcache-server settings, EN/RU/ZH comment translations |
| `configs/<model>/` | per-model identity pins and L2 adapter template (example: `mimo-v2.6-pro`), EN/RU/ZH |
| `scripts/` | preflight, cache start/stop, launcher, recovery test, benchmark, admin CLI, observability |
| `observability/` | Prometheus + Grafana + Tempo + OTel Collector stack with the official LMCache dashboards |
| `implementation/` | `kvpersist` package: identity, budgets, retention ranking, admin CLI, runtime patches |
| `tests/` | unit (run anywhere), integration (needs `lmcache`), recovery (GPU host) |

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
2. **Select the model config** — with a second config dir present the scripts no
   longer auto-pick one (ambiguous layouts are refused, never guessed):
   ```bash
   export KVP_MODEL_CONFIG=<your-model>
   ```
   `KVP_MODEL_CONFIG` is read by `setup-identity.sh`, `start-cache.sh`,
   `run-vllm-with-persist.sh`, `verify-engine-identity.sh` and the `kvpersist`
   config loader. `CONFIG_FILE` (used only by `setup-identity.sh`) additionally
   lets you target the exact file to write; you normally do not need it.
3. Edit `configs/<your-model>/identity.env`:
   * `KVP_MODEL_ID` = your model's HF id (or any unique label).
   * `KVP_TP_SIZE`, `KVP_BLOCK_SIZE`, `KVP_NUM_KV_HEADS`, `KVP_HEAD_DIM`,
     `KVP_MAX_MODEL_LEN`, `KVP_KV_CACHE_DTYPE` = the KV geometry vLLM reports
     for YOUR model (see `docs/architecture.md`, "identity fields").
   * `KVP_CONNECTOR_VERSION` = a descriptive tag for your vLLM+LMCache combo.
4. Run `bash scripts/setup-identity.sh "$HOME/models/<your-model-dir>"` to fill
   and verify the revision/digest fields automatically.
5. Launch with `KVP_MODEL_CONFIG=<your-model>` exported; the scripts resolve
   `configs/<your-model>/` from it (see `docs/deployment.md`).

Each model gets its own cache namespace — KV data of different models is never
mixed, and a change to any identity field safely falls back to a cache miss.

**Note:** support for non-MiMo models is untested until exercised on hardware;
treat the `configs/mimo-v2.6-pro/` directory as the single REFERENCE configuration
(shape and fields are pinned; nothing in this repo has run on GPU hardware yet —
the "Status" section above is authoritative) and expect to adapt the launcher
(patches / model loader) for architecturally different models.

## License

Apache-2.0 — see [LICENSE](LICENSE). If you **distribute** a modified version,
fork, bundle, or SaaS built on this code, you must carry the [NOTICE](NOTICE)
content (author: **sikkatu** / project: **kv-persistence** /
https://github.com/sikkatu/kv-persistence), per Apache-2.0 Section 4(d).
Running it unmodified in a private backend does not require credit — §4(d)
binds distributors, not every internal user.