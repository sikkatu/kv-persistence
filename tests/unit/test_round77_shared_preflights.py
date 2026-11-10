"""Round 77 regression: shared pre-flights on BOTH engine paths.

Review round 76 moved the HotSplit config checks to a shared module because
"checks that live only in the kv launcher vanish when the default switches".
The same defect class applied to the pre-flights: the lmcache RPC ping
(mp_ping) and the CUDA IPC boundary test (ipc-test.sh) were wired ONLY into
run-vllm-with-persist.sh -- the delegated recipe path (KVP_ENGINE=recipe)
loaded 130 shards for ~15 minutes and then died at register_kv_cache
(CUDA_ERROR_MAP_FAILED from cuIpcOpenEventHandle) with no check before it.

Round 77:
* env.sh gains ONE shared _kvp_pre_flights() (RPC ping + IPC boundary test),
  called from the kv launcher AND from start-all.sh's recipe branch BEFORE
  the model load;
* the refusal text names the honest fix (KVP_LAUNCH_MODE=docker or one UID on
  both sides) and the honest scope (a pre-flight catches a dead channel at
  start, not a server that dies MID-load);
* ipc-test.sh labels stop lying: "same-UID test -- no root" used to print for
  ANY PRODUCER_USER including 0:0 (a ROOT producer).
"""
from __future__ import annotations
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_SH = ROOT / "scripts" / "env.sh"
START_ALL = ROOT / "scripts" / "start-all.sh"
IPC_TEST = ROOT / "scripts" / "ipc-test.sh"


def _sh(cmd: str, env_extra=None, cwd=None, timeout=30):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                          timeout=timeout, env=env, cwd=str(cwd or ROOT))


def _listen():
    """A real TCP listener on a free port (the shared pre-flight probes
    reachability first -- the old tests just used a dead 5555)."""
    import socket
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


# ---------------------------------------------------------------- shared function

def test_env_sh_defines_shared_pre_flights():
    t = ENV_SH.read_text()
    assert "_kvp_pre_flights(){" in t, (
        "env.sh must hold the ONE shared pre-flight function")
    fn = t[t.index("_kvp_pre_flights(){"):]
    fn = fn[:fn.index("\n}")]
    assert "mp_ping" in fn, "the shared pre-flight must probe the lmcache RPC protocol"
    assert "ipc-test.sh" in fn, "the shared pre-flight must test the CUDA IPC boundary"
    assert "KVP_SKIP_IPC_TEST" in fn, (
        "the explicit opt-out must keep working on both engine paths")


def test_recipe_branch_calls_pre_flights_before_the_model_load():
    t = START_ALL.read_text()
    assert "_kvp_pre_flights" in t, (
        "start-all.sh recipe branch must run the shared pre-flights")
    idx = t.index("_kvp_pre_flights")
    run_idx = t.index('bash "$RECIPE_DIR/scripts/run-tp4-hotsplit.sh"')
    assert idx < run_idx, (
        "the pre-flights must run BEFORE the delegated launcher (the model load)")
    assert "refusing the delegated launch" in t, (
        "a failed pre-flight must refuse to start (nothing was started)")


def test_refusal_text_names_the_fix_and_the_honest_scope():
    t = ENV_SH.read_text()
    assert "KVP_LAUNCH_MODE=docker" in t, (
        "the IPC refusal must name the container fix")
    assert "KVP_SAME_UID=1" in t, (
        "the IPC refusal must name the same-UID fix")
    assert "cannot catch a" in t and "MID-load" in t, (
        "the pre-flight must state its honest scope (dead channel at start only)")


# ---------------------------------------------------------------- behaviour

def _fake_root(tmp_path, ipc_rc: int):
    """A kv-repo-shaped root whose ipc-test.sh exits ipc_rc (no RPC probe)."""
    root = tmp_path / "kv"
    (root / "scripts").mkdir(parents=True)
    ipc = root / "scripts" / "ipc-test.sh"
    ipc.write_text(f"#!/usr/bin/env bash\necho FAKE-IPC rc={ipc_rc}\nexit {ipc_rc}\n")
    ipc.chmod(ipc.stat().st_mode | stat.S_IEXEC)
    return root


def test_shared_pre_flight_refuses_a_broken_ipc_boundary(tmp_path):
    root = _fake_root(tmp_path, 1)
    srv, port = _listen()
    try:
        r = _sh(
            f'. scripts/env.sh && _kvp_pre_flights "{root}" 127.0.0.1 {port} '
            f'"{tmp_path}/cache" "img@sha256:x" 1000 1000 0 host',
            env_extra={"KVP_SKIP_IPC_TEST": "0"})
    finally:
        srv.close()
    assert r.returncode != 0, (
        f"a broken CUDA IPC boundary must refuse the launch:\n{r.stdout}{r.stderr}")
    out = r.stdout + r.stderr
    assert "CUDA IPC BOUNDARY FAILED" in out, out[-800:]
    assert "KVP_LAUNCH_MODE=docker" in out, (
        "the refusal must name the fix the operator can apply")


def test_shared_pre_flight_passes_a_working_boundary(tmp_path):
    root = _fake_root(tmp_path, 0)
    srv, port = _listen()
    try:
        r = _sh(
            f'. scripts/env.sh && _kvp_pre_flights "{root}" 127.0.0.1 {port} '
            f'"{tmp_path}/cache" "img@sha256:x" 1000 1000 0 host')
    finally:
        srv.close()
    assert r.returncode == 0, f"working boundary must pass:\n{r.stdout}{r.stderr}"
    assert "CUDA IPC boundary OK" in r.stdout, r.stdout


def test_shared_pre_flight_honours_the_explicit_skip(tmp_path):
    root = _fake_root(tmp_path, 1)
    srv, port = _listen()
    try:
        r = _sh(
            f'. scripts/env.sh && _kvp_pre_flights "{root}" 127.0.0.1 {port} '
            f'"{tmp_path}/cache" "img@sha256:x" 1000 1000 0 host',
            env_extra={"KVP_SKIP_IPC_TEST": "1"})
    finally:
        srv.close()
    assert r.returncode == 0, f"KVP_SKIP_IPC_TEST=1 must skip:\n{r.stdout}{r.stderr}"
    assert "SKIPPED" in r.stdout, r.stdout


def test_shared_pre_flight_refuses_a_dead_rpc_channel(tmp_path):
    """A python stub that fails like a dead RPC channel: the ping must refuse
    BEFORE any model load (the 300s register_kv_caches hang)."""
    root = tmp_path / "kv"
    (root / "implementation").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py = bindir / "python3"
    py.write_text("#!/usr/bin/env bash\necho 'rpc: no answer'\nexit 1\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    srv, port = _listen()
    try:
        r = _sh(
            f'PATH="{bindir}:$PATH" . scripts/env.sh && '
            f'_kvp_pre_flights "{root}" 127.0.0.1 {port} "{tmp_path}/cache" "img" "" "" 0 host',
            env_extra={"KVP_SKIP_IPC_TEST": "1"})
    finally:
        srv.close()
    assert r.returncode != 0, f"a dead RPC channel must refuse:\n{r.stdout}{r.stderr}"
    out = r.stdout + r.stderr
    assert "did not answer the RPC ping" in out, out[-800:]
    assert "stop-cache.sh" in out and "start-cache.sh" in out, (
        "the refusal must name the restart fix")


# ---------------------------------------------------------------- ipc-test honesty

def test_ipc_test_labels_do_not_lie_about_root():
    t = IPC_TEST.read_text()
    assert '0:0|0) echo "   producer UID:GID = $PRODUCER_USER (root producer)"' in t, (
        "a root producer must be labelled as root")
    assert "SAME-UID result: PASS -- the transfer works WITHOUT root." in t, (
        "the same-UID verdict stays for a non-root producer")
    assert "ROOT-PRODUCER result: PASS" in t, (
        "a root producer must get its own PASS verdict")
    # The lying line must no longer be unconditional (it lived directly in the
    # if-branch before round 77) -- it may now only appear inside a `*)` arm.
    lines = t.splitlines()
    for i, line in enumerate(lines):
        if "same-UID test -- no root" in line:
            assert "echo" in line and (")" in line.split("echo")[0]), (
                "the same-UID label must sit in a case arm, not unconditional")