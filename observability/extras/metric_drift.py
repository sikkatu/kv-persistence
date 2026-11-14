"""metric_drift.py -- metric-name drift comparison for check.sh (H1, r89).

Compares the LIVE vllm:* names from a /metrics scrape against the
fingerprint `expected-metrics.txt`. Shared by check.sh and the unit tests
(tests/unit/test_obs_extras_drift.py) so the comparison logic is TESTED --
the old inline snippet silently produced 19 false "NEW" hits because
prometheus_client appends a `_created` series to every counter.

Rules (both sides see the SAME normalisation):
  * `*_created` series are THROW AWAY entirely (they are bookkeeping of
    prometheus_client, never a metric) -- never normalised into anything;
  * histogram/summary suffixes `_bucket` / `_count` / `_sum` fold into the
    family name on BOTH sides (expected-metrics.txt lists families).

Usage:
    from metric_drift import compare
    missing, unexpected = compare(expected_text, live_names)
"""
from __future__ import annotations

import re

_HIST_SUFFIX = re.compile(r"_(bucket|count|sum)$")


def normalize(name: str) -> str:
    """Family-normalised metric name. `_created` must be dropped by the
    CALLER (is_created) -- it is not a metric family member, and folding it
    into the family produced false 'NEW' entries (r89 H1)."""
    return _HIST_SUFFIX.sub("", name.strip())


def is_created(name: str) -> bool:
    """True for prometheus_client bookkeeping series `*_created` -- these are
    dropped BEFORE comparison on the live side, and never present in the
    fingerprint either."""
    return name.strip().endswith("_created")


def compare(expected_text: str, live_names):
    """Return (missing, unexpected) sorted tuples of family names.

    expected_text : content of expected-metrics.txt (comments with '#',
                    entry format `name [annotation]` -- only name is used)
    live_names    : iterable of metric names from a live scrape
    """
    exp = set()
    for ln in expected_text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        exp.add(normalize(ln.split()[0]))

    live = set()
    for n in live_names:
        n = n.strip()
        if not n or is_created(n):
            continue          # drop *_created entirely (H1)
        live.add(normalize(n))

    missing = tuple(sorted(exp - live))
    unexpected = tuple(sorted(live - exp))
    return missing, unexpected