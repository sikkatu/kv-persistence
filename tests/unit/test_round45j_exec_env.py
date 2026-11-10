"""Round 45j: execute the real ipc-test call line (catches the bash trap).

Round 45h wrote ${_ipc_pu:+PRODUCER_USER="$_ipc_pu"} in front of the
command. bash does NOT treat an assignment coming out of a parameter
expansion as an env assignment -- it runs it as a command (exit 127).
Text-matching tests cannot see this. This test EXECUTES the exact call
line from the launcher against a fake ipc-test.sh and asserts the value
really arrives.

Also covers the reviewer's fixes:
* HOME/USER/LOGNAME are set in same-UID mode (a uid without a passwd
  entry breaks getpass.getuser() and ~-based caches);
* the cache-server PID check lives under PERSIST=true (it used to fire
  even with KVP_PERSIST_ENABLED=false, contradicting the comment).
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = _T.recipe_launcher()  # Round 80b: recipe owns the engine launch


def _call_line() -> str:
    m = re.search(
        r'^\s*if (IMAGE="\$_image" CONSUMER="\$_ipc_mode" .*bash "\$_root/scripts/ipc-test\.sh"); then',
        _T.contract_text(), re.M)
    assert m, "ipc-test call line not found in the launcher"
    return m.group(1)


def _run(tmp_path, producer_user):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "ipc-test.sh").write_text(
        '#!/usr/bin/env bash\n'
        'echo "PU=[${PRODUCER_USER-unset}] C=[$CONSUMER]"\n')
    script = (f'_root="{tmp_path}"; _image=img; _ipc_mode=host; '
              f'_ipc_pu="{producer_user}"; '
              f'if {_call_line()}; then echo CALLED; fi')
    return subprocess.run(["bash", "-c", script],
                          capture_output=True, text=True)


def test_same_uid_producer_user_reaches_ipc_test(tmp_path):
    """The UID must really arrive at ipc-test.sh (not 'command not found')."""
    r = _run(tmp_path, "1234:5678")
    assert "command not found" not in r.stderr, r.stderr
    assert "PU=[1234:5678] C=[host]" in r.stdout, (r.stdout, r.stderr)


def test_root_mode_passes_empty_producer_user(tmp_path):
    """Empty is fine -- ipc-test treats it as unset (root producer)."""
    r = _run(tmp_path, "")
    assert "command not found" not in r.stderr, r.stderr
    assert "PU=[] C=[host]" in r.stdout, (r.stdout, r.stderr)


def test_same_uid_sets_home_user_for_any_uid():
    """A uid without a passwd entry breaks getpass.getuser() and
    ~-based caches -- HOME/USER/LOGNAME must be set explicitly."""
    t = _T.contract_text()
    assert "-e HOME=/tmp/engine-home" in t
    assert '-e "USER=$_me"' in t and '-e "LOGNAME=$_me"' in t


def test_pid_check_lives_under_persist_true():
    """With the persistent connector OFF BOTH cache checks must be skipped --
    the PID check must be inside the KV_PERSIST=1 block (round 80b: the recipe
    launcher owns it now)."""
    t = _T.contract_text()
    check_idx = t.index('if [ ! -f "$_kvp_pidf" ]')
    # the check must appear AFTER the KV_PERSIST=1 gate starts
    persist_idx = t.index('if [ "${KV_PERSIST:-0}" = "1" ]')
    assert check_idx > persist_idx, (
        "the cache-server PID check must be under KV_PERSIST=1"
    )