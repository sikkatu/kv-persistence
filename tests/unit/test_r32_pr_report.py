# SPDX-License-Identifier: Apache-2.0
"""r32: machine-free tests for scripts/test-pr-checks.sh + scripts/restart-lib.sh
(stub docker, stub cache scripts, stub API with controllable first-byte delay,
python3 shim logging argv). Covers r32's items: R1 restart order + docker-stop
failure -> INCONCLUSIVE; R2 disk-retrieve gate; R3 early-abort metric (late vs
instant first byte); R4 the fixed-string ERROR allow-list; R5 key never in
argv; R6 token divergence (fail window 32, warn beyond) + short prompt gate;
R7 TEST MODE banner + SKIP_RESTART -> D SKIPPED; R8 timestamped report + latest
copy, wheel/hardware/PR-heads, no key; R10 per-check FAIL lines + exit codes.
"""
from __future__ import annotations
import json
import os
import re
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
PRC = ROOT / "scripts" / "test-pr-checks.sh"

class _Stub(BaseHTTPRequestHandler):
    # r32 fix: the stub runs ThreadingHTTPServer (4 parallel C aborts must NOT
    # queue on the server -- a queued 4th abort waits past its abort window and
    # is misread as "early", the flake in test_r3_instant_first_byte...).
    # The shared counters/log writes go under a lock; the first-byte delay and
    # the response send stay OUTSIDE it (the abort timing must be real).
    _lock = threading.Lock()
    drift = None            # int|None: the 3rd sighting of a prompt diverges here
    delay_first_byte = 0.0  # seconds before ANY response byte
    stores_limit = 0        # r32.1 R2: >0 -> stop logging L2 store events after
                            # this many (simulates an INCOMPLETE warmup)
    no_store_phases = ""    # r33 R3: comma-separated phases that produce NO
                            # L2 store event (T1 resends T0's prompt and may
                            # not store again in real LMCache)
    pause_on_first = 0.0    # r32.1 R1/R3: sleep this long BEFORE the first
                            # response ever (holds first-byte for hung clients)
    stream_chunk_delay = 0.0  # r32.1 R3: for phase "stream", send the body in
                              # parts with this delay between them
    stream_body_tokens = 0    # r32.1 R3: token count for phase "stream" bodies
    llprobs = True          # serve logprobs tokens (else words, threshold 24)
    prompt_tokens = 0       # 0 -> derived from the prompt length
    len_token_divisor = 0.0 # r33 R1: >0 -> prompt_tokens = ceil(len(prompt)/divisor)
                            # (modeles a real tokenizer; 2.7 is a common chars-per-token)
    stuck = "0 0"           # vllm:num_requests_{running,waiting}
    read_events = True      # post-restart L2 read events in the server log
    fail_store_once = False  # append ONE store-failure line AFTER the restart
    fail_done = False
    fail_store_at = 0  # >0: append the store-failure at the N-th POST AFTER
                       # the restart (deterministic position inside C/D windows)
    fail_store_phase = ""  # when set: the failure lands on the FIRST such
                           # request phase (e.g. "d2" = inside the D window)
    post_n = 0
    _first_seen = False   # r32.1: tracks the very first request (pause_on_first)
    fail_dead_after = 0  # >0: create the 'dead' marker after N post-restart
                         # requests (the engine dies BETWEEN aborts and the C checks)
    turn_changed = False  # on the c-after phase return a DIFFERENT answer (S6)
    base_answer = ""        # the canned answer body
    seen = {}
    n = 0
    root = Path("/tmp/pcs")
    def log_message(self, *a): pass
    def _send(self, obj):
        b = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if self.path == "/v1/models":
            self._send({"data": [{"id": "mimo-pro"}]})
        elif self.path == "/metrics":
            run, wait = (self.stuck or "0 0").split()
            self._send(f"vllm:num_requests_running{{}} {run}\nvllm:num_requests_waiting{{}} {wait}\n")
        else:
            self._send("{}")
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(n))
            prompt = body["messages"][0]["content"]
            phase = (body.get("metadata") or {}).get("phase", "")
        except Exception:
            prompt, phase = "", ""
        delay = self.delay_first_byte
        toks = []
        with _Stub._lock:
            first = not _Stub._first_seen
            _Stub._first_seen = True
            _Stub.n += 1
            key = prompt[:32]
            _Stub.seen[key] = _Stub.seen.get(key, 0) + 1
            words = (_Stub.base_answer or " ".join(f"word{i}" for i in range(24))).split()
            if self.turn_changed and phase == "cafter":
                words = ["DIFFERENT", "answer", "now"]*3   # the post-series answer changed
            toks = [f"t{i}" for i in range(64)] if self.llprobs else []
            if self.drift is not None and _Stub.seen[key] == 3:
                i = self.drift
                if self.llprobs and i < len(toks):
                    toks = toks[:i] + ["KWON"] + toks[i + 1:]
                else:
                    words = words[:i] + ["KWON"] + words[i + 1:]
            # server-log evidence: store events everywhere, read events only after
            # the restart (post_restart marker made by the docker start stub)
            with open(_Stub.root / "lmcache-server.log", "a") as fh:
                _skip = phase and phase in (self.no_store_phases or "")
                if _skip:
                    pass                    # r33 R3: this phase stores NOTHING
                elif not self.stores_limit or _Stub.n <= self.stores_limit:
                    fh.write(f"[stub] L2 store completed: adapter 0, 1 succeeded, 0 failed #{_Stub.n}\n")
                if (_Stub.root / "post_restart").exists() and self.read_events:
                    fh.write(f"[stub] L2 prefetch load completed: request {_Stub.n}, 1 loaded, 0 failed\n")
                if (_Stub.root / "post_restart").exists() and self.fail_store_once and not _Stub.fail_done:
                    _Stub.fail_done = True
                    fh.write("[stub] LMCache ERROR Serde task 1 (SERIALIZE) failed after restart\n")
            if (_Stub.root / "post_restart").exists():
                _Stub.post_n += 1
                if self.fail_store_at and _Stub.post_n == self.fail_store_at and not _Stub.fail_done:
                    _Stub.fail_done = True
                    with open(_Stub.root / "lmcache-server.log", "a") as fh:
                        fh.write("[stub] LMCache ERROR Serde task 1 (SERIALIZE) failed after restart\n")
                if self.fail_store_phase and phase == self.fail_store_phase and not _Stub.fail_done:
                    _Stub.fail_done = True
                    with open(_Stub.root / "lmcache-server.log", "a") as fh:
                        fh.write("[stub] LMCache ERROR Serde task 1 (SERIALIZE) failed after restart\n")
            # the engine "dies" between the aborts and the C checks
            if (_Stub.root / "post_restart").exists() and self.fail_dead_after and \
                    _Stub.n >= self.fail_dead_after:
                (_Stub.root / "dead").write_text("1")
        if delay > 0:
            time.sleep(delay)
        if first and self.pause_on_first > 0:
            time.sleep(self.pause_on_first)
        if phase == "stream":
            # r32.1 R3: a real streaming body, sent in CHUNKS with a pause
            # between parts -- used by the direct abort_stream tests to prove
            # the client close is deadline-driven and mid-headers/mid-body.
            body_resp = json.dumps({
                "choices": [{"message": {"content": " ".join(words)},
                             "logprobs": {"content": [{"token": t} for t in toks]}
                             if (self.llprobs and toks) else None}],
                "usage": {"prompt_tokens": (-(-len(prompt) // int(self.len_token_divisor)) if self.len_token_divisor else (self.prompt_tokens or len(prompt.split()))),
                          "completion_tokens": len(toks) or len(words),
                          "prompt_tokens_details": {"cached_tokens": 0}},
            }).encode()
            count = max(3, int(self.stream_body_tokens) or 3)
            part1 = body_resp[: max(1, len(body_resp) // count)]
            part2 = body_resp[len(part1):]
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_resp))); self.end_headers()
            self.wfile.write(part1); self.wfile.flush()
            if self.stream_chunk_delay > 0:
                time.sleep(self.stream_chunk_delay)
            self.wfile.write(part2); self.wfile.flush()
            return
        self._send({
            "choices": [{"message": {"content": " ".join(words)},
                         "logprobs": {"content": [{"token": t} for t in toks]}
                         if (self.llprobs and toks) else None}],
            "usage": {"prompt_tokens": (-(-len(prompt) // int(self.len_token_divisor)) if self.len_token_divisor else (self.prompt_tokens or len(prompt.split()))),
                      "completion_tokens": len(toks) or len(words),
                      "prompt_tokens_details": {"cached_tokens": 0}},
        })

DOCKER_STUB = """#!/usr/bin/env bash
P="${STUB_ROOT:-/tmp/pcs}"
STATE="$P/engine_state"
[ -f "$STATE" ] || echo running > "$STATE"
case "$*" in
  *inspect*State.Status*) cat "$STATE" ;;
  *inspect*State.Running*) [ -f "$P/dead" ] && echo false || echo true ;;
  *inspect*State.Pid*) cat "$P/serverpid" 2>/dev/null || echo 1 ;;
  *inspect*Config.User*) echo "1000:1000" ;;
  *inspect*Labels*) echo sha256:stubhash ;;
  *logs*) [ -f "$P/engine.log" ] && cat "$P/engine.log" ;;
  stop\\ *)
      [ -f "$P/stop_fails" ] && { echo "stub: docker stop daemon failure" >&2; exit 1; }
      echo exited > "$STATE"
      echo ENGINE_STOP >> "$P/order.log" ;;
  start\\ *)
      echo running > "$STATE"
      echo ENGINE_START >> "$P/order.log"; touch "$P/post_restart"
      [ -f "$P/on-start.sh" ] && bash "$P/on-start.sh" ;;
  *exec*) echo "0.5.8" ;;
  *) echo stub-ok ;;
esac
exit 0
"""
def _cache_stub(marker):
    return "#!/usr/bin/env bash\necho %s >> \"$STUB_ROOT/order.log\"\nexit 0\n" % marker

def _start_api(tmp_path: Path):
    """Reset the stub knobs to healthy, put the root under tmp_path, bind a
    free port. Returns (server, port)."""
    _Stub.seen = {}; _Stub.n = 0
    _Stub.drift = None; _Stub.delay_first_byte = 0.0
    _Stub.stores_limit = 0; _Stub.pause_on_first = 0.0
    _Stub.no_store_phases = ""
    _Stub.len_token_divisor = 0.0
    _Stub.stream_chunk_delay = 0.0; _Stub.stream_body_tokens = 0
    _Stub._first_seen = False
    _Stub.llprobs = True; _Stub.prompt_tokens = 0
    _Stub.stuck = "0 0"; _Stub.read_events = True
    _Stub.fail_store_once = False; _Stub.fail_done = False
    _Stub.fail_store_at = 0; _Stub.post_n = 0
    _Stub.fail_store_phase = ""
    _Stub.fail_dead_after = 0
    _Stub.turn_changed = False
    _Stub.base_answer = " ".join(f"word{i}" for i in range(24))
    _Stub.root = tmp_path / "stubroot"
    _Stub.root.mkdir(parents=True, exist_ok=True)
    srv = None; port = 0
    for port in range(18100, 18400):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), _Stub)
            break
        except OSError:
            continue
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port

def _wc(parent: Path, name: str, text: str):
    p = parent / name
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p

def _run_pr(tmp_path: Path, port: int, *, rounds=1, abort_ms=100, settle="0",
            extra_env=None):
    """One run of the REAL test-pr-checks.sh against the stubs. Returns
    (CompletedProcess, stub_root)."""
    stub = _Stub.root                     # already created by _start_api
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(exist_ok=True)
    real_py = subprocess.run(["which", "python3"],
                             capture_output=True, text=True).stdout.strip()
    _wc(bin_dir, "docker", DOCKER_STUB)
    _wc(bin_dir, "id", '#!/usr/bin/env bash\ncase "$1" in -u) echo 1000;; -g) echo 1000;; *) echo tester;; esac\n')
    _wc(bin_dir, "nvidia-smi", '#!/usr/bin/env bash\necho "NVIDIA Test GPU"\necho "NVIDIA Test GPU"\n')
    _wc(bin_dir, "python3", '#!/usr/bin/env bash\necho "argv: $*" >> "$STUB_ROOT/pyargv.log"\nexec %s "$@"\n' % real_py)
    _wc(bin_dir, "python", (bin_dir / "python3").read_text())
    for name, marker in (("stop-cache.sh", "CACHE_STOPPED"),
                         ("start-cache.sh", "CACHE_STARTED"),
                         ("ipc-test.sh", "IPC_RAN")):
        _wc(stub, name, _cache_stub(marker))
    # preserve evidence files a test may have already written
    if not (stub / "lmcache-server.log").exists():
        (stub / "lmcache-server.log").write_text("boot\n")
    if not (stub / "engine.log").exists():
        (stub / "engine.log").write_text("")
    home = tmp_path / "home"
    (home / ".config" / "kvp").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "kvp" / "api_key").write_text("stub-key\n")
    (tmp_path / "live").mkdir(exist_ok=True)
    live_pid = subprocess.Popen(["sleep", "60"])
    (stub / "serverpid").write_text(str(live_pid.pid))
    env = dict(os.environ,
               HOME=str(home),
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               API_BASE=f"http://127.0.0.1:{port}",
               KVP_CACHE_ROOT=str(stub),
               KVP_SCRIPTS_DIR=str(stub),
               KVP_LIVE_DIR=str(tmp_path / "live"),
               KVP_ORDER_LOG=str(stub / "order.log"),
               STUB_ROOT=str(stub),
               CONTAINER="mock-eng",
               LMCACHE_CHUNK_SIZE="32",
               PR_CHECKS_ROUNDS=str(rounds),
               PR_CHECKS_ABORT_MAX_MS=str(abort_ms),
               PR_CHECKS_SETTLE=settle,
               KVP_STOP_RETRIES="2", KVP_STOP_WAIT_LOOPS="1",
               KVP_STOP_WAIT_INTERVAL="1")
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(["bash", str(PRC)], capture_output=True,
                           text=True, timeout=600, env=env, cwd=str(tmp_path))
    finally:
        live_pid.kill()
    return p, stub

def _order(stub: Path) -> list[str]:
    f = stub / "order.log"
    return f.read_text().splitlines() if f.exists() else []

# ===========================================================================
# R1: the ONE restart is engine-first (round 46); a failed docker stop is
#     INCONCLUSIVE (exit 5), never FAIL
# ===========================================================================
def test_r1_restart_order_is_engine_first(tmp_path):
    """> R1: ENGINE_STOP -> CACHE_STOPPED -> CACHE_STARTED -> IPC_RAN ->
    ENGINE_START in the order journal. MUTATION S12 (stop-cache before the
    engine stop) is red here."""
    srv, port = _start_api(tmp_path)
    try:
        p, stub = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    expect = ["ENGINE_STOP", "CACHE_STOPPED", "CACHE_STARTED", "IPC_RAN", "ENGINE_START"]
    j = _order(stub)
    first = {m: j.index(m) for m in expect if m in j}
    assert set(first) == set(expect), (
        f"missing markers: {set(expect) - set(first)} (journal {j})")
    got = [m for m in sorted(first, key=first.get)]   # in first-appearance order
    assert got == expect, f"restart order wrong: {got} != {expect} (journal {j})"

def test_r1_docker_stop_failure_is_inconclusive(tmp_path):
    """> R1: docker stop keeps failing and the container stays Running ->
    INCONCLUSIVE (rc 5) with the 'did NOT stop' text, never FAIL."""
    srv, port = _start_api(tmp_path)
    (_Stub.root / "stop_fails").write_text("1")
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 5, f"docker stop failure -> rc 5:\n{out[-1500:]}"
    assert "INCONCLUSIVE" in out and "did NOT stop" in out, out[-1500:]

# ===========================================================================
# R2: scenario C must really hit DISK -- zero L2 read events during C mean
#     nothing about a retrieve was proven -> INCONCLUSIVE (rc 5)
# ===========================================================================
def test_r2_no_disk_reads_during_c_is_inconclusive(tmp_path):
    """> R2: a restart that serves no L2 'prefetch load completed' events ->
    C is INCONCLUSIVE ('the retrieve did NOT come from disk'), rc 5."""
    srv, port = _start_api(tmp_path)
    _Stub.read_events = False
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 5, out[-1500:]
    assert "did NOT come from disk" in out, out[-1500:]

def test_r2_disk_reads_present_allows_c(tmp_path):
    """> R2: with read events present the disk gate passes (the C log line
    shows a positive count). Mutant 'no restart before C' is red in
    test_r1_restart_order_is_engine_first (the single chain)."""
    srv, port = _start_api(tmp_path)
    _Stub.read_events = True
    _Stub.delay_first_byte = 1.0    # aborts land early -> C's early gate OK
    try:
        p, _ = _run_pr(tmp_path, port, abort_ms=100)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    m = re.search(r"L2 disk-read events during C: (\d+)", out)
    assert m and int(m.group(1)) > 0, out[-1500:]

# ===========================================================================
# R3: the early-abort metric is deterministic on the stub's first-byte delay
# ===========================================================================
def test_r3_late_first_byte_all_aborts_early(tmp_path):
    """> R3: 1s first-byte delay, 0..100ms abort window -> every abort is
    before_first_byte=1 and every abort printed its line. NOTE: this test
    does NOT kill S2 (window 0) -- its assertions still hold at window 0.
    S2 is killed by test_r3_instant_first_byte_no_early_gives_inconclusive
    (window 0 makes every abort look EARLY there -> C is not INCONCLUSIVE)."""
    srv, port = _start_api(tmp_path)
    _Stub.delay_first_byte = 1.0
    _Stub.read_events = True
    try:
        p, _ = _run_pr(tmp_path, port, abort_ms=100)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    m = re.search(r"abort lines: (\d+)/(\d+), early \((?:before first byte|no body byte yet)\): (\d+)", out)
    assert m, out[-1500:]
    lines, total, early = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert lines == total, f"lost aborts: {lines}/{total}"
    assert early == total, f"1s delay -> all early: got {early}/{total}"

def test_r3_instant_first_byte_no_early_gives_inconclusive(tmp_path):
    """> R3: instant first byte -> 0 early aborts -> the early gate is not met
    -> C is INCONCLUSIVE (the retrieve window was not engaged). The abort
    window is deliberately WIDE (5000 ms): with 'instant' answers any real
    RTT must fall inside it, so a busy host can never fake an EARLY abort
    (that was the flake: a >100 ms RTT made early=2/4 at delay 0)."""
    srv, port = _start_api(tmp_path)
    _Stub.delay_first_byte = 0.0
    _Stub.read_events = True
    try:
        p, _ = _run_pr(tmp_path, port, abort_ms=5000)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 5, out[-1500:]
    assert "landed before the first response byte" in out, out[-1500:]

# ===========================================================================
# R4: the ERROR allow-list is fixed-string; allowed lines never FAIL
# ===========================================================================
def test_r4_allowed_error_lines_do_not_fail(tmp_path):
    """> R4: engine-log ERROR lines that ARE in the allow-list
    ('client disconnected by peer', 'connection reset by peer') leave C green
    (no 'outside the allow-list'). MUTATIONS S13 (all ERROR allowed) and S9
    (no ERROR check) are red via test_r4_disallowed_error_fails."""
    srv, port = _start_api(tmp_path)
    _Stub.delay_first_byte = 1.0
    _Stub.read_events = True
    (_Stub.root / "engine.log").write_text(
        "2026-11-24 12:00:00 ERROR connection reset by peer\n"
        "2026-11-24 12:00:01 ERROR client disconnected by peer\n")
    try:
        p, _ = _run_pr(tmp_path, port, abort_ms=100)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert "outside the allow-list" not in out, f"allow-list must really allow:\n{out[-1500:]}"

def test_r4_disallowed_error_fails(tmp_path):
    """> R4: an ERROR line NOT in the allow-list ('KeyError') -> C FAIL +
    exit 1, with the offending lines printed."""
    srv, port = _start_api(tmp_path)
    _Stub.delay_first_byte = 1.0
    _Stub.read_events = True
    (_Stub.root / "engine.log").write_text(
        "2026-11-24 12:00:01 ERROR unexpected KeyError 'req-id'\n")
    try:
        p, _ = _run_pr(tmp_path, port, abort_ms=100)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 1, out[-1500:]
    assert "outside the allow-list" in out, out[-1500:]
    assert "KeyError" in out, out[-1500:]

# ===========================================================================
# R5: the API key must never reach argv (python3 shim logs every argv)
# ===========================================================================
def test_r5_key_never_in_argv(tmp_path):
    """> R5: the python3 shim logs argv of every python invocation; the key
    ('stub-key') must appear in NONE (it travels via the KVP_KEY env).
    MUTATION 'key back in argv' is red here."""
    srv, port = _start_api(tmp_path)
    try:
        p, stub = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    argv_log = stub / "pyargv.log"
    assert argv_log.exists(), "the python3 shim must have been used"
    body = argv_log.read_text()
    assert "stub-key" not in body, (
        "the API key leaked into argv:\n"
        + "\n".join(l for l in body.splitlines() if "stub-key" in l)[:1200])

# ===========================================================================
# R6: D prompt length by prompt_tokens, drift WARN/FAIL, unequal lengths
# ===========================================================================
def test_r6_short_prompt_gives_inconclusive(tmp_path):
    """> R6a: prompt_tokens below the D token target -> D is INCONCLUSIVE
    (the prompt is not longer than the window, recovery is not exercised)."""
    srv, port = _start_api(tmp_path)
    _Stub.prompt_tokens = 5           # far below any target
    _Stub.read_events = True
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 5, out[-1500:]
    assert "prompt_tokens" in out and "NOT longer than the window" in out, out[-1500:]

def test_r6_late_drift_is_warn_rc0(tmp_path):
    """> R6c: divergence at token 50 (past the 32-tokens fail window) ->
    WARN, rc 0 (numeric drift is legal across prefix-cache paths)."""
    srv, port = _start_api(tmp_path)
    _Stub.read_events = True
    _Stub.delay_first_byte = 1.0       # C's early-abort gate stays green
    _Stub.drift = 50
    _Stub.prompt_tokens = 4000
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 0, f"WARN is still rc 0:\n{out[-1500:]}"
    assert "WARN" in out and "first diff token 50" in out, out[-1500:]

def test_r6_early_drift_is_fail_rc1(tmp_path):
    """> R6c: divergence at token 10 (inside the fail window) -> FAIL (corrupt
    recovery), rc 1."""
    srv, port = _start_api(tmp_path)
    _Stub.read_events = True
    _Stub.delay_first_byte = 1.0       # C's early-abort gate stays green
    _Stub.drift = 10
    _Stub.prompt_tokens = 4000
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 1, f"early drift -> exit 1:\n{out[-1500:]}"
    assert "FAIL" in out and "first diff token 10" in out, out[-1500:]

def test_r6_unequal_length_reports_shorter_answer_len():
    """> R6c: when one answer is a strict prefix of the other (identical
    common part, different length), the divergence index is the length of
    the SHORTER answer -- never -1. Verdict: WARN when that index is >= 24
    (word threshold), else FAIL. Direct unit call of the d_compare block
    (extracted from the real script)."""
    import tempfile, json as _json
    src = (ROOT / "scripts" / "test-pr-checks.sh").read_text()
    anchor = "d_compare(){"
    py_start = src.index("python3 - <<'PYEOF'", src.index(anchor))
    py_body = src[src.index("\n", py_start) + 1:
                  src.index("\nPYEOF", py_start)]
    base = [f"w{i}" for i in range(40)]         # 40 words in T0
    t0 = {"text": " ".join(base), "tokens": []}
    t1 = {"text": " ".join(base), "tokens": []}
    t2 = {"text": " ".join(base[:30]), "tokens": []}   # strict prefix, 30 words
    with tempfile.TemporaryDirectory() as td:
        f0, f1, f2 = (Path(td) / f for f in ("t0.json", "t1.json", "t2.json"))
        for f, d in ((f0, t0), (f1, t1), (f2, t2)):
            f.write_text(_json.dumps(d))
        env = dict(os.environ, KVP_F0=str(f0), KVP_F1=str(f1),
                   KVP_F2=str(f2), KVP_TAG="unit", KVP_PT="100")
        r = subprocess.run(["python3", "-"], input=py_body,
                           capture_output=True, text=True, env=env)
    out = r.stdout
    assert "-1" not in out, f"never -1:\n{out}"
    assert "first diff word 30" in out, out          # min(40, 30) = 30
    assert "WARN" in out, f"30 >= 24 word threshold -> WARN:\n{out}"

# ===========================================================================
# R7: TEST MODE banner; SKIP_RESTART -> D SKIPPED, never PASS
# ===========================================================================
def test_r7_skip_restart_is_bannered_and_skipped(tmp_path):
    """> R7: PR_CHECKS_SKIP_RESTART=1 prints the TEST MODE banner first (stdout
    AND report); D is SKIPPED (rc 5), never PASS."""
    srv, port = _start_api(tmp_path)
    try:
        p, _ = _run_pr(tmp_path, port,
                       extra_env={"PR_CHECKS_SKIP_RESTART": "1"})
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert out.lstrip().startswith("TEST MODE:"), f"banner must be FIRST:\n{out[:300]}"
    assert "PR_CHECKS_SKIP_RESTART" in out and "NOT a hardware proof" in out, out[:600]
    assert "D result: SKIPPED" in out, out[-1500:]
    assert p.returncode == 5, f"SKIPPED exits 5:\n{out[-1500:]}"
    rep = (tmp_path / "live" / "pr-checks-report.md").read_text()
    assert "TEST MODE:" in rep, rep[:600]
    assert "**SKIPPED**" in rep, rep[:900]

# ===========================================================================
# R8: timestamped report + latest copy; wheel/hardware/PR-heads; no key
# ===========================================================================
def test_r8_two_reports_latest_copy(tmp_path):
    """> R8: two consecutive runs leave TWO timestamped reports (old kept);
    pr-checks-report.md == the newest; report carries wheel, hardware and
    BOTH 40-hex PR head SHAs (from SOURCES.md); no key in the report."""
    srv, port = _start_api(tmp_path)
    try:
        _run_pr(tmp_path, port)
        time.sleep(1.1)
        _Stub.seen = {}
        _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    live = tmp_path / "live"
    ts = sorted(live.glob("pr-checks-report-*.md"))
    assert len(ts) >= 2, f"two runs -> two timestamped reports: {[t.name for t in ts]}"
    latest = live / "pr-checks-report.md"
    assert latest.exists(), "pr-checks-report.md (latest copy) must exist"
    assert latest.read_bytes() == ts[-1].read_bytes(), "latest == newest report"
    rep = latest.read_text()
    assert "- wheel" in rep, rep[:600]
    assert "- hardware:" in rep, rep[:600]
    assert re.search(r"- PR #4136 head: [0-9a-f]{40}", rep), rep[:900]
    assert re.search(r"- PR #5972 head: [0-9a-f]{40}", rep), rep[:900]
    assert "stub-key" not in rep, "the key must never reach the report"

# ===========================================================================
# R10: per-check FAIL checkpoints (exact lines + exit 1)
# ===========================================================================
def test_r10_dead_engine_fails(tmp_path):
    """> R10/S3: the engine dies BETWEEN the aborts and the C checks ->
    exact 'is DEAD after the abort series' note + exit 1."""
    srv, port = _start_api(tmp_path)
    _Stub.delay_first_byte = 1.0       # aborts land early -> C's early gate OK
    _Stub.read_events = True
    _Stub.fail_dead_after = 8          # die after the warm-up + restart traffic
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 1, out[-1500:]
    assert "is DEAD after the abort series" in out, out[-1500:]

def test_r10_stuck_requests_fail(tmp_path):
    """> R10/S5: vllm:num_requests_running=1 after settle ->
    exact 'stuck requests' note + exit 1."""
    srv, port = _start_api(tmp_path)
    _Stub.stuck = "1 0"
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 1, out[-1500:]
    assert "stuck requests" in out and "running=1 waiting=0" in out, out[-1500:]

def test_r10_changed_answer_fails(tmp_path):
    """> R10/S6: the plain answer AFTER the abort series differs from the one
    before it -> exact 'the plain answer CHANGED' note + exit 1."""
    srv, port = _start_api(tmp_path)
    _Stub.delay_first_byte = 1.0       # C's early-abort gate stays green
    _Stub.read_events = True
    _Stub.turn_changed = True          # c-after answers differently
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 1, out[-1500:]
    assert "the plain answer CHANGED" in out, out[-1500:]

def test_r10_l2_store_failure_fails(tmp_path):
    """> R10/S8: an L2 store failure DURING scenario C (deterministic trigger:
    the 2nd POST after the restart falls in the C window) -> exact C store-
    FAILURE note + exit 1. MUTATION S8c (removing the C l2_failures check)
    is red."""
    srv, port = _start_api(tmp_path)
    _Stub.delay_first_byte = 1.0       # C's early-abort gate stays green
    _Stub.read_events = True
    _Stub.fail_store_at = 2            # lands on the 1st C abort round
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 1, out[-1500:]
    assert "L2 store failure during scenario C" in out, out[-1500:]

def test_r10_l2_store_failure_in_d_fails(tmp_path):
    """> R10/S8: an L2 store failure DURING scenario D -> exact D store-
    FAILURE note + exit 1. MUTATION S8d (removing the D l2_failures check)
    is red."""
    srv, port = _start_api(tmp_path)
    _Stub.delay_first_byte = 1.0
    _Stub.read_events = True
    _Stub.fail_store_phase = "d2"     # lands on the 1st T2 request, INSIDE D
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 1, out[-1500:]
    assert "L2 store failure during scenario D" in out, out[-1500:]

def test_r10_d_fail_drives_exit_1(tmp_path):
    """> R10/S10/S11: D FAIL (early drift) with healthy C still exits 1 and
    the report exit-code line agrees."""
    srv, port = _start_api(tmp_path)
    _Stub.read_events = True
    _Stub.delay_first_byte = 1.0       # C's early-abort gate stays green
    _Stub.drift = 10
    _Stub.prompt_tokens = 4000
    try:
        p, _ = _run_pr(tmp_path, port)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 1, f"D FAIL must drive exit 1:\n{out[-1500:]}"
    assert "D result: FAIL" in out, out[-1500:]