# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


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

## Round 55 (KVP_CACHE_DEVICE auto-detection)

* `KVP_CACHE_DEVICE` no longer needs manual guessing (VM + LUKS friendly): a shared helper
  `_kvp_resolve_cache_device` in env.sh resolves the backing device from
  `live/cache_device` (remembered) or `findmnt SOURCE` of the cache root --
  handles VM disks (`/dev/vda1`), LUKS mapper nodes (`/dev/mapper/cryptroot`,
  resolved via readlink) and NVMe partitions. Used by test-recovery.sh and
  preflight.sh; an explicit pin still wins. The choice is recorded in
  `live/cache_device`.
* docs/final-report.md leftovers cleaned: LMCache runtime row, version-pin
  row and the serde end-to-end row are now VERIFIED (hardware 2026-11-07).

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
