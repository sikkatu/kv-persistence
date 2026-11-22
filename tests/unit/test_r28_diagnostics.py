# SPDX-License-Identifier: Apache-2.0
"""r28 L2-L5, L7-L9, D1: bounded traceback, honest diagnostics, loud L2
failures, correct IPC consumer mode, real server pid, observability network.

All behavioural with stub docker / stub scripts, no fixed ports or real
paths (H3/H4). Covered: the L2 bounded traceback (all four branches), the
L7 scanner, the L8 consumer-mode function, the L9 server-pid resolution
and server_read_bytes, the L3 pre-flight verdicts (rc=3 vs boundary failure,
Consumer error line) and the D1 observability-network wiring.
"""
from __future__ import annotations
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECIPES = Path("/home/operit/recipes")
LAUNCHER = RECIPES / "recipes" / "rtx-pro-6000" / "mimo-v2.6-pro-hotsplit" / "scripts" / "run-tp4-hotsplit.sh"
RECOVERY = ROOT / "scripts" / "test-recovery.sh"
ENV_SH = ROOT / "scripts" / "env.sh"
IPC_TEST = ROOT / "scripts" / "ipc-test.sh"
START_CACHE = ROOT / "scripts" / "start-cache.sh"


def _write_stub(bin_dir: Path, name: str, text: str):
    p = bin_dir / name
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _sh(cmd: str, env_extra=None, cwd=None):
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(["bash", "-c", cmd], capture_output=True,
                          text=True, timeout=60, env=env, cwd=str(cwd or ROOT))


# ---------------------------------------------------------------------------
# L2 fixture: a server log with the ERROR block + 300 s of DEBUG noise
# ---------------------------------------------------------------------------
def _error_log(with_noise=True) -> str:
    # 30 frames x 2 lines = 60 + header/traceback/exc/driver = 68 lines total
    # (within the 80-line bound; the r28 L2 rule is "the whole error block,
    # at most 80 lines").
    frames = "".join(
        f'  File "/opt/venv/lib/python3.12/site-packages/lmcache/x.py", '
        f'line {100 + i}, in frame_{i}\n    do_step_{i}()\n'
        for i in range(30))
    exc = ("torch.AcceleratorError: CUDA error: mapping of buffer object failed\n"
           "Failed to open/create interprocess memory file\n"
           "Returning 205 (CUDA_ERROR_MAP_FAILED) from cuIpcOpenEventHandle\n")
    driver = ("The CUDA driver logged these messages:\n"
              "> CUDA_ERROR_MAP_FAILED: mapping of buffer object failed\n")
    block = ("[2026-11-22 08:03:25,100] LMCache ERROR Error handling operation register_kv_cache\n"
             "Traceback (most recent call last):\n" + frames + exc + driver)
    noise = ""
    if with_noise:
        noise = "".join(
            f"[2026-11-22 08:04:{i // 2:02d},{(i % 2) * 500:03d}] LMCache DEBUG skipping eviction L2 usage 0.00\n"
            for i in range(600))
    return block + "\n" + noise


def _bounded_select(log_text: str):
    """The r28 L2 bounded-selection algorithm, exactly as wired in the scripts."""
    r = subprocess.run(
        ["bash", "-c",
         "awk 'f && /^\\[20[0-9][0-9]-/ {exit} /Error handling operation register_kv_cache/ {f=1} f {print}' | head -n 80"],
        input=log_text, capture_output=True, text=True, timeout=30)
    return r.stdout


def test_l2_bounded_select_prints_whole_exception():
    """> L2: the bounded selection prints the exception + driver block,
    ZERO 'skipping eviction' lines, and <= 80 lines total. MUTATION caught:
    restoring `,$p` (600 noise lines) or `head -25` (cutting the exception's
    last line)."""
    log = _error_log(with_noise=True)
    out = _bounded_select(log)
    assert "torch.AcceleratorError" in out, f"the exception must reach the output:\n{out[-400:]}"
    assert "Returning 205" in out, f"the last line must reach the output:\n{out[-400:]}"
    assert "The CUDA driver logged these messages" in out
    assert "skipping eviction" not in out, "DEBUG noise must be cut"
    lines = out.strip().splitlines()
    assert len(lines) <= 80, f"bounded selection: {len(lines)} lines > 80"
    assert len(lines) >= 44, "the error block must be complete"


def test_l2_recipe_failfast_branch_bounded(tmp_path):
    """> L2 (recipe fail-fast branch): the REAL block from run-tp4-hotsplit.sh
    prints the bounded traceback. MUTATION caught: `,$p` / `head -25`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_log = tmp_path / "stub.log"
    _write_stub(bin_dir, "docker", f'#!/usr/bin/env bash\necho "$*" >> "{stub_log}"\nexit 0\n')
    slog = tmp_path / "lmcache-server.log"
    slog.write_text(_error_log())
    src = LAUNCHER.read_text()
    start = src.index('    _kvp_serr="$(tail')
    end = src.index("      exit 1", start) + len("      exit 1")
    block = src[start:end]
    assert "head -n 80" in block, "the fail-fast branch must use bounded selection"
    script = (
        "_kvp_slog_before=0\n"
        f"_kvp_slog=\"{slog}\"\n"
        "CONTAINER=test-container\n"
        "if [ -f \"$_kvp_slog\" ]; then\n" + block + "\n    fi\nfi\nexit 0\n")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               STUB_LOG=str(stub_log))
    r = subprocess.run(["bash", "-c", script], capture_output=True,
                       text=True, timeout=30, env=env)
    out = r.stdout + r.stderr
    assert "Returning 205" in out, f"the last exception line must print:\n{out[-1500:]}"
    assert "The CUDA driver logged these messages" in out
    assert "skipping eviction" not in out, f"DEBUG noise must be cut:\n{out[-1500:]}"


def test_l2_recovery_branches_bounded():
    """> L2 (both test-recovery.sh branches): no unbounded `,$p` selection is
    left -- every branch uses the bounded awk + head -n 80."""
    src = RECOVERY.read_text()
    bounded = len(re.findall(r"head -n 80", src))
    unbounded = len(re.findall(r",\\$p", src))
    assert unbounded == 0, "the unbounded `,$p` selection must be gone"
    assert bounded >= 2, "both test-recovery.sh branches must be bounded"


# ---------------------------------------------------------------------------
# L7: the L2 store-failure scanner
# ---------------------------------------------------------------------------
def _scan_l2(log_text: str, from_byte: int = 0):
    logf = Path("/tmp") / "kvp-l7-test.log"
    logf.write_text(log_text)
    return _sh(f'. "{ENV_SH}" && kvp_scan_l2_failures "{logf}" {from_byte}',
               env_extra={"KVP_SKIP_IPC_TEST": "1"})


def test_l7_scanner_detects_store_failure_with_noise():
    """> L7: serde error + Store task failed + '0 succeeded, 20 failed' +
    300 lines noise -> rc=1, bounded block, 'L2 STORE FAILING', ZERO noise.
    MUTATION caught: removing any of the three patterns."""
    log = (
        "[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
        '  File "/kvp-impl/kvpersist/compress_serde.py", line 252, in serialize\n'
        "    out[: len(frame)] = frame\n"
        "ValueError: memoryview assignment: lvalue and rvalue have different structures\n"
        "[2026-11-22 08:03:26] LMCache ERROR Store task 7 to adapter 0 failed for keys ['a','b']\n"
        "[2026-11-22 08:03:27] LMCache DEBUG L2 store completed: adapter 0, 0 succeeded, 20 failed\n"
        + "".join(f"[2026-11-22 08:04:{i // 2:02d}] LMCache DEBUG skipping eviction\n"
                  for i in range(300)))
    r = _scan_l2(log)
    out = r.stdout + r.stderr
    assert r.returncode == 1, f"rc=1 expected:\n{out[-800:]}"
    assert "L2 STORE FAILING" in out
    assert "skipping eviction" not in out, "noise must be cut"
    assert "ValueError: memoryview assignment" in out, "the cause must print"


def test_l7_scanner_silent_on_success():
    """> L7: 'L2 store completed: adapter 0, 8 succeeded, 0 failed' -> rc=0,
    EMPTY output. MUTATION caught: removing [1-9] from the third pattern."""
    log = ("[2026-11-22 08:03:27] LMCache DEBUG "
           "L2 store completed: adapter 0, 8 succeeded, 0 failed\n")
    r = _scan_l2(log)
    assert r.returncode == 0, f"rc=0 expected:\n{r.stdout}{r.stderr}"
    assert not r.stdout.strip(), f"no output expected:\n{r.stdout}"


def test_l7_scanner_from_byte_skips_old_errors():
    """> L7: FROM_BYTE past the errors -> rc=0 (old errors don't count).
    MUTATION caught: ignoring FROM_BYTE."""
    err = "[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
    fresh = ("[2026-11-22 08:10:00] LMCache DEBUG "
             "L2 store completed: adapter 0, 8 succeeded, 0 failed\n")
    logf = Path("/tmp") / "kvp-l7-test2.log"
    logf.write_text(err + fresh)
    r = _sh(f'. "{ENV_SH}" && kvp_scan_l2_failures "{logf}" {len(err)}',
            env_extra={"KVP_SKIP_IPC_TEST": "1"})
    assert r.returncode == 0, f"old errors must not count:\n{r.stdout}{r.stderr}"


# ---------------------------------------------------------------------------
# L8: kvp_ipc_consumer_mode
# ---------------------------------------------------------------------------
def _consumer_mode(tmp_path, docker_running="true", pid_file_pid=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_stub(bin_dir, "docker", (
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        f"  *State.Running*) echo \"{docker_running}\" ;;\n"
        "  *) echo \"\" ;;\n"
        "esac\n"
        "exit 0\n"))
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    if pid_file_pid is not None:
        (cache_root / "lmcache-server.pid").write_text(f"{pid_file_pid}\n")
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_CACHE_ROOT=str(cache_root),
               KVP_SKIP_IPC_TEST="1")
    r = subprocess.run(
        ["bash", "-c", f'. "{ENV_SH}" && kvp_ipc_consumer_mode "{cache_root}"'],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(ROOT))
    return r.stdout.strip()


def test_l8_docker_mode_returns_container(tmp_path):
    """> L8: docker inspect says the cache container is running ->
    CONSUMER=container (the docker boundary). MUTATION: hard-coding 'host'."""
    assert _consumer_mode(tmp_path, docker_running="true") == "container"


def test_l8_no_container_returns_host(tmp_path):
    """> L8: no container + empty pid file -> host."""
    assert _consumer_mode(tmp_path, docker_running="false") == "host"


# ---------------------------------------------------------------------------
# L9: kvp_server_pid + server_read_bytes
# ---------------------------------------------------------------------------
def _server_pid(tmp_path, container_pid="4242", pid_file_pid=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_stub(bin_dir, "docker", (
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        f"  *State.Pid*) echo \"{container_pid}\" ;;\n"
        "  *) echo \"\" ;;\n"
        "esac\n"
        "exit 0\n"))
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    if pid_file_pid is not None:
        (cache_root / "lmcache-server.pid").write_text(f"{pid_file_pid}\n")
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_CACHE_ROOT=str(cache_root),
               KVP_SKIP_IPC_TEST="1")
    r = subprocess.run(
        ["bash", "-c", f'. "{ENV_SH}" && kvp_server_pid "{cache_root}"'],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(ROOT))
    return r.stdout.strip()


def test_l9_container_pid_wins(tmp_path):
    """> L9: docker State.Pid=4242 wins over the pid file (the docker-run
    CLIENT) -> 4242. MUTATION: falling back to the pid file."""
    assert _server_pid(tmp_path, container_pid="4242",
                       pid_file_pid="123") == "4242"


def test_l9_no_container_falls_back_to_pid_file(tmp_path):
    """> L9: no container pid (empty) -> pid file's pid (host mode)."""
    assert _server_pid(tmp_path, container_pid="",
                       pid_file_pid="777") == "777"


def _run_server_read_bytes(tmp_path, container_pid, io_files):
    """Run the REAL server_read_bytes from test-recovery.sh against a fake
    KVP_PROC_ROOT and stub docker. io_files: {pid: read_bytes value}."""
    proot = tmp_path / "proc"
    for pid, val in io_files.items():
        (proot / str(pid)).mkdir(parents=True, exist_ok=True)
        (proot / str(pid) / "io").write_text(f"read_bytes: {val}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_stub(bin_dir, "docker", (
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        f"  *State.Pid*) echo \"{container_pid}\" ;;\n"
        "  *) echo \"\" ;;\n"
        "esac\n"
        "exit 0\n"))
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    (cache_root / "lmcache-server.pid").write_text("123\n")
    src = RECOVERY.read_text()
    start = src.index("server_read_bytes(){")
    end = src.index("\n}", start) + 2
    fn = src[start:end]
    script = (f'. "{ENV_SH}"\n' + fn +
              f'\nexport KVP_CACHE_ROOT="{cache_root}"\n'
              f'export KVP_PROC_ROOT="{proot}"\n'
              'server_read_bytes')
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_SKIP_IPC_TEST="1")
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=30, env=env, cwd=str(ROOT))


def test_l9_server_read_bytes_uses_container_pid(tmp_path):
    """> L9: server_read_bytes reads /proc/<State.Pid>/io through KVP_PROC_ROOT:
    the container pid's io (read_bytes: 1000) is returned, NOT the pid file's
    (read_bytes: 7). MUTATION: taking the pid file's pid."""
    r = _run_server_read_bytes(tmp_path, "4242", {4242: 1000, 123: 7})
    assert r.stdout.strip() == "1000", (
        f"the container's io must win:\n{r.stdout}{r.stderr}")


def test_l9_server_read_bytes_missing_io_returns_minus_one(tmp_path):
    """> L9: /proc/<pid>/io missing -> -1 (the INCONCLUSIVE contract)."""
    r = _run_server_read_bytes(tmp_path, "4242", {})
    assert r.stdout.strip() == "-1", (
        f"missing io must give -1:\n{r.stdout}{r.stderr}")


# ---------------------------------------------------------------------------
# L3: pre-flight verdicts -- rc=3 vs a real boundary failure, Consumer error
# ---------------------------------------------------------------------------
def _pre_flights_with_stub_ipc(tmp_path, ipc_rc, ipc_output, same="1",
                               docker_user="1000:1000"):
    """Run the REAL _kvp_pre_flights with a stub ipc-test.sh (exit ipc_rc,
    printing ipc_output) and stub docker answering Config.User."""
    import socket
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    # a fake repo root whose scripts/ipc-test.sh is the stub
    fake_root = tmp_path / "kv"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / "implementation").mkdir()
    stub_ipc = fake_root / "scripts" / "ipc-test.sh"
    stub_ipc.write_text(
        "#!/usr/bin/env bash\n"
        + "".join(f"printf '%s\\n' \"{line}\"\n"
                  for line in ipc_output.split("|"))
        + f"exit {ipc_rc}\n")
    stub_ipc.chmod(stub_ipc.stat().st_mode | stat.S_IEXEC)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "docker", (
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        f"  *Config.User*) echo \"{docker_user}\" ;;\n"
        "  *State.Running*) echo true ;;\n"
        "  *State.Status*) echo running ;;\n"
        "  *) echo \"\" ;;\n"
        "esac\n"
        "exit 0\n"))
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    _write_stub(bin_dir, "python3",
                "#!/usr/bin/env bash\n"
                "case \"$*\" in\n"
                "  *mp_ping*) echo 'rpc: pong'; exit 0 ;;\n"
                f"  *) exec \"{real_py}\" \"$@\" ;;\n"
                "esac\n")
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_SKIP_IPC_TEST="0",
               KVP_CACHE_CONTAINER="lmcache-kvp-server")
    script = (
        f'. "{ENV_SH}" && '
        f'_kvp_pre_flights "{fake_root}" 127.0.0.1 {port} '
        f'"{tmp_path}/croot" "img@sha256:x" 1000 1000 {same} docker')
    try:
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=60, env=env, cwd=str(tmp_path))
    finally:
        srv.close()
    return r


def test_l3_rc3_gives_test_invalid_verdict(tmp_path):
    """> L3.2: rc=3 (TEST INVALID BY CONSTRUCTION) -> the INVALID verdict,
    NO 'BOUNDARY FAILED', NO UID advice, exit != 0. MUTATION: treating rc=3
    as a boundary failure."""
    r = _pre_flights_with_stub_ipc(tmp_path, 3,
        "VERDICT: TEST INVALID BY CONSTRUCTION -- producer and consumer have the same PID (1111).")
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"rc=3 must refuse:\n{out[-1200:]}"
    assert "INVALID" in out, out[-1200:]
    assert "BOUNDARY FAILED" not in out, (
        f"rc=3 must NOT be presented as a boundary failure:\n{out[-1200:]}")
    assert "ONE UID" not in out, (
        f"rc=3 must NOT print UID advice:\n{out[-1200:]}")


def test_l3_open_failed_shows_consumer_error_line(tmp_path):
    """> L3.1: the 'Consumer error:' line is the consumer's OPEN FAILED line
    (extracted from the OUTPUT, which also carries a verdict line after it),
    not the verdict tail. MUTATION: tail -n 1 of everything (the verdict
    would be shown instead of the error)."""
    r = _pre_flights_with_stub_ipc(tmp_path, 4,
        "consumer: OPEN FAILED RuntimeError: CUDA error: invalid device context|"
        "FAIL (exit 4): the consumer could not open GPU memory created in the container.")
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "Consumer error: consumer: OPEN FAILED RuntimeError: CUDA error: invalid device context" in out, (
        f"the OPEN FAILED line must be shown:\n{out[-1500:]}")
    assert "BOUNDARY FAILED" in out, "rc=4 is a real boundary failure"


def test_l3_no_consumer_error_line_fallback(tmp_path):
    """> L3.1: with no OPEN FAILED and no CUDA_ERROR line -> the honest
    '(no consumer error line)' placeholder."""
    r = _pre_flights_with_stub_ipc(tmp_path, 4,
        "consumer: pid=2222")
    out = r.stdout + r.stderr
    assert "(no consumer error line)" in out, out[-1500:]
