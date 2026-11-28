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
### Round 33 (D prompt calibration, "first byte" = first BODY byte, warmup gate, chunk-size grep, empty-report trap, local stalled connect)
**Review (Nov 27, 2026, kv 4ef6a8d / recipes 273506d):** two blockers from r32
(D-prompt undersized; early = before headers, not before the first body byte)
plus R3-R6 housekeeping items. Only `test-pr-checks.sh` and tests touched.
* **R1 (blocker -- D prompts calibrated)**: D prompts now use the SAME
  `PER_TOK` calibration as the C prefixes. Each D prompt is verified against
  the real `usage.prompt_tokens`; if the first attempt is below the target
  (>= 1.1 x), the prompt is rebuilt ONCE, a second miss is INCONCLUSIVE with
  both numbers. Clean-clone runs no longer end D early. The stale "2x" comment
  for the C sizing is corrected.
* **R2 (blocker -- before the first BODY byte)**: `early = 1` only when ZERO
  body bytes arrived before the close (not when the response headers are
  still missing -- Starlette sends `http.response.start` before the first SSE
  chunk, so headers alone say nothing about the retrieve window). The ABRT
  line carries `headers_seen=0|1`; the C report shows both shares and any
  late abort (r32.2) still waives its early mark.
* **R3 (warmup gate)**: the expected-store count is now
  `C_ROUNDS + 4` (probe + C prefixes + 3 D T0); T1 re-sends T0's prefix and
  may NOT produce a new store event in real LMCache -- T1 no longer counted.
  (Fragment of a real `lmcache-server.log` showing whether T1 emits a new
  `L2 store completed` line is left to the hardware run; if it does, this
  gate loosens safely.)
* **R4 (chunk-size grep)**: added `|| true` inside the `grep` substitution so
  a `-Eeuo pipefail` run without any `*.env` falls through to the ordinary
  INCONCLUSIVE text (exit code 5) instead of dying bare with code 2.
* **R5 (empty reserved report)**: `trap ... EXIT` removes a still-empty
  reserved `pr-checks-report-*.md` when the script exits early (the r32.2
  atomic reservation created the placeholder before the run reached the
  report block).
* **R6 (local stalled connect)**: the delayed-connect test no longer dials
  `10.255.255.1` (that ENETUNREACH-fails on route-less hosts); it saturates a
  local listener's accept queue instead and SKIPs with a reason when the
  kernel cannot stall a local connect -- identical behaviour with/without a
  default route.
* Trivia: `-- -> ---` in the TEST MODE banner, `stop_rc` declared `local`, the
  zero-store warmup text now mentions `LMCACHE_LOG_LEVEL` DEBUG.
* Tests: new `tests/unit/test_r33_regression.py` (7 tests) covering R1-R5;
  r32.2's R3 tests tightened (550 ms cap on a 300 ms window) and the R3
  tolerance note documented. Mutants M1-M5 (no PER_TOK in D, early=by
  headers, gate counts T1, no `|| true`, no EXIT trap) all RED.
* Hardware run NOT yet performed -- "verified on hardware" is intentionally
  absent from this entry until the run lands.
**Final follow-up review (Nov 27, 2026):** r32.1's fixes were accepted; two
narrow issues closed here without touching R1/R2/R6/R7.
* **R3 (deadline enforcement)**: `abort_stream` now uses `time.monotonic()`
  (never wall clock) and ONE absolute deadline covering connection
  establishment, request transmission AND response reading -- connect and
  send run non-blocking (`select`-bounded, no `create_connection(timeout=30)`
  / `sendall()` blocking), so a stalled connect or a backed-up TCP window can
  no longer blow past the abort window and masquerade as an early abort.
  The recorded `ms` is the REAL close time and each abort line now carries
  `late=1/0`: any close past window + tolerance (`KVP_ABORT_TOL_MS`, default
  250 ms) is `late=1` and is WAIVED from the early-abort count. Scenario C
  reports early/late/after-body separately and any `late` abort makes the
  timing proof invalid -> INCONCLUSIVE (never a false PASS from late aborts).
  Tolerances tightened: deadline-accuracy tests now allow window + 250 ms
  (300 ms window -> 550 ms cap), not 1500 ms.
  New deterministic tests (5): delayed connection (unroutable black-hole
  address), send backpressure (accepting server that never reads), normal
  deadline-respecting operation with the tightened bound, late aborts tagged
  `late=1` and never counted as early (zero-tolerance run makes this exact),
  plus the R8 concurrency test.
* **R8 (atomic report names)**: the timestamped report name is reserved with
  an ATOMIC `O_EXCL` create (`set -C; : > file` -- kernel-unique) instead of
  a check-then-create loop, so two concurrent runs in the same UTC second
  can never write the same file.
* Tests: `tests/unit/test_r322_regression.py` (5 tests) + the r32.1 suite's
  R3 tolerances tightened. Mutants (4): blocking-create_connection+sendall
  client, wall-clock deadline, late-counted-as-early, non-atomic filename
  loop -- all RED on their matching tests.
* Hardware run NOT performed (per the review: until R3 passes the new
  deadline-enforcement tests -- it does, on the machine-free suite).
**Follow-up review (Nov 25, 2026):** r32 accepted R1-R10's shape but its tests
were still not proofs. This round keeps the working implementation and fixes
only what the review listed.
* **R3 (the core fix)**: `abort_stream` now runs a raw deadline-driven
  client (no `http.client`): the request is sent over a plain socket and the
  client SELECTs until the abort deadline and closes exactly then, even if
  the server has not sent any response headers yet. The first byte is
  detected from the full received buffer (nothing buffered is bypassed),
  and `before_first_byte` is `1` only when the response never started.
  New tests prove the close LANDS at the deadline (thumbnails: before-headers
  at 300 ms, mid-body at 400 ms, silent server at 250 ms) -- the old version
  blocked in `getresponse()` and would close ~3 s late.
* **R1**: fixed `rc=$?` capture after `if ! cmd` (it always reported 0);
  a successful `docker stop` is now verified by inspecting for `exited`/
  `dead`/gone (a running container after a `stop`-rc-0 is INCONCLUSIVE,
  not a pass); a SKIPPED or MISSING IPC pre-flight is named in the very
  first TEST MODE line (a bypassed CUDA-IPC boundary is not a hardware proof).
* **R2**: the warm-up requires the FULL expected L2 store count
  (`C_ROUNDS + 7` for probe + prefixes + D prompts), not just `> 0`, and
  each prefix is warmed exactly once (the old quiet probe warmed prefix 1
  a second time).
* **R6**: the hardcoded `D_CHUNK=384` fallback is removed (no chunk size ->
  INCONCLUSIVE, never a guessed target); token comparison only when ALL
  T0/T1/T2 carry logprobs, words for all three otherwise.
* **R7**: TEST MODE is literally the first line of the report (even before
  the title, computed before any key/docker check); real abort LAUNCHES are
  counted separately from the expected plan.
* **R8**: the GPU name line keeps full multi-word names (`2x NVIDIA RTX
  PRO 6000`, not `2x NVIDIA RTX`), report names are collision-safe (same-UTC-
  second runs get a suffix instead of clobbering).
* Tests: 16 new deterministic regression tests
  (`tests/unit/test_r321_regression.py`), the old suite re-verified green.
  Machine test counts vary per host (the verified runs are `37 passed`
  for the combined r32+r32.1 stub suites).
* Mutants (r32.1): the getresponse-style blocking client (R3), `rc=$?`
  (R1), no-exited (R1), IPC bypass (R1), warmup `> 0` (R2), double prefix-1
  (R2), chunk fallback (R6), partial-logprobs tokens (R6), TEST MODE inside
  the report (R7), launch counter off (R7), truncated GPU names (R8), and
  collision-unsafe report name (R8) -- all red on their matching tests.
**Review (Nov 24, 2026, kv 9717cd1):** the r31 PR-checks script was rejected
(R1-R10 defects). This round rewrites it against the same findings.

* **R1**: ONE shared restart (`scripts/restart-lib.sh`) with the round-46
  order (engine stopped first, `-t 120` with retries, INCONCLUSIVE exit 5 on
  daemon failure). A tested `stop-cache.sh`/`start-cache.sh` refusal prints
  their last 20 output lines.
* **R2**: scenario C now PROVES a disk retrieve -- warmup stores long unique
  prefixes + the D prompts, one full restart, then aborts per round k use
  prefix k. Zero `L2 prefetch load completed` events during C → INCONCLUSIVE,
  not PASS.
* **R3**: `abort_stream` runs on raw `http.client` (no file-buffer noise) and
  records `before_first_byte=0|1 ms=<real> body_bytes=<n>` per abort. The
  C verdict requires ≥ `PR_CHECKS_MIN_EARLY_ABORT_PCT`% (default 50) early
  aborts AND `rounds × 4` printed lines; otherwise INCONCLUSIVE. Lost aborts
  count against the total. (Tests are deterministic via stub first-byte delay.)
* **R4**: the ERROR allow-list is a file of fixed strings (`grep -Fv -f`), not
  BRE asterisks -- an allowed line now really passes.
* **R5**: the API key travels only via `KVP_KEY` env (exported child python),
  never in argv (a python3 shim test proves it). Prompts travel via files, not
  argv (R6a: arg-length cap).
* **R6**: D prompt length is calibrated by `usage.prompt_tokens` (target ≥
  `2 × sliding_window`, or `4 × lmc_chunk` without that field). Comparison is
  token-level via `logprobs.content` (word fallback, 24-unit threshold);
  unequal lengths report the SHORTER answer's length (never -1). Zero L2 reads
  after the restart → INCONCLUSIVE. Table rows have identical cell counts.
* **R7**: any test knob prints `TEST MODE: <var> set -- this run is NOT a
  hardware proof` first in stdout and in the report; `PR_CHECKS_SKIP_RESTART`
  gives D verdict `SKIPPED`, never PASS; the report prints real numbers.
* **R8**: wheel = `lmcache` version inside the running container (`docker
  exec`); hardware = `nvidia-smi` GPU names; PR heads = the SHA `PR head`
  lines of `SOURCES.md`; timestamped report + `pr-checks-report.md` latest
  copy (old reports kept); the control section honestly says no control run.
* **R9**: `test-pr-checks.sh` described in all three CHANGELOG locales below.
* **R10**: per-check FAIL checkpoints (engine dead, stuck requests, answer
  changed, L2 store failures) with exact notes and exit 1; `nr=max/marker`
  mutants are red in the r33 verification run.

**Tests:** 20 machine-free tests (stub docker + stub API + python3 argv shim)
cover R1-R10. The D SHA extraction uses the `PR head` row of `SOURCES.md`.
Full-suite counts are machine-dependent; the review run is the reference.

### Round 31 (Q1-Q3 accepted; Q4 test-pr-checks.sh rewritten by r32)
**Review (Nov 23, 2026, kv 0dd4522):** Q1 (label immobility on late refusals),
Q2 (no shared /tmp paths), Q3 (CHANGELOG Round 30) were accepted. Q4 -- the
PR-checks script (r31) -- was rejected (defects R1-R10) and rewritten by r32.
The r31 Q4 script already contained the user-visible skeleton (scenarios C
abort/ D recovery over prefixes, restart between warm-up and the probe, a
markdown report per run) but its first version hung: a stub-only early-abort
metric counted "aborts that had already read response bytes" instead of
"aborts closed before the first byte", the ERROR allow-list was fed to grep
as `*text*` BREs and never matched, an unused key file existed while the key
was passed as argv, prompt lengths were counted in characters not tokens, and
scenario C never actually proved a disk retrieve (the prefix was warm in
memory). r32 fixes all of these -- see the Round 32 section above. What the
script proves and what it does NOT: it asserts no failure with the patches
present; it has no control branch (unpatched images are refused by the
identity gate) and therefore cannot prove the two PRs fix anything either.
Machine test counts are unreliable across machines; the review run is the
reference.
### Round 30 (P1-P5: honest tests for the r29 fixes -- fresh-clone kvp status, the log-start mark, SIGPIPE-proof tracebacks)
**Review (23 Nov 2026, kv 7f723e5 / recipes d943b4f):** the r29 code was
accepted, but the acceptance was not met: one red test in a clean clone,
two code defects and test gaps. All fixed here, r29 code untouched except
where named below.
* **P1**: `kvp status` died silently (rc=1) without `live/engine_container`
  (fresh clone): the `head|tr` substitution needed `|| true` INSIDE it under
  `set -e` (both status and logs lines fixed). The N2 test now runs against a
  tmp copy of the repo WITHOUT live/ -- genuinely machine-independent.
* **P2**: the log-start mark moved on refused starts (a second
  `start-cache.sh` against a live server slid it to the end of the log ->
  false `L2 writes: OK`) and `wc -c < missing 2>/dev/null` printed stderr
  noise on first start. `start-cache.sh` now writes the mark ONCE per start,
  after EVERY refusal (already running, codec, persist policy, adapter
  render, argv build), immediately before the launch, both modes; the size
  is taken via `[ -f ]`. Tests: refusals leave the mark byte-identical, mark
  0 without a log, mark == log size. (Q1 round 31 extends this to the late
  refusals.)
* **P3** (recipes, tests only): the fail-fast block now runs under the REAL
  `set -Eeuo pipefail` of the launcher with a 40-frame traceback + 5 MB DEBUG
  noise -- killing the surviving `exit` (rc 141 SIGPIPE) and marker-number
  mutants; `test_t6a` asserts `frame_39` again.
* **P4**: exact `_kvp_bound_block` arithmetic on line_NNN inputs without
  `strip()` (0/80/81/125 lines at 80/20, 100 at 40/10); the scanner survives
  a 9.4 MB log under strict mode (rc 1, not 141); the hand-copied test
  selector is gone -- selectors are cut from the real scripts.
* **P5**: selfcheck docstring names the signed line; r29 CHANGELOG notes now
  describe what the tests really do (all three locales); the collector dying
  between `State.Running` and the network inspect must not flip the exit
  code (N7++); garbage mark offsets are normalized under the real strict
  header.

**Tests:** r30 added 15 behavioural tests (P1-P5 + Q1 mark-immobility);
verified-red mutations: 11 (P1-P5 material + Q1 mark-before-render and
mark-before-argv). Full suite numbers are machine-dependent (the skip count
varies); the review run is the reference.
### Round 29 (N1-N7: finishing r28 -- real diagnostics values, honest `kvp status`, SIGPIPE-safe tracebacks, obs start regression)
**Review (22 Nov 2026, kv 9992bd6 / recipes 781a5b2 + hardware run):** the
hardware run PASSED (L2 writes 366212 KiB, test-recovery VERDICT: PASS,
D1 metrics 298 lmcache lines) but the "make L2 store failures visible" path
had two defects that defeated exactly that goal, plus test holes and an r28
regression.
* **N7** (first): `start-observability.sh` exited 1 silently under
  `set -Eeuo pipefail` when the cache container did not exist yet (the
  NORMAL start-all order: step 1/3 precedes the cache) -- start-all printed
  a false "observability failed to start". Both `docker inspect` assignments
  get `|| true`; the D1 block never flips the exit code.
* **N1**: `shapes`/`dtypes` in the L6 diagnostic were always `?` -- a double
  wrapper `_safe(_layout_field(...), "?")` called a VALUE as a function.
  Unwrapped; values print via str(), exceptions fall back to `?`. New tests
  assert exact values for a deterministic 1024/8192 pair (numbers computed
  independently) -- kills the surviving mutants M24/M25 too.
* **N2**: `kvp status` judged the WHOLE append-only log, so pre-fix failures
  stayed forever ("L2 writes: FAILING" on a healthy server -- confirmed on
  hardware). `start-cache.sh` records `wc -c` at every start into
  `lmcache-server.log.start`; `kvp status` passes it as the offset and the
  verdict names the period: `FAILING (N failed since this server started)`.
* **N4** (two halves): (a) `head -n 80` cut exactly the LAST exception line
  on deep stacks -- all five traceback sites now use `_kvp_bound_block`
  (keeps the first 20 + a `lines skipped` marker + the last 60); (b) the
  selector awk `exit` closed the pipe and tail died with SIGPIPE under
  pipefail (rc 141, script dies SILENTLY before `docker stop` / `return 2`)
  -- replaced with a `done` flag + `next` so the whole input is consumed.
  The T6 fixture is back to 40 frames.
* **N3** (five surviving r28 mutants now killed): M18 -- a REAL
  `python3 -m kvpersist.launch server --l2-adapter ...` run against a fake
  `lmcache` package (3 cases: kvp-serde installs + patched=True / no serde
  does not install / mutated source -> loud WARNING, patched=False, rc 0);
  M19/M20 -- the three `kvp_scan_l2_failures` call lines of test-recovery.sh
  are extracted from the script text by regex (one regex per call site) and
  then EXECUTED against a stub scanner that always fails, verifying the
  scenario tags (scenario pre / A / B) and the fail() wiring; M24/M25 --
  exact diagnostic values (in N1).
* **N5**: the recipes locator and the pinned fixture are machine-independent
  -- recipes path from `KVP_RECIPES_DIR`
  (else sibling ../recipes, else skip) and the pinned `_alloc_temp_buffers`
  source captured VERBATIM into `tests/fixtures/lmcache_alloc_temp_buffers.py.txt`
  (normalize-equal to
  REFERENCE_SOURCE; built into a temp lmcache module and installed by the
  REAL install() without any reference_matches monkeypatching; mutated
  source refused). The NEW N2 test however still read live/ of the working
  tree and was red in a fresh clone (r30 P1).
* **N6**: (a) `tr < "$_pidf"` before `2>/dev/null` printed
  "No such file or directory" in the normal "server not started" state ->
  guarded by `[ -f ]`; (b) install() is now idempotent (a second call in the
  same process returns `patched`, no double wrapping); (c) the selfcheck
  line is SIGNED: `serde_alloc_patch selfcheck: OK`.
* **Tests**: 16 new/updated behavioural tests (test_r29_n3.py 9,
  test_r28_serde_alloc.py +4, test_r28_diagnostics.py +3). 16 mutations
  verified killed (N1, N2, N4a-e, N6b, N6c, N7, M18, M19, M20, M-pre, M24,
  M25).
### Round 28 (L1-L9 + D1: NVMe stores were silently failing -- now loud and diagnosed)
**Hardware review (22 Nov 2026):** every L2 store batch ended "0 succeeded,
20 failed" (`kvp-serde: destination buffer too small` -- LMCache sizes the
temp buffers from objects[0] only) while the service looked healthy;
ipc-test blamed ECC on a real CUDA refusal; the traceback drowned in
DEBUG noise; test-recovery probed the wrong IPC boundary and the docker
CLIENT's I/O; the dashboard showed "No data" (the OTLP endpoint was
unreachable). Order of work: L6 -> L7 -> L8 -> L9 -> D1 -> L1-L5.
* **L6** (blocker): `compress_serde.serialize` refuses an undersized
  destination with `kvp-serde: destination buffer too small: dst_len=..
  need=.. frame=.. src_len=.. src_size=.. plaintext_by_layout=..
  estimate_by_layout=.. codec=.. shapes=.. dtypes=.. dst_size=.. dst_phy=..
  object_group_id=.. kv_rank=..` (diagnostics collected safely, never
  replacing the error); `kvpersist/serde_alloc_patch.py` sizes every temp
  buffer from the LARGEST object of the batch (drift-protected: installs
  only when the pinned `_alloc_temp_buffers` source matches, else a loud
  WARNING and L7 catches the failures); `--selfcheck` wired into
  verify-image-custom.sh. Test test_r28_serde_alloc.py reproduces the
  LMCache allocator VERBATIM over fake TensorMemoryObj objects (7 tests).
* **L7**: `kvp_scan_l2_failures LOG [FROM_BYTE]` (env.sh) prints the first
  error block (bounded, <= 40 lines, no DEBUG noise) + "L2 STORE FAILING:
  the cache server cannot write to NVMe..." and returns 1 -- called after
  every store request in test-recovery.sh BEFORE the indirect counters;
  `kvp status` prints one line `L2 writes: OK|FAILING (n failed)`.
* **L8**: `kvp_ipc_consumer_mode` (env.sh) decides host/host-root/CONTAINER
  for both the pre-flight and test-recovery.sh -- the docker-mode boundary
  is server-container <-> engine-container (the old test probed the wrong
  one and could pass while the real one was broken).
* **L9**: `kvp_server_pid` (env.sh) resolves the REAL server pid (docker
  `State.Pid` in docker mode, the pid file in host mode); `server_read_bytes`
  and the liveness probe follow it through `KVP_PROC_ROOT` -- the old code
  measured the docker-run CLIENT.
* **D1**: the server container joins the observability network of a running
  `obs_otel_collector` and pushes to `http://obs_otel_collector:4317` (the
  host.docker.internal rewrite could never work: the collector publishes
  4317 only on 127.0.0.1:4320 of the HOST). An explicit KVP_OTLP_ENDPOINT is
  used AS-IS (never rewritten); no collector -> metrics off with an honest
  "not detected" line; start-observability.sh NOTEs a stale server network;
  docs/security.md documents the network join.
* **L1**: the real consumer.py prints `consumer: pid=` FIRST (before any
  import), wraps import/pickle/fn(*args) in try/except ->
  `consumer: OPEN FAILED <type>: <text>` + traceback + exit 4; exit 1 only
  when the memory opened and the sum is wrong; the script reports
  `PIDs: producer=<n> consumer=<m>` and equal PIDs give the TEST INVALID
  BY CONSTRUCTION verdict (exit 3); rc=1 without a sum= line gets the
  ONE-UID verdict, never the ECC one.
* **L2**: all four `sed ... ,$p` traceback selections are BOUNDED (from the
  error line to the next timestamped line, <= 80 lines, mawk-safe) -- the
  300 s of "skipping eviction" DEBUG noise no longer buries the exception.
* **L3/L5**: `Consumer error:` extracts the real OPEN FAILED / CUDA_ERROR
  line from the output (not the verdict tail); rc=3 is reported as a
  test-construction bug, never as a boundary failure; the ipc-test output
  streams LIVE through `tee` (rc from PIPESTATUS[0], the temp file removed);
  the stray ")" in the consumer banner is gone.
* **L4** (test holes): 6 surviving mutants now killed by new tests -- the
  KVP_SAME_UID default (UNSET -> operator UID), SERVER_GID from `id -g`,
  the GID comparison in the K3 check, the exact `sudo chown -R <uid>:<gid>`
  command text, CONSUMER_USER defaulting to PRODUCER_USER, and the
  test-recovery.sh branches (bounded selection).
* **Tests**: test_r28_serde_alloc.py (7), test_r28_diagnostics.py (15),
  test_r28_l1.py (3), test_r28_l4.py (8) -- all behavioural (stub docker/id,
  IPC_TEST_DIR/IPC_CONTAINER/KVP_SHM_DIR/KVP_PROC_ROOT isolation, free
  ports). 19 mutations verified killed (incl. the 6 survivors of r27).
### Round 25 (G1-G10: readiness race fixed, live/ guard hardened, recipes isolated)
**Review (22 Nov 2026, HEAD 931a9d0):** the r24 readiness gate refused at 0s
(the container is created asynchronously), its ready limit ran 3-4x past the
configured seconds, the F2 stubs held fixed ports (repeated runs went red),
and one violation cascaded into every later test.
* **G1/G2**: readiness waits for CONTAINER CREATION (KVP_CACHE_CREATE_TIMEOUT,
  default 60s) and distinguishes "docker-run client died" / "container
  exited" / "not created yet"; the ready limit is REAL elapsed seconds
  (date +%s) and the refusal reports the real time.
* **G3/G4**: start-all.sh exports the resolved KVP_LAUNCH_MODE (the resolver
  runs in a subshell -- its own export is lost); every test stub gets a FREE
  port and a finalizer kills the fake servers (50x runs green).
* **G5-G10**: the live/ guard restores what it flags (one red test, the next
  green, live/image back to its original bytes) and costs O(1) per test in
  the number of files under lmcache-build/; the recipes suite runs against a
  per-session tmp COPY of kv-persistence (its live/ and observability/extras
  stay byte-identical); Round 21 restored in this CHANGELOG; the wiki
  documents the readiness/shutdown timers + a Troubleshooting entry.

### Round 24 (hardware blockers F1-F3 + test gaps E1-E4)
**Hardware log (22 Nov 2026, HEAD f45f3e3):** the cache server never started
(`vllm: error: unrecognized arguments: -m kvpersist.launch ...`) and the
readiness gate printed READY against a dead container.
* **F1** (P0): `--entrypoint python3` for the server `docker run` (the vllm
  ENTRYPOINT image rejected the command); + a static audit that every
  docker run with a non-vllm command sets --entrypoint.
* **F2** (P1): docker readiness = container alive + RPC pong inside
  (`docker exec mp_ping`), KVP_CACHE_READY_TIMEOUT=180 -- the old gate only
  checked the docker-run client pid and the Docker-proxy port.
* **F3** (P1): ONE launch-mode resolver (default docker, refusal on bogus)
  in start-cache / start-all / pre_flights -- three different defaults made
  the bare start-all refuse on a false "LMCACHE VERSION MISMATCH".
* **E1** (P2): conftest no longer masks violations -- restore only the
  launcher-record allowlist, any other live/ or observability/extras write
  fails the test; cheap metadata snapshot; extras-up.sh honours KVP_OBS_DIR.
* **E2-E4**: refusal-before-launch proven for the docker branch + smoke +
  argv reader (3 surviving mutations killed); build-image identity-dir
  regression test; KVP_SMOKE_TERM_TIMEOUT validation.

### Round 23 (acceptance of Round 22: flake, live/ pollution, wrong socket type)
**Review (23 Nov 2026):** r22 tests were flaky (4/12 runs red -- the stub
answered ready before the argv was written and the SIGTERM trap installed);
7 test files wrote into the REPO's real `live/` (tests only passed because
earlier tests polluted the tree); the fd-channel probe used SOCK_STREAM
while the pinned lmcache uses SOCK_SEQPACKET (EPROTOTYPE misread as
stale -> smoke proceeded over a live server); an invalid
`LMCACHE_EVICTION_POLICY` still launched the server with an empty argv
(process substitution swallowed rc=1); 3 surviving mutations (B5-4/B5-5/B6-2).
* **D1** (P1): `ready`-file synchronization in every test stub -- the
  `mp_ping` stub waits for the launch stub to finish writing the argv and
  install the SIGTERM trap. 50x flake check green.
* **D2** (P1): `tests/conftest.py` -- an isolated `KVP_LIVE_DIR` for the
  whole session + a session GUARD that hashes the repo's real `live/`
  before/after and FAILS the run if anything changed; the helpers
  (`_read_argv_via_stub` / `_run_smoke_with_server_stub` / `_start_cache_argv`)
  own their tmp `live/` (image + image-identity.env written there, never the
  repo's). The clean-clone acceptance runs (no `live/`) exposed two more
  holes the polluted tree hid behind pre-existing artifacts: the launchers'
  contract records (engine_container, obs-relay/, .engine-env, ...) restore
  per test, and a REAL build blocker in build-image.sh (it wrote
  live/image-identity.env BEFORE mkdir -p of the directory -- a fresh-clone
  build died after a successful smoke).
* **D3** (P2): the fd-channel probe now connects with AF_UNIX SOCK_SEQPACKET
  (the pinned lmcache type, fd_channel.py:14,53,92) and fails SAFE: refuse
  unless staleness is PROVEN (FileNotFoundError / ConnectionRefusedError);
  EPROTOTYPE and any other OSError -> refuse. 3 new tests + 2 mutations.
* **D4** (P2): `_kvp_server_argv_read` builds the argv itself (temp file +
  rc check -- the old process substitution swallowed rc=1) and validates
  argv[0]=="server" via a bash nameref; both start-cache.sh branches and the
  smoke refuse BEFORE any launch on a bad `LMCACHE_EVICTION_POLICY`.
* **D5** (P2): separate closed/persist marker tests (B5-4 caught);
  `KVP_SMOKE_TERM_TIMEOUT` (B5-5 caught -- the wait was hard-coded);
  build-image.sh passes `${KVP_SMOKE_PORT:-15555}` to the smoke (B6-2
  caught -- it passed the production 5555).
* **D6** (P3): case-insensitive eviction-policy normalization (`${_p,,}`),
  stale r22 comment removed, `tests/tools/gen_pin_spec.py` committed (the
  pin fixture is reproducible from the lmcache source commit).
### Round 22 (smoke must run the command the pin accepts; operator pin; IMAGE paths)
**Hardware log (21 Nov 2026):** `build-image.sh` died on the server smoke --
`--eviction-policy: invalid choice: 'lru'` (the pin parser accepts LRU/ARC/
IsolatedLRU/noop, case-sensitive); after bypassing that the multi-line
adapter JSON split into argv elements ("unrecognized arguments") -- and that
was a r20 K6.3 regression that broke the PRODUCTION `start-cache.sh` too;
a clean SIGTERM shutdown exited 143, which the smoke called unclean.
* `139cb13` **B1-B6** (P0/P2): the server command the smoke runs is the
  command the pin accepts. B1: `_kvp_normalize_eviction_policy` -- default
  LRU, normalize (`lru`->LRU, `isolatedlru`->IsolatedLRU), refuse unknown
  values naming the variable and the four choices. B2: `mp_ping` imports the
  PIN path (`lmcache.v1.client.transport.factory`) first with a legacy
  fallback, code 3 when neither; the smoke stops IMMEDIATELY on exit 3.
  B3: the smoke loads the SHIPPED configs (`configs/generic/*.env`, no
  `*.local`) -- chunk-size 256, policy, watermark, ratio; `<chunk-gb>`
  renamed `<chunk-size>`. B4: argv is NUL-separated with a compact one-line
  adapter JSON and ONE shared reader `_kvp_server_argv_read` (the three
  former copies replaced). B5: clean exit = code 0 OR 143 (128+SIGTERM is
  NORMAL for this server) + both shutdown markers. B6: the smoke runs on
  its own port (15555, `KVP_SMOKE_PORT`) and refuses when the fd-channel
  socket is held by a live server. Spec fixture `pin_server_spec.json`
  extracted from the pin source 07f1c6bb (101 flags, AST walk, never
  hand-typed).
* `24d61f1` **M1-M4** (P2/P3): M1 -- the operator pin `KVP_LMCACHE_BUILD`
  works: the resolver exports `KVP_RUNNING_LMCACHE_BUILD` (its own name),
  the pin keeps precedence in `identity_from_env_files` and the gate (pin >
  KVP_RUNNING > read_lmcache_build, cross-check ALWAYS, mismatch -> exit 4
  with both builds); a real defect fixed (an env pin without a file entry was
  silently dropped); golden namespace `kvp-ef44c5ce3016e00c3f450ccdaac0cf9c`
  (computed on 7a9e615) is fixed in the test -- the cache from that commit
  stays valid. M2 -- the `IMAGE=sha256:<id>` and `IMAGE == live/image` paths
  are tested (stock override refused, explicit custom wins, broken
  live/image refused) + 2 mutation detectors; a real ordering defect fixed
  (the `== live/image` branch was unreachable behind the `sha256:*` case).
  M3 -- the dead `--print-wheel-version` flag removed, docs describe the real
  mechanism. M4 -- the r21 "mutation" tests (green at any state of the real
  code) replaced by DETECTORS; the namespace test computes it in the SAME
  shell after the resolver.
* `d67eab7` **docs**: standalone smoke one-liner for hardware diagnostics +
  "exit 143 after SIGTERM is normal" in `docs/lmcache-build` x3.
* Full run: see the follow-up counts in the report (kv-persistence and
  recipes, sequential, 0 failed).



### Round 21 (namespace bound to the running image, honest K6.4 test, test holes)
* `775cc64` **L1** (P1, K7 bound to the RUNNING image): `check_image_identity.py`
  `--print-wheel-version` was dead code; `cli model-name` derived `lmcache_build`
  from `live/image-identity.env` (the LAST build's record, not the image being
  launched); the gate cross-checked only with an operator pin (empty by default).
  Now `verify_custom_image` returns `wheel_version`, the CLI prints
  `KVP_WHEEL_VERSION=`, `_kvp_require_custom_image` captures it and exports
  `KVP_LMCACHE_BUILD` on every successful resolve path (except
  `KVP_SKIP_IMAGE_CHECK=1`). `read_lmcache_build` priority: env > baked identity
  JSON > live/ record. `verify-engine-identity.sh` cross-checks ALWAYS (pin or
  effective value) vs the container's identity JSON; mismatch -> exit 4 naming
  `lmcache_build`. Tests: scenario A!=B (live/ says A, image carries B) -- the
  namespace follows B; the gate without a pin refuses; 3 honest mutations RED
  (resolver stops exporting / gate only checks with a pin / module copy with
  live/ preferred).
* `626dcb1` **L2** (P1, the K6.4 test was green by accident): the r20 stub
  answered `image inspect` with `{}` -- start-cache.sh refused earlier on the
  custom-image check and the codec probe was NEVER reached. The stub now
  answers labels + identity of a real build (the deep check passes) and only
  the codec probe fails. Asserts: `lossless codec 'zstd' is not available`,
  `build-image.sh`, NO `MISMATCH`/`NOT the custom`, the server was NEVER
  started. Mutation: neutralizing the codec probe in a mutant start-cache.sh
  makes the refusal DISAPPEAR (RED detector).
* `cc675d8` **L3** (P2, two surviving mutants closed):
  **K1d** -- `test_b2_same_digest_other_repo_tag_accepted` (the same base
  digest under another repo:tag is accepted; a different digest is refused) +
  honest mutation (a COPY of `check_image_identity.py` with string comparison
  restored -> REFUSES the same-digest case). **K6c** --
  `test_smoke_runs_for_both_codecs_and_markers_gated` is now BEHAVIOURAL:
  the stub prints smoke markers for only ONE codec -> build refuses AND
  `live/image` is NOT written (both zstd-only and none-only).
* `d98f59c` **L4** (P2, honest error text): the "LMCache build is unknown"
  message names the REAL fix -- launch via `scripts/start-all.sh` (it resolves
  the image and exports the build) or run `bash scripts/build-image.sh`.
  No `/etc` in-image path, no `setup-identity.sh` hint for this cause (that
  hint stays only for identity.env pin errors). Tests: a complete identity.env
  without the resolver gives exactly this text; the gate's `lmcache_build`
  hint block names the real actions.
* `0f0248d` **L5** (P2, K7 test hygiene): `test_gate_crosschecks_lmcache_build`
  no longer writes `identity.env.local` into the REAL `configs/` -- it runs on
  a MINI REPO in tmp (configs copied + pins filled, real repo never touched).
  `test_mutation_dropping_lmcache_build_from_namespace_is_red` is now an HONEST
  mutation: a COPY of the identity module with `lmcache_build` removed from the
  hashed payload -> builds A and B collapse to ONE namespace on the mutant.
* `189ce4e` **L6** (P3, bare tag in IMAGE): `IMAGE=kvp-lmcache:latest` gets a
  clear refusal -- "IMAGE must be `repo:tag@sha256:<64hex>` (base pin) or
  `sha256:<id>` (custom), a bare tag is NOT accepted, take `$(cat live/image)`"
  -- not the misleading "base pin mismatch, rebuild" advice. A properly pinned
  stock still resolves (K1 path intact). Tests: behavioural, on the REAL
  `_kvp_resolve_runtime_image`.
* Full run: kv-persistence **716 passed, 26 skipped** (all skips environment:
  promtool, nginx, zstd/lz4, lmcache CLI, live-LMCache, hardware-only);
  recipes **448 passed, 5 skipped** (unchanged this round).

### Round 16 (4136 test fixtures, junit gates, engine-pin blind spot, fast rerun)
* `c354ba9` **P0** (hardware log 19.11 18:53: C0-C10 passed on the new base,
  C11 = 150 passed / 5 failed): all five failures in
  `tests/v1/test_lmcache_mp_connector.py` (the file the 4136 patch ADDS) --
  the upstream tests were written against the August PR head and built the
  connector via `__new__` with only part of the current `__init__` attributes
  (`_delay_free_req_ids`, `_can_store`, `_pinned_boundary_state_blocks`,
  `_lazy_offload_manager`); the request stubs lacked `num_computed_tokens`;
  one test asserted the obsolete `_pending_store` API (gone from production).
  Fixtures ONLY (production untouched, verified byte-for-byte): the helper
  sets the four attributes, stubs carry `num_computed_tokens=0` / `[0]` with
  strengthened `update_num_engine_computed_tokens(0, 32)` checks, and the
  lazy-offload test is rewritten against the manager API
  (`on_request_finished` -> `sessions_to_end=[]`, `end_session` not called).
  The patch was re-exported from the real patched tree (git diff, hunks not
  hand-edited); SOURCES.md records the adaptation + the new SHA-256.
  Expected C11 on hardware: 155 passed, 0 skipped. NEW static test
  (test_r16_patch4136_fixtures.py): fixture attributes present in the added
  lines, no `_pending_store`, SOURCES.md carries the current patch hash.
  Mutation: restoring `_pending_store` -> 2 tests RED.
* `b589b98` **P1** (hidden defect, prechecked): LMCache's pytest.ini has
  `log_cli = true` and real pytest decorates the summary line with `=` --
  the old `grep -E '^[0-9]+ (passed|...)'` matched NOTHING on a GREEN run ->
  "pytest summary not found" on a healthy suite (the r11-r15 stubs printed an
  undecorated summary, so coder tests never saw it). C12/C13 now count from
  the `--junitxml` report via the shared module (tests/failures/errors/
  skipped summed over all `<testsuite>`, passed = tests - failures - errors -
  skipped; rc==0 AND failures==0 AND errors==0 AND skipped==0 AND passed>0;
  missing/empty/broken XML -> explicit `REGRESSION FAILURE: junit report
  missing/unparsable`). The text grep is gone; the summary line is built by
  the script (`CPU gate PASSED (155 passed, 0 failed, 0 errors, 0 skipped)`).
  NEW tests (test_r16_c12_junit_gate.py) with REAL pytest in BOTH
  configurations (with the LMCache-style log_cli pytest.ini and without):
  green passes, skip refused with SKIPPED rows, failure refused, missing XML
  refused, plus a static no-text-grep gate. Mutation: restoring the text
  grep -> 5 tests RED.
* `d43576e` **P2** (hidden defect): `engine_pins` matched only `name==ver`
  lines -- a direct-reference freeze line (`vllm @ file:///...`) silently
  emptied both sides and the "engine unchanged" check passed blind. C14 now
  parses BOTH freeze formats (`name==ver` + `name @ url`, names normalised
  PEP 503, direct references pinned as `ref:<url>`), REQUIRES torch and vllm
  in the baseline freeze (`BUILD REFUSED: engine pins not found in baseline
  freeze (format not recognised)` + the first 20 baseline lines otherwise),
  and prints the compared pins (`torch==... vllm==...`), not just a count.
  NEW tests (test_r16_c14_engine_pins.py, the REAL C14 block): both formats,
  unrecognised baseline refused, changed torch refused with names+versions,
  unchanged passes with pins printed, PEP 503 name normalisation. Mutation:
  dropping the presence requirement -> RED. Fixtures adapted (r11 extraction
  marker + vllm line in the freeze fixtures; r12 pytest stub writes a minimal
  valid junit report).
* `6efc3cd` **P3**: `scripts/rerun-lmcache-gate.sh [WORK_DIR]` -- fast CPU-gate
  rerun WITHOUT rebuilding the wheel (~2 min iterations: the tests come from
  the source tree, not from the wheel). Re-applies the patches to a fresh
  pinned checkout, runs the r15 P1 import gate, takes the already built wheel
  (arg or the newest /tmp/kvp-lmc-build.* with wheels/*.whl) and runs C8-C12
  in the pinned image. Conditions: the PRODUCTION part of the patched tree
  (everything except tests/ and docs/) must match the tree the wheel was
  built from (hash in work_dir/rerun-prod-hash.txt -> "Need a full build" on
  mismatch); the end banner `NOT production-acceptable: no provenance/
  identity written` (nothing written to live/lmcache-build); the SAME gate
  logic as _incontainer_build.sh via the new shared module
  `kvpersist.gate_common` (junit-counts + check-imports), never a copy.
  NEW tests (test_r16_rerun_gate.py) run the REAL script end to end against
  a LOCAL mini git repo with REAL git-diff patches (only docker stubbed):
  hash mismatch refused, missing wheel refused, banner printed, nothing in
  live/, shared module used everywhere. Final acceptance is still ONLY the
  full build-lmcache.sh run.
* `58fceaf` **P4**: SOURCES.md corrections (5972 also touches
  worker_transfer.py / attn_window.py / 5 docs files; "20 files together" ->
  22 verified; the stale "rebased manually... docs-only changes were not
  carried over" wording is gone -- the patch is the exact PR diff). NEW tests
  (test_r16_shell_gates.py) extract the REAL #6608-ancestor-gate and the
  empty-GATE_SCOPE-refusal blocks from build-lmcache.sh and run them under
  real bash: a non-ancestor PR6608 aborts the build, an empty scope is
  refused with BUILD REFUSED. Mutations `if false;` / `true || {` -> RED.
Hardware validation status (new wording, x3 docs):
"implemented, unit-tested; hardware validation: C0-C10 passed on the
operator's host (new base 07f1c6bb6), C11 (4136 test fixtures) fixed in r16,
C12-C16 pending".
Full suite runs (both repositories, round 16 final):
* kv-persistence: 620 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Round 15 addendum (P0': move to the rebased PR #5972 base)
* `18e8cde` **P0'** (replaces the round-15 P0 above -- that fix stays in git
  history as the documented ROLLBACK variant: previous pin + old 5972 + the
  one-line import fix; history is not rewritten). The PR #5972 developer
  rebuilt the PR on `dev` by our request: the import is now
  `lmcache.v1.common.custom_types` and the last commit ("Unify retained-block
  validation across MP store paths") is substantive new code (17 files,
  +409/−117). Pinned: base `07f1c6bb6` (28 commits ahead of the previous
  pin, `dev` #6659), PR head `bd79d4c6` by exact SHA (head-moves rule in
  SOURCES.md). The patch is the EXACT `git diff 07f1c6bb6..bd79d4c6a` (not
  hand-edited). Verified on the real clone: 4136 + 5972 apply cleanly on the
  new base (20 files together), the r15 P1 import gate reports 0 problems;
  the new diff does NOT apply to the previous pin. Version label
  `0.5.8rc1.post48+kvp.<fp12>` (= `git rev-list --count v0.5.8rc1..<pin>` =
  48, PEP 440, not below the real base). #6608 (`bcafb0314`) gets the same
  ancestor gate as #6551. NEW tests (test_lmcache_pin_consistency.py): one
  pin everywhere, 40-hex, label tracks the pin base, patch order
  4136-before-5972, old pin/label gone outside historical CHANGELOG.
  Mutation: restoring the old SHA in one file -> 2 tests RED, verified.
  **Dashboard metrics re-check (numbers per the spec):** 117 `lmcache_*`
  metric names appear in the Grafana dashboards' PromQL (the reviewer's "108"
  could not be reproduced with a documented method -- the exact counting
  method was never fixed in the repo, so both counts are recorded honestly
  here). Source-side name literals in `lmcache/`: 223 (old pin) vs 224 (new
  base) -- 0 removed, 1 added (the `lmcache_nixl.` prefix of the new NIXL
  connector, out of `lmcache/` packaging scope anyway). Cross-check of every
  dashboard name against the source names + the coordinator's prefix
  concatenations: matched 80 / static-method-missing 37 -- IDENTICAL on both
  bases, so **lost by migration: 0**. The 37 static misses are a limit of the
  name-reconstruction method (composite names/suffixes), not a migration
  regression -- the canonical OTel→Prometheus mapping (METRICS.md, 90
  exported names) exists unchanged on the new base. The dashboards' `vllm:*`
  families come from vLLM itself, not from the LMCache pin.
Hardware validation status (new wording, x3 docs):
"implemented, unit-tested; hardware validation: C0-C8 passed on the
operator's host (old base); new base 07f1c6bb6 pending".
Full suite runs (both repositories, round 15 addendum final):
* kv-persistence: 598 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Round 15 (stale patch import at C11 + static import gate + r14 add-ons)
* `279f17e` **P0** (hardware log 19.11 14:57): C0-C8 passed, then C11 (test
  collection) died rc=2 -- `ImportError: cannot import name
  'IPCCacheServerKey' from 'lmcache.v1.multiprocess.custom_types'`. The 5972
  patch was made from the PR head (Oct 27); upstream c4fb19dd moved the class
  to `lmcache.v1.common.custom_types` AFTER that head -- the patch applied
  cleanly (fuzz) but was SEMANTICALLY stale ("applies cleanly" != "current").
  Fix: the patch is REBUILT from the real patched tree (apply to the pin, fix
  the import, re-export with git diff -- hunks not hand-edited); SOURCES.md
  Patch SHA-256 updated; the same stale import in our own
  tests/integration/lmc_protocol_client.py fixed (it silently disabled the
  protocol client under try/except). NEW tests (test_r15_patch_porting.py):
  no added (`^+`) line carries the stale path, 5972 carries the fixed path,
  every Patch SHA-256 in SOURCES.md matches the real bytes. Patch 4136
  audited the same way: the gate below reports 0 problems on both patches.
* `5c54612` **P1**: static import gate for the patched tree --
  `implementation/kvpersist/check_patched_imports.py` (the reviewed reference
  implementation, AST only, no torch/vllm) wired into build-lmcache.sh right
  after the patches/fingerprint, BEFORE the docker phase. Scope: files
  touched by the patches UNION the five CPU-gate tests UNION their transitive
  tests/... imports; an EMPTY scope refuses. Refusals say
  `BUILD REFUSED (stale patch/test import): <file>:<line>: ...`. NEW tests
  (test_r15_patched_import_gate.py, offline, real Python, synthetic
  mini-trees): (a) stale import refused rc=1, (b) fixed import rc=0,
  (c) try/except ImportError fallback not flagged, (d) PEP 420 namespace
  package accepted, (e) _proto_gen / lmcache.lmcache_* exempt, (f) empty
  file list refused, + the wiring check (the gate is CALLED in code before
  docker -- comment mentions do not count). Verified on the real pinned
  clone: 0 problems on the fixed tree, exactly 1 on the old 5972. Fixtures
  (test_docker_stdin_delivery / test_r9_gpu_step_honesty /
  test_r11_gates_real_programs) adapted to the gate. Mutation: removing the
  gate call -> wiring test RED.
* `ca1d449` **P2** (r14 review add-ons): C11/C13 pass KVP_LMC_SRC="$LMC_SRC"
  to the plugin (the source-tree guard was weaker than in C8), and the
  missing guard tests added: a distribution root INSIDE source_roots
  (editable-like) -> BUILD REFUSED, via assert_installed and via the plugin
  in real pytest (exit 4). Mutation: disabling the guard -> both RED.
Honest limit (per the spec): the gate catches stale import PATHS/NAMEs, not
behavioral drift -- a failing patched test means the PATCH gets ported, never
deselected/skipped/xfailed.
Hardware validation status (new wording, x3 docs):
"implemented, unit-tested; hardware validation: C0-C8 passed on the
operator's host; C9-C16 pending".
Full suite runs (both repositories, round 15 final):
* kv-persistence: 593 passed, 26 skipped
* recipes:        443 passed, 5 skipped
### Round 14 (C8 false refusal: dist-packages layout vs "site-packages" substring)
* `d15c5fc` **P0** (hardware log 19.11 12:52): C0-C7 passed (r13 confirmed on
  hardware), then C8 FALSELY refused a correctly installed wheel --
  `BUILD REFUSED: lmcache imported from
  /usr/local/lib/python3.12/dist-packages/lmcache/__init__.py, not
  site-packages`. The vLLM image is Debian/Ubuntu layout and pip installs
  into dist-packages; the recipe launchers already knew that, the build
  scripts did not (the r11 fixtures used site-packages paths, so the tests
  stayed green). Fix: the criterion goes by SUBSTANCE -- a new shared
  `assert_installed(mods, source_roots)` in scripts/kvp_assert_installed.py
  locates the installed lmcache distribution root via importlib.metadata and
  requires every module file UNDER it (any layout: site-packages,
  dist-packages, ...), with the root NOT inside the source tree
  ($KVP_LMC_SRC/cwd); refusal messages carry both paths (file, dist root).
  C8 imports the shared function (PYTHONPATH=$KVP_SCRIPTS) and the pytest
  plugin uses the same one (lazy `import pytest` so the module works before
  the test-deps install). NEW tests (REAL python3 -P + REAL plugin, fake
  installed distributions with dist-info/METADATA): (a) site-packages passes,
  (b) dist-packages passes (RED before: substring refused -- the exact
  hardware reproduction), (c) source tree -> BUILD REFUSED, (d)
  metadata/module mismatch -> BUILD REFUSED; the same through the plugin
  (exit 4). r11 layout test gains the dist-packages variant. Static gates:
  no substring criterion left (grep-clean), C8 uses the shared function.
  Mutation: restoring the substring check -> (b) x3 + lint RED, verified.
Hardware validation status (kept per the spec):
"implemented, unit-tested; hardware validation: C0-C7 passed on the
operator's host; C8-C16 pending".
Full suite runs (both repositories, round 14 final):
* kv-persistence: 581 passed, 26 skipped
* recipes:        443 passed, 5 skipped
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



