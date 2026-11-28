# SPDX-License-Identifier: Apache-2.0
"""r33: regression tests for the round-33 review (kv 4ef6a8d).

R1: D prompts are calibrated with PER_TOK and rebuilt when undersized (the
    old formula omitted PER_TOK and D almost always came out INCONCLUSIVE).
R2: "before the first byte" now means the first BODY byte (the first SSE /
    token chunk) -- headers alone prove nothing (Starlette sends
    `http.response.start` before the first chunk). A real vLLM sends headers
    early, so counting a header-only close as "early" would INFLATE the
    early-abort share; counting a body-byte-close as "early" would let the
    retrieve window never be engaged while still passing.
R3: the store-event warmup gate counts UNIQUE prompts (T1 resends T0's
    prompt and may not store again).
R4: the D_CHUNK grep cannot die bare under `set -Eeuo pipefail`.
R5: an early INCONC must not leave an empty reserved report file behind.
R6: the delayed-connect test must not depend on a network route.
"""
from __future__ import annotations
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRC = ROOT / "scripts" / "test-pr-checks.sh"
LIB = ROOT / "scripts" / "restart-lib.sh"


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


def _setup_standalone(tmp_path):
    """Minimal harness to run test-pr-checks.sh far enough to reach the
    chunk-size probe: a live engine container stub in PATH, a live API
    stub, and a known LMCACHE_CHUNK_SIZE knob (overridden by the test)."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *inspect*Status*) echo running ;;\n"
        "  *inspect*Running*) echo true ;;\n"
        "  *inspect*User*) echo 1000:1000 ;;\n"
        "  *inspect*Labels*) echo sha256:stub ;;\n"
        "  *exec*) echo 0.5.8 ;;\n"
        "  *) echo stub ;;\n"
        "esac\nexit 0\n")
    docker.chmod(0o755)
    nvidia = bin_dir / "nvidia-smi"
    nvidia.write_text("#!/usr/bin/env bash\necho 'NVIDIA Stub GPU'\n")
    nvidia.chmod(0o755)
    import http.server, threading
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            b = json.dumps({"data": [{"id": "mimo-pro"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers()
            self.wfile.write(b)
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(n)
            b = json.dumps({"choices": [{"message": {"content": "hi"}}],
                           "usage": {"prompt_tokens": 10}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers()
            self.wfile.write(b)
    srv = None
    for port in range(20300, 20600):
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            continue
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return bin_dir, srv, port


def _minimal_env(tmp_path, bin_dir, port, **overrides):
    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    (home / ".config" / "kvp").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "kvp" / "api_key").write_text("stub-key\n")
    env = dict(os.environ, HOME=str(home), API_KEY="stub-key",
               API_BASE=f"http://127.0.0.1:{port}",
               KVP_LIVE_DIR=str(live), KVP_SCRIPTS_DIR=str(tmp_path),
               PATH=f"{bin_dir}:{os.environ['PATH']}",
               KVP_STOP_RETRIES="1", KVP_STOP_WAIT_LOOPS="1",
               KVP_STOP_WAIT_INTERVAL="1",
               KVP_CACHE_ROOT=str(tmp_path / "cache"))
    env.update(overrides)
    return env


def _abort_once(tmp_path: Path, host: str, port: int, ms: int, tol_ms: str = "250") -> dict:
    af = tmp_path / "ab-r33.txt"
    af.write_text("")
    pfile = tmp_path / "pr-r33.txt"
    pfile.write_text("hello")
    fn = _extract_fn("abort_stream")
    env = dict(os.environ, KVP_KEY="stub-key", SERVED_MODEL_NAME="mimo-pro",
               KVP_ABORT_TOL_MS=tol_ms)
    cmd = (f"API='http://{host}:{port}'; "
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


def _api_headers_fast_body_slow(body_delay: float):
    """r33 R2 stub: headers are sent IMMEDIATELY (like Starlette's
    `http.response.start`), the first body chunk is delayed `body_delay`."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(n)
            body = json.dumps({"ok": True, "pad": "x" * 200}).encode()
            self.send_response(200)          # headers go out now
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.flush()
            if body_delay > 0:
                time.sleep(body_delay)       # first BODY byte is late
            self.wfile.write(body)
            self.wfile.flush()
    srv = None
    for port in range(19900, 20200):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            continue
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ===========================================================================
# R2: "before the first byte" = before the first BODY byte, not headers
# ===========================================================================
def test_r3_headers_fast_body_slow_all_aborts_early(tmp_path):
    """> R3/R2 Test A: headers come back instantly (Starlette behaviour) but
    the first body chunk is 1 s late; abort window 0-400 ms -> EVERY abort is
    early (body_bytes=0, headers_seen=1). The old header-based rule would
    have marked these NOT early (headers already seen) and made C
    INCONCLUSIVE -- a false negative on the retrieve window."""
    srv, port = _api_headers_fast_body_slow(1.0)
    try:
        for ms in (50, 200, 400, 100):
            d = _abort_once(tmp_path, "127.0.0.1", port, ms)
            assert d.get("headers_seen") == "1", d
            assert int(d["body_bytes"]) == 0, f"no body byte within the window: {d}"
            assert d.get("before_first_byte") == "1", (
                "zero body bytes -> the abort IS early under the r33 definition: " + str(d))
            assert d.get("late") == "0", d
    finally:
        srv.shutdown()


def test_r3_instant_body_means_not_early(tmp_path):
    """> R3/R2 Test B: headers AND body arrive immediately -> every abort has
    body_bytes>0 and is NOT early (the retrieve window was already delivering
    tokens). In scenario C this lowers the early share and forces
    INCONCLUSIVE, never a false PASS."""
    srv, port = _api_headers_fast_body_slow(0.0)
    try:
        for ms in (100, 300):
            d = _abort_once(tmp_path, "127.0.0.1", port, ms)
            assert d.get("headers_seen") == "1", d
            assert int(d["body_bytes"]) > 0, d
            assert d.get("before_first_byte") == "0", (
                "a body byte arrived -> NOT early: " + str(d))
    finally:
        srv.shutdown()


# ===========================================================================
# R1: D prompts calibrated by PER_TOK with a rebuild (target reached)
# ===========================================================================
def test_r1_d_prompt_calibrated_reaches_the_target(tmp_path):
    """> R1: the stub computes prompt_tokens = ceil(len(prompt)/2.7) (2.7
    chars/token, in the real-world range); with no model config.json the D
    target is 4 * LMCACHE_CHUNK_SIZE = 1024; the run must REACH the D verdict
    (PASS/FAIL), never end early with the D-INCONCLUSIVE 'undersized prompt'
    message -- the r33 blocker (the old sizing omitted PER_TOK and produced
    ~570-690 tokens for a 1024 target)."""
    sys.path.insert(0, str(Path(__file__).parent))
    h = __import__("test_r32_pr_report")
    srv, port = h._start_api(tmp_path)
    h._Stub.delay_first_byte = 1.0     # C's early gate stays green
    h._Stub.len_token_divisor = 2.7    # ceil(len/2.7), like a real tokenizer
    try:
        p, _ = h._run_pr(tmp_path, port, rounds=1, abort_ms=100,
                         extra_env={"LMCACHE_CHUNK_SIZE": "256"})
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode != 5 or "D prompt" not in out, (
        "the D prompts must reach the calibrated target (optionally rebuilt once), "
        "never end INCONCLUSIVE for undersized prompts:\n" + out[-1500:])
    m = re.search(r"L2 disk-read events during D: (\d+)", out)
    assert m, "the D verdict block must be reached:\n" + out[-1500:]
    assert "D prompt 1 is undersized after a rebuild" not in out, (
        "after a successful rebuild the second attempt must hit the target:\n" +
        out[-1500:])
    assert "result: **PASS**" in out or "result: **FAIL**" in out or "result: **WARN**" in out, (
        "a real D verdict (PASS/FAIL/WARN), not INCONCLUSIVE, must appear:\n" +
        out[-1500:])


def test_r1_stub_rejects_undersized_prompts(tmp_path):
    """> R1: the SAME stub (ceil(len/2.7)) with the OLD un-calibrated sizing
    would go undersized; here we prove the harness CAN detect undersize by
    capping prompt_tokens low -> the run is INCONCLUSIVE with both numbers
    (proves the safety trip works and reports what it knows)."""
    sys.path.insert(0, str(Path(__file__).parent))
    h = __import__("test_r32_pr_report")
    srv, port = h._start_api(tmp_path)
    h._Stub.delay_first_byte = 1.0
    h._Stub.prompt_tokens = 5          # absurdly short, always below any target
    try:
        p, _ = h._run_pr(tmp_path, port, rounds=1, abort_ms=100)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert p.returncode == 5, out[-1500:]
    assert "NOT longer than the window" in out or "undersized" in out, out[-1500:]


# ===========================================================================
# R3: the warmup gate counts UNIQUE prompts (T1 stores nothing new)
# ===========================================================================
def test_r3_warmup_gate_excludes_t1(tmp_path):
    """> R3: T1 reuses T0's prompt and (real LMCache may) log NO store event
    for it. A run where the T1 phases store nothing must NOT become
    INCONCLUSIVE -- the expectation counts UNIQUE prompts (1 probe +
    C_ROUNDS + 3 D T0), not T1."""
    sys.path.insert(0, str(Path(__file__).parent))
    h = __import__("test_r32_pr_report")
    srv, port = h._start_api(tmp_path)
    h._Stub.delay_first_byte = 1.0
    h._Stub.prompt_tokens = 4000
    h._Stub.no_store_phases = "d1"      # T1 never logs a store event
    try:
        p, _ = h._run_pr(tmp_path, port, rounds=1, abort_ms=100)
    finally:
        srv.shutdown()
    out = p.stdout + p.stderr
    assert "warmup is INCOMPLETE" not in out, (
        "T1 must not be required to produce a store event:\n" + out[-1500:])
    assert "L2 store success events after warm-up:" in out


# ===========================================================================
# R4: no silent pipefail death while probing for the chunk size
# ===========================================================================
def test_r4_no_env_files_gives_inconclusive_not_rc2(tmp_path):
    """> R4: with NO *.env anywhere under ROOT/live and ROOT/configs the bare
    `grep` used to die under `set -Eeuo pipefail` with exit code 2 and NO
    message (reproduced by the reviewer); now the script must fall through to
    the proper INCONCLUSIVE text (exit 5). ROOT is derived from the script
    location, so we run a COPY of the scripts in a bare tmp ROOT with no
    live/ or configs/ env files."""
    bin_dir, srv, port = _setup_standalone(tmp_path)
    # bare ROOT: only the scripts themselves, no live/*.env, no configs/*/*.env
    bare = tmp_path / "bare"
    (bare / "scripts").mkdir(parents=True)
    for name in ("test-pr-checks.sh", "env.sh", "restart-lib.sh"):
        shutil.copy(ROOT / "scripts" / name, bare / "scripts" / name)
        (bare / "scripts" / name).chmod(0o755)
    (bare / "live").mkdir()
    try:
        env = _minimal_env(tmp_path, bin_dir, port,
                           LMCACHE_CHUNK_SIZE="",
                           KVP_SCRIPTS_DIR=str(bare / "scripts"))
        r = subprocess.run(["bash", str(bare / "scripts" / "test-pr-checks.sh")],
                           env=env, capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    out = r.stdout + r.stderr
    assert r.returncode != 2, (
        "the grep must not die bare under pipefail (exit 2):\n" + out[-1200:])
    assert r.returncode == 5, "the usual INCONCLUSIVE path (exit 5):\n" + out[-1200:]
    assert "cannot determine the LMCache chunk size" in out, out[-1200:]


# ===========================================================================
# R5: an early INCONCLUSIVE must not leave an empty reserved report
# ===========================================================================
def test_r5_early_exit_leaves_no_empty_report(tmp_path):
    """> R5: run the script to an early INCONCLUSIVE (no chunk size -> we
    already know that path exits before the report block) and assert that
    zero-byte pr-checks-report-*.{md} files do NOT linger in live/."""
    bin_dir, srv, port = _setup_standalone(tmp_path)
    try:
        env = _minimal_env(tmp_path, bin_dir, port, LMCACHE_CHUNK_SIZE="")
        r = subprocess.run(["bash", str(PRC)], env=env,
                           capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    out = r.stdout + r.stderr
    live = tmp_path / "live"
    assert r.returncode != 0, out[-800:]
    leftovers = [p for p in live.glob("pr-checks-report-*") if p.stat().st_size == 0]
    assert not leftovers, (
        "the early INCONCLUSIVE must not leave 0-byte reports: "
        f"{[p.name for p in leftovers]}")
    # and no full report either (the run never got a verdict)
    assert not (live / "pr-checks-report.md").exists(), (
        "no latest copy may exist after an early exit")