> 🌐 Language versions: **English** | [Русский](final-report.ru.md) | [中文](final-report.zh.md)

> This page is the in-repo technical record. Full docs in three languages: [wiki](https://github.com/sikkatu/kv-persistence/wiki) | [Русский](https://github.com/sikkatu/kv-persistence/wiki/Home.ru) | [中文](https://github.com/sikkatu/kv-persistence/wiki/Home.zh)

# Final report — kv-persistence

Repository: https://github.com/sikkatu/kv-persistence (branch `main`,
commit `HEAD` (see `git rev-parse HEAD`), pushed and verified against the
GitHub remote API).

**Overall status: PARTIAL** — implementation complete, offline tests pass
(59 unit tests), architecture proven against the pinned vLLM build's source
code. Hardware acceptance tests (Tests 1–16 of the brief, the restart-recovery
evidence) are scripted and ready (`scripts/test-recovery.sh`,
`scripts/benchmark.sh`) but **have not been executed on a target GPU host
host**, because network/SSH access to the target machine was
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
bash scripts/budget-report.sh purge --namespace kvp-<ns> --yes   # explicit only

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

## NVMe bytes per cached token

Pending measurement on hardware (formula: `used_bytes ÷ persisted tokens`,
reported by `benchmark.sh` + `budget-report.sh status` per run). Theoretical
footprint for the current dtype (bf16) is
`2 x num_kv_heads x head_dim x 2(K,V) x layers` bytes/token of TOTAL KV (per-token across all TP ranks; NVMe stores the whole tensor, NOT per-rankdivided). For hybrid-attention models per-group geometry differs —
layer/head counts for MiMo-V2.6-Pro are recorded from the model config during
the hardware run, before any capacity promises are made.

## Cache budget and observed capacity

Configured: 1500 GB default (hard), bound = free space on the shared VM disk (same volume holds Docker, the model and the OS) (preflight-verified). Observed usage:
pending hardware run. Bytes-per-token estimate will be filled from real
measurements (per the brief: do not promise a fixed number of conversations
before measuring).

## Eviction and deduplication test results

* Eviction policy semantics: **VERIFIED offline** (56 unit tests, incl. stale
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

## Provenance

* Commit hash of this report: run `git log -1 --format=%H` in the repository.
* Push credentials are NOT stored in this repository (verified: zero
  `github_pat_` occurrences in tree and in git history). Credentials used by
  maintainers follow the same rotate-after-use policy documented in
  docs/security.md.