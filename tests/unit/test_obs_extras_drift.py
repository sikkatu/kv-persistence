"""H1 review fix (r89): the metric-drift comparison is UNIT-TESTED.

The reviewer simulated prometheus_client output (every `_total` counter also
exposes a `_created` bookkeeping series) and got 19 false 'NEW' hits: the old
inline snippet FOLDED `_created` into the family name (vllm:x_created ->
vllm:x) which never matched the `_total` fingerprint entry (vllm:x_total).
Fix: `*_created` are DROPPED from the live list entirely -- never normalised.

MUTATIONs caught here:
  * folding `_created` into the family (the reviewed bug) -> false NEW/MISSING;
  * comparing raw names without _bucket/_count/_sum folding -> false NEW/MISSING;
  * dropping `_created` from the EXPECTED side only (asymmetry) is fine when
    live still has them -- the drop must happen on the live side.
"""
from __future__ import annotations

import sys
from pathlib import Path

KV = Path(__file__).resolve().parents[2]
EXTRAS = KV / "observability" / "extras"
sys.path.insert(0, str(EXTRAS))

from metric_drift import compare, is_created, normalize  # noqa: E402


def _fingerprint() -> str:
    return (EXTRAS / "expected-metrics.txt").read_text()


def test_created_series_are_detected_not_normalised():
    """is_created() must catch the bookkeeping series; normalize() must NOT
    swallow them into the family name (mutation: _created folded -> the
    reviewed 19 false NEW hits)."""
    assert is_created("vllm:generation_tokens_total_created")
    assert is_created("vllm:e2e_request_latency_seconds_created")
    assert not is_created("vllm:generation_tokens_total")
    # normalize() keeps _created visible so a caller that forgets is_created()
    # still sees the marker -- the drop decision is explicit (compare()).
    assert normalize("vllm:generation_tokens_total_created") == \
        "vllm:generation_tokens_total_created"   # NOT folded


def test_prometheus_client_output_with_created_is_clean():
    """REALISTIC prometheus_client scrape: every _total counter comes with a
    _created twin, histograms with _bucket/_count/_sum(+_created). If ONLY the
    fingerprint's own names are 'live' (+ their _created twins), there must be
    ZERO drift -- the reviewed bug reported 19 false NEW here.
    MUTATION caught: folding _created into names / not folding hist suffixes."""
    exp_text = _fingerprint()
    live = []
    for ln in exp_text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        name = ln.split()[0]
        live.append(name)
        live.append(name + "_created")          # prometheus_client bookkeeping
        if name.endswith("_total"):
            live.append(name[:-len("_total")] + "_created")  # counter _created
        if "(hist)" in ln:
            base = name
            for suf in ("_bucket", "_count", "_sum", "_created"):
                live.append(base + suf)
    missing, unexpected = compare(exp_text, live)
    assert not missing, f"no missing expected -- got {missing}"
    assert not unexpected, (
        f"prometheus_client _created/_bucket family output must be CLEAN, "
        f"got false NEW entries: {unexpected}")


def test_total_with_created_twins_gives_no_new():
    """The reviewer's exact case: fingerprint says vllm:x_total, live exports
    vllm:x_total AND vllm:x_created. `vllm:x_created` must NOT surface as NEW
    (it is bookkeeping), and vllm:x_total must match -- zero drift."""
    exp_text = "vllm:generation_tokens_total\nvllm:num_requests_running\n"
    live = ["vllm:generation_tokens_total",
            "vllm:generation_tokens_total_created",
            "vllm:generation_tokens_created",
            "vllm:num_requests_running",
            "vllm:num_requests_running_created"]
    missing, unexpected = compare(exp_text, live)
    assert missing == () and unexpected == (), (
        f"expected zero drift, got missing={missing} unexpected={unexpected}")


def test_histogram_family_folding():
    """Histograms are fingerprinted as ONE family name; live exposes
    _bucket/_count/_sum/_created expansions -- all must fold to the family.
    MUTATION caught: comparing raw names without folding."""
    exp_text = "vllm:e2e_request_latency_seconds (hist)\n"
    live = ["vllm:e2e_request_latency_seconds_bucket",
            "vllm:e2e_request_latency_seconds_count",
            "vllm:e2e_request_latency_seconds_sum",
            "vllm:e2e_request_latency_seconds_created"]
    missing, unexpected = compare(exp_text, live)
    assert missing == () and unexpected == (), (
        f"histogram family must fold cleanly, got {missing} / {unexpected}")


def test_genuine_drift_is_still_reported():
    """The detector must not go blind: a renamed metric still shows up as
    MISSING + a brand-new one as UNEXPECTED."""
    exp_text = "vllm:old_name_total\n"
    live = ["vllm:old_name_renamed_total", "vllm:brand_new_total"]
    missing, unexpected = compare(exp_text, live)
    assert missing == ("vllm:old_name_total",)
    assert unexpected == ("vllm:brand_new_total", "vllm:old_name_renamed_total")