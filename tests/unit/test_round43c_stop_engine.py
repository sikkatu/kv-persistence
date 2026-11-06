"""Round 43c regression: stop-engine.sh exists and is documented.

Stopping the vLLM engine and stopping the cache service are separate
actions; the repo previously shipped stop-cache.sh only, leaving the
engine container running after Ctrl-C with no documented stop path.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "stop-engine.sh"


def test_stop_engine_script_exists_and_is_safe():
    t = SCRIPT.read_text()
    assert "docker rm -f" in t, "the engine container must be removed, not just stopped"
    assert "stop-cache.sh" in t, (
        "the script must tell the operator the cache service is NOT touched"
    )
    assert "budget-report.sh" in t, (
        "the script must point at the verification command"
    )
    assert "nothing to stop" in t, (
        "a missing container must be a clean no-op, not an error"
    )


def test_stop_engine_documented_in_all_locales():
    for name in ("deployment.md", "deployment.ru.md", "deployment.zh.md"):
        t = (ROOT / "docs" / name).read_text()
        assert "stop-engine.sh" in t, f"{name} must document stop-engine.sh"
        assert "stop-cache.sh" in t, f"{name} must keep documenting stop-cache.sh"