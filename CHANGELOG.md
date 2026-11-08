# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 66 (small fixes + decompression cap + launcher parity)

* Small fixes (separate commit per the reviewer): test-recovery.sh key
  priority is now env > shared canonical key > local mirror (an explicit
  API_KEY is never overwritten); `kvp_curl` escapes `\` and `"` before the
  curl config file so ANY key bytes reach the server intact (a raw quote or
  backslash used to be silently mangled); the dead `AUTH=()` in
  check-tp4-api.sh is gone; the env.sh doc comment is split cleanly.
* Decompression size cap in compress_serde (deferred item 1): before running
  the codec, `plain_len` is checked against the destination buffer AND a
  configurable ceiling (`KVP_DECOMPRESS_MAX_BYTES`, default 1 GiB); zstd
  gets `max_output_size`, lz4 the streaming `max_length` bound. A lying
  plain_len or a decompression bomb now raises a clean ValueError (cache
  MISS) with no unbounded allocation. Codec errors are wrapped into
  ValueError (the cache-MISS contract). 6 new tests incl. a real zstd bomb.
* Launcher parity test (deferred item 2, step 1): `tests/test_launcher_parity.py`
  runs BOTH engine launchers against a stub docker and compares the final
  `docker run` -- caps, compilation-config, /w mounts, /live mount and the
  key via --env-file must match. It caught (and this commit fixes) a silent
  divergence: the KV launcher's readiness loop was hard-coded (`seq 1 300`,
  `sleep 10`) and ignored `WAIT_TRIES`/`WAIT_SLEEP` that the recipe launcher
  honours -- now both honour them.
* The reviewer's repro-round65.sh is now `tests/test_manage_check_http.py`
  (scenarios A-F: check-tp4-api with/without key, status with right/wrong
  key, dead server exit 0, no-auth mode, special-character keys).

## Round 65 (kvp_curl: key on a pipe -- no file, no ps, no unbound vars)

* Round 64's `.curl-header` file approach caused three regressions -- all
  fixed by the reviewer's suggestion: NO key file at all. A tiny `kvp_curl()`
  sends the bearer key on a stdin pipe (`printf | curl -K -`; printf is a
  bash builtin, invisible in `ps`) and runs plain `curl` when no key is set
  (the documented ALLOW_NO_AUTH loopback mode).
* manage.sh / check-tp4-api.sh no longer die with "LIVE_DIR: unbound
  variable" (they never define LIVE_DIR; the round-64 `-K "$LIVE_DIR/..."`
  referenced it under `set -u`).
* The uva no-auth mode works again: without a key the old code called
  `curl -K <missing-file>` (exit 26), so the readiness loop could never
  succeed. `kvp_curl` sends no header when no key is set.
* manage.sh `status` with a stopped server no longer exits 7 under `set -e`
  (long-standing bug: `code="$(curl ...)"` killed the script before the
  "not reachable" branch). The assignment is guarded.
* test-recovery.sh: the mktemp header file is removed via `trap ... EXIT`
  (no key material left in /tmp), and the shared canonical key
  (`~/.config/kvp/api_key`) now WINS over a stale `$PWD/live/api_key`.
* kv-persistence env.sh gained the same `kvp_curl()` helper; the engine
  launcher uses it for the readiness loop (no `.curl-header` file anywhere).
* Tests updated to the kvp_curl contract.

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

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
