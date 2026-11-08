# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 63 (parser parity, key out of ps, /live mount, kvp CLI)

* `_tp4_seed` in the engine launcher now has the recipe parser's exact
  semantics: `$HOME`/`${HOME}` are expanded, surrounding quotes stripped, inline
  comments cut only at " #", and non-identifier lines (an indented comment)
  are skipped instead of aborting with "invalid variable name".
* The API key no longer appears in `ps` output (all 5 recipe launchers, the
  KV launcher and test-recovery.sh): it travels via `docker --env-file`
  (chmod 600) and `curl -K` config files instead of `-e VLLM_API_KEY=<value>`
  and `-H "Authorization: Bearer ..."` command-line arguments.
* `/live` is mounted in the KV launcher (`$ROOT/live:/live`) so the routing
  counters persist; the activation check now also fails on
  "live snapshot failed" and verifies `HOOK STATUS numa_bind=ACTIVE` when
  NUMA binding is on.
* Key migration actually writes the canonical `~/.config/kvp/api_key` (the
  "migrate below" promise in `_kvp_load_api_key` was never implemented -- two
  repos kept using different keys).
* `USE_LOSSLESS_SERDE`/`LMCACHE_L2_SERDE` from tp4.env now reach the single
  codec decision (start-all seeds tp4.env BEFORE resolving), and a
  user-supplied `VLLM_EXTRA_ARGS` no longer silently removes the MiMo
  parsers (opt out: `KVP_MIMO_PARSERS=0`).
* `KVP_COMPAT_VERSION` pin file added to kv-persistence (the recipe launcher
  already checked it); `KVP_REPO`/`KVP_REPO_ROOT` are both accepted.
* `kvp` CLI on PATH (`scripts/install-kvp.sh` -> `~/.local/bin/kvp`):
  `kvp up|down|status|logs`, repo locations remembered in `~/.config/kvp/`.
* Stale docs fixed (README EN/RU/ZH + lmcache-server.env comments): omitting
  the codec now gets lossless zstd, raw needs an explicit `LMCACHE_L2_SERDE=none`.
* Tests: recipe launcher tests assert the env-file contract (the key must NOT
  be a command-line argument) and follow `~/recipes` as a lookup layout.

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

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
