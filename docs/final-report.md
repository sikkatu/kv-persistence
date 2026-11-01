> 🌐 Language versions: **English** | [Русский](final-report.ru.md) | [中文](final-report.zh.md)

> This page is the in-repo technical record. Full docs in three languages: [wiki](https://github.com/sikkatu/kv-persistence/wiki) | [Русский](https://github.com/sikkatu/kv-persistence/wiki/Home.ru) | [中文](https://github.com/sikkatu/kv-persistence/wiki/Home.zh)

# Final report — kv-persistence

Repository: https://github.com/sikkatu/kv-persistence (branch `main`,
commit `HEAD` (see `git rev-parse HEAD`), pushed and verified against the
GitHub remote API).

**Overall status: PARTIAL** — implementation complete, offline tests pass
(74 unit tests), architecture proven against the pinned vLLM build's source
code. Hardware acceptance tests (Tests 1–16 of the brief, the restart-recovery
evidence) are scripted and ready (`scripts/test-recovery.sh`,
`scripts/benchmark.sh`) but **have not been executed on a target GPU host**,
because network/SSH access to the target machine was
not reachable during this work session. Nothing untested is claimed as working.

## Architecture and selected backend

Chat client → vLLM (TP4, production recipe untouched) → `LMCacheMPConnector`
(loaded via `kv_connector_module_path` = `lmcache.integration.vllm.lmcache_mp_connector`,
kv_role=kv_both) → standalone `lmcache server` (MP mode) → L2 tier
`nixl_store_dynamic` backend `POSIX` → NVMe. L1 = host RAM buffer (64 GB default).
Persistence: `persist_enabled=true`, deterministic file per chunk key, lazy
index rebuild on startup (documented upstream behavior of this adapter).

## Tested runtime versions

| Item | Value | Status |
|---|---|---|
| vLLM commit | `8e2beaa3bff595577a069d49d96bde9074294ec2` (2026-10-21) | VERIFIED (source) |
| vLLM image | `vllm/vllm-openai:nightly-8e2beaa3bff…@sha256:167ccd86…` (pin format) | matches production recipe |
| LMCache | MP server + `LMCacheMPConnector` | NEEDS TESTING (host) |
| Python | 3.12 | VERIFIED |

## Verified compatibility with the MiMo deployment

* `lmcache_mp_connector.py` + `lmcache_integration/` present at the pinned vLLM
  commit (VERIFIED by GitHub API contents listing of the exact commit).
* `kv_connector_module_path` supported in `vllm/config/kv_transfer_config.py`
  at that commit (VERIFIED).
* Model patches (`mimo_v2_hotsplit.py`, `mimo_v2_omni.py`) are mounted read-only
  and verified against `MANIFEST.sha256` before launch — the launcher refuses to
  run otherwise. The production recipe is never modified.

## FP16 KV cache and MAX_NUM_SEQS=2 — verification status

| Claim (from brief) | Effective reality | Status |
|---|---|---|
| KV_CACHE_DTYPE=fp16 | production runs `auto` (=bf16); brief's serving settings say fp16 | **NEEDS TESTING / CONFIG CHOICE** — the launcher defaults to the *effective* dtype (bf16) and the cache identity requires a resolved dtype; switching to fp16 changes KV memory and must be re-measured on hardware. Set `KV_CACHE_DTYPE=fp16` + `KVP_KV_CACHE_DTYPE=fp16` to enforce the brief's setting and re-run all benchmarks. |
| MAX_NUM_SEQS=2 | `run-vllm-with-persist.sh` sets `--max-num-seqs 2` explicitly (production file default is 1) | VERIFIED (flag); engine log confirmation when run |

## Commands

```bash
# install: docs/deployment.md §1–§4 (identity setup + lmcache venv from pinned requirements)
bash scripts/setup-identity.sh "$HOME/models/MiMo-V2.6-Pro-MOPD"  # fills AND verifies identity
bash scripts/preflight.sh
bash scripts/start-cache.sh
export IMAGE='…@sha256:<digest>'; export MODEL_DIR="$HOME/models/<your-model-dir>"
bash scripts/run-vllm-with-persist.sh
bash scripts/verify-engine-identity.sh          # gate: engine geometry == identity pins

# acceptance tests -- machine verdict; exit 0 = PASS
bash scripts/test-recovery.sh                   # scenarios A and B (v2 protocol)
bash scripts/benchmark.sh cold 32768           # per config, see docs/benchmarks.md

# monitoring / storage
bash scripts/budget-report.sh status
bash scripts/budget-report.sh purge --namespace=<model_name> --yes   # = form (name starts with '-'), explicit only

# rollback (one command, production untouched)
bash $HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/scripts/run-tp4-uva.sh
```

## Benchmark results

Not yet collected (hardware run pending). Matrix, metric definitions and the
PASS verdict rule are in `docs/benchmarks.md`; the recorder writes
`benchmarks/results/<UTC-ts>/bench.jsonl` automatically.

## Evidence of disk-backed recovery after restart
Pending the hardware run (Definition-of-done items 1–7). The exact evidence
that will be collected: LMCache `Retrieved …` log lines sourced from the L2 tier
appearing *after* a vLLM restart, `usage.prompt_tokens_details.cached_tokens > 0`
on the first post-restart request, TTFT drop vs cold baseline, and
`budget-report.sh status` proving the bytes were read (not rewritten).

## Per-identity isolation at the LMCache storage level (review P1-3)

The identity namespace is now enforced at the REAL LMCache storage level, not
just as a local label. Verified against the pinned LMCache 0.5.3 source and a
live store/lookup run (integration test
`test_identity_namespace_isolates_lmcache_storage`, see docs/benchmarks.md):

* The LMCache cache key (`ObjectKey.model_name`) feeds both the key and the
  `nixl_store_dynamic` L2 file name
  (`<safe_model_name>_<kv_rank>_<group>_<chunkhash>.bin`).
* `CacheIdentity.lmcache_model_name()` embeds the namespace into that
  `model_name`, so changing ANY identity field moves the data to a different
  key AND a different file on disk. Proven per field: the changed identity's
  lookup returns `0/N` chunks at the LMCache API where the original returns
  `N/N`.
* **Residual gap — RESOLVED at the disk-key level (round 15)**: `cache_salt` is
  part of the key's equality but upstream `nixl_store_dynamic` does NOT put it
  into the file name (the fs/s3/bigtable adapters do include it). The measured
  leak (`store salt=tenantA`, `lookup salt=tenantB` -> `4/4` chunks after a
  restart) is closed by `kvpersist/salt_filename_patch.py`, which appends the
  salt to the L2 file name and is used by ALL launchers. Verified live:
  `store salt=tenantA`, `lookup salt=tenantB` -> `0/4` chunks
  (`test_cache_salt_isolates_l2_storage` fails loudly if the leak returns).
  Running a bare `lmcache server` WITHOUT the patch keeps the upstream
  behaviour: use `lmcache_model_name(extra_discriminator=...)` or a separate
  cache root per tenant then.

## NVMe bytes per cached token

Pending measurement on hardware (formula: `used_bytes ÷ persisted tokens`,
reported by `benchmark.sh` + `budget-report.sh status` per run). Theoretical
footprint for the current dtype (bf16) is
`2 x num_kv_heads x head_dim x 2(K,V) x layers` bytes/token of TOTAL KV (per-token across all TP ranks; NVMe stores the whole tensor, NOT per-rank divided). For hybrid-attention models per-group geometry differs —
layer/head counts for MiMo-V2.6-Pro are recorded from the model config during
the hardware run, before any capacity promises are made.

## Cache budget and observed capacity

Configured: 1500 GB default (hard), bound = free space on the shared VM disk (same volume holds Docker, the model and the OS) (preflight-verified). Observed usage:
pending hardware run. Bytes-per-token estimate will be filled from real
measurements (per the brief: do not promise a fixed number of conversations
before measuring).

## Eviction and deduplication test results

* Eviction policy semantics: **VERIFIED offline** (74 unit tests, incl. stale
  before recent, rare before frequent, huge useless first, long reused prefix
  protected, shared-parameter determinism).
* Automatic L2 eviction on hardware with a small budget (Test 8): NEEDS TESTING.
* Dedup: **understood and documented** (content-derived chunk keys ⇒ single
  stored object per identical chunk); integration measurement NEEDS TESTING
  (`tests/integration/test_dedup.py`).

## Known limitations / untested scenarios

1. Hardware acceptance (Tests 1–16) not executed in this session — no route to
   the host. This is the ONLY reason the overall status is PARTIAL, not PASS.
   The v2 `test-recovery.sh` now enforces the acceptance protocol mechanically:
   log-position markers, L2-vs-L1 evidence distinction, store-before-restart
   verification, hard-fail API readiness waits and a PASS/FAIL exit code per the
   review's protocol.
2. Value-based retention ranking applies where per-entry stats are available;
   LMCache's in-service LRU is used otherwise (documented, honest split).
   Review round 13: the score's prefix-length term is now LINEAR in tokens
   (was `log1p`), which made long reused prefixes evict FIRST; the offline
   planner is the only consumer of this ranking today.
3. LMCache is version-pinned (`requirements-lmcache.txt`, `lmcache==0.5.3`); the
   exact wheel must match the production image ABI. If it does not, `lmcache
   server` runs from the production image (documented fallback). This exact
   combination is NEEDS TESTING on host.
4. Dedup semantics: LMCache maps each chunk key 1:1 to a content-derived object
   file; there is NO caller-side refcount and no cross-client key→object sharing
   across namespaces. Eviction may drop a byte range that a live query would read
   as a pure liveness trade-off (that query misses and recomputes), never as a
   correctness issue. See docs/eviction-and-deduplication.md.
5. 1M-token prompts: only run if the hardware window allows meaningful execution.
6. `{KV_CACHE_DTYPE, KVP_KV_CACHE_DTYPE}` must switch to `fp16` if the brief's
   serving setting is to be the effective one; all numbers must then be
   re-measured. KV-geometry values (block size, heads, dim, TP, max-model-len)
   are now cross-checked against the RUNNING engine by
   `scripts/verify-engine-identity.sh` before any cached data is trusted.
7. The wording of two external working documents ("the brief" of the original
   internal project request and "the review's protocol" cited in the fix history)
   refers to design decisions reachable only for the project author's process;
   the generic operations described here are reproducible without them, but exact
   pass criteria should be adapted before third-party site acceptance.

## Review round 13 fixes (verified on this host)

* `setup-identity.sh` no longer writes quoted values (its own verifier used to
  fail with `got '"76ce..."'`); quoting is now stripped on read too.
* `run-vllm-with-persist.sh`: the identity gate runs BEFORE launch as a
  config-only check (`verify-engine-identity.sh --check-config-only`) and AFTER
  `docker run` against the NEW container's args (the old order inspected a
  container that did not exist yet, or one with stale args). `KVP_PERSIST_ENABLED=0`
  genuinely skips both gates (no cache to mistrust).
* Per-identity isolation is now enforced in the REAL vLLM path: the model is
  mounted at `/models/<lmcache_model_name>` (namespace-coded) so
  `model_config.model` — the value LMCache derives ObjectKey and L2 file names
  from — differs per identity. Previously every deployment used `--model /model`.
* `kvpersist.cli` sees the real L2 layout (flat `*.bin` files written by
  `nixl_store_dynamic`): `status` reports their bytes, `purge --all` and
  `emergency-evict` actually delete them. The budget default is 1500 GB (was
  750). `KVP_CACHE_ROOT`/`KVP_CACHE_BUDGET_GB` fall back to the repo config
  files, so `emergency-evict` works from cron without exported env. Quotas are
  documented as report-only (they are not wired into the store path).
* systemd units: `lmcache-server.service` (plain, `User=kvp`) and
  `lmcache-server@.service` (template, `User=%i` — the only valid form for
  that), plus `systemd/kvp-lmcache-server` wrapper that inlines the rendered
  adapter JSON (`--l2-adapter` takes a JSON string, not a path).
* `test-recovery.sh`: hard FAILs no longer get erased by an INCONCLUSIVE
  (`V`/`VI` counted separately, FAIL dominates); the cold prefix carries a
  per-run nonce so a repeated run is genuinely cold.
* Docker launch mode: `--help` probing happens inside the container, the server
  binds `0.0.0.0` inside it (published ports are still loopback-only on the
  host), `--gpus all --ipc host` added, and the optional-flag log line no longer
  crashes on an unset variable.
* `retention.py`: linear prefix-length term (the `log1p` version evicted long
  reused prefixes first); `min_keep_tokens` documented as the only direct
  protection. `prompt_probe.py` serializes `tool_calls`/`tool_call_id`.
* `preflight.sh`: `lsblk -p` output no longer yields `/dev//dev/...` device
  names. `.commitmsg11` removed from the tree.

## Review round 14 fixes (verified on this host)

* **CRITICAL**: `run-vllm-with-persist.sh` passed `--block-size` nowhere, so the
  post-launch identity gate could never verify `block_size` (INCONCLUSIVE,
  exit 5) and the launcher immediately `docker rm -f`'d its own container.
  `--block-size "${KVP_BLOCK_SIZE:-16}"` is now passed to vLLM (and
  `test-recovery.sh`'s gate passes too, since it verifies the same container).
* `l2_serde` is now actually part of the identity namespace: `model-name` reads
  the generic config layers (where `LMCACHE_L2_SERDE` lives) in the same order
  the bash loader seeds them, so `none` vs `fp8` produce DIFFERENT names. The
  process environment WINS over the files (same as `scripts/env.sh`).
* `purge --namespace` matches the namespace EXACTLY. Real namespaces look like
  `--models--MyModel--<hash>` (leading dash), so the docs show `--namespace=NAME`
  (or the new positional form); `--namespace NAME` is parsed as an unknown
  option by argparse. No match exits 1 with a message. File names with `_` are
  parsed with a right-anchored regex (`my_model` no longer truncates to `my`).
* `retention.py`: the hit weight is now meaningful. Score:
  `(hits + 1) * tokens * freshness / bytes`. At fixed bytes-per-token this is
  length-neutral between entries with equal hit counts (the earlier `log1p`
  form evicted long prefixes first; the previous `3*hits + 1.5*tokens` form
  made hits ~1% of the benefit). To favour a long entry explicitly, raise
  `min_keep_tokens` or add a small positive `tokens` exponent.
* Config loaders agree byte-for-byte on precedence: the Python loader applies
  files in the same order as the bash seeder (cache.env -> lmcache-server.env
  -> identity.env -> *.local).
* systemd: `kvp-lmcache-server` is 100755 with INSTALL instructions; renders
  the L2 spec on demand and reads `cache.env`/`.local` for `KVP_CACHE_ROOT` and
  the budget (same precedence as `start-cache.sh`, so a custom cache root in
  `cache.env.local` is honoured -- no silent 1.5 TB on the system disk);
  `StateDirectory=kv-persistence` handles the default-path ownership; for a
  custom `KVP_CACHE_ROOT` run `sudo install -d -o kvp -g kvp -m 0700 <dir>`;
  `KVP_REPO_ROOT` defaults to `/home/kvp/kv-persistence` (README clones to
  `~/kv-persistence`; override in `/etc/kv-persistence.env`).
* Docs sync (EN/RU/ZH): `eviction-and-deduplication.*` formulas updated to
  the linear form; `storage-management.*`, `security.*` and `final-report.*`
  describe the real L2 layout (flat `<safe_model_name>_<rank8>_<group>
  _<hash64>.bin` files) instead of the nonexistent `kvp-<hash>` directories;
  `deployment.*` gained a systemd section with the ownership and
  `--namespace=NAME` form documented.

## Review round 15 fixes (verified on this host)

* **cache_salt isolation fixed AT THE DISK-KEY LEVEL**: upstream 0.5.3 omits
  the salt from the `nixl_store_dynamic` L2 file name (measured leak: store
  salt=tenantA -> lookup salt=tenantB returned 4/4 chunks after restart).
  New `kvpersist/salt_filename_patch.py` appends the salt to the file name;
  ALL launchers (`scripts/start-cache.sh` host+docker, `systemd/kvp-lmcache-server`)
  start the server through it. Verified live: tenantA -> tenantB = 0/4
  (`test_cache_salt_isolates_l2_storage` -- the test now REQUIRES isolation
  and fails loudly if the leak reappears). Empty salt keeps the upstream name
  (backward compatible); salt data written by an unpatched server is a safe
  miss, never a wrong read.
* **README compatibility claim narrowed** (EN/RU/ZH): "works with any model"
  -> "designed for reuse with any vLLM-served model; the ONLY
  hardware-confirmed stack is MiMo-V2.6-Pro on 4x RTX PRO 6000 via
  sikkatu/recipes; other models are experimental compatibility until the
  acceptance suite passes".
* **README.ru security wording fixed**: "Никаких неаутентифицированных
  эндпоинтов управления" was wrong -- the management endpoints (HTTP + ZMQ)
  are loopback-only, which is NOT authentication; another local user can
  reach them. All three README locales now state this (matching
  docs/security.*).
* **KV dtype BF16 confirmed everywhere** (review instruction): the launcher
  default, `identity.env` pin and the identity gate all use bf16; the
  comments claiming "target is fp16" are corrected (EN/RU/ZH in
  identity.env.* and run-vllm-with-persist.sh; compatibility.* table).
  KV dtype remains part of the cache identity -- a dtype change is a MISS,
  never a wrong-format read.

## Lossless L2 codec and observability stack (task round)

The brief asked for two additions, both implemented WITHOUT forking LMCache and
without touching the production recipe:

### 1. Lossless-only L2 storage

Upstream LMCache 0.5.3 registers only lossy (fp8, turboquant) and encryption
(aesgcm) serdes -- there is NO lossless compression serde upstream. This repo
adds one via `implementation/kvpersist/compress_serde.py`, which registers the
`kvp-zstd` / `kvp-lz4` / `kvp-raw` serde types in LMCache's own registry at
runtime (same launcher pattern as the salt filename patch, now unified in
`implementation/kvpersist/launch.py`).

* Default `LMCACHE_L2_SERDE=none` -- byte-exact raw chunks, identical to the
  uncompressed upstream path. Nothing is transformed.
* `LMCACHE_L2_SERDE=zstd|lz4` (or the `USE_LOSSLESS_SERDE` operator switch) --
  lossless compression. A block that does not shrink is stored raw, so a chunk
  never grows by more than one 4 KiB page (28-byte frame header + page
  padding). The commonly cited 1.1-1.4x reduction is a PLANNING ESTIMATE from
  the LMCache ecosystem, NOT a measurement from this repository -- no
  compression-ratio benchmark has been run here yet.
* Lossy codecs (fp8 / turboquant / lbq / cachegen / kivi) are REFUSED loudly at
  startup -- lossless is the contract, not a default that can drift.
* The codec is folded into the cache-identity namespace (`l2_serde`) by BOTH
  the shell loader (`scripts/env.sh _kvp_resolve_l2_serde`) and the Python
  loader (`kvpersist.identity`), and unit tests assert the two agree
  (`tests/unit/test_lossless_serde_switch.py`). Switching codecs is a safe
  cache miss, never a wrong read.
* Frame format `KVCZ` v2 carries a CRC32 of the uncompressed payload: a
  truncated DMA read, bit flip, or a frame from another codec is detected at
  deserialize time and reported as a cache MISS.
* The padding after the payload is explicitly zeroed -- a recycled L1 slab
  block must never leak another tenant's bytes into our cache file
  (`tests/unit/test_compress_serde.py::test_padding_is_zeroed`).
* Fail-fast: `register()` and `start-cache.sh` verify the codec's Python
  bindings BEFORE the server starts (host venv probe, or a probe INSIDE the
  container in docker mode). Missing bindings abort the launch instead of
  reaching READY and failing later inside the serde thread pool.
* The L2 file-size patch installs ONLY when a kvp-* serde is actually built;
  with `LMCACHE_L2_SERDE=none` the adapter class is untouched (byte-for-byte
  upstream load/store), so a short DMA read can never be masked by our
  wrapper on the raw path.

**Honest status:** the serde frame logic is unit-tested (round-trip, framing,
malformed-frame rejection, incompressible-data passthrough) WITHOUT torch/lz4/
zstandard installed. The end-to-end path through LMCache's `SerdeL2AdapterWrapper`
and the `nixl_store_dynamic` DMA write is **NEEDS TESTING on the target GPU
host** -- same status as the rest of the hardware acceptance suite. Two
surgical runtime patches (`_install_size_patch`) make the on-disk file size
follow the serialized frame rather than the full L1 allocation; these are
covered by unit-level logic but not yet by a hardware run.

### 2. Prometheus + Grafana monitoring

`observability/` vendors the OFFICIAL LMCache example stack
(`LMCache/LMCache` `examples/observability`, Apache-2.0): OTel Collector +
Tempo + Prometheus + Grafana, with the official LMCache and Coordinator
dashboards pre-provisioned (JSON copied verbatim).

```bash
bash scripts/start-observability.sh     # one command, Grafana on :3000
```

Telemetry is opt-in and off by default (`KVP_OTLP_ENDPOINT` /
`USE_OBSERVABILITY=1`); the cache service works fully without the stack.
**Status: the compose files and dashboards are verbatim official assets and the
script is exercised for syntax; the live stack has NOT been brought up on the
target host in this session** (no GPU host / Docker daemon reachable).

## Provenance

* Commit hash of this report: run `git log -1 --format=%H` in the repository.
* Push credentials are NOT stored in this repository (verified: zero
  `github_pat_` occurrences in tree and in git history). Credentials used by
  maintainers follow the same rotate-after-use policy documented in
  docs/security.md.