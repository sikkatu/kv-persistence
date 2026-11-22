# SPDX-License-Identifier: Apache-2.0
"""Round 27 review K4/K5: ipc-test.sh must test the DEPLOYED configuration and
must never abort without a verdict.

K4: the producer runs via `sh -c 'python3 /ipc/producer.py; true'` (its PID is
    NOT 1 -- the hardware table rows 4/5 pass with separate PID namespaces and
    producer PID != 1; NO --pid host); the container consumer runs with
    --user $CONSUMER_USER (the EXPECTED server UID, defaults to PRODUCER_USER);
    both pythons print os.getpid() and the script reports
    `PIDs: producer=<n> consumer=<m>` -- equal PIDs give the
    "TEST INVALID BY CONSTRUCTION" verdict, not a boundary FAIL.
K5: a failing consumer must not abort the script under set -e: the verdict
    block `case $rc` prints for any non-zero rc (including 1 and 124), cleanup
    (`touch done`, `docker rm -f`) runs via trap EXIT, and the script exits
    non-zero.

Behavioural: stub docker + stub host python in PATH write argv to files;
IPC_TEST_DIR/IPC_CONTAINER isolate every path (no /tmp/kvp-ipc-test, no real
container names).
"""
from __future__ import annotations
import os
import pathlib
import stat
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IPC_TEST = ROOT / "scripts" / "ipc-test.sh"


def _write_stub(bin_dir: pathlib.Path, name: str, text: str):
    p = bin_dir / name
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _run_ipc(tmp_path, consumer="container", consumer_rc=0, same_pid=False,
             extra_env=None):
    """Run the REAL ipc-test.sh with stub docker + stub HOST_PY.

    The docker stub records argv per call; `logs` prints a producer line whose
    pid the consumer line must differ from (unless same_pid=True). The stub
    consumer prints its line and exits consumer_rc.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    argv_log = tmp_path / "argv.log"
    itd = tmp_path / "ipc-test"
    prod_pid = "1111"
    cons_pid = "1111" if same_pid else "2222"
    # stub docker: record argv; logs = producer line; the consumer run
    # (second container) prints the consumer line and exits consumer_rc
    _write_stub(bin_dir, "docker", f'''#!/usr/bin/env bash
echo "$*" >> "{argv_log}"
case "${{1:-}}" in
  rm) exit 0 ;;
  logs) echo "producer: torch 2.5, python 3.12, pid={prod_pid}, IPC handle written"; exit 0 ;;
  run)
    case "$*" in
      *consumer.py*)
        if [ "{consumer_rc}" = "0" ]; then
          echo "consumer: torch 2.5, python 3.12, pid={cons_pid}, sum=523776.0 expected=523776.0"
        else
          echo "consumer: pid={cons_pid}"
          echo "consumer: OPEN FAILED RuntimeError: CUDA error: invalid device context"
        fi
        exit {consumer_rc} ;;
      *producer.py*)
        # the producer writes the handle file so the wait loop breaks at once
        touch "{itd}/handle.pkl"
        exit 0 ;;
    esac
    exit 0 ;;
esac
exit 0
''')
    # stub HOST_PY (used for CONSUMER=host|host-root): same consumer line
    _write_stub(bin_dir, "hostpy", f'''#!/usr/bin/env bash
echo "consumer: torch 2.5, python 3.12, pid={cons_pid}, sum=523776.0 expected=523776.0"
exit {consumer_rc}
''')
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               IMAGE="img@sha256:" + "a" * 64,
               CONSUMER=consumer,
               HOST_PY=str(bin_dir / "hostpy"),
               IPC_TEST_DIR=str(itd),
               IPC_CONTAINER="kvp-ipc-test-stub",
               PRODUCER_USER="1000:1000",
               CONSUMER_USER="1000:1000",
               GPU_IDX="0")
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(["bash", str(IPC_TEST)], capture_output=True,
                       text=True, timeout=60, env=env, cwd=str(tmp_path))
    return r, argv_log, itd


# ===========================================================================
# T4 (K4): topology and CONSUMER_USER
# ===========================================================================
def test_t4_producer_runs_via_sh_no_pid_host(tmp_path):
    """> T4: the producer is launched with `--entrypoint sh ... -c '...; true'`
    and WITHOUT --pid host (hardware: separate PID namespaces pass).
    MUTATION caught: returning the bare python entrypoint or adding --pid."""
    r, argv_log, _ = _run_ipc(tmp_path)
    lines = argv_log.read_text().splitlines()
    prod = [l for l in lines if "producer.py" in l]
    assert prod, f"the producer must be launched:\n{r.stdout}{r.stderr}"
    line = prod[0]
    assert "sh" in line and "-c" in line, (
        f"the producer must run via sh -c (PID != 1): {line}")
    assert "true" in line, f"the sh -c command must end with '; true': {line}"
    assert "--pid" not in line, f"--pid must NOT be used: {line}"


def test_t4_consumer_carries_consumer_user(tmp_path):
    """> T4: the container consumer's argv has --user equal to CONSUMER_USER
    (the expected server UID). MUTATION caught: dropping --user."""
    r, argv_log, _ = _run_ipc(tmp_path)
    lines = argv_log.read_text().splitlines()
    cons = [l for l in lines if "consumer.py" in l]
    assert cons, f"the container consumer must be launched:\n{r.stdout}{r.stderr}"
    assert "--user 1000:1000" in cons[0], (
        f"the consumer must run as the expected server UID: {cons[0]}")


def test_t4_equal_pids_give_invalid_verdict(tmp_path):
    """> T4: equal producer/consumer PIDs -> 'TEST INVALID BY CONSTRUCTION',
    not a boundary verdict. MUTATION caught: dropping the PID check."""
    r, _, _ = _run_ipc(tmp_path, same_pid=True)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"an invalid topology must exit non-zero:\n{out[-800:]}"
    assert "INVALID BY CONSTRUCTION" in out, out[-800:]
    assert "PIDs: producer=1111 consumer=1111" in out, out[-800:]


def test_t4_pids_reported_on_success(tmp_path):
    """> T4: the PIDs line is printed and a successful run gives PASS."""
    r, _, _ = _run_ipc(tmp_path, consumer_rc=0)
    out = r.stdout + r.stderr
    assert "PIDs: producer=1111 consumer=2222" in out, out[-800:]
    assert "PASS:" in out, out[-800:]
    assert r.returncode == 0, out[-800:]


# ===========================================================================
# T5 (K5): a failing consumer still yields a verdict + cleanup
# ===========================================================================
def test_t5_failing_consumer_gets_verdict_and_cleanup(tmp_path):
    """> T5: the stub consumer exits 1 (r28 L1: NO sum= line -> an OPEN failure,
    not "data does not match") -> the script exits non-zero, prints the
    ONE-UID verdict, touches done and runs `docker rm -f`.
    MUTATION caught: bare `rc=$?` under set -e (no verdict, no cleanup) or
    dropping the trap."""
    r, argv_log, itd = _run_ipc(tmp_path, consumer_rc=1)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a failed consumer must exit non-zero:\n{out[-800:]}"
    # r28 L1: rc=1 without a sum= line is an OPEN failure -> the UID verdict,
    # NOT "data does not match" (that one needs a sum= line).
    assert "share ONE UID" in out, f"the verdict must be the UID one:\n{out[-800:]}"
    assert "data does not match" not in out, (
        "without a sum= line the data verdict would be a lie")
    assert (itd / "done").exists(), "the done marker must be created (cleanup)"
    rm_lines = [l for l in argv_log.read_text().splitlines()
                if l.startswith("rm")]
    assert rm_lines, f"the container must be removed:\n{argv_log.read_text()}"


def test_t5_timeout_rc_gets_timeout_verdict(tmp_path):
    """> T5: rc=124 (timeout) also reaches the verdict block -- the K5 contract
    says the case block prints for ANY non-zero code."""
    r, _, itd = _run_ipc(tmp_path, consumer_rc=124)
    out = r.stdout + r.stderr
    assert r.returncode == 124, out[-800:]
    assert "TIMEOUT:" in out, f"rc=124 must print the TIMEOUT verdict:\n{out[-800:]}"
    assert (itd / "done").exists(), "cleanup must run for rc=124 too"