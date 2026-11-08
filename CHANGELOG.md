# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 62 (float knob ranges + fresh CI counts)

* The float knob validation now enforces the plan code's REAL ranges on the
  host: `HOTSPLIT_DYN_DECAY` must be 0..1 and `HOTSPLIT_DYN_MARGIN` >= 1
  (hotsplit.py `_dyn_float` bounds). Before, only "is a number" was checked,
  so `DYN_DECAY=2` / `DYN_MARGIN=0.5` passed host validation and were rejected
  inside the container after the model load. Pure-bash range arithmetic (no
  external tools, no fail-open).
* CI counts refreshed after the cross-repo path fix: recipes CI is now
  **336 passed + 7 skipped** (was 330+13 in the round-61 note); kv-persistence
  CI unchanged at 344 passed + 4 skipped. Docs cite the current figures.

## Round 61 (cross-repo CI path, boolean equivalence, compatibility probe)

* Cross-repo contract tests in the recipes CI actually execute now: the test
  looks for the kv-persistence checkout INSIDE the workspace first
  (`RECIPES/kv-persistence`, the GitHub Actions layout) before falling back to
  developer home paths. The six contract tests no longer skip in CI.
* Boolean HotSplit knobs are normalized on the KV launcher exactly like the
  recipe launcher (`normalize_bool`: true/yes/on -> 1, false/no/off -> 0)
  before the shared module is sourced. The HotSplit components compare
  against the string "1" (`HOTSPLIT_NUMA == "1"`), so a raw `true` used to
  silently disable the feature. Covers DYNAMIC / DRYRUN / NUMA /
  ALLOW_STATIC_FALLBACK / STRICT_HOOK / NUMA_STRICT / DYN_LOG.
* The KV launcher now runs the SAME model-patch compatibility probe as the
  recipe launcher (base_loader seam, model_loader.utils definition,
  mimo_v2/mimo_v2_omni `load_weights`) BEFORE mounting the replacements,
  plus provenance SHA-256 of the shadowed originals. A changed image digest
  is caught before the 13-16 minute model load, not after.
* Validation completeness (P2): `HOTSPLIT_HOST_FLOOR_GIB` and
  `HOTSPLIT_VRAM_RESERVE_GIB` are validated as non-negative floats; integer
  knobs carry ranges (DYN_STEPS/TOPK/LIVE_SECS/DECODE_MAX/error limits >= 1,
  DYN_MAX_SWAPS 1..1024 -- the plan code's own bounds); MAX_TOKENS /
  DYN_PRIOR_TOKENS allow 0.
* Test-count honesty: sandbox runs (346+2 / 338+5) and CI runs (344+4 /
  336+7) differ because CI lacks optional dependencies (wiki checkout, lz4
  bindings). The docs cite the CI figures; both suites are green.

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

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
