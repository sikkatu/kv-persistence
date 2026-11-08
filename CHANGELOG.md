# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 68 (lz4 boundary fix, behavioural parser test)

* lz4 boundary bug (review): `max_length=cap - len(out)` became 0 at the cap
  -- and 0 means UNLIMITED in the lz4 streaming API, so a 100 MiB bomb came
  back in a single unbounded call. Fixed to `cap + 1 - len(out)` (never <= 0
  while len(out) <= cap; the extra byte trips the overflow check). An honest
  frame is unaffected. The forwarding test's stub now records every call and
  asserts max_length > 0 and chunk <= cap+1 on bomb, boundary and honest
  paths.
* Behavioural parser test (review: substring checks cannot catch drift):
  `tests/test_parser_behaviour.py` runs BOTH parsers (kv-persistence env.sh
  and the recipe's kvp-common.sh) on the SAME tp4.env fixture and compares
  the resulting environments key-by-key. The fixture covers every tricky
  rule ($HOME expansion, quotes, inline comments, # inside values, export
  prefix, indented comments, invalid identifiers). The test immediately
  caught a shared semantic gap: `KEY = value` kept a leading space in the
  value -- both parsers now strip leading AND trailing whitespace (all 5
  files updated identically; the byte-identity contract test keeps them in
  sync).
* Still open on real codecs (no zstandard/lz4 in CI): the exact zstandard
  behaviour for a header size above max_output_size, and lz4's real boundary
  semantics. `pytest tests/unit/test_decompress_bomb.py -rs` on a host with
  the packages must show 0 SKIPPED.

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

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
