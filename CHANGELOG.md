# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **LMCache 0.5.6** (`requirements-lmcache.txt`).
### Round 80 review fixes (honest metrics, state drift, lifecycle)
* `ask()` metric renamed `ttft_s` -> `request_latency_s` (the non-stream probe
  measures the FULL request latency, not Time-To-First-Token -- honest name);
* `start-all.sh` exports ONE canonical `LIVE_DIR=$ROOT/live` (the recipe's
  `$PWD/live` default drifted when launched from another directory); the
  `live/image` record is written AFTER digest validation (an invalid IMAGE
  never lands there);
* `stop-all.sh` stops the observability stack too (start-all promised the
  pair); `stop-engine.sh` does a graceful `docker stop --time 30` before
  `rm -f`;
* the recipe launcher runs the shared pre-flights (TCP / codec / lmcache
  version) when launched directly (guarded by KVP_PREFLIGHTS_DONE so
  start-all.sh does not probe twice) -- one diagnostics level, not two;
* doc cleanup: every reference to the deleted kv launcher replaced (README
  x3, docs x3 langs, cli.py/mp_ping.py docstrings), container name and
  hardware-acceptance date (2026-11-10) corrected;
* weakened tests restored to real checks (image-write-after-validation,
  MODEL_DIR resolution-before-gate) + regression: start-all.sh from a foreign
  cwd must write all state to the repo live/ dir.

## Round 80b (legacy kv launcher deleted; every check PRESERVED)
* **`run-vllm-with-persist.sh` is DELETED** -- `start-all.sh` now has ONE
  engine path (`KVP_ENGINE=recipe`). `KVP_ENGINE=kv` fails with an honest,
  actionable error (nothing is lost: the shared checks live in `env.sh` /
  `hotsplit_env.sh`).
* **Every check the old launcher owned was audited and PRESERVED** --
  three pre-flight checks (TCP reachability, L2 codec match, lmcache
  version match), the cache-server diagnostics on engine death, the
  GPU pre-flight + cache-server PID check, `engine-identity.env` writing,
  and the namespace-override CONSENT gate (round 72) all moved to the
  recipe launcher / the shared `_kvp_pre_flights`. They used to live ONLY
  in the deleted script (the round-76 defect class).
* 22 kv + 5 recipes test files migrated: textual contract checks now read
  the contract UNION (`_targets.contract_text()` / `_contract.py`),
  behavioural tests drive the recipe launcher. The parity test (two
  launchers) became the engine docker-run CONTRACT test (one launcher).
* kv 378+20, recipes 388+5 -- all green.

## Round 80 (default engine = recipe, hardware validation PASSED)
* **The hardware run PASSED end-to-end on 2026-11-10** (`test-recovery.sh`
  `VERDICT: PASS`, exit 0): same-UID engine (1000:1000) on the recipe path,
  scenario A (engine-only restart) memory/L1 hit with `cached_tokens: 2560`
  and 2.44 s TTFT vs 11.49 s cold; scenario B (full restart) disk-backed
  recovery with L2 read events + 411 MB read by the cache-server process.
* **`KVP_ENGINE=recipe` is now the DEFAULT** (`start-all.sh`). The old kv
  launcher stays as the explicit `KVP_ENGINE=kv` legacy path until the
  round-80 cleanup deletes it; every shared check lives in `env.sh` and
  runs on both paths.

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



