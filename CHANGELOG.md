# Changelog

All notable changes to kv-persistence, newest first. Per-round change
notes live here (the long technical audits stay in
[docs/final-report.md](docs/final-report.md)).

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

## Round 55 (KVP_CACHE_DEVICE auto-detection)

* `KVP_CACHE_DEVICE` no longer needs manual guessing (user: "я сам тоже не
  знаю, у меня все через вм и зашифрованный раздел"): a shared helper
  `_kvp_resolve_cache_device` in env.sh resolves the backing device from
  `live/cache_device` (remembered) or `findmnt SOURCE` of the cache root --
  handles VM disks (`/dev/vda1`), LUKS mapper nodes (`/dev/mapper/cryptroot`,
  resolved via readlink) and NVMe partitions. Used by test-recovery.sh and
  preflight.sh; an explicit pin still wins. The choice is recorded in
  `live/cache_device`.
* docs/final-report.md leftovers cleaned: LMCache runtime row, version-pin
  row and the serde end-to-end row are now VERIFIED (hardware 2026-11-07).

## Round 54 (hardware acceptance recorded)

* Restart-recovery acceptance PASSED on the target GPU host (4x RTX
  PRO 6000, MiMo-V2.6-Pro-MOPD, same-UID, 2026-11-07):
  - scenario A (vLLM restart): cached_tokens=2560, ttft 0s vs 17.53s
    cold (L1/RAM memory hit);
  - scenario B (BOTH services restarted, NVMe kept): cached_tokens=2560,
    ttft 0s, **L2 read events = 1** (disk-backed recovery proven);
  - identity gate passed at every stage; **0 hard failures**.
* Machine verdict INCONCLUSIVE only because KVP_CACHE_DEVICE is unset
  (independent NVMe counters unavailable) -- set it in cache.env.local
  and rerun for the full machine PASS.
* README (EN/RU/ZH), compatibility matrix (EN/RU/ZH) and final-report
  (EN/RU/ZH) updated: ACCEPTED status with the evidence; the remaining
  NEEDS TESTING items are peripheral and kept honest.

## Round 53 (cold baseline waits for the API)

* `test-recovery.sh` cold baseline fired its first HTTP request
  immediately and died with a raw Python traceback (Connection refused)
  when the engine was still loading or had been stopped. It now waits via
  `api_ready` exactly like scenarios A/B, and a refused connection prints
  a short actionable message instead of a traceback.
* `api_ready` announces what it is waiting for (no silent 30-min loop).

## Round 52 (scenario B: honest failure reporting)

* `test-recovery.sh` scenario B: `docker stop` no longer swallows its
  error output (`>/dev/null 2>&1` hid the real cause). The container
  state is probed first -- an already stopped/removed container is NOT a
  failure (the goal is a clean cache restart with the engine down). A
  real failure shows the docker output.
* `PA()` now takes the scenario tag explicitly -- the old form printed
  `FAIL(A)` even for scenario B failures. All call sites updated.

## Round 51 (standalone gate resolves MODEL_DIR like the launcher)

* A standalone `scripts/verify-engine-identity.sh` in a fresh terminal
  reported `model_dir=unset` -> num_kv_heads/head_dim UNAVAILABLE ->
  INCONCLUSIVE, while the launcher's own gate run passed: the resolution
  chain (live/model_dir > identity pin > ~/models) lived ONLY in the
  launcher. ONE shared helper `_kvp_resolve_model_dir` in env.sh is now
  used by both -- a standalone gate resolves exactly like the launcher.
* Fixed a stale-array bug in the new helper (config-dir scan leftovers
  mixed into the models scan).

## Round 50 (MODEL_DIR reaches the gate + codec default in configs)

* The auto-resolved MODEL_DIR was never EXPORTED, so the post-launch
  identity gate (a subprocess) saw `model_dir=unset`, could not read
  config.json and refused to verify num_kv_heads / head_dim
  (INCONCLUSIVE -> cache reuse blocked). The launcher now exports it
  after resolution.
* `L2 codec: none` in a zero-config launch: `lmcache-server.env` still
  pinned `LMCACHE_L2_SERDE=none`, outranking the round-48 default. All
  three locale files now agree on `zstd`; tests locked to prevent
  locale divergence.

## Round 49 (PATH auto, monitoring first, report cleanup)

* `export PATH="$HOME/.venvs/lmcache/bin:$PATH"` is no longer needed
  (user): `env.sh` resolves the lmcache venv bin itself (sudo-aware) and
  prepends it to PATH for every script. The version pre-flight now reads
  the host lmcache through the venv python (the system python3 has none).
* Monitoring is NOT optional: Quick-Start step 6 and the README quick
  starts now start `start-observability.sh` FIRST, then `start-cache.sh`
  (auto-attaches, round 47b). The "Optional:" labels are gone.
* `docs/final-report.{md,ru.md,zh.md}` stripped of every per-round fix
  section (user: the changelog holds them; early fixes need not be
  remembered) -- the report keeps only the verified status and the
  technical record and links CHANGELOG.

## Round 48 (zero-config launch)

* `bash scripts/run-vllm-with-persist.sh` in a fresh terminal needs NO
  exports and NO flags: IMAGE resolves from the recipe digest (remembered
  in `live/image`), MODEL_DIR resolves from `live/model_dir`/identity
  pin/`~/models`, `KVP_SAME_UID=1` is the DEFAULT (root fallback:
  `KVP_SAME_UID=0`), and the codec defaults to lossless zstd
  (`USE_LOSSLESS_SERDE=0` opts out to byte-exact raw).
* ONE codec semantic table across env.sh / identity.py / _fold_serde
  (executable test): empty -> zstd default, `raw`/`none` -> explicit raw,
  switch beats configured. The cache namespace is identical in bash and
  Python with zero exports.
* 0.5.3 leftovers purged from source comments too (docs: round 47).

## Round 47b (observability auto-detect)

* `start-cache.sh` AUTO-DETECTS the OTel collector at `127.0.0.1:4320`
  and enables metrics automatically -- no flag needed (user request).
  Explicit opt-out: `KVP_NO_OBSERVABILITY=1`; force on:
  `USE_OBSERVABILITY=1`; custom endpoint: `KVP_OTLP_ENDPOINT=...`.
* README + observability README (EN/RU/ZH) updated: "start the stack,
  then the cache" flow; Grafana dashboard descriptions refreshed.

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