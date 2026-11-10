"""Round 44 regression: protocol ping + automatic server diagnostics.

The install run died at register_kv_caches (300s timeout) while the TCP
pre-flight passed -- the socket was open but the RPC channel was dead.
Round 44 adds:
* kvpersist.mp_ping -- a protocol-level probe (RequestClient.ping) wired
  into the pre-flight, refusing to launch when the server is silent;
* run-vllm-with-persist.sh -- automatic cache-server diagnostics (pid
  liveness, log freshness, last lines) printed when the container dies;
* start-cache.sh -- PYTHONFAULTHANDLER=1 so a hung thread dumps stacks.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
LAUNCHER = _T.recipe_launcher()  # Round 80b: recipe owns the engine launch
START_CACHE = ROOT / "scripts" / "start-cache.sh"


def test_mp_ping_skip_without_lmcache():
    """Without lmcache installed mp_ping must exit 3 (skipped), not fail."""
    r = subprocess.run(
        [sys.executable, "-m", "kvpersist.mp_ping", "127.0.0.1", "5555", "2"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(IMPL)},
    )
    out = r.stdout + r.stderr
    assert r.returncode == 3, f"expected skip exit 3, got {r.returncode}:\n{out}"
    assert "skipped" in out, out


def test_mp_ping_failure_exit_code():
    """A dead server must exit 2 with a FAIL message (not hang forever)."""
    # use a port that nothing listens on; timeout 2s keeps the test fast
    r = subprocess.run(
        [sys.executable, "-m", "kvpersist.mp_ping", "127.0.0.1", "1", "2"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(IMPL)},
    )
    out = r.stdout + r.stderr
    # without lmcache on the test host this is a skip (3); with lmcache it
    # must FAIL (2). Both are acceptable -- never 0 and never a hang.
    assert r.returncode in (2, 3), f"unexpected exit {r.returncode}:\n{out}"


def test_launcher_pre_flight_wires_mp_ping():
    """The launcher must probe the RPC protocol before docker run and
    refuse to launch on a dead RPC channel."""
    t = _T.contract_text()
    assert "kvpersist.mp_ping" in t, "the pre-flight must call mp_ping"
    idx = t.index("kvpersist.mp_ping")
    # the ping call must come before the docker run of the engine
    run_idx = t.index('docker run -d --name "$CONTAINER"')
    assert idx < run_idx, "mp_ping must run BEFORE the engine starts"
    assert "RPC ping" in t, "the refusal message must name the RPC ping"


def test_launcher_prints_server_diagnostics_on_failure():
    """When the container dies the launcher must dump the cache server's
    state automatically (round 44)."""
    t = _T.contract_text()
    assert "cache server diagnostics (automatic)" in t
    for needle in ("lmcache-server.pid", "lmcache-server.log",
                   "last 30 server log lines"):
        assert needle in t, f"the diagnostics must show {needle}"


def test_start_cache_enables_faulthandler():
    """Both launch branches (docker + host) must set PYTHONFAULTHANDLER=1
    so a hung server thread dumps stacks instead of hanging silently."""
    t = START_CACHE.read_text()
    assert "PYTHONFAULTHANDLER=1" in t
    assert t.count("PYTHONFAULTHANDLER=1") >= 2, (
        "both the docker branch and the host branch need the faulthandler"
    )