# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 67 (codec cap = plain_len, one shell library, parity trio)

* Decompression cap fixed (the reviewer's finding): the codec output bound is
  now the frame's **plain_len** (already validated against the destination
  buffer and the KVP_DECOMPRESS_MAX_BYTES ceiling), NOT the ceiling. A frame
  lying downward (2 KiB declared, a bomb inside) can no longer inflate to the
  ceiling first. plain_len == 0 gets a 1-byte bound (zstandard reads
  max_output_size=0 as "not set"). Verified by a spy test that asserts the
  codec receives plain_len, and an empty-frame test.
* ONE shell library: `scripts/kvp-common.sh` per recipe carries `kvp_curl`
  (key on a pipe, escaped) and `_kvp_seed_env_file` (the tp4.env parser).
  The 12 inline `kvp_curl` copies and the 2 inline parser blocks are gone --
  every script sources the shared file. Recipes stay standalone (copy per
  recipe) and `test_cross_repo_contract.py` enforces BYTE-IDENTICAL copies;
  a second test keeps kv-persistence's scripts/env.sh functions in sync
  (the escape lines must match exactly).
* Parity test now checks the review's explicit trio on BOTH launchers: the
  `/live` counters mount, `--env-file` key transport and NO key in argv.
  Plus the shared HOTSPLIT_* env and the caps/compilation-config profile.

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

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
