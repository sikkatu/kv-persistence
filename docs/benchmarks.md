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
<scenario> <tokens>`), plus real Operit histories when available
(`implementation/operit_probe.py` quantifies the stable prefix first — dynamic
values like timestamps are NOT part of the reusable prefix). 1M prompts are run
only if the hardware window allows meaningful execution.

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

## Acceptance verdict rule

PASS for the primary milestone requires ALL of:

1. `cached_tokens > 0` on the request following each restart (tests 4 and 5);
2. LMCache log shows `Retrieved` entries sourced from the L2/disk tier after
   restart (not only L1);
3. TTFT/prefill measurably lower than the comparable cold baseline;
4. output text remains correct (manual spot-check; KV reuse must not corrupt
   generation);
5. cache bytes on NVMe unchanged across the restarts (proving data was reused,
   not rewritten).

If a row is missing conditions (e.g. cache was not actually cold), rerun before
concluding.