"""Round 45i regression: PRODUCER_USER must be a LITERAL assignment.

bash does NOT treat ${var:+NAME=val} as an assignment -- the expanded word
becomes a command name ("PRODUCER_USER=1000:1000: command not found").
The fix passes the assignment literally; an empty value is the ipc-test
default (root producer). This test catches the trap: the ipc-test call
must contain a literal PRODUCER_USER=, never a ${..:+..} expansion in
that position.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = _T.recipe_launcher()  # Round 80b: recipe owns the engine launch


def test_producer_user_is_literal_assignment():
    t = _T.contract_text()
    idx = t.index('PRODUCER_USER="$_ipc_pu"')
    call_line = t[t.rindex("\n", 0, idx) + 1: t.index("\n", idx)]
    assert 'PRODUCER_USER="$_ipc_pu"' in call_line, (
        f"PRODUCER_USER must be a literal assignment, got: {call_line!r}"
    )
    assert "${_ipc_pu:+" not in call_line, (
        "parameter expansion in the assignment position is the bash trap"
    )


def test_empty_producer_user_is_safe_default():
    """An empty PRODUCER_USER must be harmless -- ipc-test treats it as
    the default (root producer), so no conditional is needed."""
    t = _T.contract_text()
    ipc = ROOT / "scripts" / "ipc-test.sh"
    it = ipc.read_text()
    assert 'PRODUCER_USER="${PRODUCER_USER:-}"' in it or (
        'PRODUCER_USER="${PRODUCER_USER:-}"' in it
    ), "ipc-test.sh must default PRODUCER_USER to empty (root producer)"