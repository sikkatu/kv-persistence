> 🌐 Language versions: **English** | [Русский](benchmarks.ru.md) | [中文](benchmarks.zh.md)

# Benchmarks

Measurement rules: every number must state request conditions (cold/warm,
process state, cache state). A speedup WITHOUT proof of persistent disk recovery
does NOT satisfy the primary milestone.

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

`prompt_tokens`, `cached_tokens` (response `usage.prompt_tokens_details`),
`ttft_s`, `total_s`, cache bytes before/after (`budget-report.sh status`),
LMCache log counts (`Stored`/`Retrieved` lines), VRAM/host RAM peaks
(`nvidia-smi`, `/proc/meminfo` during run), errors/fallbacks.

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
| **PERSIST hit** | After a restart, log events matching the L2-read pattern (`retriev\|loaded\|restore` x `L2\|disk\|secondary\|file` -- see test-recovery.sh) + `cached_tokens > 0` (if reported) + no L2 rewrite of same data | YES — the project's purpose |
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

Log parsing alone (words like `Retrieved`/`L2`) is inherently version-dependent
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
2. LMCache log shows L2/disk-sourced read events (matches the retrieval pattern: 
   `retriev|loaded|restore` with `L2|disk|secondary|file`) after
   restart (not only L1);
3. TTFT/prefill measurably lower than the comparable cold baseline;
4. output text remains correct (manual spot-check; KV reuse must not corrupt
   generation);
5. cache bytes on NVMe unchanged across the restarts (proving data was reused,
   not rewritten).

If a row is missing conditions (e.g. cache was not actually cold), rerun before
concluding.