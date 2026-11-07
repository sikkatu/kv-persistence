# Changelog

All notable changes to kv-persistence, newest first. Per-round change
notes live here (the long technical audits stay in
[docs/final-report.md](docs/final-report.md)).

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

## Round 47 (documentation + dashboard honesty)

* All docs now refer to the pinned **LMCache 0.5.6** (90 stale `0.5.3`
  mentions fixed across README/docs EN/RU/ZH); historical references to
  the old upstream bugs now say "pre-0.5.6" without stale numbers.
* GPU free-memory pre-flight runs **after** the old engine is removed
  (launcher) and **after** both services are stopped (test-recovery
  scenario B) — round 46 checked too early and refused on the engine's
  own legal VRAM (78 GiB per GPU on the reference host).
* Grafana: the *LMCache coordinator* dashboard is annotated — it needs
  multi-server coordinator telemetry and legitimately shows "No data" in
  this single-server repo; the *LMCache* dashboard explains that data
  appears only with `USE_OBSERVABILITY=1` on the cache server.
* This CHANGELOG file created (EN/RU/ZH) — per-round notes live here
  from now on.

## Round 46b/46c (codec gate inside test-recovery + instructions)

* `engine-identity.env` fallback lost to the `LMCACHE_L2_SERDE` env
  default in `test-recovery.sh` — precedence fixed to
  switch > pin > engine-identity.env > env, locked by an executable test.
* Instructions updated with the required flags: wiki Quick-Start step 7
  and deployment (EN/RU/ZH) show `KVP_SAME_UID=1` (mode A, recommended)
  vs sudo (mode B); broken code fences repaired.

## Round 46 (engine-identity.env + reviewer batch)

* Launcher records the effective codec + namespace in
  `$KVP_CACHE_ROOT/engine-identity.env`; standalone scripts read it in a
  fresh terminal without repeating `USE_LOSSLESS_SERDE`.
* `ipc-test.sh` stale "NOT yet wired" text replaced with the real flag.
* block_size INCONCLUSIVE line quiet in `--check-config-only`.
* test-recovery scenario B: engine stopped first (`docker stop` ->
  `stop-cache` -> `start-cache` -> `docker start`), `api_ready` aborts on
  a dead container instead of waiting 20 min.
* GPU free-memory pre-flight (`_kvp_gpu_preflight`) added (see round 47
  for the corrected check position).
* Deployment docs (EN/RU/ZH): restart-order rule.

## Round 45h–45j (same-UID mode + bash traps)

* `KVP_SAME_UID=1` launcher mode: engine container runs as
  `$(id -u):$(id -g)` (dynamic UID, no root), caches under `/tmp`.
* `HOME`/`USER`/`LOGNAME` set for any UID (no passwd entry insurance).
* `PRODUCER_USER` must be a literal assignment (bash
  `${var:+NAME=val}` trap), locked by executable tests.
* Cache-server PID check moved under `PERSIST=true`.

## Round 45–45g (CUDA IPC root cause + ipc-test)

* Root cause proven on hardware: `register_kv_caches` fails with
  `CUDA_ERROR_MAP_FAILED` when a non-root cache server opens GPU memory
  created by root in the container. Two fixes: run both as root
  (`sudo`) or same UID (`KVP_SAME_UID=1`).
* New `scripts/ipc-test.sh` reproduces the exact failing step (~1 min,
  no model load), wired into the pre-flight; `PRODUCER_USER` support for
  the same-UID probe.

## Round 42–44 (LMCache version + API drift + diagnostics)

* LMCache version pre-flight: host and image must match (0.5.6).
* Runtime patches adapted to the 0.5.6 API (`dynamic_store`,
  `@`-suffix salt in file names is native upstream).
* Protocol-level RPC ping (`mp_ping.py`), automatic cache-server
  diagnostics on engine death, `PYTHONFAULTHANDLER=1` on server start.

## Round 41 and earlier

* Real liveness = TCP connect (not just a live PID).
* Identity gate split into pre-launch config check and post-launch
  container-args check.
* Lossless-only cache (zstd/lz4/none, fp8/turboquant refused loudly),
  trilingual docs, actionable error messages.