# SPDX-License-Identifier: Apache-2.0
"""r6-7 B-4: pre-flight checks in build-lmcache.sh -- every refusal carries
ONE clear 'what to do' line (docker missing, daemon unreachable, disk)."""
from __future__ import annotations
import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
PIN_FILE = ROOT / "vendor" / "pinned-base-image.env"


def _run(tmp_path: pathlib.Path, docker_mode: str):
    """docker_mode: ok | no-docker | daemon-down."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if docker_mode != "no-docker":
        info_rc = "1" if docker_mode == "daemon-down" else "0"
        (bin_dir / "docker").write_text(
            f'#!/usr/bin/env bash\ncase "$1" in\n'
            f'  info) exit {info_rc} ;;\n'
            f'  *) exit 0 ;;\nesac\n')
        (bin_dir / "docker").chmod(0o755)
        path = f"{bin_dir}:{os.environ.get('PATH', '')}"
    else:
        # a PATH WITHOUT docker but with every other tool the script needs
        import shutil
        for tool in ("bash", "git", "python3", "sha256sum", "mktemp", "cut",
                     "tr", "awk", "find", "head", "tail", "grep", "sed",
                     "date", "basename", "dirname", "df", "mkdir", "cat",
                     "printf", "wc", "chmod", "rm", "cp", "ls"):
            real = shutil.which(tool)
            if real:
                (bin_dir / tool).symlink_to(real)
        path = str(bin_dir)
    env = dict(os.environ, PATH=path,
               KVP_PIN_FILE=str(PIN_FILE),
               # r23 D2: keep the default OUT_DIR (repo live/lmcache-build)
               # out of the REAL live/ -- build-lmcache.sh does mkdir -p
               # "$OUT_DIR" before any pre-flight.
               KVP_OUT_DIR=str(tmp_path / "out"),
               KVP_BUILD_DIR=str(tmp_path / "build"))
    r = subprocess.run(["bash", str(BUILD_LMC)], capture_output=True,
                       text=True, timeout=60, env=env, cwd=str(tmp_path))
    return r


def test_missing_docker_names_what_to_do(tmp_path):
    r = _run(tmp_path, "no-docker")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "docker is required" in out and "What to do" in out, (
        f"missing docker must explain what to do:\n{out[-400:]}"
    )


def test_daemon_unreachable_names_what_to_do(tmp_path):
    r = _run(tmp_path, "daemon-down")
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "daemon" in out.lower() and "What to do" in out, (
        f"unreachable daemon must explain what to do:\n{out[-400:]}"
    )