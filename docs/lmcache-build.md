# Custom LMCache build: patches, build, identity, reset, future migration

This document covers the custom LMCache deployment that backs the shared MP
cache server and every vLLM model worker. Russian mirror: `lmcache-build.ru.md`.
Chinese mirror: `lmcache-build.zh.md`.

## 1. LMCache build identity

| Item | Value |
| --- | --- |
| Upstream project | https://github.com/LMCache/LMCache |
| Pinned source commit (base) | `03ee5b82418b6bd44e424cdd46932d9346f43eb2` |
| Base release identity | `0.5.7.dev428` (nightly of 2026-11-16) |
| PR #6551 merge commit (in base) | `61faa902a81462177d40d74f8c64bb9df02163c4` |
| PR #4136 source revision (patch) | `b10588bd46288192e01ab06da3c3fa923ad0401a` |
| PR #5972 source revision (patch) | `1ccd6f698794cc45d189c14223d339623abe6ec2` |
| Patch directory | `vendor/patches/lmcache/` |
| Patch detail + checksums | `vendor/patches/lmcache/SOURCES.md` |

The mutable `nightly` tag is never used in production; the exact base commit is
checked out every build. `scripts/build-lmcache.sh` verifies the base revision,
verifies #6551 is an ancestor of it, applies the two patches deterministically
(fails loudly if a patch does not apply or is already applied), runs the
focused upstream regression suites for #4136 and #5972, builds the wheel with
`--no-build-isolation` against the pinned image's installed PyTorch, and writes
`live/lmcache-build/provenance.json` with the full source identity and the
wheel SHA-256.

Both patches are small, share no files, and are individually removable:

* `4136-mp-retrieve-abort.patch` — vLLM MP integration layer only
  (explicit `LOADING` retrieve lifecycle, scheduler-confirmed abort
  propagation, invalid store-completion suppression, abort dedup).
* `5972-incomplete-kv-objects.patch` — MP cache-engine layer only
  (block-availability check after window downsampling; incomplete objects with
  a remaining `null_block_id` are rejected before persistence; independently
  complete groups are still stored).

## 2. Deployment

`scripts/build-image.sh` builds the **custom derivative image** from the pinned
vLLM image digest (`vllm/vllm-openai:nightly-8e2beaa3...@sha256:167ccd86...`,
CUDA 13.0.2, PyTorch 2.14.0+cu130, Python 3.12) and installs exactly one
extra package: the custom LMCache wheel. vLLM, PyTorch, CUDA and Python are
preserved. The resulting image identity is written to
`live/image-identity.env` AND baked into the image as
`/etc/kvp-lmcache-identity.json` (base commit, patch SHA-256s, wheel SHA-256)
-- the wheel version alone (`0.5.7.dev428`) cannot tell our build from the
upstream nightly (`no-local-version`).

**Deployment status (r2 P0-1 — live):** the cache server now defaults to
`KVP_LAUNCH_MODE=docker` AND both recipe launchers refuse to start whenever
the image does not carry the custom LMCache identity (`verify-image-custom.sh`
/ `kvpersist.check_image_identity` compare `org.kvp.lmc-*` labels against
`vendor/patches/lmcache/*.patch` SHA-256 before any model load). A stock
vllm-openai image launches only with explicit `KVP_LAUNCH_MODE=host` +
`KVP_SKIP_IMAGE_CHECK=1` (test-harness opt-out) and at the operator's own
risk. `verify-engine-identity.sh` additionally checks the running container's
`/etc/kvp-lmcache-identity.json` (MISMATCH, exit 4).

This image must be used by:

* the shared MP cache server (`scripts/start-cache.sh`),
* every vLLM model worker (recipes launchers).

Startup must fail if a worker and the MP server disagree on the LMCache build
identity (`kvpersist.lmc_identity` compares the baked identity file against
`vendor/patches/lmcache/` per component). The pre-flight identity gates
(`verify-engine-identity.sh`, `_kvp_pre_flights`) run before any model load.

Connector selection: the deployed vLLM version requires the external
`LMCacheMPConnector` to be selected explicitly via `--kv-transfer-config`
(never assumed from auto-detection).

## 3. Persistent cache: L1/L2, identity, diagnostics

* L1 = host DRAM (`LMCACHE_L1_SIZE_GB=64`, translated to the server's actual
  option — verify in `lmcache-server.log`, do not assume the env var is
  consumed).
* L2 = persistent NVMe tier (`KVP_CACHE_ROOT=/var/lib/kv-persistence/l2` by
  default; MUST be a local NVMe path).
* Codec: lossless only — `zstd` (default), `lz4`, `none`.
* Cache identity (namespace, model identity, KV dtype `bfloat16`, cache format,
  object-group config, backend identity) is checked per-worker in preflight;
  any mismatch fails startup.

**A `STORE_END` event is not proof of a persistent write.** The STORE/RETRIEVE
diagnostics distinguish: store requested → worker/adapter completion → object
accepted for L2 → L2 write succeeded/failed → objects/bytes persisted;
retrieve requested → completed / aborted / prefetched from L2 / invalid object
rejected. Check `lmcache-server.log` "L2 store completed" / "L2 prefetch load
completed" lines for actual persistence evidence.

## 4. Safe full-cache reset (one-time migration)

`scripts/reset-persistent-cache.sh` is the ONLY destructive operation and is
never called by start/stop/restart:

```
KVP_RESET_CONFIRM=FULL-RESET bash scripts/reset-persistent-cache.sh
```

Requirements: explicit confirmation string; services stopped first (or
`KVP_RESET_FORCE_STOP=1`); every target resolves inside the configured cache
root; `/`, home dirs, model dirs, repos, build artifacts and escaping symlinks
are refused. A reset report (cleared paths with byte counts, preserved data
confirmation) is written to `live/cache-reset-report-<UTC>.txt`.

NEVER delete: model weights, model configuration, application data, recipes,
source code, LMCache build artifacts, diagnostic logs, unrelated NVMe files or
the NVMe mount itself.

## 5. Cache-miss / failure diagnosis

* Persistent round-trip check: run `scripts/test-recovery.sh` — cold baseline
  stores to L2, engine-only restart recovers from L1, full restart (engine +
  cache server) must show L2 read events and non-zero `cached_tokens`.
  A successful generation is not proof of a cache hit; verify cached tokens and
  L2 events.
* Incomplete-object rejection: `STORE` skips with "invalid object" in the log
  mean #5972 rejected an object whose retained geometry had a null block;
  those can never appear as cache hits later.
* Backend failure: L2 write failures are reported as failures — the engine
  never silently reports them as persisted.
* `KVP_LOAD_FAILURE_POLICY` remains EXPERIMENTAL (`fail` is the default). The
  old C2 guard pairing warning is obsolete: the retrieve/abort race is fixed
  inside the custom build (PR #4136), so no local guard exists to conflict.

## 6. Future upstream migration (when #4136 and #5972 merge upstream)

Planned follow-up, not part of the current deployment. When both PRs are merged
upstream:

1. Inspect the upstream merge commits; select a new pinned LMCache source
   commit or official release that contains #6551, #4136 and #5972 (verify in
   Git history and source — a new release does not automatically contain them).
2. Remove `vendor/patches/lmcache/4136-mp-retrieve-abort.patch`.
3. Remove `vendor/patches/lmcache/5972-incomplete-kv-objects.patch`.
4. Remove their application entries from `scripts/build-lmcache.sh` and their
   source-revision lines from `SOURCES.md` / `provenance.json`.
5. Remove any temporary downstream-patch-specific tests now duplicated by
   upstream; keep project-level integration tests.
6. Update this document and the three READMEs to stop describing #4136/#5972
   as downstream patches.
7. Build the new pinned LMCache against the same pinned vLLM runtime; verify
   the MP server and every worker report the SAME new build identity.
8. Run the full regression suite and re-run hardware validation
   (persistent STORE / RETRIEVE after full restart, cancellation stress,
   L1-pressure LRU behaviour) before retiring the current build.

Do not delete the current custom build or its patches before the replacement
build passes this validation. The migration must leave no manually copied
#4136/#5972 implementation anywhere in the project repositories.