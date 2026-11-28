# SPDX-License-Identifier: Apache-2.0
"""r32.2: regression tests for the follow-up review.

R3: the abort deadline is enforced across EVERY phase (connect, request
transmission, response reading) with time.monotonic(); late aborts (any
phase past window+tolerance) are recorded with late=1 and WAIVED from the
early count. Tolerances here are tight on purpose (a timestamped deadline
test must not allow 1500 ms slack on a 300-400 ms window).
R8: timestamped report names are reserved ATOMICALLY -- two concurrent
reservations in the same UTC second get distinct files.
"""
from __future__ import annotations
import os
import re
import select
import socket
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRC = ROOT / "scripts" / "test-pr-checks.sh"


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


def _abort_once(tmp_path: Path, host: str, port: int, ms: int, tol_ms: str = "250") -> dict:
    """Run the REAL abort_stream and parse its one metric line."""
    af = tmp_path / "ab-r322.txt"
    af.write_text("")
    pfile = tmp_path / "pr-r322.txt"
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


class _SlowHandler:
    """For the r322 tests: controllable response delay (via class knob)."""
    pass


def _api(delay: float = 0.0):
    """A tiny stub HTTP server honouring KVP-style POST /v1/chat/completions."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(n)
            time.sleep(delay)
            body = json.dumps({"ok": True, "pad": "x" * 200}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = None; port = 0
    for port in range(19500, 19900):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            continue
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# ===========================================================================
# R3: the deadline is enforced in EVERY phase (connect, send, read)
# ===========================================================================
def _fill_accept_queue(listen_sock):
    """Keep connecting (and holding) sockets until the accept queue is
    saturated so a NEW connect stalls -- fully local, no routing involved
    (this is what r33 R6 wants instead of 10.255.255.1)."""
    held = []
    for _ in range(64):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            s.connect(listen_sock.getsockname())
        except Exception:
            s.close()
            break
        held.append(s)
    return held


def test_r3_connect_delayed_closes_at_the_deadline(tmp_path):
    """> R3/R6: a STALLED connect (local listener with a saturated accept queue
    -- no route lookup failure or network dependency): with a 300 ms abort
    window the close must LAND at the deadline, `late=0`, `before_first_byte=1`.
    The old code blocked in `create_connection(timeout=30)`. If the local
    kernel cannot stall a connect this exact way, the test SKIPS with the
    reason (never red/green by network luck -- r33 R6)."""
    srv_listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv_listen.bind(("127.0.0.1", 0))
    srv_listen.listen(1)              # tiny backlog -> saturate it quickly
    host, port = srv_listen.getsockname()
    held = _fill_accept_queue(srv_listen)
    # confirm the queue is actually saturated: a probe must NOT complete
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setblocking(False)
    try:
        probe.connect((host, port))
    except BlockingIOError:
        pass
    _, w, _ = select.select([], [probe], [], 0.2)
    probe.close()
    if w and not held:
        srv_listen.close()
        import pytest
        pytest.skip("cannot saturate the accept queue on this kernel (connect "
                    "does not stall) -- delayed-connect test skipped, not faked")
    try:
        t0 = time.monotonic()
        d = _abort_once(tmp_path, host, port, 300)
        elapsed = (time.monotonic() - t0) * 1000.0
    finally:
        for s in held:
            try:
                s.close()
            except Exception:
                pass
        srv_listen.close()
    assert d.get("late") == "0", f"close must be on-time (not late): {d}"
    assert d.get("before_first_byte") == "1", d
    ms = float(d["ms"])
    assert 250 <= ms <= 700, f"close must land near 300 ms, got {ms:.0f} ms"
    assert elapsed < 2500, f"TOTAL wall time (incl. bash startup) {elapsed:.0f} ms -- must never wait on a stalling connect"


def test_r3_send_backpressure_closes_at_the_deadline(tmp_path):
    """> R3: request transmission stalls (the server accepts the connection
    but does NOT read the request -- TCP window fills up and `send` would
    block forever in the old code): with a 400 ms abort window the close
    must STILL land near the deadline, not after some giant send timeout."""
    srv_listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv_listen.bind(("127.0.0.1", 0))
    port = srv_listen.getsockname()[1]
    srv_listen.listen(1)
    acceptor = []
    def _accept():
        c, _ = srv_listen.accept()
        acceptor.append(c)          # hold the connection open, NEVER read
        time.sleep(5.0)
        try:
            c.close()
        except Exception:
            pass
    threading.Thread(target=_accept, daemon=True).start()
    t0 = time.monotonic()
    d = _abort_once(tmp_path, "127.0.0.1", port, 400)
    elapsed = (time.monotonic() - t0) * 1000.0
    srv_listen.close()
    assert d.get("late") == "0", f"close must be on-time despite send backpressure: {d}"
    ms = float(d["ms"])
    assert 300 <= ms <= 800, f"close must land near 400 ms, got {ms:.0f} ms"
    assert elapsed < 2500, f"TOTAL wall time {elapsed:.0f} ms -- must never block in sendall"


def test_r3_normal_operation_respects_the_deadline(tmp_path):
    """> R3 (healthy case): instant response, abort window 400 ms -> the abort
    is EARLY (headers never seen), late=0 and the close lands within the
    window + a TIGHT tolerance (window + 250 ms = 650 ms -- the old test
    allowed 1500 ms on a 400 ms window, far too permissive)."""
    srv, port = _api(3.0)      # response never arrives within the window
    t0 = time.monotonic()
    d = _abort_once(tmp_path, "127.0.0.1", port, 400)
    elapsed = (time.monotonic() - t0) * 1000.0
    srv.shutdown()
    assert d.get("before_first_byte") == "1", d
    assert d.get("late") == "0", f"a healthy on-time abort must not be late: {d}"
    ms = float(d["ms"])
    assert 350 <= ms <= 650, (
        f"deadline accuracy: close must land in [350, 650] ms for a 400 ms "
        f"window + 250 ms tolerance, got {ms:.0f} ms")


def test_r3_late_abort_is_rejected_not_counted(tmp_path):
    """> R3: an abort whose close lands PAST its window (even by 1 ms under a
    ZERO tolerance) is recorded `late=1` and MUST NOT count as a successful
    early abort (before_first_byte forced to 0). The old code would have
    counted `before_first_byte=1` even if the socket only died seconds after
    the requested window -- a false proof. A late abort is a distinct outcome,
    never silently accepted as early.

    Zero tolerance makes this DETERMINISTIC: no real socket close can land
    exactly on the deadline bound, so `ms` is always strictly > `win`, hence
    `late=1` every single run."""
    srv, port = _api(3.0)   # no response within the (tiny) window
    d = _abort_once(tmp_path, "127.0.0.1", port, 10, tol_ms="0")
    srv.shutdown()
    assert d.get("late") == "1", (
        f"with tol=0 any real close overshoot must be tagged late: {d}")
    assert d.get("before_first_byte") == "0", (
        "a LATE abort must NEVER be counted as early: " + str(d))
    # the report line must carry the `late=` tag for external inspection
    assert "late=" in d["_line"], f"metric line must carry late=: {d['_line']}"


# ===========================================================================
# R8: atomic report filename reservation under concurrency
# ===========================================================================
def test_r8_concurrent_reservations_get_distinct_names(tmp_path):
    """> R8: two processes race to reserve pr-checks-report-<same-ts>.md in
    the same UTC second -- the shell-level O_EXCL (noclobber) reservation
    must give BOTH a unique file (neither overwrites the other). We exercise
    the ACTUAL block from test-pr-checks.sh (not a copy) by extracting it
    from the script source, so any mutation to the real reservation code
    is what this test judges (this is the piece that kills the R8race mutant).
    """
    import re as _re
    src = PRC.read_text()
    # extract the real reservation block: from REPORT_TS_BASE= to the first
    # top-level `done` closing its while
    m = _re.search(
        r"(REPORT_TS_BASE=\"[^\"]+\"\n(?:.*\n)*?^done)", src, _re.M)
    assert m, "reservation block not found in test-pr-checks.sh"
    block = m.group(1)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = tmp_path / f"pr-checks-report-{ts}"
    # the block defines REPORT_TS_BASE itself; override via env-style wrapper
    # so the extracted code runs exactly as in production, only with our path
    inner = block.replace("REPORT_TS_BASE=\"$LIVE_DIR/pr-checks-report-$(date -u +%Y%m%dT%H%M%SZ)\"",
                          f'REPORT_TS_BASE="{base}"')
    cmd = inner + '\necho "$REPORT_TS"'
    procs = [subprocess.Popen(["bash", "-c", cmd],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    outs = [p.communicate()[0].strip() for p in procs]
    assert len(set(outs)) == 2, (
        f"two concurrent reservations must land on DIFFERENT filenames, got {outs}")
    for o in outs:
        assert o.endswith(".md"), o
        assert o.startswith(str(base)), o