"""Round 83 regression (user report):
stop-all.sh refused to stop an observability stack that THIS repo started
when the live/obs_started marker was missing (earlier run crashed, or the
stack was already up before the marker-write path existed). start-all.sh
then saw "already running" and skipped it too -- the stack was never
stoppable. Ownership is now PROVEN by the container's compose label
(working_dir == this repo's observability/ dir) with the marker as
fallback, and start-all.sh records the marker when the running stack is
proven ours.

These are text-level checks on the stop/start scripts (the same style as
the round 81 review tests): the ownership probe, the label comparison and
the marker fallback must all exist and be ordered correctly.
"""
from __future__ import annotations
from pathlib import Path

KV = Path(__file__).resolve().parents[2]
STOP_ALL = KV / "scripts" / "stop-all.sh"
START_ALL = KV / "scripts" / "start-all.sh"


def test_stop_all_uses_compose_label_for_ownership():
    t = STOP_ALL.read_text()
    # the label probe exists and reads compose working_dir
    assert 'com.docker.compose.project.working_dir' in t, (
        "stop-all.sh must probe the compose label to prove the stack is ours")
    # ownership falls back to the recorded marker
    assert "live/obs_started" in t
    # the label comparison compares against this repo's observability dir
    assert "_obs_dir" in t, (
        "stop-all.sh must normalize the observability dir before comparing")


def test_stop_all_force_and_foreign_messages():
    t = STOP_ALL.read_text()
    assert "KVP_STOP_OBS" in t
    assert "foreign" in t, (
        "stop-all.sh must SAY when it leaves a foreign stack alone")


def test_start_all_records_marker_when_stack_is_ours():
    t = START_ALL.read_text()
    i_marker = t.index("live/obs_started")
    i_label = t.find("com.docker.compose.project.working_dir")
    assert i_label >= 0, (
        "start-all.sh must probe the compose label to detect an already-"
        "running stack that this repo owns")
    # the marker write must come after the ownership probe (round 83: the
    # marker used to be written only when the stack was JUST started)
    assert i_marker > i_label, (
        "the obs_started marker must be written after the ownership check")