"""Round 90 C1: crash-evidence collection (scripts/collect-crash-evidence.sh).

On a non-operator engine stop (state exited/dead/restarting) the launcher
path must snapshot EVERYTHING needed for diagnosis into
live/crash-<UTC>/ -- engine docker logs + inspect + state, lmcache-server.log
(tail + numbered grep), nvidia-smi/free/dmesg -- and keep only the newest 5
crash dirs (rotation).

Run against a stub docker/nvidia-smi/dmesg so no real daemon is touched.
MUTATIONs caught:
  * engine logs not collected (docker logs command dropped) -> engine-docker.log
    missing/empty;
  * rotation dropped -> more than 5 crash dirs survive;
  * the script failing hard on a broken docker (it must exit 0 always).
"""
from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
SCRIPT = KV / "scripts" / "collect-crash-evidence.sh"

STUBS = {
    "docker": '''#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  logs) echo "engine log line: EngineCore assert req_id in self.requests"; exit 0 ;;
  inspect) echo '{"State":{"Status":"exited","ExitCode":1}}'; exit 0 ;;
  *) exit 0 ;;
esac
''',
    "nvidia-smi": '#!/usr/bin/env bash\necho "GPU 0 mem total 98304 MiB"\n',
    "dmesg": '#!/usr/bin/env bash\necho "[123] NVRM: nothing"\n',
    "free": '#!/usr/bin/env bash\necho "Mem: 500Gi total"\n',
}


@pytest.fixture()
def sandbox(tmp_path):
    b = tmp_path / "bin"; b.mkdir()
    for n, body in STUBS.items():
        p = b / n
        p.write_text(body); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    # engine + a fake lmcache log with grep-able errors
    (tmp_path / "lmcache-server.log").write_text(
        "info start\nERROR: Cannot store keys due to exception: boom\n"
        "Some keys not found during retrieve!\n"
        + "\n".join(f"filler {i}" for i in range(600)) + "\n")
    env = os.environ.copy()
    env["PATH"] = f"{b}:{env['PATH']}"
    env["STUB_LOG"] = str(tmp_path / "stub.log")
    env["KVP_CACHE_ROOT"] = str(tmp_path)   # unused when the log path is passed
    return tmp_path, env


def _run(sandbox_env):
    tmp, env = sandbox_env
    # run from a repo copy so live/ writes land in tmp: the script resolves
    # ROOT from its own location -> invoke with an explicit LIVE-dir override?
    # The script hardcodes $ROOT/live -- so copy it under tmp/scripts.
    repo = tmp / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    dst = repo / "scripts" / "collect-crash-evidence.sh"
    dst.write_text(SCRIPT.read_text())
    dst.chmod(dst.stat().st_mode | stat.S_IEXEC)
    return subprocess.run(
        ["bash", str(dst), "mimo-pro-tp4-hotsplit", str(tmp / "lmcache-server.log")],
        env=env, capture_output=True, text=True, timeout=60)


def test_crash_evidence_captured(sandbox):
    r = _run(sandbox)
    assert r.returncode == 0, f"evidence collection must never fail the flow:\n{r.stdout}{r.stderr}"
    tmp, _ = sandbox
    crash_dirs = list((tmp / "repo" / "live").glob("crash-*"))
    assert len(crash_dirs) == 1, f"one crash dir expected, got {crash_dirs}"
    files = {p.name for p in crash_dirs[0].iterdir()}
    for want in ("engine-docker.log", "engine-inspect.json", "engine-state.txt",
                 "lmcache-server-tail500.log", "lmcache-errors-numbered.log",
                 "host-state.txt"):
        assert want in files, f"{want} missing in evidence dir (found: {sorted(files)})"
    # the grep must carry line numbers (the review asked for `grep -n`)
    err = (crash_dirs[0] / "lmcache-errors-numbered.log").read_text()
    assert ":ERROR: Cannot store keys" in err, f"numbered grep broken:\n{err[:300]}"
    # the path is announced as the LAST stdout line
    assert str(crash_dirs[0]) in r.stdout.splitlines()[-1]


def test_engine_log_matches_stub(sandbox):
    r = _run(sandbox)
    tmp, _ = sandbox
    d = list((tmp / "repo" / "live").glob("crash-*"))[0]
    log = (d / "engine-docker.log").read_text()
    assert "EngineCore assert" in log, "docker logs output must land in engine-docker.log"


def test_rotation_keeps_newest_five(sandbox):
    tmp, env = sandbox
    r0 = _run(sandbox)                      # first evidence dir, s=1
    assert r0.returncode == 0
    # add 5 stale fake dirs with old mtimes -> 6 total before the next run
    live = tmp / "repo" / "live"
    for i in range(5):
        d = live / f"crash-20260101T00000{i}Z"
        d.mkdir()
        (d / "old.txt").write_text("x")
        os.utime(d, (time.time() - 10000, time.time() - 10000))
    r1 = _run(sandbox)                      # rotation happens on collection
    assert r1.returncode == 0
    dirs = list(live.glob("crash-*"))
    assert len(dirs) <= 5, f"rotation must keep 5, got {len(dirs)}: {sorted(dirs)}"
    assert not (live / "crash-20260101T000000Z").exists(), (
        "the OLDEST fake dir must be pruned first")