"""Round 79 follow-up regression (review items 1+2+5+6): measurements,
container-user check and the per-process disk counter in test-recovery.sh.

Item 1+2 (measurements): the stream-then-repeat design broke BOTH metrics --
this vLLM build names the first stream delta 'reasoning' (not
'reasoning_content') so a field-based ttft started after the reasoning, and
the repeat after an empty stream usage POISONED cached_tokens (the first
request had already stored the prefix in the vLLM cache, so the repeat read
it back -- scenario A's "cached_tokens==0" check would false-PASS). Fix: ONE
non-stream probe with max_tokens=8 (usage always present, wall time ~ TTFT,
no repeat = clean counter).
Item 5: scenario B must verify WHO the container was created as (docker start
reuses it -- a pre-round-79 root container would bring back the root engine
while the same-UID producer pre-flight passes) and must resolve IMAGE from
live/image (the recorded pin), not the recipe template (ipc-test exit 2 would
false-fail).
Item 6: the disk counter must be the LMCache server PROCESS's own read_bytes
(/proc/<pid>/io) -- the device-wide counter proved nothing (the model shares
the disk; scenario A recorded 571 GB of unrelated reads).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
RECOVERY = KV / "scripts" / "test-recovery.sh"


@pytest.fixture(scope="module")
def recovery_text() -> str:
    assert RECOVERY.is_file(), "scripts/test-recovery.sh is missing"
    return RECOVERY.read_text()


def _ask_python(recovery_text: str) -> str:
    """Extract the embedded ask() heredoc (the request Python)."""
    m = re.search(r"<<'PYEOF' \|\| PA pre.*?\n(.*?)\nPYEOF", recovery_text, re.S)
    assert m, "ask() heredoc not found in test-recovery.sh"
    return m.group(1)


def test_ask_embedded_python_is_valid(recovery_text):
    """The heredoc Python must compile -- a broken literal would kill the
    request path at runtime."""
    compile(_ask_python(recovery_text), "ask.py", "exec")


def test_ask_is_one_non_stream_probe(recovery_text):
    """Item 2: ONE request, no stream-then-repeat. A repeat poisons
    cached_tokens (the first request stores the prefix in the vLLM cache and
    the repeat reads it back -> false PASS in scenario A)."""
    src = _ask_python(recovery_text)
    assert '"stream":False' in src or '"stream": False' in src, (
        "the probe must be NON-STREAM (stream usage is unreliable on this build)")
    assert src.count("/v1/chat/completions") == 1, (
        "exactly ONE request per probe -- a second request poisons cached_tokens")
    assert "body2" not in src, (
        "the stream-usage fallback repeat must be removed (it poisons cached_tokens)")


def test_ask_tiny_generation_budget(recovery_text):
    """Item 1: with a tiny max_tokens (1..8) the non-stream wall time IS the
    TTFT approximation -- and usage is always populated (no stream-usage
    dependency, no 'reasoning'/'reasoning_content' field guessing)."""
    src = _ask_python(recovery_text)
    m = re.search(r'"max_tokens":(\d+)', src)
    assert m, "ask() must set max_tokens"
    assert 1 <= int(m.group(1)) <= 8, (
        f"max_tokens={m.group(1)}: the probe must generate 1..8 tokens so the "
        "wall time approximates TTFT and no repeat is needed")


# ---- item 5: container user + live/image in scenario B --------------------

def test_scenario_b_checks_container_user(recovery_text):
    """docker start REUSES the container -- one created before round 79 is
    root and the same-UID pre-flight would pass while the engine is root.
    The test must check docker inspect .Config.User and refuse."""
    assert "docker inspect -f '{{.Config.User}}'" in recovery_text, (
        "scenario B must verify WHO the container was created as")
    idx = recovery_text.index("docker inspect -f '{{.Config.User}}'")
    seg = recovery_text[max(0, idx - 800):idx + 1200]
    assert "start-all.sh" in seg, (
        "the refusal must tell the operator to recreate the container via start-all.sh")


def test_scenario_b_image_from_live_pin(recovery_text):
    """IMAGE must resolve from live/image (the recorded pin) -- the recipe
    template may drift or not resolve and ipc-test.sh would exit 2, making
    the pre-flight FALSE-fail."""
    assert "live/image" in recovery_text, (
        "scenario B must resolve IMAGE from the recorded live/image pin")


# ---- item 6: per-process disk counter ------------------------------------

def test_disk_counter_is_per_process(recovery_text):
    """The counter must be the LMCache server PROCESS's own read_bytes
    (/proc/<pid>/io) -- the device-wide /sys/block counter proved nothing."""
    assert "server_read_bytes()" in recovery_text, (
        "a per-process server_read_bytes() counter is required")
    idx = recovery_text.index("server_read_bytes()")
    seg = recovery_text[idx:idx + 900]
    assert "/proc/$pid/io" in seg, "server_read_bytes must read /proc/<pid>/io"
    assert "read_bytes" in seg, "it must read the read_bytes line"


def test_measurement_windows_use_server_counter(recovery_text):
    """A and B must sample server_read_bytes AROUND the request (the engine
    restart reads the model off-disk and must not pollute the window) -- not
    the legacy device counter."""
    for tag in ("A_DISK0", "B_DISK0"):
        assert f"{tag}=$(server_read_bytes)" in recovery_text, (
            f"{tag} must sample the per-process server counter around the request")
    assert "A_DISK0=$(disk_read_bytes)" not in recovery_text, (
        "the legacy device-wide counter must not drive the A measurement")
    assert "B_DISK0=$(disk_read_bytes)" not in recovery_text, (
        "the legacy device-wide counter must not drive the B measurement")