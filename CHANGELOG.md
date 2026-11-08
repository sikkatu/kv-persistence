# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 70 (delegation: KVP_ENGINE=recipe, default OFF)

* ONE engine launch, TWO paths: `start-all.sh` now honours `KVP_ENGINE`
  (`kv` = the kv launcher, DEFAULT; `recipe` = the hardware-verified
  `run-tp4-hotsplit.sh` with KV_PERSIST=1). The default is OFF and the old
  engine code is untouched until the hardware run confirms the recipe path --
  exactly as the review required.
* The reviewer's step-2 checklist, all wired and tested:
  * WITHOUT the flag nothing changes (the docker-run contract snapshot pins
    the behaviour; the default-path test asserts the kv launcher runs);
  * ONE container name: `start-all` records the effective name in
    `live/engine_container`; `stop-engine.sh` (kvp down / stop-all.sh) and
    `kvp logs/status` read it -- they stop exactly what was started
    (the recipe names its container mimo-pro-tp4-hotsplit, the kv launcher
    mimo-pro-tp4-kvp);
  * ONE key source: the shared `~/.config/kvp/api_key` is exported as API_KEY
    in the environment -- never in argv -- so the recipe launcher reuses it
    and never generates a second one;
  * KV_CACHE_DTYPE: pinned to bfloat16 (the recipe default is auto). The
    difference is SHOWN in the tests, never normalized away; the identity
    hash accounts for the dtype (each dtype -> its own namespace; the
    auto->bfloat16 resolution contract is asserted in the launcher source);
  * exit codes: the recipe launcher runs in the foreground -- its exit 1
    (activation check) reaches start-all.sh / kvp up unchanged (tested with
    missing markers -> exit 1).
* `tests/test_engine_delegation.py` (6 tests) covers the checklist; the
  mandatory identity gate is stubbed (same device as test_kv_persist_behaviour)
  -- real identity is kv-persistence's own territory.

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

> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
