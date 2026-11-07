"""Round 45g regression: PRODUCER_USER (same-UID path) in ipc-test.sh.

Reviewer proposal B: run BOTH processes with the SAME UID (--user
$(id -u):$(id -g) on the engine container) to avoid root entirely. The
GPU-level probe for that: run the producer container with the same UID as
the host consumer (PRODUCER_USER=...). PASS means the transfer works
without root; the engine launcher path still needs --user/-e HOME fixes
(HF cache at /root/.cache/huggingface, triton/flashinfer expect /root) --
and the verdict must say so honestly.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IPC_TEST = ROOT / "scripts" / "ipc-test.sh"


def test_producer_user_adds_user_flag():
    t = IPC_TEST.read_text()
    assert "PRODUCER_USER" in t, "the script must support the same-UID test"
    assert '--user "$PRODUCER_USER"' in t, (
        "the producer container must run with the requested UID:GID"
    )
    assert "same-UID" in t, "the output must label the same-UID run"


def test_same_uid_verdict_is_honest():
    """PASS must now say the same-UID path IS wired (round 46: the stale
    "NOT yet wired" text is gone -- the launcher supports KVP_SAME_UID=1);
    FAIL must say matching UIDs are not enough."""
    t = IPC_TEST.read_text()
    assert "SAME-UID result: PASS" in t
    assert "NOT yet wired" not in t, (
        "the path IS wired since round 45h -- the stale warning must be gone"
    )
    assert "This path IS wired" in t and "KVP_SAME_UID=1" in t, (
        "a PASS must point to the launcher flag that uses this path"
    )
    assert "SAME-UID result: FAIL" in t
    assert "even matching UIDs is not enough" in t
    assert "PRODUCER_USER=\\$(id -u):\\$(id -g)" in t, (
        "the fail path must show the exact command to test same-UID"
    )


def test_default_run_is_root_producer():
    """Without PRODUCER_USER the producer must run as before (root in the
    container) -- the proven baseline."""
    t = IPC_TEST.read_text()
    assert '${PRODUCER_USER:+--user "$PRODUCER_USER"}' in t, (
        "--user must be conditional (empty = root producer, as before)"
    )