"""Round 46: engine-identity.env + reviewer batch regressions.

Covers:
* the launcher writes the effective codec/namespace to
  $KVP_CACHE_ROOT/engine-identity.env;
* verify-engine-identity.sh READS that file when neither the
  USE_LOSSLESS_SERDE switch nor the KVP_L2_SERDE pin is set
  (EXECUTABLE: the embedded Python reader is extracted and run);
* test-recovery.sh scenario B stops the engine FIRST (order check) and
  its api_ready aborts when the container dies (EXECUTABLE against a
  stub docker);
* the GPU pre-flight helper exists in env.sh and is wired into the
  launcher and test-recovery.sh;
* ipc-test.sh no longer claims the same-UID path "is NOT yet wired".
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text()


# ---------------------------------------------------------------- launcher writes the file
def test_launcher_writes_engine_identity_env():
    t = _read("run-vllm-with-persist.sh")
    assert 'engine-identity.env' in t
    assert 'KVP_L2_SERDE=${LMCACHE_L2_SERDE:-none}' in t
    assert 'KVP_MODEL_NAME=$KVP_MODEL_NAME' in t


# ---------------------------------------------------------------- verify reads the file (EXECUTABLE)
def _extract_reader() -> str:
    t = _read("verify-engine-identity.sh")
    m = re.search(r'(def _read_engine_identity\(\):.*?\n    return out\n)', t, re.S)
    assert m, "_read_engine_identity not found in verify-engine-identity.sh"
    return m.group(1)


def test_engine_identity_file_reader_executes(tmp_path, monkeypatch):
    """The embedded reader must really parse the file written by the launcher."""
    monkeypatch.setenv("KVP_CACHE_ROOT", str(tmp_path))
    (tmp_path / "engine-identity.env").write_text(
        "# comment\nKVP_L2_SERDE=zstd\nKVP_MODEL_NAME=mimo-pro/abc123\n")
    code = "import os\n" + _extract_reader() + "\nprint(_read_engine_identity())\n"
    r = subprocess.run(["python3", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "'KVP_L2_SERDE': 'zstd'" in r.stdout, r.stdout


def test_engine_identity_reader_tolerates_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KVP_CACHE_ROOT", str(tmp_path))
    code = "import os\n" + _extract_reader() + "\nprint(_read_engine_identity())\n"
    r = subprocess.run(["python3", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "{}" in r.stdout, r.stdout


def test_verify_precedence_uses_engine_identity_file():
    """switch > pin > engine-identity.env > LMCACHE_L2_SERDE."""
    t = _read("verify-engine-identity.sh")
    assert '_engine_identity.get("KVP_L2_SERDE"' in t
    assert 'if not _l2_switch and not _l2_pin:' in t


# ---------------------------------------------------------------- test-recovery scenario B order
def test_scenario_b_stops_engine_first():
    t = _read("test-recovery.sh")
    b = t[t.index('== SCENARIO B'):]
    stop_e = b.index('docker stop "$CONTAINER"')
    stop_c = b.index('stop-cache.sh')
    start_c = b.index('start-cache.sh')
    start_e = b.index('docker start "$CONTAINER"')
    assert stop_e < stop_c < start_c < start_e, (
        "scenario B must be: docker stop -> stop-cache -> start-cache -> docker start")
    assert 'docker restart "$CONTAINER"' not in b, (
        "scenario B must not use docker restart (the engine is restarted cleanly)")


# ---------------------------------------------------------------- api_ready aborts on dead container (EXECUTABLE)
def _extract_api_ready() -> str:
    t = _read("test-recovery.sh")
    m = re.search(r'(api_ready\(\)\{.*?\n  return 1; \})', t, re.S)
    assert m, "api_ready not found"
    return m.group(1)


def test_api_ready_aborts_when_container_dead(tmp_path):
    """A dead container must abort the wait immediately (20 min hang fixed)."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "inspect" ]; then echo exited; exit 0; fi\n')
    (fake / "curl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake / "docker").chmod(0o755)
    (fake / "curl").chmod(0o755)
    script = (f'CONTAINER=fake; KEY=k; API=http://x; '
              f'{_extract_api_ready()}; api_ready; echo "rc=$?"')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{fake}:/usr/bin:/bin"})
    assert "rc=2" in r.stdout, (r.stdout, r.stderr)
    assert "ABORT" in r.stderr or "ABORT" in r.stdout, (r.stdout, r.stderr)


def test_api_ready_succeeds_when_running(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "inspect" ]; then echo running; exit 0; fi\n')
    (fake / "curl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake / "docker").chmod(0o755)
    (fake / "curl").chmod(0o755)
    script = (f'CONTAINER=fake; KEY=k; API=http://x; '
              f'{_extract_api_ready()}; api_ready; echo "rc=$?"')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={"PATH": f"{fake}:/usr/bin:/bin"})
    assert "rc=0" in r.stdout, (r.stdout, r.stderr)


# ---------------------------------------------------------------- GPU pre-flight wiring
def test_gpu_preflight_defined_and_wired():
    env = _read("env.sh")
    assert "_kvp_gpu_preflight(){" in env
    assert "query-compute-apps" in env
    t = _read("run-vllm-with-persist.sh")
    assert "_kvp_gpu_preflight 2048" in t
    rec = _read("test-recovery.sh")
    assert "_kvp_gpu_preflight 2048" in rec
    assert "KVP_SKIP_GPU_CHECK" in t and "KVP_SKIP_GPU_CHECK" in rec


# ---------------------------------------------------------------- ipc-test stale text gone
def test_ipc_test_no_stale_not_wired_text():
    t = _read("ipc-test.sh")
    assert "NOT yet wired" not in t
    assert "This path IS wired" in t


# ---------------------------------------------------------------- block_size quiet in config-only
def test_block_size_info_quiet_in_config_only():
    t = _read("verify-engine-identity.sh")
    assert "if not check_config_only:" in t
    idx = t.index("block_size could not be determined")
    pre = t[max(0, idx - 400):idx]
    assert "if not check_config_only:" in pre, (
        "the block_size INCONCLUSIVE line must be suppressed in config-only mode")