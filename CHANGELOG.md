# Changelog

All notable changes to kv-persistence, newest first. Only the last 3
rounds are kept here (user request) -- the full fix history lives in the
git log of `main`.

Language versions: **English** | [Русский](CHANGELOG.ru.md) | [中文](CHANGELOG.zh.md)

## Unreleased

Runtime: pinned vLLM image (`@sha256:167ccd86…`) + **custom LMCache build**
(base `0.5.7.dev428` = `03ee5b82418b6bd44e424cdd46932d9346f43eb2`,
downstream patches for upstream PRs #4136 and #5972, see
`docs/lmcache-build.md`); the old raw **LMCache 0.5.6** pin is replaced by this
custom build. `requirements-lmcache.txt` now points at the derived image.
### Round 13 (SIGPIPE in C7: `pip show | head -3` = silent rc=120)
* `7bda1bc` **P0** (first hardware run, 19.11 11:42): the build passed C0-C7
  (wheel built, cuda_ops has only sm_120, four native extensions, `pip
  install` OK) and then died SILENTLY with `docker run rc=120` before C8.
  Cause: `pip show lmcache | head -3` -- `head` closes the pipe, `pip show`
  writes to the closed stdout and exits 120 (SIGPIPE, `ERROR: Pipe to stdout
  was broken`), which under `pipefail` ends the script with no message (the
  line WITHOUT `||`). The BEFORE line also printed a FALSE `(not installed)`
  for an installed package. Same defect class as r12 but OUTSIDE `$(...)` --
  the r12 audit and the message-walker missed plain pipelines. Fix: both
  lines use `sed -n '1,3p'` (reads the whole output), and `(not installed)`
  is printed only when `pip show` really fails. NEW tests (real bash, real
  C7 block extracted by markers, python pip stub writing >=200 KB and
  exiting 120 on a closed stdout): the block must reach the after-marker
  (RED before: rc=120 -- exact hardware reproduction), no false `(not
  installed)`, real failure DOES print it. Acceptance lint: no unprotected
  `| head` pipeline in _incontainer_build.sh. Mutation: restoring `| head -3`
  -> RED, verified. All `| head` / `| grep -q` / `sed q` pipelines audited
  across scripts/ + implementation/ -- every occurrence is fail-soft
  (`|| true`), guarded, or a bounded diagnostics dump.
Hardware validation status (new wording, x3 docs):
"implemented, unit-tested; hardware validation: C0-C7 passed on the
operator's host; C8-C16 pending".
Full suite runs (both repositories, round 13 final):
* kv-persistence: 570 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Round 12 (provenance JSON, C6c silent deaths, C14 order)
* `125b571` **P0**: the brace-default literal ( `${VAR:-{}}` form ) appended a
  stray `}` to a NON-EMPTY JSON -- bash closes the parameter expansion at the
  first closing brace, so the second brace became a literal. The junk flowed
  through identity.env into provenance.json (`native_extensions` became
  invalid JSON) and build-image.sh died after an already-successful long
  build. Fix: the default is assigned on its own line
  (`[ -n "$X" ] || X='{}'`); no such literal is left anywhere
  (`grep -rn ':-{' scripts implementation` is empty). NEW end-to-end test
  (real programs, no manual substitutions): the REAL `kvpersist.check_wheel_native`
  on a fake wheel with 3 native .so -> the C6b block extracted by its markers
  from _incontainer_build.sh -> the C15 block -> identity.env -> the provenance
  block extracted from build-lmcache.sh -> `json.loads(provenance.json)`;
  `native_extensions` must be a JSON object with lmcache/cuda_ops,
  lmcache/lmcache_native, lmcache/lmcache_redis. Mutation: restoring the old
  line -> RED (JSONDecodeError: Extra data).
* `6bbc68e` **P1**: C6c silent deaths under `set -Eeuo pipefail`. The `.so`
  discovery used `unzip` (NOT guaranteed in the image -- rc=127 silent exit)
  and the cuobjdump pipeline died silently on empty output. Fix: the `.so` is
  located and extracted via python3 zipfile (no unzip), every substitution
  pipeline is fail-soft (`|| true`) or refuses with an EXPLICIT `BUILD REFUSED`
  (empty cuobjdump -> `BUILD REFUSED: cuobjdump produced no architectures` +
  the raw `cuobjdump --list-elf` output). NEW tests run the REAL C6c block
  (extracted by markers) under real bash: (a) works without unzip (was rc=127
  silent), (b) empty cuobjdump -> loud refusal, (c) sm_89-only -> `does NOT
  contain sm_120`. Acceptance walker: 7 refusal scenarios of the REAL whole
  script -- every non-zero exit must carry a `BUILD REFUSED` / `REGRESSION
  FAILURE` message (it also caught a silent `nvcc --version | tail -1` death,
  fixed the same way). Mutation: restoring the unzip-based C6c -> (a)-(c) RED.
* `d4cb10a` **P2**: the engine freeze diff (C14) moved AFTER the test phase
  (C9-C13), before C15 -- the pytest install is now covered by the diff and
  the gate runs at `KVP_BUILD_SKIP_TESTS=1`. NEW tests drive the REAL whole
  script with a stateful pip shim: a numpy mutation at the pytest install
  (tests ON) or at the wheel install (tests OFF) must refuse with `BUILD
  REFUSED: engine packages changed`; clean run passes. Mutation: moving C14
  back before C9 -> RED.
* `19f1ded`: r8 real-run fixtures adapted to the new C6c (python3 zipfile
  probes must run for real in the shim) -- fixtures adapt to the pipeline,
  gates unchanged.
Step->test table in docs/lmcache-build.* updated (x3); wording kept
"implemented, unit-tested; hardware validation pending".
Full suite runs (both repositories, round 12 final):
* kv-persistence: 566 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Round 11 (reference build pipeline C0-C16)
* `f6c4312`: `_incontainer_build.sh` rebuilt to the exact C0-C16 order
  (baseline freeze -> apt/git -> toolchain gate -> constrained build deps ->
  exactly one `pip wheel` -> wheel checks (files + BOTH native extensions +
  cuobjdump sm_120) -> install -> import-check from site-packages (`python3
  -P`) -> engine freeze diff -> minimal test deps -> staging without
  lmcache/ -> staged pytest with the new `kvp_assert_installed` plugin ->
  summary gate -> optional GPU step -> single-quoted identity.env). Host:
  identity.env sourced strictly, provenance fields via separate default
  lines (the old ${VAR:-{}} appended a stray `}`), KVP_TORCH_CUDA_ARCH_LIST
  forwarded.
* `c77dc6f`: test fixtures adapted to the new phase (python3 shim strips
  `-P`, recreates the wheel after C5's rm; toolchain stubs; staging
  fixtures; extractor accepts the staged pytest form; wiring test requires
  EVERY pytest run staged).
* Step->test table in docs/lmcache-build.*; mutations (duplicate pip wheel /
  old brace default / dropped -P) all caught.

Full suite runs (both repositories, round 11 final):
* kv-persistence: 557 passed, 26 skipped
* recipes:        443 passed, 5 skipped

### Round 10 review fixes (P0 + P1, one commit per item -- behavioral tests first)
* **P0-1** `425adec`: the in-container phase now builds the wheel FIRST
  (build deps incl. grpcio-tools==1.78.0 -> `pip wheel` -> wheel checks ->
  `pip install --no-deps --force-reinstall` -> import-check FROM site-packages
  via `python3 -P` with three `__file__` proof lines -> pytest). The old order
  imported lmcache from PYTHONPATH=$LMC_SRC BEFORE the build -- but the
  native modules and gRPC stubs only exist afterwards, so the phase always
  died ModuleNotFoundError("lmcache.lmcache_native"). Mutations (pre-build
  import / PYTHONPATH to source) red.
* **P0-2** `c0efd97`: wheel + image gates require BOTH native extensions
  (cuda_ops*.so AND lmcache_native*.so -- without the latter LMCache drops to
  CLI-only mode); optional redis/fs recorded as `native_extensions`; new
  KVP_NATIVE_IMPORT=OK marker in the strict build-image gate. Mutation
  (cuda_ops-only) red.
* **P1** `425adec`: TORCH_CUDA_ARCH_LIST comes from the project variable
  KVP_TORCH_CUDA_ARCH_LIST (image value is only the fallback, override named
  in the log); identity.env records TORCH_CUDA_ARCH_LIST_USED + cuda_ops_archs
  (cuobjdump --list-elf must list sm_120 or BUILD REFUSED); provenance.json
  takes torch_cuda_arch_list / cuda_ops_archs / native_extensions from
  identity.env -- it now reflects what was really built. Mutation (old
  image-fallback) red.
* **Tests follow-up** `d52ba18`: r8/build-image/live-image fixtures follow
  the new phase order.

Full suite runs (both repositories, round 10 final):
* kv-persistence: 549 passed, 26 skipped
* recipes:        443 passed, 5 skipped

### Round 9 review fixes (P0 + P1, one commit per item -- behavioral tests first)
* **P0** `20fafcf`: the vLLM image ships NO git -- the in-container phase died
  rc=127 on its first line (`git config`). Fix: `apt-get install git` right
  after the mandatory `apt-get update` (fail-loud, apt tail visible), the
  `git config` safe.directory line moved below it, and after `pip wheel` the
  wheel's `lmcache/` listing is checked against `git ls-files lmcache`
  (non-py, non-so) -- without git the setuptools_scm file-finder silently
  drops package data from the wheel. External-program audit table in the
  commit. Mutation (git config above the install) red.
* **P1-1** `0ad1f2d`: KVP_BUILD_GPU_TESTS=1 now passes `--gpus all` (array
  args) with an nvidia-runtime pre-flight BEFORE docker run; the GPU step
  requires torch.cuda.is_available() and parses the pytest summary like the
  CPU gate -- a module-level skip is NOT a pass (skipped != 0 or passed == 0
  -> REGRESSION FAILURE with SKIPPED rows). GPU_REGRESSION_RUN=true only
  after a real passed run. Mutations (skip check removed / --gpus removed)
  red.
* **P1-2** `18e0f5d`: provenance.json records the BOOLEAN `gpu_regression_run`
  (the field the r8 commit promised but never wrote); identity.env variable
  renamed to GPU_REGRESSION_RUN (the name the host sources); docs x3 clarify
  tests_skipped = whole-phase skip via KVP_BUILD_SKIP_TESTS=1, not pytest
  skips. Mutation (field removed) red.

### Round 8 review fixes (P0 + P2, one commit per item -- behavioral tests first)
* **P0** `03029c2`: the GPU-integration suite `test_cache_server.py` (module
  skip without CUDA) no longer gates the GPU-less build. Gate = FIVE CPU
  files (skipped==0); the GPU suite runs only with `KVP_BUILD_GPU_TESTS=1`
  (--gpus all); `provenance.json` records `gpu_regression_run`; gate failure
  lists the SKIPPED rows from `-rs`. Checked the five files: no skipif /
  pytest.skip; only `importorskip` in test_vllm_mp_adapter (importable in the
  vLLM image). Mutation (suite back in the gate) red.
* **P2-1** `f78b7d0`: root-owned build artifacts -- `trap EXIT` chowns
  /usr/src/lmcache /kvp-wheels /kvp-work back to KVP_HOST_UID:GID (also on
  failure); build-lmcache.sh forwards the host uid/gid. Mutation (trap
  removed) red.
* **P2-2** `29d51cd`: GPU_CC in identity.env is shell-safe (`12.0`, not the
  tuple `(12, 0)` that bash parses as an array). Mutation (tuple back) red.

### Round 6-7 review fixes (P0 + P1, one commit per item -- behavioral tests first)
* **A-1/A-3/A-4** `68f68c9`: in-container build phase moved from the stdin
  heredoc to a mounted FILE (`scripts/_incontainer_build.sh`). Reproduced on
  the operator machine: `docker run` WITHOUT `-i` left `bash -s` an EMPTY
  stdin, the phase silently vanished and the script died 'no wheel produced'
  into empty output. The docker stub now behaves like REAL docker (stdin only
  with `-i`; a mounted file in the argv is what runs) and the failure carries
  docker rc + work dir + last 20 lines of pytest-report.txt. All other
  stdin-heredoc sites audited (build-image.sh fixed in A-2; probes use -c).
* **A-2** `9223306` + `6ebaa04`: build-image.sh verify phase is a mounted
  FILE (`scripts/_verify_image.py`) and a STRICT marker gate demands
  `KVP_CUDA_OPS_IMPORT=OK`, `KVP_PY_IMPORTS=OK`, `KVP_IDENTITY_FILE=OK`,
  `KVP_GPU_SMOKE_OK_COUNT == KVP_GPU_COUNT > 0` BEFORE anything is recorded
  -- the old heredoc produced an empty verify.txt and a FAKE success.
* **B-1** `8937148`: ONE pin file `vendor/pinned-base-image.env` for the base
  vLLM image. Clean clone builds with NO `export IMAGE`; `KVP_BASE_IMAGE`
  overrides; `IMAGE`/`KVP_LMC_IMAGE` are ignored (warned) in the build
  scripts; a floating tag is refused and the refusal names the pin file.
* **B-2** `049a90f` (kv) + `71dc08c` (recipes): custom image id recorded in
  `live/image` ONLY after the verify gate; `start-cache.sh`/`start-all.sh`
  and the KV_PERSIST=1 recipe launchers pick it up when `IMAGE` is empty; a
  failed smoke never creates or overwrites the record.
* **B-3** `4db5b3f`: pin consistency -- the digest lives ONLY in the pin
  file (build-image.sh header de-hardcoded); cross-check test against
  mimo-v2.6-pro-hotsplit's `tp4.env.example` (skips with a clear reason
  without a recipes checkout).
* **B-4** `10df544`: pre-flight checks in build-lmcache.sh, each refusal with
  ONE 'what to do' line (docker missing / daemon unreachable / base image
  pull size / free space), plus 'Zero to running in three commands' quick
  start in README EN/RU/ZH with the base-vs-custom image distinction.
* **Tests follow-up** `3cac6c9`: static wiring tests follow the A-1 file
  move; quick-start wording keeps the monitoring-before-cache order.

Full suite runs (both repositories, round 6-7 final):
* kv-persistence: 533 passed, 26 skipped
* recipes:        443 passed, 5 skipped

### Round 5 review fixes (P1, one commit per item -- behavioral tests first)
* **P1-1** `df8f096`: reset refused a REAL pre-marker cache dir (the allowlist
  did not know the artifacts the LMCache adapter actually writes). Source of
  truth `fs_l2_adapter.py` / `key_codec.py`: `encode_fs_filename` ->
  `<name>.data` per ObjectKey, temp files fall back to `final.with_suffix('.tmp')`
  or land in the configured `relative_tmp_dir` sub-dir. Added to the
  allowlist: `*.data`, `*.tmp` (narrow masks, no bare `*`) and the
  `relative_tmp_dir` top-level name read from the cache's own
  `l2-adapter.rendered.json` (no key -> nothing added). Behavioral tests
  (test_reset_legacy_cache_allowlist.py): legacy cache without marker resets
  WITHOUT a manual touch; one foreign file refuses with an unknown listing
  naming exactly the foreign entry; an UNconfigured tmp-ish sub-dir stays
  refused. Mutation (`*.data` removed) red.
* **P1-2** `c5d75f0`: cache-root marker documented for operators x3
  (EN/RU/ZH, docs/lmcache-build.* reset section): why the marker exists and
  that start-cache.sh creates it at cache init; the exact pre-marker
  procedure `ls -A <KVP_CACHE_ROOT>` + `touch <KVP_CACHE_ROOT>/.kvp-cache-root-marker`
  (byte-identical to the hint in the reset refusal, where `ls -A` was added
  to keep doc and script synchronized); unconditional refusals
  ($HOME / /root / /home/<user>, checkout ancestors, .git at depth <= 2).
  Verdict wording kept "implemented, unit-tested; hardware validation
  pending". No static word-check tests, per the round's rule.

Full suite runs (both repositories, round 5 final):
* kv-persistence: 515 passed, 26 skipped
* recipes:        443 passed, 5 skipped

### Round 4 review fixes (P0/P1, one commit per item -- behavioral tests first)
* **P0-1** `0d20ce6`: CUDA dev-package suffix computed wrong (`%.*` answered
  `13` for torch CUDA `13.0` -> nonexistent `cuda-cudart-dev-13`, build
  always "BUILD REFUSED"); no `apt-get update` (the vLLM image purges apt
  lists); apt failures hidden behind `>/dev/null 2>&1`. Fixed:
  `scripts/_cuda_dev_suffix.sh` (single source, `major-minor`:
  13.0->13-0 / 13.0.2->13-0 / 12.8->12-8), `apt-get update -y` before the
  install loop, last 20 lines of apt output printed on any refusal. Also P1:
  empty pytest summary now fails explicitly with "pytest summary not found".
  Behavioral tests (test_build_lmcache_cuda_dev.py) extract the real
  in-container apt block and run it with a stubbed apt-get; suffix verified
  by direct helper invocation; mutation of `%.*` red.
* **P0-2** `2493925`: reset security regression -- r3 P1-2 dropped the
  protection together with the /mnt//home blocks, so an explicit
  `KVP_CACHE_ROOT=$HOME` (cache.env typo) wiped the home directory. Fixed:
  an explicit cache root must prove itself -- `.kvp-cache-root-marker`
  (created by start-cache.sh at cache init) or EVERY top-level entry from the
  known LMCache/kvpersist set (lmcache-server.log/.pid,
  l2-adapter.rendered.json, engine-identity.env, index.db, crash-*,
  objects/, kvp-*, *.bin, *.bin.tmp.<uuid>, .kvp-active-writes). ALWAYS
  refused: $HOME (current and any /home/<user> / /root), any ancestor of the
  checkout, any dir with .git at nesting depth <= 2. Refusal lists the
  unknown entries + hint to inspect and mark the dir manually. `/mnt/<x>/<dir>`
  marked or known-set stays accepted. Behavioral tests (6 cases in
  test_reset_explicit_root_policy.py) -- red first, mutation of the known-set
  check red.
* **P0-3** `58a1866`: --deep existed in check_image_identity.py, but
  verify-image-custom.sh and _kvp_require_custom_image invoked the module
  WITHOUT --deep, so every real launch stayed LABEL-only. Fixed: both entry
  points run the deep path BY DEFAULT; KVP_VERIFY_SHALLOW=1 is the sole
  explicit test-stand opt-out (same category as KVP_SKIP_IMAGE_CHECK). Deep
  now cross-checks the REAL installed version
  (importlib.metadata.version('lmcache') == identity's wheel_version),
  refuses any identity without the `+kvp.` local part and refuses when the
  metadata cannot be read. Behavioral tests (test_deep_default_wiring.py)
  run the real verify-image-custom.sh against a docker stub (labels OK,
  identity cat fails -> rc != 0); mutation of `--deep` red.
* **Housekeeping** `7ae5464`: exec bit on `_cuda_dev_suffix.sh` (100755).
### Round 3 review fixes (P0/P1, one commit per item -- behavioral tests first)
* **P0-1** `5f45986`: build-image.sh aborted on FIRST run -- the Dockerfile
  heredoc used `$PR4136_SHA`/`$PR5972_SHA`/`$WHEEL_SHA_ID` before computing
  them (`unbound variable` under `set -u`). Fixed: compute all three sha256
  vars first, drop the later duplicate. Behavioral test
  (tests/unit/test_build_image_runs.py) runs the real script with stub docker
  and tmp fixtures (KVP_PATCH_DIR/KVP_BUILD_DIR/KVP_IDENTITY_OUT -- no writes
  into repo live/); mutation detector restores the buggy order and asserts the
  exact crash.
* **P0-2** `b1afd2e`: git dubious-ownership + wheel version pin. Container
  runs as root over a host-uid mounted /usr/src/lmcache; git refused to
  read the repo ("detected dubious ownership") and setuptools_scm had no
  fallback_version -> the wheel came out 0.0.0 / 0.1.dev. Fixed:
  `git config --global --add safe.directory /usr/src/lmcache` before
  `pip wheel`; `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=0.5.7.dev428+kvp.<12
  hex of PATCHED_STATE>`; `compare_wheel_version()` added to
  kvpersist/lmc_identity.py (single source; a *bare* upstream nightly string
  is refused even if it matches). Provenance + baked identity record
  wheel_version. Behaviorally-verified dubious-ownership test SKIPS in this
  sandbox (chown is a silent no-op on tmpfs) -- the test runs and proves the
  bug on the operator stand.
* **P0-3** `390b6a7`: CUDA dev-header install in the build container. The
  vLLM runtime image ships nvcc + cuda-cudart but NOT cuda-cudart-dev /
  libcusparse-dev / libcusolver-dev / libcublas-dev (vLLM's own Dockerfile
  purges them after its source-build stage). `apt-get install -y
  --no-install-recommends` of the 4 packages, suffix derived from
  `torch.version.cuda` (13.0 -> 13-0); missing package aborts the build
  NAMING the package (no `|| true`). `export CUDA_HOME=/usr/local/cuda`. The
  derivative Dockerfile stays free of dev packages (test: build-image.sh's
  Dockerfile carries zero apt-get/-dev strings).
* **P1-1** `d7275a5`: pytest exit code captured (no more `|| true`), skip
  counts from the pytest SUMMARY line (never `grep '^SKIPPED'` rows).
* **P1-2** `12ccd8e`: reset path policy -- explicit KVP_CACHE_ROOT wins over
  /mnt /home blanket refusals (deep NVMe mounts), strict '/' prefix boundary
  (root .../l2 no longer swallows sibling .../l2-old), KVP_LIVE_DIR env for
  test-run staging (never the repo live/).
* **P1-3** `a2deeaf`: deep image verification beyond LABELs (LABELs are
  forgeable): reads baked /etc/kvp-lmcache-identity.json via
  `docker run --entrypoint cat`, cross-checks patch shas
  vs vendor/patches/lmcache, and probes find_spec('lmcache.cuda_ops') via
  `docker run --entrypoint python3` -- any mismatch aborts launch. CLI flag
  `--deep`, wiring via verify-image-custom.sh.
* **P1-4** `docs`: honest wording "implemented, unit-tested; hardware
  validation pending" replaces unchecked "verified/works" claims about the
  race fix, the custom build and the native extension (EN + RU + ZH); new
  section 5b "Implementation stages" records the 6-step hardware validation
  chain, to be reworded "validated on hardware" only after it runs.

### Round 2 review fixes (P0/P1, one commit per item)
* **P1-1** `87b1083`: restored `nixl==1.5.2`/`openai`/`zstandard`/`lz4` pins
  dropped by the r1 rewrite (the L2 adapter refused to start without nixl, the
  CLI died without openai); pin test retargeted to the new contract.
* **P1-2** `95fa1a8`: build identity baked into the image
  (`/etc/kvp-lmcache-identity.json`, base + patch SHA-256 + wheel SHA-256) and
  verified in the gate (MISMATCH -> exit 4); `PATCHED_STATE` hashes diff
  content; `pytest -rs` with skipped==failure; honest docs x3.
* **P0-2** `03fe204`: wheels without `cuda_ops*.so` are REFUSED (the silent
  no-nvcc fallback caused the cudaMemcpy error 1 incident): nvcc preflight,
  `BUILD_WITH_CUDA=1 TORCH_CUDA_ARCH_LIST=12.0`, in-wheel .so check, honest
  GPU smoke of the native kernel on every GPU (`--gpus all`), server-log
  fallback warning.
* **P0-3** `4fdff85`/`1678964`: reset-persistent-cache.sh rewritten --
  diagnostic log/pid/rendered-json/crash-* preserved (archived to live/),
  symlinked cache roots really cleared (resolved-path walk + leftover
  post-check), dedicated-cache confirmation (resolved-path compare so a
  symlinked KVP_CACHE_ROOT works), network-FS refusal, live host-MP detection
  (stale pids ignored), full spec 7.4 report. 9 tests.
* **P0-1** `a259929`: custom-LMCache-image mandate at launch -- stock vLLM
  images refused by start-cache.sh (default `KVP_LAUNCH_MODE=docker`) and by
  both recipe launchers under KV_PERSIST=1 (image LABELS vs patch SHA-256
  before model load, `verify-image-custom.sh`); bare image-ID pin
  (`sha256:<64hex>`) accepted for locally built derives. 13 tests.

### LMCache integration: two required upstream fixes + cache reset
* `vendor/patches/lmcache/` created: `4136-mp-retrieve-abort.patch` (retrieve/
  abort race: explicit `LOADING` lifecycle, scheduler-confirmed abort
  propagation to workers, invalid store-completion suppression, abort
  dedup), `5972-incomplete-kv-objects.patch` (post-downsampling block
  availability check; incomplete objects with a `null_block_id` are rejected
  before persistence, independently complete groups still stored), and
  `SOURCES.md` with pinned base commit + per-PR source revisions and SHA-256
  checksums. PR #6551 confirmed already merged in the base (verified by
  `git merge-base --is-ancestor`, no local duplicate).
* `scripts/build-lmcache.sh` (new): reproducible custom LMCache build inside
  the pinned vLLM image; verifies base revision and #6551 ancestry; applies
  both patches deterministically (fails loudly if a patch does not apply or is
  already applied); runs focused upstream regression suites for #4136 and
  #5972; builds the wheel with `--no-build-isolation` against the pinned
  PyTorch; records full source identity + wheel SHA-256 in
  `live/lmcache-build/provenance.json`.
* `scripts/build-image.sh` (new): builds the custom derivative image from the
  pinned vLLM digest, installing exactly the custom LMCache wheel; preserves
  vLLM/PyTorch/CUDA/Python bit-for-bit; writes `live/image-identity.env` with
  the resulting digest + runtime identity; this single image serves the shared
  MP cache server AND every model worker.
* `scripts/reset-persistent-cache.sh` (new): the ONLY destructive operation
  (one-time migration). Requires `KVP_RESET_CONFIRM=FULL-RESET`; refuses
  unsafe paths (`/`, homes, models, repos, escaping symlinks); forces/verifies
  services stopped first; writes `live/cache-reset-report-<UTC>.txt`. Never
  called by start/stop/restart.
* `docs/lmcache-build.{md,ru.md,zh.md}` (new): LMCache build identity, custom
  image deployment, L1/L2 + cache identity + STORE/RETRIEVE diagnostics
  ("STORE_END is not proof of a persistent write"), safe cache-reset
  procedure, cache-miss diagnosis, and the explicit future migration procedure
  to remove the two downstream patches when PR #4136/#5972 merge upstream.

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



