# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 64 (one parser, auth out of ps everywhere, codec opt-out restored)

* ONE env-file parser (`_kvp_seed_env_file` in scripts/env.sh) replaces the two
  diverging `_tp4_seed` copies. The start-all copy lacked identifier
  validation and aborted on an indented comment ("  # X=1" -> "invalid
  variable name"); both callers now use the same function with the recipe's
  exact semantics. start-all also seeds tp4.env BEFORE loading the config
  layer, so `LMCACHE_L2_SERDE=lz4|none` from tp4.env is no longer silently
  ignored (lmcache-server.env used to pre-fill zstd).
* The bearer key is out of `ps` output in ALL scripts (the round-63 claim was
  incomplete): uva launchers, manage.sh and check-tp4-api.sh of all 5 recipes
  now use `curl -K <chmod-600 config>` instead of `-H "Authorization: Bearer
  ..."`; the `.curl-header` files that were written but never used are wired
  in. test-recovery.sh reads the shared `~/.config/kvp/api_key` too (works
  outside the repo directory) and keeps its header file in /tmp, not in the
  cache data dir.
* Codec opt-out restored in the recipe launchers: `export LMCACHE_L2_SERDE`
  now happens only when NONE of LMCACHE_L2_SERDE / KVP_L2_SERDE /
  USE_LOSSLESS_SERDE is set -- `USE_LOSSLESS_SERDE=0` and `KVP_L2_SERDE=none`
  reach the identity gate again (round 63's unconditional export overrode
  them with zstd).
* `kvp up` passes the remembered recipe location (`export RECIPE_DIR=`) --
  a custom clone location no longer fails with "production recipe not found".
  `kvp` only writes the saved paths on first use or when explicitly given
  (a one-off `KVP_REPO=/tmp/x kvp status` no longer rewrites the config), and
  the installer symlinks instead of copying (a `git pull` updates the command).
* hotsplit_env.sh: shebang first again (the compat pin comment was prepended
  above it).
* Tests updated to the new contracts: the codec default must yield to the
  opt-out switches, auth must go through the curl config file.

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

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
