"""Round 47b: observability metrics auto-detect (user: why the flag?).

The metrics used to need USE_OBSERVABILITY=1 on every cache start --
pointless friction. start-cache.sh now AUTO-DETECTS an OTel collector at
127.0.0.1:4320 and enables telemetry on its own; explicit opt-out is
KVP_NO_OBSERVABILITY=1.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START_CACHE = ROOT / "scripts" / "start-cache.sh"


def test_auto_detect_block_exists_in_order():
    t = START_CACHE.read_text()
    assert "KVP_NO_OBSERVABILITY" in t, "explicit opt-out must exist"
    assert "/dev/tcp/127.0.0.1/4320" in t, "the collector probe must target 4320"
    assert "metrics enabled automatically" in t, "the operator must SEE the auto-detect"
    # resolution order: flag first, then auto-detect only when nothing set
    flag_idx = t.index("USE_OBSERVABILITY")
    probe_idx = t.index("/dev/tcp/127.0.0.1/4320")
    optout_guard_idx = t.index('KVP_NO_OBSERVABILITY:-0')
    assert flag_idx < probe_idx, "explicit flag must be checked before the probe"
    assert optout_guard_idx < probe_idx, "the opt-out must guard the probe"


def test_auto_detect_is_gated_by_opt_out():
    t = START_CACHE.read_text()
    guard = t[t.index('KVP_NO_OBSERVABILITY:-0'):]
    guard = guard[:guard.index("fi") + 2]
    assert '!= "1"' in guard, (
        "the probe must be skipped when KVP_NO_OBSERVABILITY=1")


def test_docs_describe_auto_detect():
    for name, needle in (
        ("README.md", "metrics flow automatically"),
        ("README.ru.md", "метрики идут сами"),
        ("README.zh.md", "指标自动流动"),
        ("observability/README.md", "no flag needed"),
        ("observability/README.ru.md", "флаг не нужен"),
        ("observability/README.zh.md", "无需任何标志"),
    ):
        t = (ROOT / name).read_text()
        assert needle in t, "%s must describe the auto-detect (%r)" % (name, needle)
        assert "KVP_NO_OBSERVABILITY" in t, "%s must document the opt-out" % name