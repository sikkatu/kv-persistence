> 🌐 Language versions: **English** | [Русский](final-report.ru.md) | [中文](final-report.zh.md)

> This page is the in-repo technical record. Full docs in three languages: [wiki](https://github.com/sikkatu/kv-persistence/wiki) | [Русский](https://github.com/sikkatu/kv-persistence/wiki/Home.ru) | [中文](https://github.com/sikkatu/kv-persistence/wiki/Home.zh)

# Final report — kv-persistence

Repository: https://github.com/sikkatu/kv-persistence (branch `main`,
commit `HEAD` (see `git rev-parse HEAD`), pushed and verified against the
GitHub remote API).

**Overall status: PARTIAL** — implementation complete, offline tests pass
(188 unit tests + 19 more offline config/docs tests = 207 offline collected,
205 passed + 2 skipped; 222 collected total including the live-`lmcache`
integration (10) and recovery (5) tests; plus 318 passed of 323 collected
in the companion `sikkatu/recipes` repo -- skips are environment-dependent,
e.g. torch-only tests are skipped on hosts without torch), architecture proven against the pinned vLLM build's source
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
| KV_CACHE_DTYPE=fp16 | production runs `auto` (=bfloat16); brief's serving settings say fp16 | **NEEDS TESTING / CONFIG CHOICE** — the launcher defaults to the *effective* dtype (`bfloat16`, the vLLM CacheDType spelling — `bf16` is not accepted by the pinned build) and the cache identity requires a resolved dtype; switching to `float16` changes KV memory and must be re-measured on hardware. Set `KV_CACHE_DTYPE=float16` + `KVP_KV_CACHE_DTYPE=float16` to enforce the brief's setting and re-run all benchmarks. |
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
footprint for the current dtype (bfloat16) is
`2 x num_kv_heads x head_dim x 2(K,V) x layers` bytes/token of TOTAL KV (per-token across all TP ranks; NVMe stores the whole tensor, NOT per-rank divided). For hybrid-attention models per-group geometry differs —
layer/head counts for MiMo-V2.6-Pro are recorded from the model config during
the hardware run, before any capacity promises are made.

## Cache budget and observed capacity

Configured: 1500 GB default (hard), bound = free space on the shared VM disk (same volume holds Docker, the model and the OS) (preflight-verified). Observed usage:
pending hardware run. Bytes-per-token estimate will be filled from real
measurements (per the brief: do not promise a fixed number of conversations
before measuring).

## Eviction and deduplication test results

* Eviction policy semantics: **VERIFIED offline** (unit tests incl. stale
  before recent, rare before frequent, huge useless first, long reused prefix
  protected, shared-parameter determinism).
* Automatic L2 eviction on hardware with a small budget (Test 8): NEEDS TESTING.
* Dedup: **understood and documented** (content-derived chunk keys ⇒ single
  stored object per identical chunk). Model semantics tested offline
  (`tests/unit/test_dedup_model.py`, simulation); real-backend measurement is
  scripted as `tests/integration/test_l2_dedup_real.py` (live `lmcache server`,
  real L2 files counted before/after a second shared-prefix request) and NEEDS
  TESTING on the target host.

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
6. `{KV_CACHE_DTYPE, KVP_KV_CACHE_DTYPE}` must switch to `float16` if the brief's
   serving setting is to be the effective one (the vLLM CacheDType spelling is
   `float16`/`bfloat16` — `fp16`/`bf16` are rejected by the pinned build); all
   numbers must then be re-measured. KV-geometry values (block size, heads,
   dim, TP, max-model-len) are now cross-checked against the CONTAINER'S
   STARTUP ARGUMENTS (`docker inspect .Config.Cmd`) and the served model's
   config.json — NOT live engine introspection — by
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
  default, `identity.env` pin and the identity gate all use bfloat16; the
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
* Fail-fast: before the server starts, `require_codec()` verifies the
  bindings of the **selected** codec only (host venv probe, or a probe INSIDE
  the container in docker mode). A `LMCACHE_L2_SERDE=none` cache needs no
  compression packages at all — lz4 and zstandard are required only when
  their codec is actually selected. Missing bindings abort the launch instead
  of reaching READY and failing later inside the serde thread pool.
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

## Review round 16 fixes

* **Active-write registry liveness (P1)**: `kvpersist.tmp_guard` no longer treats
  a marker as dead by age. A marker records `(pid, process start-time)` and its
  writer counts as alive while that exact process exists (start-time defeats PID
  reuse). A registered in-flight write is never deleted however long it runs;
  liveness is judged by `/proc/<pid>/stat`, and when the probe cannot decide
  (no /proc, unreadable marker) the marker is KEPT — safer to retain one tmp
  file too long than to delete a live store. Markers whose writer provably
  exited are swept after an hour. A heartbeat thread refreshes the marker while
  the store runs. Tests: `test_live_marker_protects_tmp_regardless_of_age`,
  `test_stale_markers_are_ignored_and_swept`, `test_writer_alive_false_for_dead_process`.
* **Advanced identity fields loaded (P1)**: `lora_adapters`,
  `multimodal_components`, `rope_config_digest`, `speculative_config` were
  declared but never read from the config — two deployments differing only in
  LoRA/RoPE silently shared one namespace. `identity_from_env_files()` now reads
  `KVP_LORA_ADAPTERS`, `KVP_MULTIMODAL_COMPONENTS`, `KVP_ROPE_CONFIG_DIGEST`
  (64-hex enforced), `KVP_SPECULATIVE_CONFIG`; `setup-identity.sh` verifies them
  and `verify-engine-identity.sh` formats-checks them. Empty is legal
  ("not applicable"); a set value splits the namespace and `model_name`.
* **`""` vs `"none"` namespace bug**: `l2_serde=""` and `l2_serde="none"` mean the
  same raw codec but hashed into DIFFERENT namespaces. `canonical()` now
  normalizes `""`/`"none"`/`"raw"` to one namespace; `zstd`/`lz4` stay distinct.
* **Real dedup test hardened** (`tests/integration/test_l2_dedup_real.py`):
  launched via `kvpersist.launch` (full patch chain), `_wait_quiet()` waits for
  the L2 directory to be STABLE before measuring (async stores must finish), the
  shared prefix files are asserted BYTE-IDENTICAL (size+mtime) after the second
  request, and both conversations must still LOOKUP all chunks. New
  `tests/integration/test_launch_registry.py` starts the server via
  `kvpersist.launch`, confirms the `[kvp]` banners and registry dir, proves
  `purge --tmp` keeps a LIVE-marker tmp file and sweeps unregistered leftovers,
  and that completed stores clear their markers.

## Review round 17 fixes

**Critical (launch-breaking / acceptance-breaking):**
* **`--kv-cache-dtype bf16` was invalid** (verified against the pinned build's
  `CacheDType` Literal, `vllm/config/cache.py` at 8e2beaa3: accepted spellings
  are `float16`/`bfloat16`, NOT `fp16`/`bf16`). The default launch would have
  died on argument parsing. Fixed everywhere: launcher default, `identity.env`
  (EN/RU/ZH), preflight now WARNS on `bf16`/`fp16` spellings, tests and docs use
  `bfloat16`. The identity namespace changes with it (KV dtype is part of the
  hash) — a safe cache miss.
* **`test-recovery.sh` could never PASS**: the launcher did not pass
  `--enable-prompt-tokens-details`, so `usage.prompt_tokens_details` was always
  `None` and `cached_of` read `MISSING`/0. The flag is now passed for every
  model (it only affects response verbosity).
* **L2 log regexes matched nothing real**: verified against the pinned 0.5.3
  source (AST scan of every logger call). The real evidence lines are
  `"L2 store completed: adapter %d, %d succeeded, %d failed"`,
  `"L2 prefetch load completed: request %d, %d loaded, %d failed"` (both DEBUG
  level, `lmcache/v1/mp_observability/subscribers/logging/l2.py`) and
  `"Prefetch request completed (L1+L2): ... (%d L1, %d L2)"` (INFO,
  `storage_manager.py`). The old patterns matched only ERROR lines ("Failed to
  read from file..."). New regexes match the real strings, and `LMCACHE_LOG_LEVEL`
  (new knob, default `DEBUG`) makes those DEBUG lines actually reach the log —
  without it the acceptance evidence does not exist.

**Serious (documentation lied / protection weaker than claimed):**
* **Restart accounting**: the hard cap does NOT hold across a restart — the
  upstream index is rebuilt lazily and the disk is not scanned at startup, so
  accounted usage starts from zero. README and storage-management docs now say
  so explicitly (NEEDS TESTING block + required hardware test).
* **systemd path never set 0700**: `StateDirectory=` creates 0755 and the
  wrapper only did `mkdir -p`. The wrapper now enforces `chmod 700` and REFUSES
  to start without it (and tightens the parent dir when it owns it) — a real
  hole in the documented threat model (KV == chat text).
* **verify-engine-identity wording**: the gate compares CONTAINER STARTUP ARGS
  (`docker inspect .Config.Cmd`) and `config.json` — not live engine
  introspection; the environment is ignored; the connector check promised in a
  comment was unimplemented. The message now states exactly what is verified,
  and the connector check exists (compares `--kv-transfer-config` against the
  pinned `KVP_CONNECTOR`).
* **`"model":"mimo-pro"` hardcoded** in test-recovery/benchmark: replaced by
  `SERVED_MODEL_NAME` (default `mimo-pro`, matching the reference launcher).

**Minor:** `identity.env` write-target contradiction fixed in
deployment/troubleshooting (EN/RU/ZH) — the tracked file is NEVER edited,
values go to `identity.env.local`; OTLP flags are now forwarded by the systemd
wrapper (the observability advice works for systemd too); `KVP_CACHE_BYPASS`
marked DEPRECATED/unused; `KVP_PERSIST_ENABLED` semantics clarified (only
`false` is meaningful — `start-cache.sh` refuses values that contradict
persistence); `emergency-evict` documented honestly as **FIFO by mtime**, not
LRU; `budget.evict_candidates` now computes under the ledger lock (was
iterating `_usage` outside it — a race); `budget-report.sh` printed a literal
`\n` (echo does not interpret escapes); `retention.py` `now or time.time()`
treated `now=0` as "unset" (now `is None` check); fingerprint comment in
`identity.env` (EN/RU/ZH) updated to the real implementation (content SHA-256
of every shard + index, cached in `.kvpersist-shard-cache.json`,
`KVP_FINGERPRINT_MODE=fast` downgrade).

**Wording:** "lmcache does not depend on nixl" corrected (the `lmcache[nixl]`
extra exists upstream — the explicit pin is for visibility and
reproducibility); benchmarks.md criteria now list the SAME evidence strings the
script actually matches; the "+0-500 ms urllib buffering" TTFT claim removed as
unproven (iterating an SSE stream with `urllib.request` yields each complete
line as it arrives — line-buffered, not whole-body buffering; relative
comparisons with the same reader stay valid).

**Heuristic guard documented as such:** the `purge --tmp` active-write
protection is the `kvpersist.tmp_guard` registry (liveness-by-process, previous
round) PLUS the mtime window as a fallback for unpatched writers. The window is
explicitly documented as a HEURISTIC (not an absolute lock) in code and in
troubleshooting ×3.

## Review round 18 fixes

**Critical (false FAIL / false safety):**
* **Scenario A could FAIL on a healthy system**: `test-recovery.sh` hard-FAILED
  when scenario A (restart vLLM ONLY) produced zero L2 read events. But the cache
  service and its L1 (host RAM) survive that restart, so a healthy system serves
  the request from L1 and logs `(N L1, 0 L2)` — the documented "memory hit".
  A now ACCEPTS a memory hit (PASS on `cached_tokens>0` alone), the disk signal is
  informative only, and only scenario B (full restart, empty L1) must show L2 read
  events. The final-verdict "reuse without L2 evidence" check was likewise moved
  from A+B to B only. The regex requiring `(N L1, N L2)` with BOTH counters
  non-zero is kept for B's corroborating signal but is not the sole gate.
* **tmp_guard was useless in docker mode (P1)**: `start-cache.sh` docker mode
  starts the service in a container WITHOUT `--pid=host`, so the marker's PID
  lives in the container's namespace — the host CLI checked it against the HOST
  `/proc`, found no such process and treated a LIVE store as dead; only the
  15-minute mtime window remained. Fixed at the mechanism level: `register_write`
  now records the writer's PID-namespace id (`/proc/self/ns/pid`) in the marker,
  and `writer_alive` falls back to the marker's HEARTBEAT when the marker belongs
  to a foreign namespace (fresh mtime ≤ `HEARTBEAT_ALIVE_SEC` = alive, older =
  crash leftover). Same-namespace markers keep the exact /proc(pid, start-time)
  check. Old two-line markers (empty ns) keep the old behaviour. Tests:
  `test_writer_alive_falls_back_to_heartbeat_in_foreign_pid_ns`,
  `test_marker_records_writer_pid_namespace`.

**Serious (dashboards lied / scenarios raced):**
* **Grafana "Cache Hit Rate" (and 3 more panels) were empty on 0.5.3**: the
  vendored dashboards were copied from a NEWER LMCache than the pinned 0.5.3.
  Verified against the 0.5.3 source (every `create_counter`/`register_gauge` in
  `lmcache/v1/mp_observability/`): 4 of 28 panels referenced metrics 0.5.3 never
  emits — `lookup_prompt_tokens_total` / `lookup_request_hit_tokens_total`
  (Cache Hit Rate, Lookup & Engine), `num_submitted/finished_stores|retrieves`
  (Inflight GPU Copies) and `transfer_phase_*` (MP Transfer DMA). Those panels
  are remapped to the REAL 0.5.3 metrics (lookup_requested/hit, l1_read/l1_write,
  l0_l1_store_throughput). CRITICAL correction during the fix: `l1_memory_usage_
  bytes`, `l2_usage_bytes`, `num_inflight_l2_*`, `active_prefetch_jobs` and the
  `event_bus_*` gauges DO exist in 0.5.3 (registered via `register_gauge` in
  `l1_manager.py`/`storage_manager.py`/`lookup.py`) — the first patch wrongly
  replaced those too; that over-replacement was reverted. The README now says
  plainly that "official" dashboards target a newer LMCache, lists exactly which
  4 panels were remapped, and states that the **Coordinator** dashboard
  legitimately stays empty (this repo runs no coordinator; 48 of its series have
  no 0.5.3 analogue).
* **L2_bytes counted registry markers**: `.kvp-active-writes/` markers are
  transient bookkeeping, not cache data — counting them could produce a false
  "L2 shrank". Now excluded from `L2_bytes`/`find`.
* **Async L2 store raced the byte delta**: `ask cold` → `L2_bytes` had no pause,
  but stores complete asynchronously — a false "nothing was stored" was possible.
  `wait_l2_quiet()` now samples `L2_bytes` until it stops changing before the
  B0/B_WARM and B_BEFORE/B_AFTER comparisons.

**Minor:**
* **preflight only warned on `bf16`/`fp16`**: a wrong CacheDType spelling makes the
  engine refuse to start, so preflight now `die`s instead of warning and
  continuing into a doomed `docker run`.
* **Comment typo "BLOAT16"** fixed in the launcher header (BFLOAT16).
* **`ask()` heredoc became unquoted** (`<<PYEOF`): a future `$` or backtick in the
  Python block would be expanded by the shell. Restored `<<'PYEOF'` (quoted) and
  `SERVED_MODEL_NAME` is now EXPORTED so the Python block reads it from the
  environment.
* **`LMCACHE_LOG_LEVEL` default vs advice**: the config defaulted to DEBUG while
  its comment suggested INFO for production. The default is now documented as
  deliberate (DEBUG is required for the acceptance evidence lines), the value is
  read consistently by `scripts/env.sh` AND `systemd/kvp-lmcache-server` (via
  `repo_cfg`) AND forwarded into the docker container (`-e LMCACHE_LOG_LEVEL=…`)
  — all three launch modes agree.
* **Token leak check for DEBUG logs (asked by the review)**: verified by static
  audit of every `logger.debug` call in the pinned 0.5.3 — DEBUG logs carry only
  counts, sizes, session/request ids and chunk-key hashes, never token ids or
  prompt text. The only raw-key-material path is the opt-in `--lookup-hash-log-dir`
  JSONL file logger (OFF unless the operator sets it). Documented in
  docs/security.md (EN) and in the config comments (EN/RU/ZH).

## Review round 19 fixes (cross-repo coherence with sikkatu/recipes)

**P1 — cache-identity namespace did NOT work through the recipe**: the recipe
launchers mounted `MODEL_DIR` at `/model`, so LMCache received one and the same
`model_name` for every deployment (`model_config.model` = `--model` value;
verified against `lmcache_mp_connector.py:680` of 0.5.3). Different revisions,
KV dtypes and chat templates shared ONE namespace — the README claim
"incompatible deployments never read each other's KV" held only for
kv-persistence's own launcher (fixed in round 13 there). Fixed in BOTH recipe
launchers (`run-tp4-uva.sh`, `run-tp4-hotsplit.sh`, identical block): with
`KV_PERSIST=1` the model is mounted at `/models/<cache-identity namespace>`
derived from `kvpersist.cli model-name` (when a kv-persistence checkout is
found) or a deterministic fallback hash of the KV-compatibility fields
(config.json, weight indexes + shard sizes, kv dtype, max-model-len, block
size). `MODEL_DIR` becomes REQUIRED for `KV_PERSIST=1` (remote fetch cannot be
isolated per revision — the launcher refuses). Contract tests in recipes
(`test_namespace_mechanism_present`, `test_off_state_needs_no_model_dir`) pin
the mechanism and keep the plain run untouched.

**P1 — the wiki "Verify the engine geometry" step could not pass on a recipe
launch**: the recipe's defaults broke the gate two ways. Both fixed:
* the recipe defaulted `KV_CACHE_DTYPE=auto` while the pin wants `bfloat16`
  (exit 4 MISMATCH). The recipe now defaults to the vLLM `CacheDType` spelling
  `bfloat16` (still overridable) — same value as kv-persistence's launcher;
* the recipe passed no `--block-size`, so the gate read MISSING (exit 5
  INCONCLUSIVE). The `KV_PERSIST=1` branch now pins `--block-size 16`
  (overridable via `BLOCK_SIZE`). Independently, `verify-engine-identity.sh`
  now RESOLVES instead of failing: `--kv-cache-dtype auto` is resolved to the
  model's `config.json` dtype (the effective dtype the engine computes), and a
  missing `--block-size` resolves through `config.json` (`block_size` /
  `hybrid_block_size`) or the pinned vLLM default (16) instead of reporting
  MISSING — so an older recipe run without the pin no longer turns into
  INCONCLUSIVE.

**P1 — `--enable-prompt-tokens-details` missing from the recipe path**: without
it `cached_tokens` is never reported and `test-recovery.sh` cannot PASS on a
recipe-launched server (kv-persistence's final-report already called this
critical). The `KV_PERSIST=1` branch now passes it (both launchers).

**P2 — "docker args mirror run-tp4-uva.sh one-to-one" was inaccurate**:
`MAX_NUM_SEQS` was 2 vs 1, the dtype spelling differed, and extra flags / a
different model path existed. Now: `MAX_NUM_SEQS` default is 2 in the recipe
launchers (matches kv-persistence), `KV_CACHE_DTYPE` default is `bfloat16` in
both, and the header comment of `run-vllm-with-persist.sh` honestly states it
is a baseline + four documented additions, not a mirror. The wiki table in
*Recipes Integration* lists every divergence and its cache consequence: a value
difference (dtype string included) is a SAFE cache miss (the namespace hash
changes), never a wrong read — the two paths stop sharing blocks but never
corrupt each other.

**CI**: kv-persistence had NO workflow at all (the reviewer could not confirm
any test run). `.github/workflows/check.yml` now exists: offline unit + config
tests (no lmcache, no GPU needed), bash syntax of every launcher/unit, and
py_compile of every module. The recipes repo already had `check.yml` (288 tests
green with the new contract tests included).

**Test counts (review note)**: "157 unit tests" in docs was stale. Real
numbers, verified: 188 unit tests (`tests/unit`, 186 passed + 2 skipped),
19 top-level offline tests, 207 offline collected (205 passed + 2 skipped),
222 collected total (plus 318 passed of 323 collected in the
companion `sikkatu/recipes` repo). Fixed in final-report (EN/RU/ZH) and wiki
Home (EN/RU/ZH).

**Port naming across repos (review note)**: three variables name one endpoint
**TP naming across repos + TP != 4 is UNTESTED (review note)**: `TP_SIZE`
**Shared effective-identity resolver (review round 23)**: the effective
**Single source of truth for engine args (review round 24)**: the
**Smoke request follows the engine (review round 25)**: the launcher
**Cross-repo consistency (review round 26)**: (1) cache.env(.local)
**Codec check follows the server (review round 27)**: the l2_serde
**Layered config resolution (review round 28)**: (1) KVP_* aliases in
**Parser parity + end-to-end gate test (review round 30)**: the bash
**Test safety + strict env format (review round 31)**: the test fixture
**ASCII-only names + full-dict parity (review round 32)**: python
**HOME expansion parity (review round 33)**: `identity._read_env_config()`
did not expand `$HOME`/`${HOME}` at all while `config._parse_env_file`
and bash `env.sh` did -- it now expands both forms exactly like the
other two parsers. All three parsers now expand `${HOME}` BEFORE
`$HOME` (canonical safe order; note `$HOME` is not actually a substring
of `${HOME}` because of the brace, so the old order did not corrupt --
verified empirically -- but the brace-first order is trivially safe).
The parity test covers both forms plus a mixed one with a CONTROLLED
`HOME` passed to all three parser subprocesses (24 cases total).

`str.isidentifier()` accepts Unicode names (КЛЮЧ=value passed) while
bash rejects them -- both Python parsers now use an explicit ASCII
regex `^[A-Za-z_][A-Za-z0-9_]*$`, matching bash exactly. The bash
parser also trims leading whitespace BEFORE the `export` prefix now
(` export KEY=value` was rejected by bash but accepted by python).
The parameterized parity test compares the FULL parsed dict (not just
one value) and covers 20 inputs including Unicode names and the
leading-space export form. Tests never leave repo config files
modified (backup + restore; a full tmp-copy of configs/ is a documented
future improvement).

now BACKS UP and RESTORES the operator's real `identity.env.local` --
it used to DELETE it after every test run (a user who ran
setup-identity.sh lost their identity pins on `pytest`). The
end-to-end test now PROVES .local-over-base honestly (base declares
lz4, .local declares zstd, no env override, adapter is zstd -> exit 0).
All three env parsers (bash env.sh, config._parse_env_file,
identity._read_env_config) now share ONE strict format -- name must be
an ASCII identifier, value is trimmed then unquoted -- and a
parameterized test runs 18 identical inputs through all of them.

env.sh parser now cuts trailing comments at space OR tab before `#`,
byte-for-byte identical to both Python parsers (config._parse_env_file,
identity._read_env_config) -- the "MUST match" contract holds for
`KEY=value<TAB># comment` too. A new end-to-end test RUNS the real
gate with a $HOME-expanded KVP_CACHE_ROOT fallback and asserts exit 0
(.local-over-base selection, adapter discovery, codec read).

cache.env are now commented out -- an LMCACHE_* edit in
lmcache-server.env.local wins naturally (the shipped KVP_* defaults
used to override it). (2) a KVP_L2_SERDE pin in identity.env(.local)
now reaches the NAMESPACE (zstd pin -> zstd namespace), closing the
gap where the gate accepted the pin but the namespace stayed raw.
(3) bare BLOCK_SIZE is not promoted in run-vllm-with-persist.sh
(round 26 rule preserved). (4) the gate reads KVP_CACHE_ROOT from
cache.env(.local) when not exported, and the recipe's manual-gate
reminder mentions it. (5) README paragraph un-glued (EN) + KVP_L2_SERDE
note mirrored in RU/ZH.

check now reads the SERVER's actual codec from
`<cache-root>/l2-adapter.rendered.json` (written at server start), not
the gate's own shell env -- the README's `USE_LOSSLESS_SERDE=1 bash
scripts/run-vllm-with-persist.sh` works again, and a server started
with zstd in another shell IS caught (MISMATCH vs the namespace).
KVP_L2_SERDE is now documented in identity.env (EN/RU/ZH), README,
deployment.md and the recipe's tp4.env.example (3 languages).
run-vllm-with-persist.sh PROMOTES operator names (KV_CACHE_DTYPE,
MAX_MODEL_LEN, TP_SIZE, SERVED_MODEL_NAME) to their KVP_* form before
the engine starts -- an explicit `KV_CACHE_DTYPE=fp8` is no longer
silently ignored in favour of the identity.env pin (the two launchers
now start the SAME engine for the same env). The systemd wrapper
routes port/L1/chunk/eviction through repo_cfg WITH the KVP_* alias,
so cache.env.local edits work under systemd too, and CONFIG WARNING no
longer fires for conflicts the propagation already resolved.

KVP_LMCACHE_* / KVP_L1_SIZE_GB values are now PROPAGATED into the
LMCACHE_* names the cache server reads (they used to be silently
ignored -- the server started with the shipped defaults), and
config.verify() is called at startup so conflicting duplicates are
reported. (2) the identity gate CHECKS the L2 codec (l2_serde):
USE_LOSSLESS_SERDE in one shell and an unpinned identity no longer
diverge silently -- a mismatch is a MISMATCH (exit 4), pin KVP_L2_SERDE
to match the server. (3) the bare BLOCK_SIZE name is no longer read
(KV_PERSIST_BLOCK_SIZE is the recipe knob). (4) the recipe's
KV_PERSIST_PORT must match kv-persistence's LMCACHE_PORT -- documented
in tp4.env.example (3 languages). (5) run-vllm-with-persist.sh states
honestly it runs the UVA BASELINE, not hotsplit. (6) RECIPE_DIR
defaults now try ~/sikkatu-recipes/recipes (the documented layout).

passes the served-model-name EXPLICITLY to the smoke-request python
(the shell assignment is not exported, so an os.environ lookup fell
back to mimo-pro when only KVP_SERVED_MODEL_NAME was set). Regression
tests cover that case plus a combined five-parameter conflict check
(docker args + namespace + smoke body all carry the resolved values).

resolved effective values are what the ENGINE IS STARTED WITH -- the
docker args (--served-model-name/--kv-cache-dtype/--max-model-len/
--tensor-parallel-size/--block-size), the smoke request and the gate
all carry the same values, so a conflicting KVP_* export can no longer
desync namespace/gate from the running engine. The inline fallback of
the resolver picks KVP_* FIRST (identical to
resolve_effective_identity). The launcher smoke request builds its JSON
with python3 (safe encoding) and uses the ACTUAL served-model-name.

engine config (auto -> model dtype, TP/block-size/served-model-name
defaults) is resolved by ONE code path -- `python3 -m kvpersist.cli
effective-identity` -- used by the recipe launchers (namespace derivation
and `live/kvp-effective-identity.env`), `run-vllm-with-persist.sh` and the
identity gate itself. The gate folds the recipe's operator names
(KV_CACHE_DTYPE, MAX_MODEL_LEN, TP_SIZE, SERVED_MODEL_NAME) into the
comparison the same way, so a non-default launch (fp8, 524288, TP!=4)
verifies clean instead of being blocked by MISMATCH against stale pins.

(the recipe) and `KVP_TP_SIZE` (kv-persistence) are two names for ONE value
-- the tensor-parallel size. Keep them consistent when overriding (both
feed the cache-identity namespace and `--tensor-parallel-size`). A hotsplit
run NOT on TP=4 has NOT been tested: `numa_bind.py`'s GPU map examples are
calculated for 4 GPUs, so any TP != 4 is unverified territory.

— `LMCACHE_PORT`/`KVP_LMCACHE_PORT` (cache side) and `KV_PERSIST_PORT` (recipe
side). Documented explicitly in *Recipes Integration* (EN/RU/ZH) with the
"change both sides together" warning, and in the recipe README (EN/RU/ZH).

**What is still NOT proven (honesty)**: the recipe's contract tests verify the
launcher carries the machinery — they do NOT prove real NVMe recovery,
cross-restart reuse or cross-tenant isolation. That evidence comes from
kv-persistence's `scripts/test-recovery.sh` + integration tests on a GPU host
(NVMe recovery = scenario B; cross-config isolation =
`test_identity_namespace_isolates_lmcache_storage`; in-flight purge safety =
`test_launch_registry.py`). The hardware acceptance run remains NOT DONE —
see the overall status at the top.

## Review round 20 fixes (cross-repo namespace coherence + honest defaults)

**P1 — namespace did not follow the engine's real arguments** (reviewer
verified: `KV_CACHE_DTYPE=fp8` in tp4.env left the namespace at the shipped
`bfloat16` pin — fp8 KV under a bf16 namespace, i.e. a WRONG-HIT risk, not a
safe miss): the recipe's `KV_PERSIST=1` branch now passes the actual engine
values to `kvpersist.cli model-name` as `KVP_KV_CACHE_DTYPE`,
`KVP_MAX_MODEL_LEN`, `KVP_BLOCK_SIZE`, `KVP_TP_SIZE` overrides (env wins over
the shipped identity files in `identity_from_env_files`), and `auto` is
resolved to the model's effective dtype (config.json `torch_dtype`/`dtype`)
before the namespace is derived — so `auto` and explicit `bfloat16` launches of
the same model land in ONE namespace, while `fp8` lands in its own. The
fallback hash (kv-persistence not installed) now also covers tokenizer files,
the chat template and TP size — the old one missed all three. The phrase
"never a wrong read" is replaced by the precise claim (namespace derived from
actual engine args ⇒ safe miss) in README ×3, tp4.env.example ×3 and wiki.

**P2 — CLI errors were swallowed** (`2>/dev/null || true` + a misleading
"kv-persistence not found" fallback, exactly as the reviewer hit with the
shipped empty pins): the CLI's stderr is now captured and printed verbatim,
and an unusable identity ABORTS the launch (`die`) with a pointer to
`setup-identity.sh` — a wrong namespace must never be guessed into service.

**P3 — "two paths, one cache" was false by default**: the recipe passed
`KVP_SERVED_MODEL_NAME=mimo-pro` while kv-persistence's launcher used
`basename(MODEL_DIR)` (e.g. `MiMo-V2.6-Pro-MOPD`) — same hash, different
prefix, never-shared cache (the reviewer measured `mimo-pro/d2089a2f…` vs
`MiMo-V2.6-Pro-MOPD/d2089a2f…`). The specialized launcher's default now
follows `SERVED_MODEL_NAME` (the string the engine is called with), so both
paths produce the same `model_name`. Covered by
`tests/unit/test_launcher_namespace_coherence.py` (executes the real
default-expression line) and `test_served_model_name_prefix_is_stable`.

**P4 — `block_size=16` substituted as fact by the gate**: `DEFAULT_BLOCK_SIZE`
is 16 in the pinned build, but `platforms/interface.py` can pick another value
via `get_preferred_block_size` and the engine logs
"Setting kv cache block size to N". The gate no longer substitutes 16: it
reads `config.json` (`block_size`/`hybrid_block_size`) first, then the
container's engine log line, and reports INCONCLUSIVE when neither is
available (a fact we cannot observe must not be claimed). The `KV_PERSIST=1`
branch still pins `--block-size` (now via the namespaced knob
`KV_PERSIST_BLOCK_SIZE`, fixing the `BLOCK_SIZE` env-collision concern), so
the gate has a verifiable argument on that path.

**P5 — recipe defaults changed for all runs** (the "byte-identically to
before" claim was false): `MAX_NUM_SEQS` 1→2 and `KV_CACHE_DTYPE` restored to
`auto` (the VERIFIED benchmarks ran on auto). Per the operator's decision
`MAX_NUM_SEQS=2` STAYS as the default (no speed loss without a parallel
request, more convenient), so the old claim is replaced by an explicit NOTE in
README ×3 / tp4.env.example ×3: two defaults changed for all runs and how to
reproduce the old benchmark conditions exactly.

**Tests now EXECUTE the block** (the reviewer's point that string-search
tests caught none of the above): `tests/test_kv_persist_behaviour.py` runs both
launchers against stub `docker`/`nvidia-smi`/`curl`/`python3` binaries and
asserts: the namespace follows real engine args (fp8 vs bfloat16 differ), a
CLI failure aborts with the real error, `KV_PERSIST_NAMESPACE` override is
honoured, the plain run derives nothing, `KV_PERSIST_BLOCK_SIZE` reaches the
engine, the model-name prefix is stable, and `auto` resolves to the model's
dtype. It caught two real bugs during development (an unbound `ROOT` variable
in the CLI call path and the namespace-prefix desync).

* Commit hash of this report: run `git log -1 --format=%H` in the repository.
* Push credentials are NOT stored in this repository (verified: zero
  `github_pat_` occurrences in tree and in git history). Credentials used by
  maintainers follow the same rotate-after-use policy documented in
  docs/security.md.