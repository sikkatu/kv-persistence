# SPDX-License-Identifier: Apache-2.0
"""r24 E1 guard tests: the repo tree protection must actually FAIL a test
that writes to the real live/ outside the contract allowlist, and to the
guarded observability/extras generated files.

These tests are the acceptance probes for E1 (the r23 restore MASKED such
writes -- the reviewer's experiment: a test writing live/image went green).
"""
from __future__ import annotations
import pathlib
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
    (root / "live").mkdir(exist_ok=True)
    (root / "live" / "obs_started").write_text("1\\n")
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