# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).


## Round 78 (CONTAINER= override, API_KEY canonical-file contract, honest parity, full exit-code chain)
* `CONTAINER=` from the environment was IGNORED on the recipe path
  (start-all.sh overwrote it with the default). Priority now:
  `CONTAINER` > `KVP_ENGINE_CONTAINER` > default -- the effective name is
  recorded in `live/engine_container` and exported to the recipe launcher.
* `API_KEY` split-brain fixed in `run-tp4-hotsplit.sh`: the env key was used
  for the run while `~/.config/kvp/api_key` kept the OLD key (and `live/api_key`
  mirrored the NEW one) -- two different keys at once. Contract (same as the kv
  launcher): the canonical file is the source of truth; an explicit env
  `API_KEY` WINS and is SAVED there with a loud WARNING.
* The full-parity test compared docker-run tokens with `set()` -- duplicate
  flags/mounts and multiplicity were invisible. Now Counter (multiset)
  comparison + duplicate single-use flag detection + mount targets as a
  multiset (flag ORDER is honestly out of scope: the two launchers build the
  command in different order and docker ignores it).
* The exit code is now verified through the FULL chain (`kvp up` -> `exec` ->
  `start-all.sh` -> foreground recipe) with an arbitrary non-zero code, not
  just exit 1 via start-all.sh.
## Round 77 (shared pre-flights on both engine paths, honest ipc-test labels)
* The lmcache RPC probe (`mp_ping`) and the CUDA IPC boundary test
  (`ipc-test.sh`) were wired ONLY into the kv launcher -- the delegated
  recipe path (`KVP_ENGINE=recipe`) loaded 130 shards for ~15 minutes and
  then died at `register_kv_cache` (CUDA_ERROR_MAP_FAILED from
  `cuIpcOpenEventHandle`) with no check before it. The same defect class as
  round 76 (checks that live in one launcher vanish when the default
  switches). One shared function `env.sh::_kvp_pre_flights` now runs in BOTH
  paths before the model load; a failed pre-flight refuses the launch
  ("nothing was started") and names the honest fix: run the cache server in
  the container (`KVP_LAUNCH_MODE=docker`) or as the engine's UID
  (`KVP_SAME_UID=1`). Honest scope note: a pre-flight catches a dead channel
  at start, not a server that dies MID-load (the launcher still fail-fasts
  on server errors).
* `ipc-test.sh` labels no longer lie: "same-UID test -- no root" used to
  print for ANY `PRODUCER_USER` including `0:0` (a ROOT producer). Root
  producers are now labelled "root producer" with their own PASS verdict
  ("ROOT-PRODUCER result"). Regression tests reproduce all of it
  (`tests/unit/test_round77_shared_preflights.py`, 8 tests, red on the old
  code).
## Round 76 (SIGPIPE false-negative in the activation check, shared config validation, TP coverage)
* The activation check lied on big engine logs (review, reproduced on the
  hardware run): `printf '%s' "$log" | grep -q ...` under `set -o pipefail`
  -- `grep -q` exits on the first match and closes the pipe, `printf` gets
  SIGPIPE (141), the pipeline is "failed" and the `if` takes the NOT-found
  branch even though the marker IS in the log. On a 17-minute model-load log
  ALL four markers read [MISSING] while the engine was fine -- and the SAME
  pipes guarded the ERROR checks, so a real error could pass as success.
  All 8 sites (4 per launcher) now use here-strings
  (`grep -q -- "..." <<<"$log"`) -- no pipe, no SIGPIPE. Regression tests on
  a 100 KiB log cover both directions (markers present -> VERIFIED; markers
  or errors absent/present -> the right verdict).
* HotSplit config validation moved to the SHARED module
  (`hotsplit_env.sh::_hotsplit_validate_config`): the checks (NUMA_MAP
  format, DYN_DECAY 0..1, DYN_MARGIN >= 1, DYN_MAX_SWAPS 1..1024, integer
  and float knobs) lived ONLY in the kv launcher -- after switching the
  engine path to the recipe launcher they would have disappeared. Both
  launchers now call the same function.
* NEW check (review): HOTSPLIT_NUMA_MAP must COVER every GPU rank 0..TP-1.
  `numa_bind.py` silently returns -1 for a missing GPU and still reports
  `numa_bind=ACTIVE` -- a map "0:0,1:0" on TP4 bound 2 of 4 GPUs and looked
  healthy. The validator now names each missing rank. Behavioural tests
  (TP4/TP2) in `tests/test_hotsplit_validation.py` -- a condition for
  switching the default to the recipe launcher.
* The namespace-override consent (round 73) is now covered:
  `tests/test_namespace_override_consent.py` -- without
  `KVP_ALLOW_NAMESPACE_OVERRIDE=1` an inherited `KVP_MODEL_NAME` is IGNORED
  with a loud WARNING (the identity-derived name wins); with the flag the
  override applies, still with a WARNING.
> Older round notes were trimmed on 2026-11-07: the full fix history lives in the git log of `main`.
