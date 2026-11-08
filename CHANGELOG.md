# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 69 (step-2 prep: activation check in the recipe launcher, contract snapshot)

* The recipe launcher (`run-tp4-hotsplit.sh`) now runs the SAME hotsplit/NUMA
  activation check as the kv engine launcher (round 59) before declaring
  success: boot-hook marker (ACTIVE or the intentional INACTIVE with model
  patches), `step hook installed`, `live counter:`, `HOOK STATUS
  numa_bind=ACTIVE` when NUMA is on, no empty-bank and no transaction errors.
  A missing marker is exit 1. This closes the "delegation will lose the
  check" gap BEFORE `KVP_ENGINE=recipe` exists. Opt out:
  `KVP_SKIP_HOTSPLIT_CHECK=1`.
* Docker-run contract snapshot (`tests/fixtures/launcher-docker-run.txt`,
  test `test_snapshot_contract`): without the delegation flag the final
  `docker run` must stay byte-identical to the recorded contract. Regenerate
  intentionally with `KVP_UPDATE_SNAPSHOT=1`.
* Negative activation tests (`tests/test_activation_negative.py`): missing
  marker -> exit 1 with each `[MISSING]` line listed; complete markers ->
  exit 0; the skip switch works. The launcher's exit code is the one
  start-all.sh / kvp up see (they run it in the foreground).
* Older test suites use the documented opt-out (their docker stub logs no
  markers and they assert other behaviour); the check itself is covered by
  the dedicated tests.

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

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
