"""Unit tests: budget accounting, quotas, eviction ordering. No GPU, no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))

import pytest

from kvpersist.budget import BudgetError, BudgetLedger, GB
from kvpersist.retention import (
    EntryStats,
    eviction_report,
    rank_for_eviction,
    select_victims,
)

NOW = 1_000_000.0


def test_budget_admit_and_release():
    led = BudgetLedger(1000)
    led.admit("ns-a", 400)
    assert led.usage("ns-a") == 400
    assert led.free_bytes() == 600
    led.release("ns-a", 100)
    assert led.total_usage() == 300


def test_hard_budget_never_exceeded():
    led = BudgetLedger(1000)
    led.admit("ns-a", 900)
    assert led.can_admit("ns-b", 100)
    assert not led.can_admit("ns-b", 101)
    with pytest.raises(BudgetError):
        led.admit("ns-b", 101)
    assert led.usage("ns-b") == 0
    assert led.report()["rejected_writes"] == 1


def test_soft_pressure_flag():
    led = BudgetLedger(1000, soft_fraction=0.9)
    led.admit("ns", 899)
    assert not led.in_soft_pressure()
    led.admit("ns", 1)
    assert led.in_soft_pressure()


def test_quota_enforced():
    led = BudgetLedger(10 * GB, quotas={"ns-a": 1 * GB})
    led.admit("ns-a", 1 * GB - 1)
    assert not led.can_admit("ns-a", 2)
    with pytest.raises(BudgetError):
        led.admit("ns-a", 2)
    # other namespaces unaffected by ns-a's quota
    assert led.can_admit("ns-b", 9 * GB)


def test_concurrent_admits_cannot_exceed_budget():
    import threading

    led = BudgetLedger(1000)
    errors = []

    def worker():
        try:
            led.admit("ns", 200)
        except BudgetError:
            errors.append(1)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert led.total_usage() <= 1000
    assert len(errors) == 5  # exactly half admitted


def test_sync_from_sizes_and_report():
    led = BudgetLedger(100)
    led.sync_from_sizes({"kvp-a": 60, "kvp-b": 30})
    rep = led.report()
    assert rep["used_bytes"] == 90
    assert rep["free_bytes"] == 10
    assert rep["per_namespace_bytes"] == {"kvp-a": 60, "kvp-b": 30}


def test_evict_candidates_targets_fraction():
    led = BudgetLedger(100, soft_fraction=0.9, target_fraction=0.8)
    led.sync_from_sizes({"kvp-a": 70, "kvp-b": 25})
    cands = led.evict_candidates()
    assert sum(cands.values()) == 15  # 95 -> 80


def _t(**over):
    base = dict(tokens=10, bytes_on_disk=100, hits=0, last_access_ts=NOW)
    base.update(over)
    key = over.get("key", base.get("key", "k"))
    base.setdefault("key", key)
    return EntryStats(**base)


def test_retention_protects_valuable_long_prefixes():
    entries = [
        EntryStats(key="long-hot", tokens=100_000, bytes_on_disk=1000, hits=50,
                   last_access_ts=NOW - 3600),
        _t(key="fresh-junk"),
        _t(key="stale-junk", last_access_ts=NOW - 90 * 86400),
        _t(key="short-cold", tokens=500, bytes_on_disk=500, hits=1,
           last_access_ts=NOW - 30 * 86400),
    ]
    # request just enough to drop ONE junk entry: long-hot must never be chosen
    victims = select_victims(entries, bytes_to_free=100, now=NOW,
                             min_keep_tokens=10_000)
    keys = [v.key for v in victims]
    assert "long-hot" not in keys          # protected: long AND reused
    assert keys[0] in ("stale-junk", "short-cold", "fresh-junk")


def test_retention_order_stale_before_recent():
    old = _t(key="old", last_access_ts=NOW - 90 * 86400)
    new = _t(key="new", last_access_ts=NOW)
    order = [e.key for e in rank_for_eviction([new, old], now=NOW)]
    assert order == ["old", "new"]
    # minimal victim set for exactly one entry keeps only the worst
    assert [v.key for v in select_victims([new, old], 100, now=NOW)] == ["old"]


def test_retention_order_rarely_reused_before_frequent():
    rare = _t(key="rare", tokens=1000)
    frequent = _t(key="frequent", tokens=1000, hits=30, last_access_ts=NOW)
    order = [e.key for e in rank_for_eviction([frequent, rare], now=NOW)]
    assert order == ["rare", "frequent"]


def test_retention_prefers_big_useless_over_small_useless():
    big = _t(key="big", tokens=10, bytes_on_disk=1000)
    small = _t(key="small", tokens=10, bytes_on_disk=100)
    order = [e.key for e in rank_for_eviction([small, big], now=NOW)]
    assert order[0] == "big"   # a huge never-reused entry cannot displace useful cache


def test_long_reused_prefix_survives_full_crisis():
    hot = EntryStats(key="long-hot", tokens=200_000, bytes_on_disk=1000, hits=100,
                     last_access_ts=NOW - 3600)
    junk = [_t(key=f"junk{i}", last_access_ts=NOW - 40 * 86400) for i in range(20)]
    victims = select_victims(junk + [hot], bytes_to_free=2000, now=NOW,
                             min_keep_tokens=100_000)
    assert hot not in victims       # 20*100 byte junk covers the request entirely


def test_eviction_report_has_no_prompt_text():
    entries = [
        EntryStats(key="k1", tokens=10, bytes_on_disk=100, hits=0, last_access_ts=NOW),
        EntryStats(key="k2", tokens=100, bytes_on_disk=100, hits=2, last_access_ts=NOW),
    ]
    victims = select_victims(entries, bytes_to_free=100, now=NOW)
    rep = eviction_report(entries, victims, now=NOW)
    assert rep["evicted_keys"] == ["k1"]
    assert rep["kept_valuable_prefixes"] == ["k2"]