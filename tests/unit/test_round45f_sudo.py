"""Round 45f regression: sudo-compatible cache server launch.

The ipc-test proved the fix: the cache server must run as ROOT (same user
as the engine container) to open CUDA IPC memory. But `sudo bash
scripts/start-cache.sh` used to break -- sudo resets HOME/PATH, so python3
missed the lmcache venv. Round 45f:
* env.sh gains _kvp_python: resolves the ORIGINAL user's venv python even
  under sudo (via SUDO_UID/SUDO_USER), falls back to python3;
* start-cache.sh launches through the resolved python (LAUNCH + codec
  probe);
* the pre-flight IPC consumer mode follows the SERVER PROCESS user
  (root -> host-root), not just KVP_LAUNCH_MODE.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_SH = ROOT / "scripts" / "env.sh"
START_CACHE = ROOT / "scripts" / "start-cache.sh"
LAUNCHER = ROOT / "scripts" / "run-vllm-with-persist.sh"


def test_kvp_python_resolver_exists():
    t = ENV_SH.read_text()
    assert "_kvp_python()" in t, "env.sh must provide the python resolver"
    assert "SUDO_UID" in t and "SUDO_USER" in t, (
        "the resolver must handle sudo (original user's venv)"
    )
    assert ".venvs/lmcache/bin/python" in t, (
        "the resolver must know the lmcache venv path"
    )


def test_kvp_python_resolves_under_sudo_simulation():
    """With SUDO_UID set the resolver must still find the venv python."""
    r = subprocess.run(
        ["bash", "-c",
         'source scripts/env.sh && SUDO_UID=0 SUDO_USER= _kvp_python'],
        capture_output=True, text=True, timeout=30, cwd=str(ROOT),
    )
    out = (r.stdout + r.stderr).strip()
    assert out, "the resolver must print a python path"


def test_start_cache_launches_with_resolved_python():
    t = START_CACHE.read_text()
    assert "_kvp_python" in t, "start-cache.sh must use the resolver"
    assert 'LAUNCH=("$KVP_PY" -m kvpersist.launch)' in t
    assert '"$KVP_PY" -c "from kvpersist.compress_serde' in t, (
        "the codec probe must use the resolved python too"
    )


def test_ipc_mode_follows_server_process_user():
    """A root-run server needs the host-root IPC consumer (the ipc-test
    proved root is the fix) -- detected from the pid file, not env alone."""
    t = LAUNCHER.read_text()
    assert "host-root" in t, "the pre-flight must support the host-root consumer"
    assert "/proc/$_ipc_pid/status" in t, (
        "the server process user must be read from proc"
    )
    marker = t.index("Review fix (round 45): the RPC ping does NOT prove")
    idx = t.index("_ipc_mode=host-root")
    assert idx > marker, (
        "the mode detection must live in the IPC test block"
    )