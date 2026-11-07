"""Round 53: cold baseline waits for the API; request errors are actionable.

User log: `COLD baseline` fired an HTTP request immediately and died with
a raw Python traceback (`ConnectionRefusedError`) -- the engine was not
up yet (or had been stopped by a previous run). Scenarios A/B already
waited via api_ready; the cold baseline did not.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REC = ROOT / "scripts" / "test-recovery.sh"


def _read() -> str:
    return REC.read_text()


def test_cold_baseline_waits_for_api():
    t = _read()
    i_cold = t.index('== COLD baseline')
    i_ask = t.index('ask cold "$M0"', i_cold)
    seg = t[i_cold:i_ask]
    assert "api_ready" in seg, (
        "the cold baseline must wait for the API before the first request")
    assert 'PA pre "API is not up' in seg, (
        "the refusal must name the fix (start the engine first)")


def test_api_ready_announces_the_wait():
    t = _read()
    assert 'waiting for the engine API' in t, (
        "api_ready must print what it is waiting for (no silent 30-min loop)")


def test_request_helper_prints_actionable_error():
    t = _read()
    assert "REQUEST FAILED: cannot reach the engine" in t, (
        "a refused connection must print a short message, not a traceback")
    assert "except urllib.error.URLError" in t
    # the raw traceback path must be gone (unprotected urlopen of the chat call)
    assert "with urllib.request.urlopen(req,timeout=1800) as r:\n  for raw in r:" not in t, (
        "the chat request must be wrapped in try/except")