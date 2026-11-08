# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


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

## Round 57 (verdict + docs honesty)

* The recovery-acceptance verdict is **PASS** in docs/final-report.*: the
  cache device auto-detection (round 55) makes the independent NVMe byte
  counters available in the same run -- no manual `KVP_CACHE_DEVICE` needed
  (override remains available).
* CHANGELOG no longer carries verbatim request quotes -- changes are
  described directly.
* Documentation for `start-all.sh` / `stop-all.sh` and the shared API key
  (`$HOME/.config/kvp/api_key`) added to both repos' READMEs (EN/RU/ZH).

## Round 56 (shared API key + dynamic hotsplit by default)

* ONE API key for BOTH repos: both launchers (kv-persistence + recipes run-tp4-hotsplit.sh /
  run-tp4-uva.sh) now read and write the canonical file
  `$HOME/.config/kvp/api_key` (override: `KVP_API_KEY_FILE=`). A legacy
  per-repo `live/api_key` is migrated on first use and kept as a mirror
  for older tooling.
* Dynamic hot/cold experts by default in kv-persistence: the launcher now mounts the recipe's `/w` machinery
  (hotsplit.py, hotsplit_plan.py, numa_bind.py, sitecustomize.py,
  expert_hist_mix.json) and passes `PYTHONPATH=/w` + the recipe's
  HOTSPLIT_* defaults (HOTSPLIT_DYNAMIC=1, HOT_GIB=30). Opt out:
  `KVP_HOTSPLIT=0`.
* CHANGELOG trimmed to the last 3 rounds -- older notes live in the git
  log of `main`.

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
