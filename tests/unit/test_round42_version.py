"""Round 42/80b regression: LMCache version-compatibility pre-flight.

The install run failed at register_kv_caches (300s timeout) because the host
served lmcache 0.5.3 while the pinned vLLM image ships 0.5.6 -- mixed minor
versions break the MP protocol silently. The check lives in the shared
_kvp_pre_flights() (env.sh) now (it was in the deleted kv launcher); this
test drives it directly: the host version comes from the _kvp_python resolver
($HOME/.venvs/lmcache/bin/python), the image version from `docker run --rm
--entrypoint python3` -- both stubbed here.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T
import os
import socket
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _listen():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _setup(tmp_path: Path, host_ver: str, img_ver: str):
    """Stub BOTH version probes:
    * host: $HOME/.venvs/lmcache/bin/python (what _kvp_python resolves);
    * image: a docker stub answering the `--entrypoint python3` probe."""
    home = tmp_path / "home"
    vpy = home / ".venvs" / "lmcache" / "bin" / "python"
    vpy.parent.mkdir(parents=True)
    vpy.write_text(f'#!/usr/bin/env bash\necho "{host_ver}"\n')
    vpy.chmod(vpy.stat().st_mode | stat.S_IEXEC)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in\n'
        '  *entrypoint*python3*) echo "' + img_ver + '"; exit 0 ;;\n'
        'esac\nexit 0\n')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return home, bindir


def _run_pre_flights(tmp_path, host_ver, img_ver):
    home, bindir = _setup(tmp_path, host_ver, img_ver)
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    srv, port = _listen()
    try:
        r = subprocess.run(
            ["bash", "-c",
             f'. scripts/env.sh && _kvp_pre_flights "{ROOT}" 127.0.0.1 {port} '
             f'"{cache_root}" "img@sha256:x" 1000 1000 0 host'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(home),
                 "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                 "KVP_SKIP_IPC_TEST": "1",
                 "LMCACHE_L2_SERDE": "zstd"},
            cwd=str(ROOT))
    finally:
        srv.close()
    return r


def test_version_mismatch_blocks_before_the_load(tmp_path):
    """Host 0.5.3 vs image 0.5.6 must abort BEFORE the model load (was:
    register_kv_caches silently timed out after 300s)."""
    r = _run_pre_flights(tmp_path, "0.5.3", "0.5.6")
    combined = r.stdout + r.stderr
    assert r.returncode != 0, (
        "a version mismatch must abort:\n" + combined[-1500:])
    assert "LMCACHE VERSION MISMATCH" in combined, (
        "the refusal must name the version mismatch:\n" + combined[-1500:])
    assert "0.5.3" in combined and "0.5.6" in combined, (
        "the refusal must name BOTH versions:\n" + combined[-1500:])


def test_version_match_passes_the_pre_flight(tmp_path):
    """Matching versions let the launch through the pre-flight."""
    r = _run_pre_flights(tmp_path, "0.5.6", "0.5.6")
    combined = r.stdout + r.stderr
    assert "pre-flight: lmcache version matches" in combined, (
        "the pre-flight must confirm matching versions:\n" + combined[-1500:])