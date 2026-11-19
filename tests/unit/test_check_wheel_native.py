# SPDX-License-Identifier: Apache-2.0
"""r2 P0-2 / r10 P0-2: the custom LMCache wheel must carry the native
extensions -- cuda_ops*.so AND lmcache_native*.so (without lmcache_native
LMCache drops to CLI-only mode). Optional redis/fs extensions are recorded
in `native_extensions` but never required. This module is the single source
of truth for "does the wheel carry the binaries?" -- called after `pip wheel`
by scripts/_incontainer_build.sh and by unit tests."""
from __future__ import annotations
import json
import pathlib
import subprocess
import sys
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
sys.path.insert(0, str(IMPL))
from kvpersist import check_wheel_native as cwn  # noqa: E402

PY = sys.executable


def _make_wheel(tmp_path: pathlib.Path, members: list[str]) -> pathlib.Path:
    whl = tmp_path / "lmcache-0.5.8rc1.post48-cp312-cp312-linux_x86_64.whl"
    with zipfile.ZipFile(whl, "w") as z:
        for m in members:
            z.writestr(m, b"\x00")
    return whl


def test_wheel_with_both_required_members_passes(tmp_path):
    whl = _make_wheel(tmp_path, [
        "lmcache/__init__.py",
        "lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so",
        "lmcache/lmcache_native.cpython-312-x86_64-linux-gnu.so",
    ])
    missing, optional, found = cwn.inspect_wheel(str(whl))
    assert missing == [], f"nothing must be missing: {missing}"
    assert set(found) == {"lmcache/cuda_ops", "lmcache/lmcache_native"}


def test_wheel_without_lmcache_native_is_detected(tmp_path):
    """r10 P0-2: a wheel with only cuda_ops must FAIL -- LMCache would drop to
    CLI-only mode (red before r10: only cuda_ops was required)."""
    whl = _make_wheel(tmp_path, [
        "lmcache/__init__.py",
        "lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so",
    ])
    missing, optional, found = cwn.inspect_wheel(str(whl))
    assert "lmcache/lmcache_native" in missing


def test_wheel_without_cuda_ops_is_detected(tmp_path):
    whl = _make_wheel(tmp_path, [
        "lmcache/lmcache_native.cpython-312-x86_64-linux-gnu.so",
    ])
    missing, optional, found = cwn.inspect_wheel(str(whl))
    assert "lmcache/cuda_ops" in missing


def test_cli_exit0_on_full_wheel_and_lists_optional(tmp_path):
    """CLI on a complete wheel exits 0 and prints the native_extensions JSON
    line including the OPTIONAL redis/fs extensions when present."""
    whl = _make_wheel(tmp_path, [
        "lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so",
        "lmcache/lmcache_native.cpython-312-x86_64-linux-gnu.so",
        "lmcache/lmcache_redis.cpython-312-x86_64-linux-gnu.so",
    ])
    r = subprocess.run([PY, "-m", "kvpersist.check_wheel_native", str(whl)],
                       capture_output=True, text=True, timeout=30,
                       env={"PYTHONPATH": str(IMPL), "PATH": "/usr/bin:/bin"},
                       cwd=str(tmp_path))
    assert r.returncode == 0, f"complete wheel must pass:\n{r.stderr}"
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("native_extensions="))
    ext = json.loads(line.split("=", 1)[1])
    assert "lmcache/lmcache_redis" in ext, f"optional redis must be listed: {ext}"
    assert "lmcache/cuda_ops" in ext and "lmcache/lmcache_native" in ext


def test_cli_exit1_when_missing_native(tmp_path):
    whl = _make_wheel(tmp_path, ["lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so"])
    r = subprocess.run([PY, "-m", "kvpersist.check_wheel_native", str(whl)],
                       capture_output=True, text=True, timeout=30,
                       env={"PYTHONPATH": str(IMPL), "PATH": "/usr/bin:/bin"},
                       cwd=str(tmp_path))
    assert r.returncode == 1
    assert "lmcache_native" in r.stderr, f"the refusal must name the miss:\n{r.stderr}"