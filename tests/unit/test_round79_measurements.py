"""Round 79 follow-up regression: MEASUREMENTS (ttft / usage / disk) in
test-recovery.sh's ask().

The hardware run showed {"ttft_s": 0, "prompt_tokens": 0, "cached_tokens": 0}
for every request -- the ONLY machine-readable reuse counters were empty,
so even a working cache would report INCONCLUSIVE. Root cause: max_tokens=32
is eaten by the model's REASONING tokens (MiMo has a reasoning parser), so
delta.content never arrives (ttft stayed 0) and the stream can end with an
EMPTY usage. Round 79 follow-up:
* max_tokens leaves room for reasoning (>= 128);
* ttft fires on the first VISIBLE token whatever field carries it
  (content / reasoning_content / tool_calls);
* an empty stream usage falls back to a NON-STREAM request (usage is always
  populated there).
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
    """The heredoc Python must compile -- a broken literal (stray newline in
    a string) would kill the request path at runtime."""
    compile(_ask_python(recovery_text), "ask.py", "exec")


def test_ask_max_tokens_leaves_room_for_reasoning(recovery_text):
    """With max_tokens=32 the model's REASONING tokens eat the budget (MiMo
    has a reasoning parser), delta.content never arrives (ttft stayed 0) and
    the stream can end with an EMPTY usage -- exactly the observed
    prompt_tokens: 0 / ttft_s: 0. The budget must be larger."""
    src = _ask_python(recovery_text)
    m = re.search(r'"max_tokens":(\d+)', src)
    assert m, "ask() must set max_tokens"
    assert int(m.group(1)) >= 128, (
        f"max_tokens={m.group(1)} is eaten by reasoning tokens -- usage/ttft break")


def test_ask_ttft_tracks_any_visible_delta(recovery_text):
    """ttft must fire on the first VISIBLE token whatever field carries it --
    content, reasoning_content or tool_calls (reasoning parsers emit
    reasoning_content first, so a content-only check yields ttft=0)."""
    src = _ask_python(recovery_text)
    for field in ("content", "reasoning_content", "tool_calls"):
        assert field in src, f"ttft capture must consider delta.{field}"


def test_ask_usage_fallback_to_non_stream(recovery_text):
    """If the stream ends with an EMPTY usage the test must fall back to a
    non-stream request (usage is always populated there) instead of
    reporting an empty metric and going INCONCLUSIVE on a working cache."""
    src = _ask_python(recovery_text)
    assert '"stream":False' in src or '"stream": False' in src, (
        "ask() must have a non-stream usage fallback")
    assert "usage fallback" in src, (
        "the fallback must be labelled (honest diagnostics)")