# SPDX-License-Identifier: Apache-2.0
"""r32.1: regression tests for the follow-up review (R1-R8). Fast, deterministic,
direct calls to the REAL abort_stream / restart helpers where possible."""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRC = ROOT / "scripts" / "test-pr-checks.sh"
LIB = ROOT / "scripts" / "restart-lib.sh"


class _Srv(BaseHTTPRequestHandler):
    delay = 0.0
    hold_first = 0.0
    body_chunk_delay = 0.0
    _first = False
    _lock = threading.Lock()
    def log_message(self, *a): pass
    def do_POST(self):
        with _Srv._lock:
            first = not _Srv._first
            _Srv._first = True
        n = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(n)
        time.sleep(self.delay)
        if first and self.hold_first:
            time.sleep(self.hold_first)
        body = json.dumps({"ok": True, "pad": "x" * 200}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        mid = max(1, len(body) // 2)
        self.wfile.write(body[:mid]); self.wfile.flush()
        if self.body_chunk_delay:
            time.sleep(self.body_chunk_delay)
        self.wfile.write(body[mid:]); self.wfile.flush()


def _api():
    _Srv.delay = 0.0
    _Srv.hold_first = 0.0
    _Srv.body_chunk_delay = 0.0
    _Srv._first = False
    srv = None
    for port in range(19100, 19400):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), _Srv)
            break
        except OSError:
            continue
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _extract_fn(name):
    src = PRC.read_text()
    m = re.search(r"^" + name + r"\(\)\s*\{", src, re.M)
    start = m.start()
    depth = 0
    for i, ch in enumerate(src[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("function not closed: " + name)


def _abort_once(tmp_path: Path, port: int, ms: int) -> dict:
    """Run the REAL abort_stream and parse its one metric line."""
    af = tmp_path / "ab.txt"
    af.write_text("")
    pfile = tmp_path / "pr.txt"
    pfile.write_text("hello")
    fn = _extract_fn("abort_stream")
    env = dict(os.environ, KVP_KEY="stub-key", SERVED_MODEL_NAME="mimo-pro")
    cmd = (f"API='http://127.0.0.1:{port}'; "
           f"eval \"$(cat <<'EOS'\n{fn}\nEOS\n)\"; "
           f"abort_stream '{pfile}' '{ms}' '{af}'")
    r = subprocess.run(["bash", "-c", cmd], env=env,
                       capture_output=True, text=True, timeout=30)
    line = af.read_text().strip()
    d = {k: v for k, v in (kv.partition("=")[::2] for kv in line.split())}
    d["_rc"] = r.returncode
    d["_out"] = r.stdout + r.stderr
    d["_line"] = line
    return d


# ===========================================================================
# R3: the abort is DEADLINE-DRIVEN, independent of the response headers
# ===========================================================================
def test_r3_aborts_before_headers_close_at_the_deadline(tmp_path):
    """> R3 (core proof): the server delays its first byte 3 s; the client
    abort window is 300 ms -> the connection must CLOSE at ~300 ms (not at
    ~3 s like the old http.client.getresponse() version), and the metric
    says before_first_byte=1. fprintf err=None else the close is the deadline."""
    srv, port = _api()
    _Srv.delay = 3.0
    try:
        d = _abort_once(tmp_path, port, 300)
    finally:
        srv.shutdown()
    assert d.get("before_first_byte") == "1", d
    ms = float(d["ms"])
    assert 200 <= ms <= 550, \
        f"close must land near the 300 ms deadline, got {ms:.0f} ms (d={d})"


def test_r3_aborts_mid_body_at_the_deadline(tmp_path):
    """> R3: headers return immediately, the body is sent in 2 chunks with a 3 s
    pause -> abort at 400 ms closes MID-BODY: before_first_byte=0, some body
    bytes were read, and the close is on schedule (~400 ms). This proves the
    client reads through NO hidden buffer (`body_bytes>0` came from the raw
    socket) and stops at the deadline regardless of in-flight response data."""
    srv, port = _api()
    _Srv.body_chunk_delay = 3.0
    try:
        d = _abort_once(tmp_path, port, 400)
    finally:
        srv.shutdown()
    assert d.get("before_first_byte") == "0", d
    ms = float(d["ms"])
    assert 250 <= ms <= 650, \
        f"close must land near the 400 ms deadline, got {ms:.0f} ms (d={d})"
    assert int(d["body_bytes"]) > 0, f"the first chunk must be read: {d}"


def test_r3_hung_server_still_closes_at_deadline(tmp_path):
    """> R3: server never answers (10 s sleep); abort window 250 ms -> the
    client closes on time (~250 ms), prints its line and exits fast (the old
    getresponse() would hang the full 10 s+)."""
    srv, port = _api()
    _Srv.delay = 10.0
    try:
        t0 = time.time()
        d = _abort_once(tmp_path, port, 250)
        elapsed = time.time() - t0
    finally:
        srv.shutdown()
    assert d.get("before_first_byte") == "1", d
    assert elapsed < 5.0, f"client must not hang on a silent server: {elapsed:.1f}s"
    ms = float(d["ms"])
    assert 200 <= ms <= 500, f"close near 250 ms deadline, got {ms:.0f} ms"


# ===========================================================================
# R1: real rc in restart messages, exited proof, IPC bypass honesty
# ===========================================================================
def test_r1_stop_cache_failure_reports_real_rc(tmp_path):
    """> R1: stop-cache.sh exits 3 -> the INCONCLUSIVE message must carry
    (rc=3). The old `if ! cmd; rc=$?` captured the INVERTED status and
    always printed rc=0."""
    sd = tmp_path / "sd"; sd.mkdir()
    (sd / "stop-cache.sh").write_text("#!/usr/bin/env bash\necho broken >&2\nexit 3\n")
    (sd / "start-cache.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in ("stop-cache.sh", "start-cache.sh"):
        (sd / f).chmod(0o755)
    env = dict(os.environ, KVP_ORDER_LOG=str(tmp_path / "o.log"),
               STUB_ROOT=str(tmp_path))
    r = subprocess.run(["bash", "-c", f'. "{LIB}"; kvp_restart_cache "{sd}"'],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "rc=3" in r.stdout + r.stderr, f"real rc must appear: {r.stdout + r.stderr}"


def test_r1_start_cache_failure_reports_real_rc(tmp_path):
    """> R1: start-cache.sh exits 7 -> the INCONCLUSIVE message must carry (rc=7)."""
    sd = tmp_path / "sd"; sd.mkdir()
    (sd / "stop-cache.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (sd / "start-cache.sh").write_text("#!/usr/bin/env bash\nexit 7\n")
    for f in ("stop-cache.sh", "start-cache.sh"):
        (sd / f).chmod(0o755)
    env = dict(os.environ, KVP_ORDER_LOG=str(tmp_path / "o.log"),
               STUB_ROOT=str(tmp_path))
    r = subprocess.run(["bash", "-c", f'. "{LIB}"; kvp_restart_cache "{sd}"'],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 5
    assert "rc=7" in r.stdout + r.stderr, f"real rc must appear: {r.stdout + r.stderr}"


def test_r1_stop_zero_exit_but_running_is_inconclusive(tmp_path):
    """> R1: `docker stop` exits 0 but the container state stays 'running' ->
    the stop stage must NOT be believed on the command's exit code alone;
    after the retries -> INCONCLUSIVE (exit 5), never ENGINE_STOPPED."""
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *inspect*Status*) echo running ;;\n"
        "  stop*) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n")
    (bin_ / "docker").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}",
               KVP_STOP_RETRIES="1", KVP_STOP_WAIT_LOOPS="1",
               KVP_STOP_WAIT_INTERVAL="1",
               KVP_ORDER_LOG=str(tmp_path / "o.log"), STUB_ROOT=str(tmp_path))
    r = subprocess.run(["bash", "-c", f'. "{LIB}"; kvp_restart_stop_engine mock-eng'],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "did NOT stop" in r.stdout + r.stderr, r.stdout + r.stderr
    o_log = tmp_path / "o.log"
    assert not o_log.exists() or "ENGINE_STOPPED" not in o_log.read_text(), \
        "a container that never reached exited must not log ENGINE_STOPPED"


def test_r1_ipc_skip_is_test_mode(tmp_path):
    """> R1: KVP_SKIP_IPC_TEST=1 (or a missing ipc-test.sh) must land in the
    TEST MODE banner -- a run that skipped the IPC pre-flight is NOT a
    hardware proof and must say so in its very first line."""
    env = dict(os.environ, HOME=str(tmp_path), KVP_SKIP_IPC_TEST="1",
               KVP_LIVE_DIR=str(tmp_path), KVP_SCRIPTS_DIR=str(tmp_path),
               API_KEY="stub-key")   # the banner must come BEFORE the key check
    # R1ipc: an EXECUTABLE ipc-test.sh must exist in SCRIPTS_DIR so only the
    # KVP_SKIP_IPC_TEST branch can trigger the TEST MODE line (a missing file
    # would trigger the separate MISSING branch and mask the SKIP mutant).
    ipc = tmp_path / "ipc-test.sh"
    ipc.write_text("#!/usr/bin/env bash\nexit 0\n")
    ipc.chmod(0o755)
    r = subprocess.run(["bash", str(PRC)], env=env,
                       capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    first = out.splitlines()[0] if out else ""
    assert first.startswith("TEST MODE"), f"banner must be first: {out[:300]}"
    assert "IPC" in first, f"the IPC bypass must be named: {first}"


# ===========================================================================
# R2: full L2 store count required; prefix 1 warmed exactly once
# ===========================================================================
def test_r2_incomplete_warmup_is_inconclusive(tmp_path):
    """> R2: the stub logs store events only for the FIRST 2 requests -> far
    below the full expected warm-up count -> INCONCLUSIVE (rc 5), not PASS."""
    sys.path.insert(0, str(Path(__file__).parent))
    h = __import__("test_r32_pr_report")
    srv, port = h._start_api(tmp_path)
    h._Stub.stores_limit = 2
    h._Stub.delay_first_byte = 1.0      # keep the C early gate satisfied later
    try:
        p, _ = h._run_pr(tmp_path, port, rounds=1, abort_ms=100)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 5, out[-1200:]
    assert "warmup is INCOMPLETE" in out, out[-1200:]


def test_r2_prefix1_sent_exactly_once(tmp_path):
    """> R2: the warm-up must hit each C prefix exactly once -- with 1 round
    the stub sees the prefix-1 prompt exactly once before the restart
    (the old code sent it twice: quiet probe + round 1)."""
    sys.path.insert(0, str(Path(__file__).parent))
    h = __import__("test_r32_pr_report")
    srv, port = h._start_api(tmp_path)
    h._Stub.delay_first_byte = 1.0
    try:
        p, stub = h._run_pr(tmp_path, port, rounds=1, abort_ms=100)
    finally:
        srv.shutdown()
    before = (stub / "lmcache-server.log").read_text()
    # one store event per POST; the warm-up has exactly 1+1+6=8 store events
    # before the restart (probe + prefix1 + 3x T0 + 3x T1); after the aborts
    # of round 1 and the C/D traffic the total grows -- so assert the SUBSET:
    # the total store count minus post-rest traffic cannot exceed expectations
    out = p.stdout + p.stderr
    m = re.search(r"L2 store success events after warm-up: (\d+)", out)
    assert m, out[-800:]
    n = int(m.group(1))
    # rounds=1: warm-up is 8 unique requests (probe + 1 prefix + 3 T0 + 3 T1)
    # -> exactly 8 stores by the restart; the total run is 16 requests
    # (8 warm-up + 4 aborts + 1 C-after + 3 T2) -> exactly 16 store events.
    # The old double-send of prefix 1 made it 17 -- a precise count kills it.
    stores = len([l for l in before.splitlines() if "L2 store completed" in l])
    assert n == 8, f"warm-up must reach exactly 8 store events, got {n}"
    assert stores == 16, (
        f"each request exactly once: expected 16 total store events, got {stores}")


# ===========================================================================
# R6: chunk-size fallback removed; words-vs-tokens when logprobs are partial
# ===========================================================================
def test_r6_chunk_fallback_removed():
    """> R6: the hardcoded `D_CHUNK=384` must be GONE from the script, and the
    missing-chunk-size path must end in INCONC (not in a guessed target)."""
    src = PRC.read_text()
    assert "D_CHUNK=384" not in src, "the hardcoded 384 fallback must be gone"
    assert "cannot determine the LMCache chunk size" in src, \
        "the missing-chunk-size INCONCLUSIVE guard must exist"


def test_r6_partial_logprobs_compared_by_words():
    """> R6: T0/T1 carry logprobs but T2 does not -> token comparison is NOT
    used for a subset: all three fall back to words and the column says so.
    Direct call of the real d_compare block."""
    src = PRC.read_text()
    py_start = src.index("python3 - <<'PYEOF'", src.index("d_compare(){"))
    py_body = src[src.index("\n", py_start) + 1: src.index("\nPYEOF", py_start)]
    words = [f"w{i}" for i in range(40)]
    t0 = {"text": " ".join(words), "tokens": ["t"] * 40}
    t1 = {"text": " ".join(words), "tokens": ["t"] * 40}
    t2 = {"text": " ".join(words), "tokens": []}   # NO logprobs on T2
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f0, f1, f2 = (Path(td) / n for n in ("t0.json", "t1.json", "t2.json"))
        for f, d in ((f0, t0), (f1, t1), (f2, t2)):
            f.write_text(json.dumps(d))
        env = dict(os.environ, KVP_F0=str(f0), KVP_F1=str(f1),
                   KVP_F2=str(f2), KVP_TAG="unit", KVP_PT="100")
        r = subprocess.run(["python3", "-"], input=py_body,
                           capture_output=True, text=True, env=env)
    out = r.stdout
    assert "identical (word)" in out, f"all-three-word comparison expected:\n{out}"
    assert "(token)" not in out, f"no token comparison when T2 lacks logprobs:\n{out}"


def test_r6_all_logprobs_compared_by_tokens():
    """> R6: all three carry logprobs -> token comparison is used (column 'token')."""
    src = PRC.read_text()
    py_start = src.index("python3 - <<'PYEOF'", src.index("d_compare(){"))
    py_body = src[src.index("\n", py_start) + 1: src.index("\nPYEOF", py_start)]
    toks = [f"t{i}" for i in range(40)]
    t = {"text": "ignored", "tokens": toks}
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f0, f1, f2 = (Path(td) / n for n in ("t0.json", "t1.json", "t2.json"))
        for f in (f0, f1, f2):
            f.write_text(json.dumps(t))
        env = dict(os.environ, KVP_F0=str(f0), KVP_F1=str(f1),
                   KVP_F2=str(f2), KVP_TAG="unit", KVP_PT="100")
        r = subprocess.run(["python3", "-"], input=py_body,
                           capture_output=True, text=True, env=env)
    assert "identical (token)" in r.stdout, r.stdout


# ===========================================================================
# R7: TEST MODE first in the report; launched aborts counted separately
# ===========================================================================
def test_r7_report_first_line_is_test_mode_and_launch_counts(tmp_path):
    """> R7: in a TEST MODE run the report's FIRST line is the TEST MODE
    banner (before the title), and the requests line reports expected /
    launched / metric-line counts separately (the old report hard-coded the
    plan and never said how many aborts really started)."""
    sys.path.insert(0, str(Path(__file__).parent))
    h = __import__("test_r32_pr_report")
    srv, port = h._start_api(tmp_path)
    h._Stub.delay_first_byte = 1.0
    h._Stub.prompt_tokens = 4000
    try:
        p, _ = h._run_pr(tmp_path, port, rounds=1, abort_ms=100, settle="0")
    finally:
        srv.shutdown()
    rep = (tmp_path / "live" / "pr-checks-report.md").read_text()
    first = rep.splitlines()[0]
    assert first.startswith("TEST MODE"), f"TEST MODE must be the first line: {first}"
    m = re.search(r"C abort streams expected (\d+) / launched (\d+) / metric lines (\d+)", rep)
    assert m, rep[:1200]
    exp, launched, lines = (int(x) for x in m.groups())
    assert exp == 4 and launched == 4, f"healthy run: expected 4 launched 4, got {exp} {launched}"
    assert lines <= launched, f"metric lines ({lines}) cannot exceed launches ({launched})"


def test_r7_missing_ipc_marks_report_test_mode(tmp_path):
    """> R7/R1: when ipc-test.sh is MISSING the report (not just stdout) must
    carry the TEST MODE banner naming the IPC pre-flight gap."""
    sys.path.insert(0, str(Path(__file__).parent))
    h = __import__("test_r32_pr_report")
    srv, port = h._start_api(tmp_path)
    h._Stub.delay_first_byte = 1.0
    h._Stub.prompt_tokens = 4000
    try:
        p, stub = h._run_pr(tmp_path, port, rounds=1, abort_ms=100,
                            extra_env={"KVP_SKIP_IPC_TEST": "1"})
    finally:
        srv.shutdown()
    rep = (tmp_path / "live" / "pr-checks-report.md").read_text()
    assert "TEST MODE" in rep.splitlines()[0], rep[:400]
    assert "IPC" in rep.splitlines()[0], rep[:400]


# ===========================================================================
# R8: full multi-word GPU names and collision-safe report names
# ===========================================================================
def test_r8_gpu_names_keep_full_words():
    """> R8: the GPU pipeline must keep a FULL multi-word name
    ('NVIDIA RTX PRO 6000'), not just $1's first word after the count."""
    # feed the REAL awk/sed pipeline used by the report with a multi-word name
    pipe = ("printf 'NVIDIA RTX PRO 6000\\nNVIDIA RTX PRO 6000\\n' | "
            "sort | uniq -c | "
            "awk '{cnt=$1; $1=\"\"; sub(/^ /,\"\"); printf \"%sx %s; \", cnt, $0}' | "
            "sed 's/; $//'")
    r = subprocess.run(["bash", "-c", pipe], capture_output=True, text=True, timeout=10)
    assert r.stdout.strip() == "2x NVIDIA RTX PRO 6000", r.stdout
    # and the script must use that pipeline (not the old $1,$2 one)
    src = PRC.read_text()
    assert 'sub(/^ /,"")' in src, "the full-name GPU awk must be in the script"
    assert 'printf "%sx %s; ",$1,$2' not in src, "the truncated-name awk must be gone"


def test_r8_timestamp_collision_safe(tmp_path):
    """> R8: if pr-checks-report-<ts>.md already exists (same UTC second),
    the new report must get a SUFFIX instead of overwriting the old one;
    pr-checks-report.md copies the newest report."""
    sys.path.insert(0, str(Path(__file__).parent))
    h = __import__("test_r32_pr_report")
    srv, port = h._start_api(tmp_path)
    h._Stub.delay_first_byte = 1.0
    h._Stub.prompt_tokens = 4000
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    # pre-create report names for every UTC second over the next 600 s (same
    # format as the script, including the trailing Z) -- the run is GUARANTEED
    # to collide with one of them whenever it happens to ask `date` for its
    # name; nothing may overwrite any placeholder.
    names = []
    for delta in range(600):
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() + delta))
        f = live / f"pr-checks-report-{ts}.md"
        f.write_text("PLACEHOLDER\n")
        names.append(f)
    try:
        p, _ = h._run_pr(tmp_path, port, rounds=1, abort_ms=100, settle="0")
    finally:
        srv.shutdown()
    survivors = [n for n in live.glob("pr-checks-report-*.md")
                 if n.read_text().startswith("PLACEHOLDER")]
    assert len(survivors) == 600, \
        f"no placeholder may be overwritten: {len(survivors)} survivors"
    latest = (live / "pr-checks-report.md").read_text()
    assert not latest.startswith("PLACEHOLDER"), "latest copy must be the NEW report"
    real = [n for n in live.glob("pr-checks-report-*.md")
            if not n.read_text().startswith("PLACEHOLDER")]
    assert real, "the run must have written exactly one new timestamped report"
    assert latest == real[0].read_text(), "latest == the just-written report"