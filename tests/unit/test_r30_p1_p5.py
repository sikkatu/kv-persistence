# SPDX-License-Identifier: Apache-2.0
"""r30: close the gaps named in the round-30 review.

P1  `kvp status`/`kvp logs` must survive a missing live/engine_container
    (fresh clone) -- the `head|tr` substitution needs `|| true` INSIDE it.
P2  start-cache.sh: the log-start mark is written AFTER every refusal and
    without stderr noise (a refused start never moves it).
P4  _kvp_bound_block arithmetic on exact inputs (no strip); the
    kvp_scan_l2_failures scanner must not die of SIGPIPE on a 9 MB log.
P5c start-observability.sh: the collector failing the network inspect
    between State.Running and the second docker inspect must not flip exits.
P5d kvp_l2_writes_status normalizes a garbage/empty offset by itself.
All behavioural: real scripts + stub docker, isolated tmp roots.
"""
from __future__ import annotations
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
ENV_SH = ROOT / "scripts" / "env.sh"
OBS = ROOT / "scripts" / "start-observability.sh"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_r27_uid import _start_cache   # noqa: E402  (the shared harness)

def _write_stub(bin_dir: Path, name: str, text: str):
    p = bin_dir / name
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p

def _sh(cmd: str, env_extra=None, cwd=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["bash", "-c", cmd], capture_output=True,
                          text=True, timeout=120, env=env, cwd=str(cwd or ROOT))
# ---------------------------------------------------------------------------
# P1: `kvp status` must work WITHOUT live/engine_container (fresh clone)
# ---------------------------------------------------------------------------
def _fresh_kvp_repo(tmp_path: Path) -> Path:
    """A tmp copy of scripts/ (+ implementation/): the state of a FRESH git
    clone before the first start-all.sh -- NO live/ dir."""
    repo = tmp_path / "kvp"
    (repo / "scripts").mkdir(parents=True)
    for f in (ROOT / "scripts").iterdir():
        if f.is_file():
            shutil.copy2(f, repo / "scripts" / f.name)
    return repo

def _kvp_status(tmp_path: Path, repo: Path, cache_root: Path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_stub(bin_dir, "docker", "#!/usr/bin/env bash\nexit 0\n")
    env = dict(os.environ,
               HOME=str(home),
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_REPO=str(repo),
               KVP_CACHE_ROOT=str(cache_root),
               KVP_SKIP_IPC_TEST="1")
    return subprocess.run(["bash", str(repo / "scripts" / "kvp"), "status"],
                          capture_output=True, text=True, timeout=60, env=env,
                          cwd=str(tmp_path))
def test_p1_kvp_status_without_live_dir_old_error(tmp_path):
    """P1: fresh clone (NO live/engine_container), old failure BEFORE the
    mark -> rc 0 + `L2 writes: OK` (history, not current health).
    MUTATIONS: dropping `|| true` in the head|tr substitution (rc=1, silent
    death); calling kvp_l2_writes_status without the offset (FAILING)."""
    repo = _fresh_kvp_repo(tmp_path)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    old_fail = ("[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
                "[2026-11-22 08:03:27] LMCache DEBUG L2 store completed: adapter 0, 0 succeeded, 20 failed\n")
    clean = "[2026-11-22 09:00:01] LMCache DEBUG L2 store completed: adapter 0, 8 succeeded, 0 failed\n"
    (cache_root / "lmcache-server.log").write_text(old_fail + clean)
    (cache_root / "lmcache-server.log.start").write_text(f"{len(old_fail)}\n")
    r = _kvp_status(tmp_path, repo, cache_root)
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"`kvp status` must survive a missing live/ (fresh clone):\n{out[-800:]}")
    assert "L2 writes: OK" in out, out[-800:]
    assert "L2 writes: FAILING" not in out, out[-800:]
    assert "engine container:" not in out, "without live/ there is no engine-container line"
def test_p1_kvp_status_new_failure_is_failing(tmp_path):
    """P1: fresh clone, a NEW failure after the mark ->
    `FAILING (1 failed since this server started)`, script still rc 0."""
    repo = _fresh_kvp_repo(tmp_path)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    clean = "[2026-11-22 09:00:01] LMCache DEBUG L2 store completed: adapter 0, 8 succeeded, 0 failed\n"
    new_fail = "[2026-11-22 09:05:00] LMCache ERROR Serde task 7 (SERIALIZE) failed\n"
    (cache_root / "lmcache-server.log").write_text(clean + new_fail)
    (cache_root / "lmcache-server.log.start").write_text("0\n")
    r = _kvp_status(tmp_path, repo, cache_root)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-800:]
    assert "L2 writes: FAILING (1 failed since this server started)" in out, out[-800:]
def test_p1_kvp_status_with_engine_container(tmp_path):
    """P1: WITH live/engine_container -> the `engine container:` line prints,
    rc 0."""
    repo = _fresh_kvp_repo(tmp_path)
    (repo / "live").mkdir()
    (repo / "live" / "engine_container").write_text("mimo-pro-tp4-kvp\n")
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    r = _kvp_status(tmp_path, repo, cache_root)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-800:]
    assert "engine container: mimo-pro-tp4-kvp" in out, out[-800:]
def test_p1_kvp_logs_survives_missing_live_dir(tmp_path):
    """P1 second line (kvp logs): missing live/ must not kill the script
    before `docker logs` runs (stub docker captures argv)."""
    repo = _fresh_kvp_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_log = tmp_path / "stub.log"
    _write_stub(bin_dir, "docker",
                f'#!/usr/bin/env bash\necho "$*" >> "{stub_log}"\nexit 0\n')
    env = dict(os.environ,
               HOME=str(home),
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_REPO=str(repo))
    r = subprocess.run(["bash", str(repo / "scripts" / "kvp"), "logs"],
                       capture_output=True, text=True, timeout=60, env=env,
                       cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert "No such file" not in out, out[-600:]
    assert stub_log.exists(), f"`kvp logs` must reach docker:\n{out[-600:]}"
    assert "logs -f mimo-pro-tp4-kvp" in stub_log.read_text(), stub_log.read_text()
# ---------------------------------------------------------------------------
# P2: the log-start mark (start-cache.sh, via the _start_cache harness)
# ---------------------------------------------------------------------------
def test_p2a_refused_start_does_not_move_the_mark(tmp_path):
    """P2a: a LIVE server (pid file with a live process whose cmdline contains
    'lmcache') -> `die "lmcache server already running"` AND the mark file is
    untouched byte for byte. MUTATIONS: writing the mark BEFORE the
    already-running check; dropping the mark write entirely."""
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    old = "[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
    old = old + "x" * (91 - len(old))          # 91 bytes BEFORE the mark
    tail = ("[2026-11-22 08:09:00] LMCache ERROR Store task 7 to adapter 0 failed for keys ['a']\n")
    tail = tail + "y" * (198 - 91 - len(tail))  # pad to a 198-byte total
    log = cache_root / "lmcache-server.log"
    log.write_text(old + tail)
    assert log.stat().st_size == 198, f"fixture must be 198 bytes: {log.stat().st_size}"
    mark = cache_root / "lmcache-server.log.start"
    mark.write_text("91\n")
    with open(mark, "rb") as fh:
        before = fh.read()
    # a live process whose cmdline LOOKS like a lmcache server
    proc = subprocess.Popen(["bash", "-c", "exec -a lmcache-fake-probe sleep 120"])
    try:
        (cache_root / "lmcache-server.pid").write_text(f"{proc.pid}\n")
        r, _, _ = _start_cache(tmp_path, cache_root=cache_root)
        out = r.stdout + r.stderr
        assert "already running" in out, out[-1000:]
        assert r.returncode != 0, "a live server must REFUSE a second start"
        with open(mark, "rb") as fh:
            after = fh.read()
        assert after == before, (
            "a REFUSED start must not move the log-start mark: "
            f"before={before!r} after={after!r}")
    finally:
        proc.kill()
        proc.wait()
def test_p2b_no_log_no_stderr_noise_mark_zero(tmp_path):
    """P2b: no server log at start -> stderr has NO 'No such file or
    directory', after the start the mark equals 0.
    MUTATION: restoring `wc -c < \"$LOG\" 2>/dev/null || echo 0` (the `<`
    redirect is parsed before 2>/dev/null -> stderr noise)."""
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "lmcache-server.pid").write_text("999999\n")   # dead pid
    r, _, _ = _start_cache(tmp_path, cache_root=cache_root)
    out = r.stdout + r.stderr
    assert "No such file or directory" not in out, out[-600:]
    mark = cache_root / "lmcache-server.log.start"
    assert mark.read_text().strip() == "0", f"the mark must be 0: {mark.read_text()!r}"
def test_p2c_mark_equals_log_size(tmp_path):
    """P2c: a 201-byte log and a clean start -> the mark equals 201."""
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "lmcache-server.log").write_text("z" * 200 + "\n")
    (cache_root / "lmcache-server.pid").write_text("999999\n")   # dead pid
    r, _, _ = _start_cache(tmp_path, cache_root=cache_root)
    mark = cache_root / "lmcache-server.log.start"
    assert mark.read_text().strip() == "201", f"mark must be 201: {mark.read_text()!r}"
# ---------------------------------------------------------------------------
# P4: _kvp_bound_block exact arithmetic (inputs line_NNN, NO strip)
# ---------------------------------------------------------------------------
def _run_bound(inp: str, max_: int = 80, hd: int = 20):
    return subprocess.run(
        ["bash", "-c", f'. "{ENV_SH}" && _kvp_bound_block {max_} {hd}'],
        input=inp, capture_output=True, text=True, timeout=30,
        env=dict(os.environ, KVP_SKIP_IPC_TEST="1"))
def test_p4_bound_zero_input():
    """P4: 0 lines -> EMPTY output (no marker)."""
    r = _run_bound("", 80, 20)
    assert r.stdout == "", repr(r.stdout)
def test_p4_bound_80_unchanged():
    """P4: 80 lines with 80/20 -> UNCHANGED, no marker."""
    inp = "".join(f"line_{i:03d}\n" for i in range(1, 81))
    r = _run_bound(inp, 80, 20)
    assert r.stdout == inp, "80 lines must pass through UNCHANGED"
    assert "lines skipped" not in r.stdout
def test_p4_bound_81_exact():
    """P4: 81 lines with 80/20 -> 1-20, `... (1 lines skipped) ...`, 22-81
    (exactly 81 output lines, the 21st is the ONLY skipped one).
    MUTATION: `NR - max` -> `NR - hd` (marker says 20)."""
    inp = "".join(f"line_{i:03d}\n" for i in range(1, 82))
    r = _run_bound(inp, 80, 20)
    out_lines = r.stdout.splitlines(keepends=True)
    assert len(out_lines) == 81, f"exactly 81 lines, got {len(out_lines)}:\n{r.stdout[-300:]}"
    assert out_lines[:20] == [f"line_{i:03d}\n" for i in range(1, 21)]
    assert out_lines[20] == "... (1 lines skipped) ...\n", repr(out_lines[20])
    assert out_lines[21:] == [f"line_{i:03d}\n" for i in range(22, 82)]
def test_p4_bound_125_exact():
    """P4: 125 lines with 80/20 -> exactly 81 lines: 1-20,
    `... (45 lines skipped) ...`, 66-125. MUTATIONS: `NR - max` -> `NR - hd`
    (marker says 60); the tail one line longer/shorter (82/80 lines)."""
    inp = "".join(f"line_{i:03d}\n" for i in range(1, 126))
    r = _run_bound(inp, 80, 20)
    out_lines = r.stdout.splitlines(keepends=True)
    assert len(out_lines) == 81, f"exactly 81 lines, got {len(out_lines)}"
    assert out_lines[:20] == [f"line_{i:03d}\n" for i in range(1, 21)]
    assert out_lines[20] == "... (45 lines skipped) ...\n", repr(out_lines[20])
    assert out_lines[21:] == [f"line_{i:03d}\n" for i in range(66, 126)]
def test_p4_bound_40_10_exact():
    """P4: the scanner params 40/10 on 100 lines -> 1-10,
    `... (60 lines skipped) ...`, 71-100 (exactly 41 output lines)."""
    inp = "".join(f"line_{i:03d}\n" for i in range(1, 101))
    r = _run_bound(inp, 40, 10)
    out_lines = r.stdout.splitlines(keepends=True)
    assert len(out_lines) == 41, f"exactly 41 lines, got {len(out_lines)}"
    assert out_lines[:10] == [f"line_{i:03d}\n" for i in range(1, 11)]
    assert out_lines[10] == "... (60 lines skipped) ...\n", repr(out_lines[10])
    assert out_lines[11:] == [f"line_{i:03d}\n" for i in range(71, 101)]
# ---------------------------------------------------------------------------
# P4: kvp_scan_l2_failures must not die of SIGPIPE on a huge noisy log
# ---------------------------------------------------------------------------
def _scan_script(logf: Path) -> str:
    """A bare `set -Eeuo pipefail` script calling the REAL scanner -- the
    shape of a future un-`||`-guarded caller: the scanner must print its
    block and return 1 (never die 141 with empty output)."""
    return (f'set -Eeuo pipefail\n. "{ENV_SH}"\n'
            f'kvp_scan_l2_failures "{logf}" 0\n')
def test_p4_scanner_sigpipe_no_silent_death(tmp_path):
    """P4: error line + 3 traceback lines + next timestamp + 90 000 DEBUG
    lines (~9.4 MB, way past the 64 KB pipe buffer) under the REAL
    `set -Eeuo pipefail` -> the error line AND `L2 STORE FAILING` print,
    rc=1 (NOT 141 with empty output). MUTATION: `done=1; next` -> `exit` in
    the scanner selector (tail dies SIGPIPE, silent death)."""
    err = ("[2026-11-22 08:03:25] LMCache ERROR Serde task 1 (SERIALIZE) failed\n"
           "Traceback (most recent call last):\n"
           '  File "/kvp-impl/kvpersist/compress_serde.py", line 252, in serialize\n'
           "    out[: len(frame)] = frame\n")
    next_ts = "[2026-11-22 08:03:26] LMCache DEBUG heartbeat\n"
    noise = "".join(
        f"[2026-11-22 08:04:{i % 60:02d}] LMCache DEBUG skipping eviction\n"
        for i in range(90000))
    logf = tmp_path / "server.log"
    with open(logf, "w") as fh:
        fh.write(err + next_ts + noise)
    script = _scan_script(logf)
    r = subprocess.run(["bash", "-c", script], capture_output=True,
                       text=True, timeout=300, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 1, (
        f"the scanner must FAIL with rc=1, not die silently (rc={r.returncode}):\n{out[-400:]}")
    assert "Serde task 1 (SERIALIZE) failed" in out, out[-400:]
    assert "L2 STORE FAILING" in out, out[-400:]
    assert "skipping eviction" not in out, "noise must be cut"
# ---------------------------------------------------------------------------
# P5c: the collector dying BETWEEN the State.Running check and the network
# inspect must not flip the exit code (start-observability.sh line 133)
# ---------------------------------------------------------------------------
def _run_obs_tail(tmp_path, stub: str):
    """The REAL `set -Eeuo pipefail` header + the REAL r28 D1 tail of
    start-observability.sh, with a custom docker stub."""
    src = OBS.read_text()
    header = next(line for line in src.splitlines()
                  if line.startswith("set -Eeuo pipefail"))
    start = src.index("# r28 D1: the network the monitoring stack needs")
    tail = src[start:]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\n" + stub)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
    return subprocess.run(["bash", "-c", header + "\n" + tail],
                          capture_output=True, text=True, timeout=30,
                          env=env, cwd=str(tmp_path))
def test_p5c_collector_dies_between_inspects(tmp_path):
    """P5c: State.Running=true, but the NETWORK inspect for the collector
    fails (exit 1, no output -- the collector just died), the cache network
    is non-empty -> rc 0, NOTE (different nets: one side lost), script reaches
    the end. MUTATION: dropping `|| true` from the _srv_net assignment
    (line 133) -> pipefail kills the script, rc != 0."""
    r = _run_obs_tail(tmp_path, (
        'case "$*" in\n'
        '  *State.Running*obs_otel_collector*) echo true ;;\n'
        '  *NetworkSettings*obs_otel_collector*) exit 1 ;;\n'      # collector gone
        f'  *NetworkSettings*lmcache-kvp-server*) echo "cache_net" ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n'))
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"a dying collector must NOT flip the exit code:\n{out[-800:]}")
    assert "NOTE: the cache server was started before observability" in out, (
        f"empty srv_net vs non-empty cache_net -> the NOTE must print:\n{out[-800:]}")
# ---------------------------------------------------------------------------
# P5d: kvp_l2_writes_status must normalize a garbage/empty offset by itself
# ---------------------------------------------------------------------------
def _l2_status_raw(arg: str, log_text: str):
    logf = Path("/tmp") / "kvp-p5d-test.log"
    logf.write_text(log_text)
    return _sh(f'. "{ENV_SH}" && kvp_l2_writes_status "{logf}" {arg}',
               env_extra={"KVP_SKIP_IPC_TEST": "1"})
def test_p5d_garbage_and_empty_offset_normalized():
    """P5d: a direct call with an EMPTY offset and with `abc` on a log with
    one failure must give the SAME verdict as offset 0:
    `FAILING (1 failed since this server started)`, no arithmetic error.
    MUTATION: dropping the `case ... _from=0` normalizer in
    kvp_l2_writes_status (bash arithmetic on `abc` -> unbound/crash)."""
    fail = "[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
    for arg in ('""', "abc"):
        r = _l2_status_raw(arg, fail)
        out = r.stdout + r.stderr
        assert "FAILING (1 failed since this server started)" in out, (
            f"offset {arg} must fall back to 0 (as with offset 0):\n{out}")
        assert "must be" not in out and "unbound" not in out, (
            f"no arithmetic error for offset {arg}:\n{out}")
        assert r.returncode == 1, f"rc 1 expected for offset {arg}:\n{out}"
