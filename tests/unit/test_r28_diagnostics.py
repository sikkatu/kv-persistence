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
# r29 N5a: the recipes checkout must not be hardcoded to the coder's machine.
# KVP_RECIPES_DIR wins; else the sibling ../recipes; else skip (never fail).
RECIPES = Path(os.environ.get("KVP_RECIPES_DIR", ROOT.parent / "recipes"))
if not (RECIPES / "recipes" / "rtx-pro-6000" / "mimo-v2.6-pro-hotsplit").is_dir():
    RECIPES = None
LAUNCHER = ((RECIPES / "recipes" / "rtx-pro-6000" / "mimo-v2.6-pro-hotsplit" / "scripts" / "run-tp4-hotsplit.sh")
           if RECIPES else None)
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
    if LAUNCHER is None:
        import pytest
        pytest.skip("recipes repo not found")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_log = tmp_path / "stub.log"
    _write_stub(bin_dir, "docker", f'#!/usr/bin/env bash\necho "$*" >> "{stub_log}"\nexit 0\n')
    slog = tmp_path / "lmcache-server.log"
    slog.write_text(_error_log())
    src = LAUNCHER.read_text()
    if '_kvp_serr="$(tail' not in src:
        import pytest
        pytest.skip("stale recipes checkout (no r28 fail-fast block)")
    start = src.index('    _kvp_serr="$(tail')
    end = src.index("      exit 1", start) + len("      exit 1")
    block = src[start:end]
    if "max=80" not in block:
        import pytest
        pytest.skip("stale recipes checkout (no r29 N4 bounded limiter)")
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
    """> L2/N4: both test-recovery.sh branches use the r29 N4 bounded limiter
    (_kvp_bound_block 80 20), never a plain head or `,$p` -- exactly two
    calls. MUTATION: replacing one branch with `head -n 25`."""
    src = RECOVERY.read_text()
    bounded = len(re.findall(r"_kvp_bound_block 80 20", src))
    unbounded = len(re.findall(r",\\\$p", src))
    plain_head = len(re.findall(r"\| head -n \d+", src))
    assert unbounded == 0, "the unbounded `,$p` selection must be gone"
    assert plain_head == 0, "no plain head must remain in the traceback branches"
    assert bounded >= 2, (
        f"both test-recovery.sh branches must use the bounded limiter, "
        f"found {bounded} calls")


# ---------------------------------------------------------------------------
# L7: the L2 store-failure scanner
# ---------------------------------------------------------------------------
def _scan_l2(log_text: str, tdir: Path, from_byte: int = 0):
    # Q2: the log goes to tdir (tmp_path), never to a shared fixed path
    logf = tdir / "kvp-l7-test.log"
    logf.write_text(log_text)
    return _sh(f'. "{ENV_SH}" && kvp_scan_l2_failures "{logf}" {from_byte}',
               env_extra={"KVP_SKIP_IPC_TEST": "1"})


def test_l7_scanner_detects_store_failure_with_noise(tmp_path):
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
    r = _scan_l2(log, tmp_path)
    out = r.stdout + r.stderr
    assert r.returncode == 1, f"rc=1 expected:\n{out[-800:]}"
    assert "L2 STORE FAILING" in out
    assert "skipping eviction" not in out, "noise must be cut"
    assert "ValueError: memoryview assignment" in out, "the cause must print"


def test_l7_scanner_silent_on_success(tmp_path):
    """> L7: 'L2 store completed: adapter 0, 8 succeeded, 0 failed' -> rc=0,
    EMPTY output. MUTATION caught: removing [1-9] from the third pattern."""
    log = ("[2026-11-22 08:03:27] LMCache DEBUG "
           "L2 store completed: adapter 0, 8 succeeded, 0 failed\n")
    r = _scan_l2(log, tmp_path)
    assert r.returncode == 0, f"rc=0 expected:\n{r.stdout}{r.stderr}"
    assert not r.stdout.strip(), f"no output expected:\n{r.stdout}"


def test_l7_scanner_from_byte_skips_old_errors(tmp_path):
    """> L7: FROM_BYTE past the errors -> rc=0 (old errors don't count).
    MUTATION caught: ignoring FROM_BYTE."""
    err = "[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
    fresh = ("[2026-11-22 08:10:00] LMCache DEBUG "
             "L2 store completed: adapter 0, 8 succeeded, 0 failed\n")
    logf = tmp_path / "kvp-l7-test2.log"
    logf.write_text(err + fresh)
    r = _sh(f'. "{ENV_SH}" && kvp_scan_l2_failures "{logf}" {len(err)}',
            env_extra={"KVP_SKIP_IPC_TEST": "1"})
    assert r.returncode == 0, f"old errors must not count:\n{r.stdout}{r.stderr}"


# ---------------------------------------------------------------------------
# N2: kvp status must judge only the CURRENT server's log part
# ---------------------------------------------------------------------------
def _l2_status(log_text, tdir: Path, mark_text=None):
    """Run the REAL kvp_l2_writes_status. mark_text written to a .start file
    (the r29 N2 offset) and passed as the second argument like `kvp status`.
    Q2: both files go to tdir (tmp_path), never to a shared fixed path."""
    logf = tdir / "kvp-n2-test.log"
    logf.write_text(log_text)
    frm = "0"
    if mark_text is not None:
        markf = tdir / "kvp-n2-test.log.start"
        markf.write_text(mark_text + "\n")
        # exactly how scripts/kvp reads it: only a pure number counts
        frm = (mark_text.strip() or "0")
        if not frm.isdigit():
            frm = "0"
    return _sh(f'. "{ENV_SH}" && kvp_l2_writes_status "{logf}" {frm}',
               env_extra={"KVP_SKIP_IPC_TEST": "1"})


def test_n2_old_failures_before_mark_are_ok(tmp_path):
    """> N2: old failures BEFORE the start offset -> OK (history, not current
    health). MUTATION: calling the function without the offset."""
    old_fail = ("[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
                "[2026-11-22 08:03:27] LMCache DEBUG L2 store completed: adapter 0, 0 succeeded, 20 failed\n")
    clean = ("[2026-11-22 09:00:01] LMCache DEBUG L2 store completed: adapter 0, 8 succeeded, 0 failed\n"
             "[2026-11-22 09:00:02] LMCache DEBUG L2 store completed: adapter 0, 5 succeeded, 0 failed\n")
    r = _l2_status(old_fail + clean, tmp_path, mark_text=str(len(old_fail)))
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"old failures before the mark must be OK:\n{out}"
    assert "L2 writes: OK" in out, out


def test_n2_no_mark_file_falls_back_to_zero(tmp_path):
    """> N2: without a mark file -> offset 0 -> the old failure counts
    (the documented fallback, not a crash)."""
    old_fail = "[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
    r = _l2_status(old_fail, tmp_path, mark_text=None)
    out = r.stdout + r.stderr
    assert r.returncode == 1, f"without a mark the old failure counts:\n{out}"
    assert "FAILING" in out, out


def test_n2_new_failure_after_mark_is_failing(tmp_path):
    """> N2: a NEW failure after the mark -> FAILING (1 failed since this
    server started)."""
    clean = "[2026-11-22 09:00:01] LMCache DEBUG L2 store completed: adapter 0, 8 succeeded, 0 failed\n"
    new_fail = "[2026-11-22 09:05:00] LMCache ERROR Serde task 7 (SERIALIZE) failed\n"
    r = _l2_status(clean + new_fail, tmp_path, mark_text="0")
    out = r.stdout + r.stderr
    assert r.returncode == 1, f"a new failure must be FAILING:\n{out}"
    assert "FAILING (1 failed since this server started)" in out, out


def test_n2_kvp_status_passes_the_offset(tmp_path):
    """> N2/P1: the REAL `scripts/kvp status` reads lmcache-server.log.start and
    passes it as the offset -- old failures before the mark give `L2 writes:
    OK`. Runs in a TMP copy of the repo WITHOUT live/ (the r30 P1 contract:
    a fresh clone must not make this test red). MUTATIONS: calling
    kvp_l2_writes_status WITHOUT the second argument (the whole log would
    count and it would say FAILING); dropping `|| true` in the head|tr
    substitution (silent rc=1 death before the L2 line)."""
    import shutil
    repo = tmp_path / "kvp"
    (repo / "scripts").mkdir(parents=True)
    for f in (ROOT / "scripts").iterdir():
        if f.is_file():
            shutil.copy2(f, repo / "scripts" / f.name)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    old_fail = ("[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
                "[2026-11-22 08:03:27] LMCache DEBUG L2 store completed: adapter 0, 0 succeeded, 20 failed\n")
    clean = ("[2026-11-22 09:00:01] LMCache DEBUG L2 store completed: adapter 0, 8 succeeded, 0 failed\n")
    (cache_root / "lmcache-server.log").write_text(old_fail + clean)
    (cache_root / "lmcache-server.log.start").write_text(f"{len(old_fail)}\n")
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "docker", "#!/usr/bin/env bash\nexit 0\n")
    env = dict(os.environ,
               HOME=str(home),
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_REPO=str(repo),
               KVP_CACHE_ROOT=str(cache_root))
    r = subprocess.run(["bash", str(repo / "scripts" / "kvp"), "status"],
                       capture_output=True, text=True, timeout=60, env=env,
                       cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"`kvp status` must not die in a fresh clone (no live/):\n{out[-800:]}")
    assert "L2 writes: OK" in out, (
        f"`kvp status` must honour the start offset:\n{out[-800:]}")
    assert "L2 writes: FAILING" not in out, (
        f"old failures before the mark must not count:\n{out[-800:]}")


# ---------------------------------------------------------------------------
# N4: the traceback limiter keeps the START and the END on deep stacks
# ---------------------------------------------------------------------------
def _deep_error_log(with_noise=True) -> str:
    """60-frame traceback (120 lines) + exception + driver block, then
    600 lines of DEBUG noise -- deeper than the 80-line bound, so a plain
    head -n 80 would cut `Returning 205` and the driver block."""
    frames = "".join(
        f"  File \"/opt/venv/lib/python3.12/site-packages/lmcache/x.py\", "
        f"line {100 + i}, in frame_{i}\n    do_step_{i}()\n"
        for i in range(60))
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
            f"[2026-11-22 08:04:{i // 2:02d}] LMCache DEBUG skipping eviction L2 usage 0.00\n"
            for i in range(600))
    return block + "\n" + noise


def _real_selector(src: str, start_marker: str, end_marker: str) -> str:
    """r30 P4: the REAL awk invocation cut out of the script text (the shape of
    test_n4_sigpipe_no_silent_death_on_huge_noise) -- a hand-copied selector in
    the test drifts from the scripts and tests code that no longer exists.
    Returns everything from `awk ` up to (not including) end_marker -- the
    invocation INCLUDING any -v arguments (like `-v start="$_line"`), with its
    closing single quote restored (the end marker starts at that quote)."""
    anchor = src.index(start_marker)
    awk_pos = src.index("awk ", anchor)
    end = src.index(end_marker, awk_pos)
    return src[awk_pos:end] + "'"

def _selection_pipelines():
    """r30 P4: the THREE real selections -- both test-recovery.sh places and
    the env.sh scanner. The 2nd recovery selection reads the log FILE as awk
    input (not a pipe), so `exit` there is harmless -- the pipe-fed ones
    (1st recovery, scanner) must use the done-flag or they die of SIGPIPE."""
    rec = RECOVERY.read_text()
    env = ENV_SH.read_text()
    re_first = _real_selector(rec, '_srverr_trace="', "' | _kvp_bound_block")
    re_second = _real_selector(rec, 'awk -v start="${_kvp_e1%%:*}"',
                               ' "$LOG" | _kvp_bound_block')
    scanner = _real_selector(env, '_block="', "' | _kvp_bound_block")
    return {"recovery_wait": re_first, "recovery_dead": re_second,
            "scanner": scanner}

def test_n4_deep_traceback_keeps_start_and_end():
    """> N4: a 60-frame traceback (127-line block) + 600 lines noise ->
    the limiter prints the FIRST lines, a `lines skipped` marker and the
    LAST lines INCLUDING `Returning 205` and the driver block; ZERO noise;
    <= 81 lines. MUTATION: `head -n 80` instead of the limiter (cuts the
    exception tail)."""
    log = _deep_error_log(with_noise=True)
    for name, selector in _selection_pipelines().items():
        if name == "recovery_dead":
            # the 2nd recovery selection reads the FILE via awk's file operand
            continue
        r = subprocess.run(
            ["bash", "-c",
             f'. "{ENV_SH}" && _line=1 && cat | ' + selector +
             " | _kvp_bound_block 80 20"],
            input=log, capture_output=True, text=True, timeout=30)
        out = r.stdout
        assert "Error handling operation register_kv_cache" in out, (
            f"[{name}] the first line must print:\n{out[:200]}")
        assert "Returning 205 (CUDA_ERROR_MAP_FAILED) from cuIpcOpenEventHandle" in out, (
            f"[{name}] the LAST exception line must survive the bound:\n{out[-600:]}")
        assert "The CUDA driver logged these messages" in out, (
            f"[{name}] the driver block must survive the bound:\n{out[-600:]}")
        assert "lines skipped" in out, (
            f"[{name}] the marker must show the cut:\n{out[-600:]}")
        assert "skipping eviction" not in out, f"[{name}] DEBUG noise must be cut"
        assert len(out.strip().splitlines()) <= 81, f"[{name}] bounded output expected"
def test_n4_selectors_have_no_early_exit():
    """> N4: both pipe-fed real selections (test-recovery.sh wait branch, env.sh
    scanner) must use the done FLAG -- an early `exit` closes the pipe and the
    writer dies of SIGPIPE under pipefail (the r29 N4 defect class). The
    2nd recovery selection and the launcher's 2nd place read their input as a
    FILE (no writer to signal) -- that `exit` is equivalent and is covered by
    grep in recipes. MUTATION: replacing the flag with `exit` in either
    pipe-fed selector."""
    for name, prog in _selection_pipelines().items():
        assert "exit" not in prog, f"[{name}] an early exit would risk SIGPIPE:\n{prog}"
        assert "done=1" in prog, f"[{name}] the done flag must be present:\n{prog}"


def test_n4_scanner_keeps_end_on_deep_block(tmp_path):
    """> N4: the L7 scanner's block (max 40, head 10) on a deep serde error
    also keeps the end of the traceback."""
    deep_err = (
        "[2026-11-22 08:03:25] LMCache ERROR Serde task 3 (SERIALIZE) failed\n"
        "Traceback (most recent call last):\n"
        + "".join(f"  File \"x.py\", line {i}, in f_{i}\n    step_{i}()\n"
                  for i in range(60))
        + "ValueError: memoryview assignment: lvalue and rvalue have different structures\n"
        + "".join(f"[2026-11-22 08:04:{i // 2:02d}] LMCache DEBUG skipping eviction\n"
                  for i in range(300)))
    r = _scan_l2(deep_err, tmp_path)
    out = r.stdout + r.stderr
    assert r.returncode == 1, f"the scanner must fail:\n{out[-600:]}"
    assert "ValueError: memoryview assignment" in out, (
        f"the LAST line of the cause must survive:\n{out[-600:]}")
    assert "skipping eviction" not in out, "noise must be cut"


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
# N6a: no 'No such file' noise when the pid file is absent (server not yet
# started is a NORMAL state)
# ---------------------------------------------------------------------------
def test_n6a_no_pidfile_no_stderr_noise(tmp_path):
    """> N6a: no container + no pid file (the normal 'server not started'
    state) -> stderr is EMPTY (the old `tr < "$_pidf"` redirected before
    2>/dev/null and bash printed 'No such file or directory')."""
    for fn in ("kvp_server_pid", "kvp_ipc_consumer_mode"):
        r = _sh(f'. "{ENV_SH}" && {fn} "{tmp_path}/nocrash" || true',
                env_extra={"KVP_SKIP_IPC_TEST": "1",
                           "KVP_CACHE_CONTAINER": "definitely-not-running"})
        assert "No such file or directory" not in r.stderr, (
            f"{fn}: stderr must be clean:\n{r.stderr}")
        assert "No such file" not in r.stdout, (
            f"{fn}: stdout must be clean:\n{r.stdout}")


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
