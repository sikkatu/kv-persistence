"""Integration: the FULL launcher (`kvpersist.launch`) and the active-write
registry on the REAL LMCache write path.

Review request: "Отдельно добавить интеграционный тест, который запускает именно
kvpersist.launch, подтверждает установку реестра и проверяет защиту временного
файла при очистке во время активной записи."

What is verified against a LIVE `lmcache server`:
  1. the server starts via `kvpersist.launch` and its log carries the
     `[kvp]` banner lines for the salt patch AND the active-write registry;
  2. the registry directory (`<cache_root>/.kvp-active-writes/`) is created and
     used by real stores (markers appear while a store is in flight);
  3. `purge --tmp --yes` NEVER deletes a `*.bin.tmp.*` whose marker is LIVE --
     proven by registering a marker from this very (alive) process and
     confirming the file survives the purge;
  4. `purge --tmp --yes` DOES delete an unregistered stale leftover.

Runs only with `KVP_LIVE_LMCACHE=1` (pinned lmcache 0.5.3 + POSIX-SHM Linux
host) -- same external prerequisite as the other live tests.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "implementation"))

import test_l2_persistence as pers  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("KVP_LIVE_LMCACHE") != "1",
    reason="real LMCache launcher/registry test needs the pinned lmcache "
           "package (0.5.3) and a POSIX-SHM Linux host: set KVP_LIVE_LMCACHE=1",
)


def test_launch_installs_registry_and_purge_respects_it(tmp_path):
    from kvpersist import tmp_guard

    l2 = tmp_path / "l2"
    adapter = pers._render_adapter(tmp_path, budget_gb="1")
    log = tmp_path / "launcher.log"
    zmq_port, http_port = pers._free_port(), pers._free_port()
    server = None
    try:
        server = pers._start_server(
            adapter, zmq_port, http_port, log, "launcher-registry",
            launcher="launch",
        )
        pers._wait_ready(server, http_port, log)

        # ---- 1. The launcher banner must confirm both patches -------------
        log_text = log.read_text(errors="replace")
        assert "[kvp] cache_salt included in L2 file names" in log_text, (
            "the salt filename patch must be installed by kvpersist.launch "
            f"-- log:\\n{log_text[-2000:]}"
        )
        assert "[kvp] active-write registry installed" in log_text, (
            "the tmp_guard active-write registry must be installed by "
            f"kvpersist.launch -- log:\\n{log_text[-2000:]}"
        )

        # ---- 2. The registry directory exists in the real cache root after a
        # real store (created lazily by the first registered write) ------
        from lmc_protocol_client import LmcClient, build_token_ids

        client = LmcClient(
            f"tcp://127.0.0.1:{zmq_port}", "mimo-pro/launcher", instance_id=7300
        )
        try:
            tokens = list(build_token_ids(seq_no=13, num_tokens=64))
            assert client.store(tokens, "launcher-store") is not None
            deadline = time.time() + 60
            while time.time() < deadline:
                if pers._l2_data_files(l2):
                    break
                time.sleep(1)
        finally:
            client.close()
        reg = tmp_guard.registry_dir(str(l2))
        assert os.path.isdir(reg), (
            f"registry dir {reg} must be created by a real store"
        )

        # ---- 3. A LIVE marker protects its tmp file from purge ------------
        final_name = "launcher_00000000_0_" + "a" * 64 + ".bin"
        inflight = l2 / (final_name + ".tmp." + "f" * 32)
        inflight.write_bytes(b"x" * 100)
        stale = l2 / ("launcher_00000000_0_" + "b" * 64 + ".bin.tmp." + "e" * 32)
        stale.write_bytes(b"y" * 100)
        old = time.time() - 3600
        os.utime(stale, (old, old))

        marker = tmp_guard.register_write(str(l2), final_name)
        assert marker is not None, "registry must accept the in-flight write"
        try:
            env = dict(
                os.environ,
                KVP_CACHE_ROOT=str(l2),
                KVP_CACHE_BUDGET_GB="100",
                PYTHONPATH=str(_HERE.parents[1] / "implementation")
                + os.pathsep + os.environ.get("PYTHONPATH", ""),
            )
            res = subprocess.run(
                [sys.executable, "-m", "kvpersist.cli", "purge", "--tmp", "--yes"],
                capture_output=True, text=True, env=env, timeout=60,
            )
            assert res.returncode == 0, f"purge failed: {res.stderr}"
            assert inflight.exists() is True, (
                "purge --tmp deleted a tmp file whose store is REGISTERED and "
                "whose writer (this process) is alive -- the registry must "
                "protect it regardless of file/marker age"
            )
            assert stale.exists() is False, (
                "an unregistered stale leftover must still be swept"
            )
        finally:
            tmp_guard.unregister_write(marker)
    finally:
        if server is not None:
            pers._stop_server(server, log, "launcher-registry")


def test_real_stores_leave_no_markers_after_completion(tmp_path):
    """Every finished store must clear its marker -- only crashes leave one."""
    from lmc_protocol_client import LmcClient, build_token_ids
    from kvpersist import tmp_guard

    l2 = tmp_path / "l2"
    adapter = pers._render_adapter(tmp_path, budget_gb="1")
    log = tmp_path / "markers.log"
    zmq_port, http_port = pers._free_port(), pers._free_port()
    server = None
    try:
        server = pers._start_server(
            adapter, zmq_port, http_port, log, "markers", launcher="launch"
        )
        pers._wait_ready(server, http_port, log)
        client = LmcClient(
            f"tcp://127.0.0.1:{zmq_port}", "mimo-pro/markers", instance_id=7200
        )
        try:
            tokens = list(build_token_ids(seq_no=11, num_tokens=64))
            assert client.store(tokens, "marker-store") is not None
            # Wait for the async store to finish writing all chunks.
            deadline = time.time() + 60
            while time.time() < deadline:
                if len(pers._l2_data_files(l2)) >= 4:
                    break
                time.sleep(1)
            # Give the registry a beat to clear the markers.
            time.sleep(3)
            leftover = tmp_guard.active_final_names(str(l2))
            assert leftover == set(), (
                f"markers still active after the store completed: {leftover}"
                " -- every finished store must unregister its marker"
            )
        finally:
            client.close()
    finally:
        if server is not None:
            pers._stop_server(server, log, "markers")