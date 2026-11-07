"""Round 52: scenario B docker stop -- no swallowed errors, honest FAIL tags.

User log: `FAIL(A): docker stop (B) failed` -- wrong scenario tag AND the
real docker error was thrown away (`>/dev/null 2>&1`). An already
stopped/removed container must NOT fail the test; a real failure must
show the docker output.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REC = ROOT / "scripts" / "test-recovery.sh"


def _read() -> str:
    return REC.read_text()


def test_pa_takes_scenario_tag():
    t = _read()
    assert 'PA(){ echo "FAIL(${1:-?}): ${*:2}"; exit 1; }' in t, (
        "PA() must print the REAL scenario tag, not a hardcoded FAIL(A)")


def test_no_bare_pa_calls_left():
    """Every PA call must carry a scenario tag as $1."""
    t = _read()
    bare = re.findall(r'\bPA (?!pre\b|A\b|B\b)"[^"]*"', t)
    assert not bare, "PA calls without a scenario tag remain: %s" % bare


def test_docker_stop_surfaces_errors():
    t = _read()
    # the swallowed form must be gone
    assert 'docker stop "$CONTAINER" >/dev/null 2>&1 ||' not in t, (
        "docker stop must not swallow its error output")
    assert '_b_stop_out' in t, "the docker stop output must be captured and shown"


def test_already_stopped_container_is_not_a_failure():
    t = _read()
    # the state probe must tolerate missing / exited / created containers
    assert 'already removed -- nothing to stop' in t
    assert 'already stopped' in t
    idx_state = t.index('_b_state=')
    idx_missing = t.index('"missing"')
    idx_exited = t.index('"exited"')
    assert idx_state < idx_missing < idx_exited, (
        "the state probe must run before the stop decision")


def test_scenario_b_keeps_correct_order():
    t = _read()
    b = t[t.index("== SCENARIO B"):]
    stop_engine = b.index('docker stop "$CONTAINER"')
    stop_cache = b.index("stop-cache.sh")
    start_cache = b.index("start-cache.sh")
    assert stop_engine < stop_cache < start_cache, (
        "scenario B: engine stop -> cache restart -> engine up")