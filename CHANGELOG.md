# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).
## Round 79 (recipe same-UID regression fix, recovery fail-fast + diagnostics)
* **Root cause of the recurring `register_kv_caches` 300 s timeout confirmed
  on hardware** (reviewer + driver log): the delegated recipe path ran the
  engine as ROOT while the cache server ran as the invoking user -- the
  exact uid mismatch behind `CUDA_ERROR_MAP_FAILED` ("Failed to open/create
  interprocess memory file / remote IPC semaphore pool" = EACCES on
  root-owned `cuda.shm.*` in /dev/shm). The kv launcher's round-45h/48
  same-UID contract was LOST when the recipe path became the delegated
  engine (the same defect class as rounds 76/77/78).
* `run-tp4-hotsplit.sh` now runs the engine as `$(id -u):$(id -g)` by
  DEFAULT (`KVP_SAME_UID=1`, opt out with `KVP_SAME_UID=0`) with HOME /
  HF / XDG / TRITON caches on `/tmp` -- identical to `run-vllm-with-persist.sh`.
* Recipe API-wait **fail-fasts** on a fresh `Error handling operation` in the
  cache-server log (the root cause is printed; the mark is taken BEFORE
  `docker run` so even a first-seconds failure is caught) instead of waiting
  out the engine's 300 s timeout.
* `test-recovery.sh`: `api_ready` fail-fasts the same way AND prints
  `docker logs` when the container dies (no manual forensics); scenario B
  runs the CUDA IPC pre-flight BEFORE `docker start` (the exact step B died
  on); every server-log read uses `grep -a` (NUL bytes make plain grep skip
  matches silently).
* `start-all.sh` passes the same-UID flag to the shared pre-flights (they
  must test the boundary the engine ACTUALLY uses).
* Regression tests: `tests/unit/test_round79_recovery_failfast.py` (kv, 5
  tests) + `tests/test_round79_same_uid.py` (recipes, 3 tests) -- red on the
  old code. Parity allowlist updated: `--user` and the `/tmp` cache mount are
  SHARED tokens now (both sides same-UID), not one-side differences.



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
