# SPDX-License-Identifier: Apache-2.0
"""r31 Q4: machine-free tests for scripts/test-pr-checks.sh (stub docker +
stub API server on a local port). Covers the r31-offline acceptance items:
C abort-on-fail stub -> FAIL rc != 0; healthy -> PASS; D mismatch at token 10
-> FAIL, at token 50 -> WARN rc 0; identical -> PASS; no key/container ->
clean exit; mutations T0-vs-T2 and abort-always-0 are red.
"""
from __future__ import annotations
import json
import os
import socket
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
PRC = ROOT / "scripts" / "test-pr-checks.sh"

def _write_stub(bin_dir: Path, name: str, text: str):
    p = bin_dir / name
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p

# ---------------------------------------------------------------------------
# a tiny stub vLLM-like API surface on a FREE local port:
#   /v1/models, /v1/chat/completions, /metrics
# The canned answer text is read from a file the test controls, so T0/T1/T2
# compare either identical text or a chosen first divergent token.
# ---------------------------------------------------------------------------
class _StubHandler(BaseHTTPRequestHandler):
    answers = {}       # url-path -> canned body (str), set by the test
    log = []           # request log: (method, path, body)
    aborted = set()    # set True by the slow-reply route when client gave up
    drift_token = None   # when set: the 3rd sighting of a prompt returns a
                         # text diverging at that token index (T2 simulate)
    seen = {}          # prompt-key -> number of sightings
    def log_message(self, *a): pass
    def _send(self, code, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == "/v1/models":
            self._send(200, json.dumps({"data": [{"id": "mimo-pro"}]}))
        elif self.path == "/metrics":
            self._send(200, self.answers.get(
                "/metrics",
                "vllm:num_requests_running{} 0\nvllm:num_requests_waiting{} 0\n"))
        else:
            self._send(404, "{}")
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n).decode()
        self.log.append((self.path, body))
        if self.path == "/v1/chat/completions":
            try:
                prompt = json.loads(body)["messages"][0]["content"]
            except Exception:
                prompt = body
            key = prompt[:24]
            self.seen[key] = self.seen.get(key, 0) + 1
            txt = self.answers.get("answer", "A sikkatu is a Sumerian property marker.")
            if self.drift_token is not None and self.seen[key] == 3:
                # the T2 (restart) sighting -- the text diverges at drift_token
                words = txt.split()
                words.insert(min(self.drift_token, len(words)),
                             f"T2DRIFT{self.drift_token}")
                txt = " ".join(words)
            if self.answers.get("slow"):
                import time
                time.sleep(2)   # long enough for the client abort to win
                return
            self._send(200, json.dumps({
                "choices": [{"message": {"content": txt}}],
                "usage": {"completion_tokens": len(txt.split())}}))
        else:
            self._send(404, "{}")

# stub docker: the ENGINE log line set is read from an env-configurable file so
# that a test can simulate a crashing engine during abort (C FAIL case).
DOCKER_ENGINE_LOG = "/dev/null"

def _start_api(tmp_path: Path) -> tuple[HTTPServer, int, Path]:
    _StubHandler.seen = {}          # fresh prompt sighting counters
    _StubHandler.log = []
    for port in range(18080, 18200):
        try:
            srv = HTTPServer(("127.0.0.1", port), _StubHandler)
            break
        except OSError:
            continue
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port, tmp_path

def _run_pr_checks(tmp_path: Path, port: int, extra_env=None, with_key=True,
                   with_container=True):
    """Run the REAL scripts/test-pr-checks.sh with a stub docker + the stub API
    on 127.0.0.1:<port>, an isolated HOME and cache root."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub_log = tmp_path / "docker-stub.log"
    _write_stub(bin_dir, "docker", (
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "' + str(stub_log) + '"\n'
        "case \"$*\" in\n"
        f'  *inspect*State.Running*{"mock-eng" if with_container else "nothing"}*) echo true ;;\n'
        "  *inspect*State.Running*) echo false ;;\n"
f'  *inspect*Labels*) echo no-label ;;\n'
        f'  *logs*) if [ -n "${{STUB_ENGINE_LOG_FILE:-}}" ] && [ -f "${{STUB_ENGINE_LOG_FILE}}" ]; then cat "${{STUB_ENGINE_LOG_FILE}}"; else echo "engine: serving"; fi ;;\n'
        f'  *stop*|*start*) exit 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if with_key:
        (home / ".config" / "kvp").mkdir(parents=True, exist_ok=True)
        (home / ".config" / "kvp" / "api_key").write_text("stub-key\n")
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    env = dict(os.environ,
               HOME=str(home),
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               API_BASE=f"http://127.0.0.1:{port}",
               KVP_CACHE_ROOT=str(cache),
               CONTAINER="mock-eng" if with_container else "absent-eng",
               PR_CHECKS_OUTDIR=str(tmp_path / "out"),
               PR_CHECKS_REPORT=str(tmp_path / "pr-checks-report.md"),
               PR_CHECKS_ROUNDS="1", PR_CHECKS_SETTLE="0",
               PR_CHECKS_SKIP_RESTART="1",
               LMCACHE_CHUNK_SIZE="32")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", str(PRC)], capture_output=True,
                          text=True, timeout=600, env=env, cwd=str(tmp_path))

# ---------------------------------------------------------------------------
# PRECONDITIONS: no API key / no container -> a clear message, ZERO traceback
# ---------------------------------------------------------------------------
def test_q4_no_key_is_a_clean_error(tmp_path):
    """> Q4: no API key anywhere -> 'no API key found' + the exact start
    command, NO Python traceback, nonzero exit."""
    srv, port, _ = _start_api(tmp_path)
    try:
        r = _run_pr_checks(tmp_path, port, with_key=False)
    finally:
        srv.shutdown()
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "no API key" in out, out[-600:]
    assert "Traceback" not in out, f"a precondition must not traceback:\n{out[-600:]}"
    assert "start-all.sh" in out, "the fix command must print"

def test_q4_no_container_is_a_clean_error(tmp_path):
    """> Q4: the engine container is NOT running -> the clear refusal with the
    start command, NO traceback, nonzero exit."""
    srv, port, _ = _start_api(tmp_path)
    try:
        r = _run_pr_checks(tmp_path, port, with_container=False)
    finally:
        srv.shutdown()
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "not running" in out or "ERROR" in out, out[-600:]
    assert "Traceback" not in out, out[-600:]

# ---------------------------------------------------------------------------
# SCENARIO C: a stub engine that CRASHES on abort -> FAIL rc != 0; healthy -> PASS
# ---------------------------------------------------------------------------
def test_q4_c_healthy_stub_passes(tmp_path):
    """> Q4 C: a healthy stub engine answers, never dies -> the abort series is
    clean, result PASS, rc 0 (WARN is rc 0 too)."""
    _StubHandler.answers = {"answer": "A sikkatu marks a Sumerian plot boundary."}
    srv, port, _ = _start_api(tmp_path)
    try:
        r = _run_pr_checks(tmp_path, port,
                           extra_env={"PR_CHECKS_ROUNDS": "1"})
    finally:
        srv.shutdown()
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"healthy stub must give PASS:\n{out[-1500:]}"
    assert "result: **PASS**" in out or " C result: PASS" in out, out[-1500:]
    assert "Traceback" not in out, out[-800:]

def test_q4_c_aborts_land_after_the_retrieve_started(tmp_path):
    """> Q4 C: the aborts must land INSIDE the retrieve window -- the report
    must show aborts that saw response bytes before closing. MUTATION: the
    abort delay stuck at 0 (a mutant `C_ABORT_MAX_MS` hardcoded to 0) makes
    every abort close before reading anything -- this test must then be red
    (no 'aborts after reading bytes: N>0' in the report)."""
    for env_max, want_read in (("400", True), ("0", False)):
        _StubHandler.answers = {"answer": _d_base(),
                                "slow": False}
        srv, port, _ = _start_api(tmp_path)
        try:
            r = _run_pr_checks(
                tmp_path, port,
                {"PR_CHECKS_ROUNDS": "2", "PR_CHECKS_ABORT_MAX_MS": env_max,
                 "PR_CHECKS_OUTDIR": str(tmp_path / f"out-{env_max}"),
                 "PR_CHECKS_REPORT": str(tmp_path / f"report-{env_max}.md")})
        finally:
            srv.shutdown()
        out = r.stdout + r.stderr
        import re as _re
        ms_ = _re.findall(r"aborts after reading bytes: (\d+)", out)
        assert ms_, f"the report must carry the read-bytes metric:\n{out[-1200:]}"
        n = int(ms_[-1])   # the LAST run in the loop is ours
        if want_read:
            assert n > 0, (
                f"at least one abort must close AFTER reading response bytes "
                f"(otherwise the abort proves nothing about the retrieve):\n{out[-1200:]}")
        else:
            # with a 0 ms delay the client closes immediately: ABRT-READ=0
            assert n == 0, (
                f"0 ms aborts must not read response bytes (got {n}):\n{out[-1200:]}")

def test_q4_c_crash_on_abort_fails(tmp_path):
    """> Q4 C: the stub engine logs a Traceback (the abort-retrieve crash PR
    #4136 is supposed to fix) -> the C section FAILs, rc != 0."""
    eng_log = tmp_path / "engine.log"
    eng_log.write_text(
        "Traceback (most recent call last):\n"
        '  File "/opt/vllm/v1/core/sched.py", line 12, in abort\n'
        "KeyError: 'req-id-aborted'\n")
    _StubHandler.answers = {"answer": "x y z"}
    srv, port, _ = _start_api(tmp_path)
    try:
        r = _run_pr_checks(tmp_path, port,
                           {"PR_CHECKS_ROUNDS": "1",
                            "STUB_ENGINE_LOG_FILE": str(eng_log)})
    finally:
        srv.shutdown()
    out = r.stdout + r.stderr
    assert r.returncode != 0 or "result: **FAIL**" in out or "C result: FAIL" in out, (
        f"a crashing engine during abort MUST be a FAIL:\n{out[-1500:]}")

# ---------------------------------------------------------------------------
# SCENARIO D: identical texts -> PASS; late mismatch -> WARN rc 0; early -> FAIL
# ---------------------------------------------------------------------------
def _d_texts(first_diff_token):
    """Three canned answer texts equal up to first_diff_token tokens."""
    base = [f"word{i}" for i in range(64)]
    t0 = " ".join(base)
    t1 = " ".join(base)
    t2 = " ".join(base[:first_diff_token] + [f"CHANGED{first_diff_token}"] + base[first_diff_token + 1:])
    return t0, t1, t2

def _d_base(n=64):
    return " ".join(f"word{i}" for i in range(n))

def test_q4_d_identical_is_pass(tmp_path):
    """> Q4 D: T0 == T1 == T2 (identical texts) -> PASS, rc 0."""
    _StubHandler.answers = {"answer": _d_base()}
    _StubHandler.drift_token = None
    srv, port, _ = _start_api(tmp_path)
    try:
        r = _run_pr_checks(tmp_path, port, {"PR_CHECKS_ROUNDS": "1"})
    finally:
        srv.shutdown()
        _StubHandler.drift_token = None
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-1500:]
    assert "**PASS**" in out or "identical" in out, out[-1500:]

def test_q4_d_late_mismatch_is_warn(tmp_path):
    """> Q4 D: the first divergence at token 50 (past the 32-token fail window)
    -> WARN and rc 0. MUTATION: comparing T0 with T1 (always identical) would
    silently report PASS -- the mutant test below must catch that."""
    _StubHandler.answers = {"answer": _d_base()}
    _StubHandler.drift_token = 50
    srv, port, _ = _start_api(tmp_path)
    try:
        r = _run_pr_checks(tmp_path, port, {"PR_CHECKS_ROUNDS": "1"})
    finally:
        srv.shutdown()
        _StubHandler.drift_token = None
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"WARN must still be rc 0:\n{out[-1500:]}"
    assert "token 50" in out or "WARN" in out, out[-1500:]

def test_q4_d_early_mismatch_is_fail(tmp_path):
    """> Q4 D: the first divergence at token 10 (inside the 32-token window)
    -> FAIL, rc != 0 (corrupt recovery)."""
    _StubHandler.answers = {"answer": _d_base()}
    _StubHandler.drift_token = 10
    srv, port, _ = _start_api(tmp_path)
    try:
        r = _run_pr_checks(tmp_path, port, {"PR_CHECKS_ROUNDS": "1"})
    finally:
        srv.shutdown()
        _StubHandler.drift_token = None
    out = r.stdout + r.stderr
    assert r.returncode != 0 or "FAIL" in out, f"early drift must be FAIL:\n{out[-1500:]}"