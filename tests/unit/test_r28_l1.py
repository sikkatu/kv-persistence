# SPDX-License-Identifier: Apache-2.0
"""r28 L1: ipc-test.sh must tell the TRUTH on a real CUDA refusal.

The r27 stub-consumer printed its pid even on failure, so the real
consumer.py path was never executed: on a real refusal
(torch.AcceleratorError ... invalid device context) Python exited rc=1
with NO pid, the script showed "PIDs: producer=1 consumer=?" and blamed
ECC instead of UIDs.

These tests run the REAL ipc-test.sh and the REAL generated consumer.py
(the docker stub rewrites the /tmp/kvp-ipc-test path to IPC_TEST_DIR and
executes it with a FAKE torch in PYTHONPATH whose reduction function
raises / returns a wrong sum). Three cases from the TZ:
  1. the function raises `invalid device context`: PIDs producer=<n>
     consumer=<m> (no '?'), OPEN FAILED, the UID hint, NO "data does not
     match", exit != 0;
  2. the function returns a tensor with the wrong sum: rc=1 and
     "data does not match";
  3. producer and consumer BOTH pid=1 and a refusal: exit 3 and
     TEST INVALID BY CONSTRUCTION, no UID hint.

MUTATIONS caught: dropping the early pid print from consumer.py;
sys.exit(4) -> sys.exit(1); disabling the equal-PID check.
"""
from __future__ import annotations
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IPC_TEST = ROOT / "scripts" / "ipc-test.sh"

FAKE_TORCH = '__version__ = "0.0-fake"\n'

FAKE_REDUCTIONS = (
    "import sys\n"
    "def raise_error(*args):\n"
    "    raise RuntimeError('CUDA error: invalid device context')\n"
    "def wrong_sum(*args):\n"
    "    class _T:\n"
    "        def sum(self):\n"
    "            return self\n"
    "        def item(self):\n"
    "            return 999.0\n"
    "    return _T()\n"
)

DOCKER_STUB = '''#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "${1:-}" in
  rm) exit 0 ;;
  logs) echo "producer: torch 0.0-fake, python 3.12, pid=${STUB_PROD_PID:-1111}, IPC handle written"; exit 0 ;;
  run)
    case "$*" in
      *consumer.py*)
        if [ -n "${STUB_FAKE_CONSUMER:-}" ]; then
          echo "consumer: pid=1"
          echo "consumer: OPEN FAILED RuntimeError: CUDA error: invalid device context"
          exit 4
        fi
        sed "s|/tmp/kvp-ipc-test|$IPC_TEST_DIR|g" "$IPC_TEST_DIR/consumer.py" > "$IPC_TEST_DIR/consumer_run.py"
        PYTHONPATH="$FAKE_DIR" exec python3 "$IPC_TEST_DIR/consumer_run.py" ;;
      *producer.py*)
        PYTHONPATH="$FAKE_DIR" python3 -c "import pickle,sys; sys.path.insert(0,'$FAKE_DIR'); import fake_reductions as fr; pickle.dump((getattr(fr,'$STUB_FN','raise_error'),()), open('$IPC_TEST_DIR/handle.pkl','wb'))"
        exit 0 ;;
    esac
    exit 0 ;;
esac
exit 0
'''


def _run_ipc(tmp_path, mode="raise", fake_consumer=False, prod_pid="1111"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_dir = tmp_path / "fake_modules"
    fake_dir.mkdir(exist_ok=True)
    (fake_dir / "torch.py").write_text(FAKE_TORCH)
    (fake_dir / "fake_reductions.py").write_text(FAKE_REDUCTIONS)
    stub_log = tmp_path / "stub.log"
    itd = tmp_path / "ipc-test"
    docker = bin_dir / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    host_py = bin_dir / "hostpy"
    host_py.write_text("#!/usr/bin/env bash\nexit 0\n")
    host_py.chmod(host_py.stat().st_mode | stat.S_IEXEC)
    fn = {"raise": "raise_error", "wrong_sum": "wrong_sum"}[mode]
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               STUB_LOG=str(stub_log),
               IMAGE="img@sha256:" + "a" * 64,
               CONSUMER="container",
               HOST_PY=str(host_py),
               IPC_TEST_DIR=str(itd),
               IPC_CONTAINER="kvp-ipc-test-stub",
               PRODUCER_USER="1000:1000",
               CONSUMER_USER="1000:1000",
               GPU_IDX="0",
               FAKE_DIR=str(fake_dir),
               STUB_FN=fn,
               STUB_PROD_PID=prod_pid)
    if fake_consumer:
        env["STUB_FAKE_CONSUMER"] = "1"
    r = subprocess.run(["bash", str(IPC_TEST)], capture_output=True,
                       text=True, timeout=60, env=env, cwd=str(tmp_path))
    return r, itd


def test_l1_real_cuda_refusal_gets_the_uid_verdict(tmp_path):
    """> L1 case 1: the real consumer.py hits `invalid device context` ->
    PIDs producer=<n> consumer=<m> (NO '?'), 'OPEN FAILED', the ONE-UID hint,
    NO 'data does not match', exit != 0. MUTATIONS: dropping the early pid
    print (PIDs shows '?'), exit(4)->exit(1) (the ECC lie returns)."""
    r, _ = _run_ipc(tmp_path, mode="raise")
    out = r.stdout + r.stderr
    assert r.returncode == 4, (
        f"an OPEN FAILED must exit 4 (the documented refusal code):\n{out[-1200:]}")
    assert "PIDs: producer=1111 consumer=" in out, (
        f"the PIDs line must carry both numbers:\n{out[-1200:]}")
    assert "consumer=?" not in out, (
        f"the consumer pid must never be '?':\n{out[-1200:]}")
    assert "OPEN FAILED RuntimeError: CUDA error: invalid device context" in out, (
        f"the OPEN FAILED line must print:\n{out[-1200:]}")
    assert "share ONE UID" in out, (
        f"the UID hint is the honest one for an OPEN failure:\n{out[-1200:]}")
    assert "data does not match" not in out, (
        f"the ECC verdict would be a lie here:\n{out[-1200:]}")


def test_l1_wrong_sum_gets_the_data_verdict(tmp_path):
    """> L1 case 2: the memory opened but the sum is wrong -> rc=1 and
    'data does not match' (the ONLY case where that verdict is true)."""
    r, _ = _run_ipc(tmp_path, mode="wrong_sum")
    out = r.stdout + r.stderr
    assert r.returncode == 1, f"rc=1 expected:\n{out[-1200:]}"
    assert "data does not match" in out, out[-1200:]
    assert "sum=" in out, (
        f"the sum line must prove the memory opened:\n{out[-1200:]}")


def test_l1_equal_pids_give_invalid_topology(tmp_path):
    """> L1 case 3: producer and consumer BOTH pid=1 + a refusal -> exit 3,
    TEST INVALID BY CONSTRUCTION, NO UID hint. MUTATION: disabling the
    equal-PID check (the script would blame the boundary instead)."""
    r, _ = _run_ipc(tmp_path, mode="raise", fake_consumer=True,
                    prod_pid="1")
    out = r.stdout + r.stderr
    assert r.returncode == 3, f"exit 3 expected:\n{out[-1200:]}"
    assert "TEST INVALID BY CONSTRUCTION" in out, out[-1200:]
    assert "share ONE UID" not in out, (
        f"an invalid topology must NOT print the UID hint:\n{out[-1200:]}")
