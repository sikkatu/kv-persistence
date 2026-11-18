# SPDX-License-Identifier: Apache-2.0
"""r2 P0-2: a wheel without the compiled cuda_ops extension must be refused.

The upstream build silently produces a pure-Python wheel when nvcc is absent
("building without extensions"), and that wheel silently falls back to the
slow Python copy path at runtime -- the exact defect behind cudaMemcpy
error 1. The build script must FAIL on such a wheel; these tests pin the
checker behaviour (wheel with .so -> 0, without -> 1, unreadable -> 2) and the
script's wiring.
"""

from __future__ import annotations

# Standard
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
PY = sys.executable

sys.path.insert(0, str(IMPL))


def _make_wheel(tmp_path: Path, members: dict[str, bytes]) -> Path:
    whl = tmp_path / "lmcache-0.5.7.dev428-cp312-cp312-linux_x86_64.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return whl


def test_wheel_with_native_member_passes(tmp_path):
    whl = _make_wheel(
        tmp_path,
        {
            "lmcache/__init__.py": b"",
            "lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so": b"\x7fELF",
        },
    )
    from kvpersist.check_wheel_native import find_native_member

    assert find_native_member(str(whl)) == "lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so"


def test_wheel_without_native_member_is_detected(tmp_path):
    whl = _make_wheel(tmp_path, {"lmcache/__init__.py": b"", "lmcache/cuda_ops.py": b"# fallback"})
    from kvpersist.check_wheel_native import find_native_member

    assert find_native_member(str(whl)) is None, (
        "a pure-Python cuda_ops.py must NOT count as the native extension"
    )


def test_cli_exit1_on_python_only_wheel(tmp_path):
    whl = _make_wheel(tmp_path, {"lmcache/__init__.py": b""})
    r = subprocess.run(
        [PY, "-m", "kvpersist.check_wheel_native", str(whl)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(IMPL), "PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NATIVE CHECK FAILED" in r.stderr


def test_cli_exit0_on_native_wheel(tmp_path):
    whl = _make_wheel(
        tmp_path, {"lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so": b"\x7fELF"}
    )
    r = subprocess.run(
        [PY, "-m", "kvpersist.check_wheel_native", str(whl)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(IMPL), "PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    assert "native extension present" in r.stdout


def test_cli_exit2_on_unreadable_wheel(tmp_path):
    bogus = tmp_path / "not-a-wheel.whl"
    bogus.write_bytes(b"this is not a zip")
    r = subprocess.run(
        [PY, "-m", "kvpersist.check_wheel_native", str(bogus)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(IMPL), "PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    assert r.returncode == 2
    assert "cannot read wheel" in r.stderr


# ---- build-script wiring ----------------------------------------------------------
def test_build_script_requires_nvcc_and_checks_wheel():
    src = (ROOT / "scripts" / "build-lmcache.sh").read_text()
    # r6-7 A-1: the compile phase lives in scripts/_incontainer_build.sh
    inc = (ROOT / "scripts" / "_incontainer_build.sh").read_text()
    src = src + inc
    assert "nvcc" in inc, "the in-container phase must preflight the nvcc compiler"
    assert "BUILD_WITH_CUDA" in src, "the CUDA extension build must be forced on"
    assert "TORCH_CUDA_ARCH_LIST" in src, (
        "the arch list must include 12.0 (RTX PRO 6000 Blackwell / SM 120)"
    )
    assert "check_wheel_native" in src, (
        "the built wheel must be checked for lmcache/cuda_ops*.so before use"
    )