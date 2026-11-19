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
### Reference build pipeline C0-C16: step -> covering test (r11)

| Step | Gate | Real-program test |
| --- | --- | --- |
| C0 | prologue: trap EXIT chown, baseline pip freeze | test_chown_runs_even_on_pytest_failure (test_r8_gpu_gate_and_artifacts.py) |
| C1 | apt update -> git + cuda dev pkgs (fail-loud) | test_git_is_installed_before_first_use / test_missing_git_package_refuses_loudly (test_r9_git_install.py, real bash+apt log) |
| C2 | git safe.directory after install | same r9 test (order asserted from the apt/git logs) |
| C3 | nvcc + g++ + ninja gate, arch override | test_project_arch_override_wins_over_image (test_r10_build_order.py) |
| C4 | build deps with engine constraints, no silent fallback | test_engine_freeze_diff_refuses_changed_torch exercises the same constraint logic (test_r11_gates_real_programs.py) |
| C5/C16 | exactly one `pip wheel` | test_exactly_one_pip_wheel_in_the_script + test_pip_wheel_runs_exactly_once (real run, recording pip shim) |
| C6a | wheel file-list vs git ls-files | covered inside the real-run fixtures (missing file = refusal) |
| C6b | both native extensions | test_wheel_without_lmcache_native_refused (test_r10_build_order.py) + test_check_wheel_native.py (real zipfile+CLI) + test_c6b_identity_provenance_chain_delivers_valid_json (r12: REAL check_wheel_native -> C6b -> C15 -> provenance, json.loads) |
| C6c | cuobjdump sm_120, no silent death | test_c6c_works_without_unzip / test_c6c_empty_cuobjdump_refuses_loudly / test_c6c_sm89_only_refuses_loudly (r12, REAL bash on the extracted C6c block) + test_every_refusal_scenario_prints_a_message (acceptance walker) + arch refusal path in test_r10_build_order.py fixtures |
| C7 | wheel install + pip show before/after (SIGPIPE-safe) | test_c7_reaches_after_marker_despite_big_pip_show / test_c7_before_line_no_false_not_installed / test_c7_before_line_reports_missing_install (r13, REAL bash, 200 KB pip stub) + test_no_head_pipeline_outside_fail_soft_substitutions (lint) |
| C8 | import-check from the installed distribution (dist-packages aware) | test_layout_run_sees_installed_not_source / test_plugin_refuses_source_tree_with_exit4 (REAL pytest + plugin) + test_installed_dist_packages_layout_passes / test_source_tree_refused / test_metadata_and_module_mismatch_refused / test_plugin_passes_dist_packages_layout (r14: shared assert_installed, both layouts) |
| C14 | engine freeze diff (after the tests, before C15; runs at KVP_BUILD_SKIP_TESTS=1) | test_engine_freeze_diff_refuses_changed_torch (real python on freeze files) + test_c14_runs_after_pytest_install_and_detects_mutation / test_c14_runs_even_when_tests_are_skipped / test_c14_positive_control_no_mutation (r12, REAL whole script, stateful pip shim) |
| C9 | minimal test deps (pytest only) | wiring in the real-run fixtures |
| C10 | staging without lmcache/ | test_layout_run_sees_installed_not_source (staging copy asserted) |
| C11 | staged pytest run + kvp_assert_installed | test_script_runs_the_exact_command (every invocation staged form) + plugin tests |
| C12 | summary gate (skipped==0, passed>0) | test_skip_in_five_cpu_files_still_aborts (test_r8_gpu_gate_and_artifacts.py) |
| C13 | GPU step (skip != 0 refused) | test_gpu_step_skip_is_not_success (test_r9_gpu_step_honesty.py) |
| C15 | identity.env single-quoted, sourced strictly | test_identity_env_sources_cleanly_under_strict_bash + test_provenance_json_fields (REAL bash/json) + test_c6b_identity_provenance_chain_delivers_valid_json (r12 end-to-end) |
| host | provenance fields from identity.env | test_provenance_json_fields (real build-lmcache.sh run) + test_c6b_identity_provenance_chain_delivers_valid_json (r12: json.loads of native_extensions) |

Build step order (r10): apt/git -> build deps (incl. `grpcio-tools==1.78.0`
for the proto stub generator) -> `pip wheel` -> wheel content checks
(git ls-files + both native extensions) -> `pip install --no-deps
--force-reinstall` -> import-check FROM site-packages (`python3 -P`, three
`__file__` proof lines) -> pytest (five CPU files, skipped == 0) ->
identity/provenance. The arch list comes from `KVP_TORCH_CUDA_ARCH_LIST`
(default 12.0; the image value is only a fallback) and the built
`cuda_ops*.so` must list sm_120 (cuobjdump). `provenance.json` records
`torch_cuda_arch_list`, `cuda_ops_archs` and `native_extensions` from
identity.env -- it reflects what was really built.

### Build-time regression gate vs GPU regression (r8 P0)

The build container runs WITHOUT `--gpus`, so the mandatory regression gate
is limited to the FIVE CPU test files (0 skipped required):
`test_lmcache_mp_connector`, `test_vllm_mp_adapter`,
`test_lmcache_driven_transfer_skip`, `test_native_state_lookup`,
`test_l1_owner_completion`. What is NOT checked at build time:
`tests/v1/multiprocess/test_cache_server.py` -- it is a GPU-integration suite
(LMCache server + CUDA IPC) that skips at module level without CUDA. It runs
as a separate step when `KVP_BUILD_GPU_TESTS=1` (the container must then be
launched with `--gpus all`); `provenance.json` records
`gpu_regression_run: true|false`. GPU coverage at build time is the GPU-smoke
in `scripts/build-image.sh` (one native kernel per GPU on the built image).

`--no-build-isolation` against the pinned image's installed PyTorch, and writes
`live/lmcache-build/provenance.json` with the full source identity and the

`tests_skipped` in `provenance.json` means the whole test phase was
skipped via `KVP_BUILD_SKIP_TESTS=1` -- it is NOT about individual pytest
skips (those are refused by the gates). `gpu_regression_run` is the
boolean record of whether the GPU regression step actually ran and
passed (a module-level skip does not count as a run).
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

### The cache-root marker (`.kvp-cache-root-marker`)

`start-cache.sh` stamps the cache root with `.kvp-cache-root-marker` at cache
initialization. The marker is how the destructive reset run proves the target
is really a cache directory: an explicitly configured `KVP_CACHE_ROOT` is
accepted either because it carries the marker, or because its top-level
entries are nothing but known LMCache/kvpersist artifacts (`*.data`, `*.tmp`,
`*.bin`, `*.bin.tmp.*`, `objects/`, `kvp-*`, `index.db`, `crash-*`,
`lmcache-server.log/.pid`, `l2-adapter.rendered.json`, `engine-identity.env`,
`.kvp-active-writes`, and the `relative_tmp_dir` name configured in
`l2-adapter.rendered.json`). Any foreign top-level entry refuses the run and
the refusal names the entries.

**On a cache created before the marker existed** (older deployments): look at
the directory first, then mark it by hand:

```
ls -A <KVP_CACHE_ROOT>
touch <KVP_CACHE_ROOT>/.kvp-cache-root-marker
```

Only do this when `ls -A` shows nothing but cache data. To wipe a cache root
of a non-standard layout containing foreign files, move those files away
first (or mark the dir only when you are fully sure it holds cache data).

Regardless of the marker, the reset **always** refuses: `$HOME` and any user
home (`/root`, `/home/<user>`), any ancestor of the repository checkout, and
any directory carrying `.git` at nesting depth <= 2.

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
  old C2 guard pairing warning is obsolete: the retrieve/abort race is handled
  inside the custom build (downstream port of PR #4136), so no local guard
  exists to conflict. **Status (honest): implemented, unit-tested; hardware
  validation: C0-C7 passed on the operator's host; C8-C16 pending.** The race
  fix, the custom build and the native cuda_ops extension are unit-tested in
  this repo; the reference build pipeline passed C0-C7 on the operator's host
  (wheel built, cuda_ops carries sm_120, four native extensions, install OK)
  and stopped on a C7 SIGPIPE defect (now fixed) -- C8-C16 still need to run:
  see section 5b "Implementation stages" for the remaining operator-stand
  validation plan (import-check -> tests -> identity/provenance -> build-image
  -> engine + cache-server startup -> parallel load with aborts).

## 5b. Implementation stages (hardware validation plan)

Per the honesty rule of the fix series: everything above is **implemented and
unit-tested in this repo** but the following chain still needs to run on the
real operator stand (4x RTX PRO 6000, NVMe L2, pinned vLLM image) before any
"verified"/"works" claim is honest:

1. `bash scripts/build-lmcache.sh` -- smoke the custom wheel build with real
   nvcc + CUDA dev headers; assert `cuda_ops*.so` present in the wheel.
2. `bash scripts/build-image.sh` -- build the derivative from the pinned vLLM
   digest; assert `import lmcache.cuda_ops` inside the container on real GPUs
   (the `--gpus all` smoke checks all 4 GPUs hit `get_gpu_pci_bus_id`).
3. `bash scripts/verify-image-custom.sh <IMAGE> <kvp-root>` -- deep check
   (identity.json + `find_spec('lmcache.cuda_ops')`).
4. Start the MP cache server and at least one model worker on the same custom
   image; assert both report the SAME LMCache identity via the startup gate.
5. Cold-cache STORE to L2; full restart (cache server + workers); verify
   RETRIEVE shows L2 read events + non-zero `cached_tokens`.
6. Parallel N>=2 load with aborts (retrieve/abort stress): engine must survive
   the retrieve/abort cycle and never emit contradictory completion
   notifications per PR #4136's contract; #5972's incomplete-object rejection
   must be visible in server logs before persistence.

After this chain passes, reword the status note above to "validated on
hardware" -- until then, do not claim more than implemented and unit-tested.

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