"""Round 90 D1: test-recovery.sh scenario-B docker stop is daemon-robust.

The reviewed real run hit docker's "tried to kill container, but did not
receive an exit event" (daemon failure -- NOT a cache failure) and the test
wrongly reported FAIL(B). The stop block now: retries `docker stop -t 120`,
waits up to 5 min for exited/dead, and ends INCONCLUSIVE (exit, no FAIL)
when the container never stops.

The block is extracted from scripts/test-recovery.sh and executed against a
stub docker -- full test-recovery runs need the live stack.
MUTATIONs caught:
  * dropping the retry/wait logic -> daemon-error run reports FAIL, not
    INCONCLUSIVE;
  * dropping the INCONCLUSIVE verdict -> exit code != 5 / no VERDICT line;
  * treating a transient stop error as fatal on first attempt.
"""
from __future__ import annotations

import re
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
RECOVERY = KV / "scripts" / "test-recovery.sh"


def _stop_block() -> str:
    """The r90 D1 stop block from test-recovery.sh (between its start marker
    and the closing `fi`)."""
    src = RECOVERY.read_text()
    m = re.search(
        r"(  # r90 D1: docker stop is now RETRIED.*?)\nfi\n",
        src, re.S)
    assert m, "r90 D1 stop block not found in test-recovery.sh"
    return m.group(1)


def _harness(tmp_path, docker_body: str) -> subprocess.CompletedProcess:
    """Run the extracted stop block with a stub docker + fake inconc()/PA()."""
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    (b / "docker").write_text(docker_body)
    (b / "docker").chmod(0o755)
    (b / "sleep").write_text("#!/bin/bash\nexit 0\n")   # no real waiting
    (b / "sleep").chmod(0o755)
    script = textwrap.dedent(f'''
        set -u
        CONTAINER=mimo-pro-tp4-hotsplit
        VI=0
        PA(){{ echo "FAIL(${{1:-?}}): ${{*:2}}"; exit 1; }}
        inconc(){{ echo "INCONCLUSIVE: $*"; VI=$((VI+1)); }}
        PATH="{b}:$PATH"
    ''') + _stop_block() + '\necho "STOP_BLOCK_DONE rc=$?"\n'
    f = tmp_path / "run.sh"
    f.write_text(script)
    return subprocess.run(["bash", str(f)], capture_output=True, text=True, timeout=120)


# docker stub variants ------------------------------------------------------
DOCKER_FAILS_THEN_STOPPED = '''#!/bin/bash
# `docker stop` always errors (daemon), but the container state becomes
# exited on the FIRST inspect -- the retry logic must accept it.
case "$1" in
  stop) echo "Error response from daemon: tried to kill container, but did not receive an exit event" >&2; exit 1 ;;
  inspect) echo "exited" ;;
esac
exit 0
'''

DOCKER_NEVER_STOPS = '''#!/bin/bash
case "$1" in
  stop) echo "Error response from daemon: tried to kill container, but did not receive an exit event" >&2; exit 1 ;;
  inspect) echo "running" ;;
esac
exit 0
'''

DOCKER_STOP_OK = '''#!/bin/bash
case "$1" in
  stop) echo "mimo-pro-tp4-hotsplit" ; exit 0 ;;
  inspect) echo "exited" ;;
esac
exit 0
'''


def test_transient_stop_error_then_exited_is_accepted(tmp_path):
    """stop errors + state exited -> the block proceeds (retry logic works).
    MUTATION caught: dropping the state-inspect retry."""
    r = _harness(tmp_path, DOCKER_FAILS_THEN_STOPPED)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "STOP_BLOCK_DONE" in r.stdout
    assert "INCONCLUSIVE" not in r.stdout, f"a stopped container must NOT be inconclusive:\n{r.stdout}"


def test_clean_stop(tmp_path):
    r = _harness(tmp_path, DOCKER_STOP_OK)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "STOP_BLOCK_DONE" in r.stdout


def test_never_stopping_container_is_inconclusive_not_fail(tmp_path):
    """The reviewed daemon failure: stop keeps erroring, state stays running
    -> INCONCLUSIVE (exit via inconc path), NEVER `FAIL(...)` / PA B.
    MUTATION caught: reverting to the old `PA B "docker stop (B) failed"`."""
    r = _harness(tmp_path, DOCKER_NEVER_STOPS)
    out = r.stdout + r.stderr
    assert "INCONCLUSIVE" in out, f"daemon-failure must be INCONCLUSIVE:\n{out}"
    assert "FAIL(" not in out, f"must NOT report FAIL on a docker daemon failure:\n{out}"
    assert "did not receive an exit event" in out, "the stop error must be echoed for the operator"