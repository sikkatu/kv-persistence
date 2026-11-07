> 🌐 Language versions: **English** | [Русский](benchmarks.ru.md) | [中文](benchmarks.zh.md)

# Benchmarks

Measurement rules: every number must state request conditions (cold/warm,
process state, cache state). A speedup WITHOUT proof of persistent disk recovery
does NOT satisfy the primary milestone.

## Test evidence levels (what each test proves)

Three levels are kept strictly separate — never read one as another (review P1-2):

| Level | Test / script | Proves | Does NOT prove |
|---|---|---|---|
| 1. File-level smoke | `tests/integration/test_l2_persistence.py::test_l2_file_survives_server_restart` | manually written files survive process termination | that LMCache can re-read its own objects |
| 2. LMCache persistence round-trip | `tests/integration/test_l2_persistence.py::test_lmcache_store_restart_retrieve_roundtrip` | LMCache stores through its real MP protocol, the first server process is fully stopped, a **new** process on the same L2 directory retrieves the pre-restart objects and the chunk checksums match | that vLLM + MiMo-V2.6-Pro reuses the cache for a live chat |
| 3. Full vLLM acceptance | `scripts/test-recovery.sh`, `tests/recovery/` | a real vLLM workload reuses persisted KV after the intended restarts (the project acceptance criterion) | — |

### Level 2: LMCache persistence round-trip (synthetic KV, no vLLM, no GPU)

This is the test that proves that **LMCache itself** can recover data from the
L2 tier after a full server restart. It does not need vLLM, a model or a GPU.

* **Pinned version / API**: LMCache **0.5.6** (pin in `requirements-lmcache.txt`,
  matches the pinned vLLM image; mechanism notes below were verified on 0.5.6).
  The test drives the real MP server protocol through the pinned CLI
  `lmcache bench server` (`--mode cpu --transfer-mode engine_driven`):
  `REGISTER_KV_CACHE_ENGINE_DRIVEN_CONTEXT`, `LOOKUP`,
  `QUERY_PREFETCH_STATUS`, `PREPARE_STORE`/`COMMIT_STORE`,
  `PREPARE_RETRIEVE`/`COMMIT_RETRIEVE` over ZMQ. Cache identity is token-hash
  based and deterministic across invocations (`IPCCacheServerKey.request_id`
  is excluded from key equality), so the same workload ids address the same
  objects in a later run. Per-chunk MD5 of the KV bytes is reported as
  `CHECKSUM: <digest> (N chunks)`. Note: `--l2-adapter` takes the adapter spec
  as a JSON **string**, not a file path (the `add_l2_adapters_args` CLI).
* **Dependencies**: Python 3.12, Linux with POSIX SHM (`/dev/shm`), the pinned
  `lmcache==0.5.6` plus `nixl==1.5.2` (the `nixl_store_dynamic` L2 adapter
  imports `nixl`), torch (CPU build is enough), pytest. GPU, CUDA and vLLM are
  NOT required.
* **Command**:
  ```bash
  python3 -m venv ~/.venvs/lmcache
  ~/.venvs/lmcache/bin/pip install -r requirements-lmcache.txt
  export PATH="$HOME/.venvs/lmcache/bin:$PATH"
  KVP_LIVE_LMCACHE=1 python3 -m pytest tests/integration/test_l2_persistence.py \
      -k roundtrip -v
  ```
* **What the test does**: renders the repository's L2 adapter template into an
  isolated `tmp_path` cache root (production settings preserved: POSIX backend,
  `persist_enabled=true`), starts a real `lmcache server` on dynamically
  allocated ports, stores a deterministic multi-chunk synthetic KV workload
  through the real protocol, waits for the adapter's own `*.bin` data files to
  appear, terminates the server process completely, starts a **fresh** process
  on the same L2 directory and retrieves the pre-restart objects — with ZERO
  new stores after the restart. The warm-pass chunk checksum (computed after
  the client zero-filled its buffers and RETRIEVE wrote the data back) must
  equal the pre-restart ground-truth checksum.
* **PASS evidence**: `LOOKUP: N/N chunks hit` on the fresh server,
  `RETRIEVE: retrieved`, no `STORE: stored` line after the restart, matching
  `CHECKSUM` digests, and a negative control (same workload, empty L2
  directory) reporting `0/N` chunks — so a full hit cannot be a false positive.
* **Failure inspection**: every server's stdout/stderr and every bench output
  is appended to `<tmp_path>/roundtrip.log`; failure messages name the failed
  step (startup / store / stop / new startup / cache miss / retrieve / checksum
  mismatch) and end with the log tail. Rerun with `-s` to see the PASS summary,
  or use `pytest --basetmp=/some/dir` to keep the temporary directory.
* **Scope limitation**: the workload is synthetic KV tensors. This test proves
  level 2 only; the vLLM + MiMo-V2.6-Pro + `MAX_NUM_SEQS=2` Operit-chat scenario
  must be proven separately by `scripts/test-recovery.sh` (level 3).

### Level 2b: per-identity isolation at the LMCache API (no vLLM, no GPU)

Proves that the kv-persistence identity namespace separates incompatible
configurations in REAL LMCache storage — not only in local labels.

* **Mechanism (verified in the pinned 0.5.6 source + live run)**: the LMCache cache
  key (`ObjectKey.model_name`) also feeds the `nixl_store_dynamic` L2 file
  name (`<safe_model_name>_<kv_rank>_<group>_<chunkhash>.bin`). The namespace
  is embedded into that `model_name` via `CacheIdentity.lmcache_model_name()`,
  so any identity change moves the objects to a different key AND a different
  file on disk.
* **Command**:
  ```bash
  KVP_LIVE_LMCACHE=1 python3 -m pytest \
      tests/integration/test_l2_persistence.py -k identity_namespace -v
  ```
* **What it proves**: the same tokens are stored under identity A's
  `model_name`; a lookup under A finds N/N chunks; for EACH single identity
  field changed (model_id, model_revision, tokenizer_*, kv_cache_dtype,
  block_size, num_kv_heads, head_dim, tp_size, connector_version,
  max_model_len, chat_template_digest) the changed identity gets `0/N`
  chunks at the LMCache API. `namespace_hint` is asserted NOT to change
  anything (it is a human label).
* **cache_salt gap RESOLVED at the disk-key level (round 15)**: upstream
  pre-0.5.6 upstream omits the salt from the `nixl_store_dynamic` file name -- a measured
  cross-tenant leak (store salt=tenantA -> lookup salt=tenantB = 4/4 chunks
  after restart). This repo's `kvpersist/salt_filename_patch.py` appends the
  salt to the L2 file name; ALL launchers start the server through it.
  Verified live: store salt=tenantA -> lookup salt=tenantB = 0/4
  (`test_cache_salt_isolates_l2_storage`, which now REQUIRES isolation and
  fails loudly if the leak reappears). Running a bare `lmcache server`
  WITHOUT the patch keeps the upstream behaviour: do NOT rely on `cache_salt`
  for isolation then -- use `lmcache_model_name(extra_discriminator=...)`
  or a separate cache root per tenant.

## Matrix (5 configurations × 3 prompt scales)

| # | Configuration | What it isolates |
|---|---|---|
| 1 | Baseline: in-memory prefix caching only (production launcher) | reference TTFT/prefill |
| 2 | Persistent cache cold (lmcache attached, cache empty for this prefix) | connector+store overhead |
| 3 | Persistent cache warm, same vLLM process | in-memory L1/L2 hits |
| 4 | vLLM restarted, cache service alive | recovery from L2 → L1 |
| 5 | BOTH services restarted, NVMe data kept | full persistence (the acceptance criterion) |

Prompt scales: synthetic 32K / 128K / 256K tokens (`scripts/benchmark.sh
<scenario> <tokens>`), plus real long chat-app histories when available
(`implementation/prompt_probe.py` quantifies the stable prefix first -- dynamic
values like timestamps are NOT part of the reusable prefix). 1M prompts are run
only if the hardware window allows meaningful execution.

**Acceptance criteria for "100k-1M tokens in seconds"** (must ALL hold on hw):
1. TTFT of a re-send after scenario B under `MAX_MODEL_LEN=1M` config is measured
   (median of >=3 runs) and lower than the cold-cache baseline by a stated factor.
2. `cached_tokens > 0` for post-restart reuse, L2/disk read events observed, and
   physical NVMe byte-count increase during the send confirms bytes came back
   from disk rather than from the engine's own RAM.
3. L1 (`KVP_L1_SIZE_GB`) must exceed the largest single reuse prefix in bytes,
   else correct recovery happens but TTFT improvement will plateau in the L1-queue
   range only -- see docs/architecture.md.

## Metrics collected per run

`prompt_tokens`, `cached_tokens` (response `usage.prompt_tokens_details` —
requires the `--enable-prompt-tokens-details` engine flag, which
`scripts/run-vllm-with-persist.sh` passes for every model),
`ttft_s`, `total_s`, cache bytes before/after (`budget-report.sh status`),
LMCache log counts (the L2 store/load evidence lines -- `l2_stored` /
`l2_retrievals` in `scripts/test-recovery.sh` match the pinned 0.5.6 wording:
`L2 store completed: adapter N, N succeeded, N failed`,
`L2 prefetch load completed: request N, N loaded, N failed`,
`Prefetch request completed (L1+L2): N/N retained keys (N L1, N L2)` -- DEBUG
level, so `LMCACHE_LOG_LEVEL=DEBUG` is required for them to appear),
VRAM/host RAM peaks
(`nvidia-smi`, `/proc/meminfo` during run), errors/fallbacks.
**TTFT measurement caveat:** `benchmark.sh`'s inline Python iterates the SSE
stream with `urllib.request`, which yields each complete line as soon as it
arrives (line-buffered iteration, not whole-body buffering) -- so there is NO
documented systematic offset from this reader alone. If absolute TTFT accuracy
matters (regression tracking across versions), cross-check against a raw-socket
or `aiohttp` reader and record the delta instead of assuming one. For
cache-hit vs cache-miss comparisons measured with the SAME reader, any
instrumentation bias cancels out -- relative TTFT differences remain valid.

## How to run

```bash
# config 1: rollback to production launcher first (docs/deployment.md)
bash scripts/benchmark.sh baseline 32768
bash scripts/benchmark.sh baseline 131072
bash scripts/benchmark.sh baseline 262144

# config 2-3: persistent cache on
bash scripts/start-cache.sh
export IMAGE=... MODEL_DIR=...
bash scripts/run-vllm-with-persist.sh
bash scripts/benchmark.sh cold 32768      # first send, cache empty
bash scripts/benchmark.sh warm 32768      # second send, same process

# config 4: restart vLLM only
docker restart mimo-pro-tp4-kvp && sleep <until-ready>
bash scripts/benchmark.sh after-vllm-restart 32768

# config 5: restart both (acceptance)
bash scripts/test-recovery.sh             # scripted A/B scenario
bash scripts/benchmark.sh after-both-restart 32768
```

Repeat each configuration ≥ 3 times and record medians; results land in
`benchmarks/results/<UTC-ts>/bench.jsonl` (append-only JSON lines).

## Results

Not yet recorded on target hardware (KV-persistence validation host access is
required and is planned once SSH/VPN connectivity to the target host is available).
The automatic recorder emits one JSON object per request — copy
`benchmarks/results/*/bench.jsonl` here after the run and fill the table below.

| Config | Scale | Prompt tokens | cached_tokens | TTFT (s) | Total (s) | Cache bytes Δ | n |
|---|---|---|---|---|---|---|---|
| 1 baseline | 32K | – | – | – | – | – | – |
| 2 cold | 32K | – | – | – | – | – | – |
| 3 warm same-process | 32K | – | – | – | – | – | – |
| 4 after vLLM restart | 32K | – | – | – | – | – | – |
| 5 after both restarts | 32K | – | – | – | – | – | – |
| … 128K / 256K rows … | | | | | | | |

## Cache-outcome taxonomy (what a run must distinguish)

Successful generation ≠ successful caching. Every acceptance run classifies each
request into EXACTLY one outcome — `scripts/test-recovery.sh` enforces this in
its checks and its final verdict:

| Outcome | Evidence required | Counts as success? |
|---|---|---|
| **PERSIST hit** | After a restart, log events matching the L2 read/store patterns (`l2_retrievals` / `l2_stored` in test-recovery.sh: `L2 prefetch load completed: request N, N loaded`, `Prefetch request completed (L1+L2): ... (N L1, N L2)`, `L2 store completed: adapter N, N succeeded` -- DEBUG level, `LMCACHE_LOG_LEVEL=DEBUG`) + `cached_tokens > 0` (if reported) + no L2 rewrite of same data | YES — the project's purpose |
| Memory (L1/RAM) hit | `cached_tokens > 0`, no post-restart L2 evidence | NO (not persistence proof) |
| Plain prefix-cache hit | engine `cached_tokens > 0` without LMCache events | NO |
| Cache miss (recompute) | no reuse, fresh prefill | expected on cold runs |
| Incompatible data | different namespace, clean miss | correct, safe behaviour |
| Read/recovery error | adapter/connector error lines, `fail` check | NO — hard failure |

Rule: only PERSIST hits prove the project works. The verdict script fails with
exit 1 when a scenario's post-restart request shows reuse WITHOUT L2 evidence —
that combination points at RAM/prefix-cache only and must not be published as
disk-backed recovery.

## Physical evidence of NVMe-sourced reads (not just log text)

Log parsing alone (words like `L2 store completed` / `L2 prefetch load
completed`) is inherently version-dependent
and was flagged in review as a risk of false PASS. `test-recovery.sh` therefore
CORROBORATES log evidence with an independent physical signal: the read-sector
counter of the shared VM disk (same volume holds Docker, the model and the OS) device (`KVP_CACHE_DEVICE`, read from
`/sys/block/<dev>/stat` field 3) measured across each scenario. A scenario that
shows cache reuse with ZERO physical NVMe bytes read cannot credit L2 as the
source.

When `KVP_CACHE_DEVICE` is not set or the counter is unreadable, the run is
**INCONCLUSIVE** (exit 5): neither PASS nor FAIL, because an unproven disk
source must never be reported as success. INCONCLUSIVE is resolved by fixing
the evidence sources (set `KVP_CACHE_DEVICE`, provide `MODEL_DIR`) and
rerunning — not by lowering the bar.

## Acceptance verdict rule

PASS for the primary milestone requires ALL of:

1. `cached_tokens > 0` on the request following each restart (tests 4 and 5);
2. LMCache log shows L2/disk-sourced read events (the retrieval pattern:
   `L2 prefetch load completed: request N, N loaded` / `Prefetch request completed
   (L1+L2): ... (N L1, N L2)` -- DEBUG level) after
restart (not only L1);
3. TTFT/prefill measurably lower than the comparable cold baseline;
4. output text remains correct (manual spot-check; KV reuse must not corrupt
   generation);
5. cache bytes on NVMe unchanged across the restarts (proving data was reused,
   not rewritten).

If a row is missing conditions (e.g. cache was not actually cold), rerun before
concluding.