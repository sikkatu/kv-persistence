# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 60 (CI module path, BOOT_HOOK equivalence, full validation)

* kv-persistence CI now checks out `sikkatu/recipes` (path: `recipes`) and the
  test helper resolves the shared `hotsplit_env.sh` module relative to the
  checkout FIRST (developer home paths remain as fallbacks) -- the two tests
  that failed on every CI run now read the real module.
* `HOTSPLIT_BOOT_HOOK` is no longer forced to 1: with the model patches
  mounted the replaced `mimo_v2_hotsplit.py` already calls
  `hotsplit.apply_hotsplit`, so the launcher defaults the boot hook to 0 --
  the same decision the recipe launcher makes (no double wrapper on the same
  loader). The activation health check follows the configured value:
  `boot_hook=INACTIVE` is the CORRECT state at 0.
* Host-side HotSplit validation now covers every numeric knob the shared
  module passes through: DYN_STEPS, DYN_MAX_SWAPS, DYN_PRIOR_TOKENS,
  MAX_TOKENS, TOPK, LIVE_DECODE_MAX, DYN_MAX_PLAN_ERRORS,
  DYN_MAX_COMMIT_ERRORS, VRAM_RESERVE_GIB, LIVE_SECS (the last six were not
  validated before). Regression tests lock all three fixes.

## Round 59 (HotSplit launcher equivalence + CI/contract fixes)

* The kv-persistence engine launcher now runs the FULL hardware-verified
  HotSplit profile (P1): `--cap-add IPC_LOCK/SYS_NICE`,
  `--compilation-config FULL_DECODE_ONLY` and the complete `HOTSPLIT_*` env
  (NUMA_MAP, LIVE_COUNTS/SECS, DYN_STEPS/MAX_SWAPS/MARGIN/DECAY/
  PRIOR_TOKENS, MAX_TOKENS, ...) are no longer missing. ONE shared module
  (`scripts/hotsplit_env.sh` in the recipe) builds the env + docker flags for
  BOTH launchers -- no hand-maintained duplicate lists to drift apart.
* Cross-repo contract tests actually run in CI (P1): the workflow now
  checks out kv-persistence as a second repository, so the 6 contract tests
  compare real files instead of skipping.
* The recipes delegator (`scripts/start-all.sh`) exports `RECIPE_DIR` pointing
  at its own recipe checkout (P1) -- launches work from arbitrary clone
  locations, no `~/sikkatu-recipes` guessing.
* The engine launcher's header comment no longer claims "UVA BASELINE only"
  (P2) -- it describes the actual hotsplit-by-default behaviour.
* Host-side HotSplit config validation runs BEFORE the 13-16 minute model
  load (bad HOT_GIB / DYN_* / NUMA_MAP values fail fast), and a mandatory
  activation health check runs after `--v1/models` answers: boot hook ACTIVE,
  dynamic hook installed, live counters present, no empty-bank or transaction
  errors. HTTP 200 alone is no longer treated as proof the hot experts work.

## Round 58 (one config layer + shared-key regression fix)

* Shared API key regression fixed (recipes): the launcher now ALWAYS mirrors
  the canonical `$HOME/.config/kvp/api_key` into `live/api_key` on every
  launch, so `manage.sh` / `check-tp4-api.sh` never report "requires a key"
  when the key exists. All five recipe launchers use the same key
  architecture; helpers read the canonical file first and no longer need
  `tr`/`head` (pure-bash reads).
* ONE codec decision: `start-all.sh` resolves the effective L2 codec ONCE
  (the shared fold) and hands the same value to the cache and the engine --
  a codec pin in identity.env can no longer split the two sides into
  different codecs.
* ONE config layer: `start-all.sh` loads cache.env / lmcache-server.env /
  identity.env BEFORE any decision -- the cache-PID probe uses the effective
  `KVP_CACHE_ROOT`, so a cache on a custom path is detected as running.
  `run-vllm-with-persist.sh` now seeds the recipe's `tp4.env` (environment
  wins over file), so HOTSPLIT_* / MAX_MODEL_LEN / MAX_NUM_SEQS / MOE_BACKEND
  reach the engine when launched via `start-all.sh`.
* ONE entry point: `scripts/start-all.sh` in recipes delegates to
  kv-persistence -- the same command works from any working directory.
  `run-tp4-hotsplit.sh` remains the independent recipe-only launcher.
* Cross-repo contract test (`tests/test_cross_repo_contract.py`, recipes):
  codec semantics, lmcache version pin (0.5.6), ports, namespace pins and the
  mounted HotSplit files must agree in both repositories -- a change in one
  repo can no longer silently break the other.
* Compression documentation harmonized (EN/RU/ZH + config comments): lossless
  zstd is the DEFAULT everywhere, lz4 is the alternative, byte-exact raw is an
  explicit opt-out (`LMCACHE_L2_SERDE=none`) only.

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
