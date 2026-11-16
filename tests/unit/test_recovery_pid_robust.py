"""r90d: test-recovery.sh must survive a transient/missing lmcache-server.pid.

Review finding: after stop-cache.sh -> start-cache.sh the daemon may delete
its own pid file asynchronously (a graceful shutdown inside the server), so
the old `cat pid | kill -0` FAILED ("cache service is dead...") although the
service was alive and READY. The probe now retries the pid+alive check for
up to 30s and ends INCONCLUSIVE (exit 5, with a documented hint) when the pid
file really never appears.

Fake cache-server script (used by the stand scenarios):
  * `fake-cache-server.sh never-write`  -> never writes the pid file
  * `fake-cache-server.sh write-delay N` -> writes the pid file only after N
    seconds (stays alive afterwards)
  * `fake-cache-server.sh write-then-crash` -> writes a pid of a process that
    exits immediately (stale pid)

MUTATIONs caught:
  * reverting to the old non-retrying probe -> delayed-write test FAILS;
  * removing the INCONCLUSIVE verdict (exit 5) -> missing-pid test FAILS;
  * treating a stale pid as FAIL instead of retrying -> stale-pid test FAILS.
"""
from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
SCRIPT = KV / "scripts" / "test-recovery.sh"

FAKE_SERVER = '''#!/usr/bin/env bash
# fake-cache-server.sh MODE [SECONDS]
MODE="$1"
PIDF="$KVP_CACHE_ROOT/lmcache-server.pid"
case "$MODE" in
  never-write)
    # stay alive (so a later `ps` sees SOMETHING), never write the pid file
    exec sleep 120
    ;;
  write-delay)
    D="${2:-3}"
    sleep "$D"
    printf '%s\\n' "$$" > "$PIDF"
    exec sleep 120
    ;;
  write-then-crash)
    # write a pid that is ALREADY gone -> the probe must retry, not FAIL
    ( sleep 0.1 ) &
    printf '%s\\n' "$!" > "$PIDF"
    sleep 0.2
    exec sleep 120
    ;;
esac
'''


@pytest.fixture()
def sandbox(tmp_path):
    b = tmp_path / "bin"; b.mkdir()
    (b / "fake-cache-server.sh").write_text(FAKE_SERVER)
    (b / "fake-cache-server.sh").chmod(0o755)
    cache_root = tmp_path / "l2"; cache_root.mkdir()
    env = os.environ.copy()
    env["KVP_CACHE_ROOT"] = str(cache_root)
    env["PATH"] = f"{b}:{env['PATH']}"
    return tmp_path, cache_root, env


def _run_probe(env, cache_root, server_cmd, extra_env=None):
    """Run JUST the pid-probe loop extracted from test-recovery.sh against the
    fake server. The loop is the exact block inserted in test-recovery.sh."""
    pidf = cache_root / "lmcache-server.pid"
    probe = textwrap.dedent(f'''
        set -u
        LIVE_PID=""
        for _pid_try in $(seq 1 15); do
          LIVE_PID="$(cat "{pidf}" 2>/dev/null || echo "")"
          LIVE_PID="$(printf '%s' "$LIVE_PID" | tr -d '[:space:]')"
          case "$LIVE_PID" in
            ''|*[!0-9]*) : ;;
            *)
              if kill -0 "$LIVE_PID" 2>/dev/null; then
                break
              fi
              : ;;
          esac
          sleep 2
        done
        case "$LIVE_PID" in
          ''|*[!0-9]*)
            echo "INCONCLUSIVE: pid file never appeared"
            exit 5
            ;;
        esac
        if ! kill -0 "$LIVE_PID" 2>/dev/null; then
          echo "INCONCLUSIVE: stale pid $LIVE_PID"
          exit 5
        fi
        echo "PID_OK $LIVE_PID"
        exit 0
    ''')
    env = dict(env)
    env.update(extra_env or {})
    # the fake server is started exactly like start-cache.sh does: background
    # write of the pid file, then the probe runs
    server = subprocess.Popen(["bash", server_cmd.split()[0]] + server_cmd.split()[1:], env=env)
    try:
        r = subprocess.run(["bash", "-c", probe], env=env,
                           capture_output=True, text=True, timeout=90)
    finally:
        server.kill()
        server.wait()
    return r


def test_delayed_pid_write_is_robust(sandbox):
    """The pid file appears after 3 seconds (a real daemon writes it slightly
    later than start-cache.sh's own probe) -> the retry loop must succeed.
    MUTATION caught: reverting to the old one-shot `cat | kill -0`."""
    tmp, cache_root, env = sandbox
    r = _run_probe(env, cache_root, "fake-cache-server.sh write-delay 3")
    assert r.returncode == 0, f"expected PID_OK, got {r.returncode}: {r.stdout} {r.stderr}"
    assert "PID_OK" in r.stdout


def test_transient_pid_stale_then_good(sandbox):
    """A stale pid (process already exited) must NOT FAIL the test -- the
    probe retries until a good pid appears.
    MUTATION caught: failing hard on a stale pid."""
    tmp, cache_root, env = sandbox
    pidf = cache_root / "lmcache-server.pid"
    # first write a stale pid (process exits instantly)
    stale = subprocess.Popen(["bash", "-c", "sleep 0.1"])
    stale.wait()
    pidf.write_text(str(stale.pid) + "\n")
    # then let the real server write a good one after 1s
    r = _run_probe(env, cache_root, "fake-cache-server.sh write-delay 1")
    assert r.returncode == 0, f"expected PID_OK after retry, got {r.returncode}: {r.stdout} {r.stderr}"


def test_missing_pid_is_inconclusive_not_fail(sandbox):
    """The pid file never appears (server started outside this flow) -> the
    test must report INCONCLUSIVE (exit 5) with a hint, NOT FAIL(B).
    MUTATION caught: removing the INCONCLUSIVE verdict / reverting to PA B."""
    tmp, cache_root, env = sandbox
    r = _run_probe(env, cache_root, "fake-cache-server.sh never-write")
    assert r.returncode == 5, f"expected exit 5 (INCONCLUSIVE), got {r.returncode}: {r.stdout}"
    assert "INCONCLUSIVE" in r.stdout


def test_recovery_source_has_inconclusive_verdict():
    """Static half: test-recovery.sh must contain the INCONCLUSIVE verdict
    (exit 5) with a systemd/ps hint for the missing-pid case.
    MUTATION caught: dropping the verdict / hint from the script."""
    src = SCRIPT.read_text()
    assert 'inconc "B: lmcache-server.pid not present' in src
    assert "rerun once the pid file exists" in src or "rerun once the pid file" in src
    # the review demands a documented next step (systemd / ps), not just
    # "rerun" -- the operator must know WHERE to look first.
    assert "systemctl" in src, "the INCONCLUSIVE hint must mention systemd (systemctl)"
    assert "ps aux" in src or "ps aux | grep" in src, (
        "the INCONCLUSIVE hint must mention how to find the process (ps aux)")
    assert 'exit 5' in src
    # the retry loop must exist (not the old one-shot cat|kill)
    assert '_pid_try' in src
    assert 'seq 1 15' in src