# SPDX-License-Identifier: Apache-2.0
"""r24 E1 guard tests: the repo tree protection must actually FAIL a test
that writes to the real live/ outside the contract allowlist, and to the
guarded observability/extras generated files.

These tests are the acceptance probes for E1 (the r23 restore MASKED such
writes -- the reviewer's experiment: a test writing live/image went green).
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

PROBE_IMAGE = '''
def test_probe_writes_real_live_image():
    import pathlib
    root = pathlib.Path.cwd()
    (root / "live").mkdir(exist_ok=True)
    (root / "live" / "image").write_text("sha256:" + "f" * 64)
    assert True
'''

PROBE_OBS = '''
def test_probe_writes_real_obs_target():
    import pathlib
    root = pathlib.Path.cwd()
    d = root / "observability" / "extras" / "targets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "gpu.json").write_text("[]")
    assert True
'''

PROBE_ALLOWLIST = '''
def test_probe_writes_contract_record():
    import pathlib
    root = pathlib.Path.cwd()
    (root / "live").mkdir(parents=True, exist_ok=True)
    (root / "live" / "obs_started").write_text("1\\n")
    assert True
'''

# r25 G5: probe that touches only the mtime of a LARGE file (content change
# of big files is re-based without a cascade -- mtime changes are restored
# and fail exactly one test).
PROBE_BIG_MTIME = '''
import os
import pathlib

def test_x_touch_big():
    root = pathlib.Path.cwd()
    big = root / "live" / "lmcache-build" / "wheel.whl"
    big.parent.mkdir(parents=True, exist_ok=True)
    if not big.is_file():
        big.write_bytes(b"w" * (200 * 1024))
    os.utime(big, (0, 0))
    assert True

def test_y_after():
    assert True

def test_z_after2():
    assert True
'''
# live/image. Exactly that one must be red, the rest green (the r24 restore
# cascaded: every later test errored too and the file kept the fake digest).
PROBE_CASCADE = '''
import pathlib

def test_a_before():
    assert True

def test_b_victim():
    root = pathlib.Path.cwd()
    (root / "live").mkdir(exist_ok=True)
    (root / "live" / "image").write_text("sha256:" + "e" * 64)
    assert True

def test_c_after1():
    assert True

def test_d_after2():
    assert True

def test_e_after3():
    assert True

def test_f_after4():
    assert True
'''


def _run_probe(tmp_path: pathlib.Path, name: str, body: str):
    """Write a probe test file into tests/unit/ and run ONLY it; remove it
    afterwards. The probe SUBPROCESS deliberately writes to the real tree
    and (correctly) fails there -- but THIS test must leave the tree clean,
    so every path the probe touches is saved and restored around the run.
    Returns (rc, out)."""
    probe = ROOT / "tests" / "unit" / name
    touched = (
        ROOT / "live" / "image",
        ROOT / "live" / "obs_started",
        ROOT / "observability" / "extras" / "targets" / "gpu.json",
    )
    saved = {}
    for p in touched:
        if p.is_file():
            st = p.stat()
            saved[p] = (p.read_bytes(), st.st_mtime_ns)
        else:
            saved[p] = None
    probe.write_text(body)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", f"tests/unit/{name}", "-q",
             "-p", "no:cacheprovider", "--no-header"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr
    finally:
        probe.unlink(missing_ok=True)
        import os
        for p in touched:
            entry = saved[p]
            if entry is None:
                p.unlink(missing_ok=True)
            else:
                content, mtime_ns = entry
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
                os.utime(p, ns=(mtime_ns, mtime_ns))


def test_e1_writing_real_live_image_is_red(tmp_path):
    """> E1 acceptance: a test writing live/image to the REAL tree must fail
    (the r23 restore masked it -- that was the defect)."""
    rc, out = _run_probe(tmp_path, "test_zz_probe_image.py", PROBE_IMAGE)
    assert rc != 0, (
        f"writing live/image outside the allowlist must fail the run:\n"
        f"{out[-600:]}")


def test_e1_writing_real_obs_target_is_red(tmp_path):
    """> E1 acceptance: a test writing observability/extras/targets/gpu.json
    to the REAL tree must fail."""
    rc, out = _run_probe(tmp_path, "test_zz_probe_obs.py", PROBE_OBS)
    assert rc != 0, (
        f"writing observability/extras/targets/gpu.json must fail the run:\n"
        f"{out[-600:]}")


def test_e1_contract_record_is_tolerated(tmp_path):
    """> E1: the allowlist contract records (live/obs_started etc.) are still
    tolerated -- the launchers write them, restore handles them."""
    rc, out = _run_probe(tmp_path, "test_zz_probe_ok.py", PROBE_ALLOWLIST)
    assert rc == 0, (
        f"a contract-record write must NOT fail the run:\n{out[-600:]}")


def _assert_no_cascade(out: str, victim: str, total: int):
    """r25 G5 acceptance: EXACTLY the victim is red and every other test is
    green. pytest reports a teardown-guard violation two equivalent ways,
    both accepted here:
      * the fixture raised in teardown -> `total passed, 1 error` (the
        victim passed its call phase, then errored in teardown), or
      * the report hook downgraded it -> `total-1 passed, 1 failed`.
    The summary line must name the victim and only the victim."""
    ok_green = (f"{total} passed" in out) or (f"{total - 1} passed" in out)
    assert ok_green, (
        f"the other tests must stay green (no cascade):\n{out[-800:]}")
    red = [l for l in out.splitlines()
           if (l.startswith("ERROR tests/") or l.startswith("FAILED tests/"))
           and victim in l]
    assert len(red) == 1, (
        f"exactly the victim ({victim}) must be red:\n{red}\n{out[-800:]}")


def test_g5_no_cascade_and_image_restored(tmp_path):
    """> G5: exactly ONE red test (the one that overwrote live/image); the
    tests after it are green again and live/image is back to its original
    bytes (the r24 restore cascaded: every later test errored too)."""
    image = ROOT / "live" / "image"
    before = image.read_bytes() if image.is_file() else None
    rc, out = _run_probe(tmp_path, "test_zz_probe_cascade.py", PROBE_CASCADE)
    assert rc != 0, f"the victim test must be red:\n{out[-600:]}"
    _assert_no_cascade(out, "test_b_victim", 6)
    after = image.read_bytes() if image.is_file() else None
    assert after == before, (
        "live/image must be restored to its original content after the "
        f"violation (before={len(before or b'')}B, after={len(after or b'')}B)")


def test_g5_big_wheel_mtime_restored(tmp_path):
    """> G5 acceptance: touching only the mtime of a LARGE wheel (>64 KiB)
    fails EXACTLY one test (r25 G5: ADDED/mtime changes fail the victim and
    restore/re-base -- no cascade), and for a known wheel the mtime is
    RESTORED."""
    big = ROOT / "live" / "lmcache-build" / "wheel.whl"
    saved = big.read_bytes() if big.is_file() else None
    saved_m = big.stat().st_mtime_ns if big.is_file() else None
    big.parent.mkdir(parents=True, exist_ok=True)
    if not big.is_file():
        big.write_bytes(b"w" * (200 * 1024))
    before_m = big.stat().st_mtime_ns
    try:
        # first probe run: the wheel is a NEW (ADDED) non-allowlist file ->
        # exactly one red, the others green (the guard re-bases it, no cascade)
        rc, out = _run_probe(tmp_path, "test_zz_probe_big1.py", PROBE_BIG_MTIME)
        assert rc != 0, f"the ADDED-wheel victim must be red:\n{out[-600:]}"
        _assert_no_cascade(out, "test_x_touch_big", 3)
        # second probe run: the wheel is now KNOWN (re-based baseline) and the
        # probe touches its mtime -> exactly one red again and the mtime is
        # RESTORED by the guard.
        m2 = big.stat().st_mtime_ns
        rc, out = _run_probe(tmp_path, "test_zz_probe_big2.py", PROBE_BIG_MTIME)
        assert rc != 0, f"the mtime victim must be red:\n{out[-600:]}"
        _assert_no_cascade(out, "test_x_touch_big", 3)
        assert big.stat().st_mtime_ns == m2, (
            "the wheel's mtime must be restored after the violation")
    finally:
        if saved is None:
            big.unlink(missing_ok=True)
        else:
            big.write_bytes(saved)
            os.utime(big, ns=(saved_m, saved_m))


def test_g6_5000_files_stays_under_30ms(tmp_path):
    """> G6 acceptance: with live/ carrying 5000 files the per-test guard cost
    stays <= 0.03s (the deep lmcache-build/** entries are checked once per
    session, not per test)."""
    import time as _t
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "tests"))
    import conftest
    tree = tmp_path / "live"
    src = tree / "lmcache-build" / "src"
    src.mkdir(parents=True, exist_ok=True)
    for i in range(5000):
        (src / f"f{i}.txt").write_text("x" * 32)
    (tree / "image").write_text("sha256:" + "c" * 64)
    (tree / "model_dir").write_text("/some/model\n")
    sess_live, sess_snap, sess_deep = (
        conftest.REPO_LIVE, conftest._SNAP, conftest._SNAP_DEEP)
    try:
        conftest.REPO_LIVE = tree
        conftest._SNAP, conftest._SNAP_DEEP = conftest._snap_live()
        # warm-up
        conftest._scan_live()
        conftest._restore_remaining()
        t0 = _t.perf_counter()
        for _ in range(10):
            conftest._scan_live()
            conftest._restore_remaining()
        per = (_t.perf_counter() - t0) / 10
        assert per <= 0.03, (
            f"per-test guard cost must stay <= 0.03s with 5000 files under "
            f"lmcache-build/src, measured {per:.4f}s")
    finally:
        conftest.REPO_LIVE = sess_live
        conftest._SNAP, conftest._SNAP_DEEP = sess_snap, sess_deep


# ---------------------------------------------------------------------------
# G7: E4.1 hardening -- the bad TERM-timeout must never launch the server
# ---------------------------------------------------------------------------
def test_g7_bad_term_timeout_never_launches(tmp_path):
    """> G7 acceptance: for every invalid KVP_SMOKE_TERM_TIMEOUT the smoke
    refuses BEFORE the kvpersist.launch stub runs and never claims
    "did NOT exit" (mutation E4-1 -- drop the exit 1 -- must go RED here).
    All paths stay inside tmp_path (the r24 test littered /tmp)."""
    launches = tmp_path / "launches"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *kvpersist.launch*) echo x >> "{launches}"; sleep 360 & wait $! 2>/dev/null; exit 0 ;;\n'
        "  *kvpersist.mp_ping*) exit 0 ;;\n"
        "  *check_codec_available*) exit 0 ;;\n"
        f'  *) exec "{real_py}" "$@" ;;\n'
        "esac\n")
    (bin_dir / "python3").chmod(0o755)
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    for bad in ("abc", "", "0", "-3", "1.5"):
        env = dict(os.environ,
                   PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                   KVP_SMOKE_REPO=str(ROOT),
                   KVP_SMOKE_TERM_TIMEOUT=bad)
        r = subprocess.run(
            ["bash", str(ROOT / "scripts" / "_smoke_cache_server.sh"),
             "zstd", str(cache_root), "0"],
            capture_output=True, text=True, timeout=30, env=env,
            cwd=str(tmp_path))
        out = r.stdout + r.stderr
        assert r.returncode != 0, (
            f"KVP_SMOKE_TERM_TIMEOUT={bad!r} must be refused:\n{out[-400:]}")
        assert "KVP_SMOKE_TERM_TIMEOUT" in out, (
            f"the error must name the variable:\n{out[-400:]}")
        assert not launches.exists(), (
            f"the kvpersist.launch stub must NEVER run on a bad timeout "
            f"({bad!r}):\n{out[-400:]}")
        assert "did NOT exit" not in out, (
            f"the smoke must not claim 'did NOT exit' for {bad!r}:\n{out[-400:]}")