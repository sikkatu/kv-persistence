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


# ================ check.sh itself (r89d: regressions in the SCRIPT) ======
CHECK = EXTRAS / "check.sh"


def _run_check(args, checkout=None):
    """Run check.sh --static. checkout: optional repo copy (for mutations) --
    must execute THAT copy's check.sh (HERE/EXTRAS are derived from the
    script path, so running the original script would inspect the wrong
    template)."""
    import subprocess
    check = (checkout or EXTRAS.parents[1]) / "observability" / "extras" / "check.sh"
    return subprocess.run(["bash", str(check), *args],
                          capture_output=True, text=True, timeout=60)


def test_check_static_clean_on_real_repo():
    """check.sh --static on a HEALTHY checkout must exit 0 (regression from
    ce9d4a0: the relay-contract regex `[^}]*` broke on the nested
    `if (...) { return 405; }` block and a healthy template reported FAIL).
    MUTATION caught: reverting to the [^}]* regex version."""
    r = _run_check(["--static"])
    assert r.returncode == 0, f"static check must be clean:\n{r.stdout}{r.stderr}"
    assert "relay contract" in r.stdout
    assert "FAIL" not in r.stdout, f"no FAIL lines expected:\n{r.stdout}"


def test_check_static_fails_on_broken_template(tmp_path):
    """Template mutations must flip the static check to exit 1:
    (a) drop the proxy_pass line in location = /metrics
    (b) drop the `return 404` from the catch-all location /
    MUTATION caught: a check that only greps fragments without understanding
    the block structure, or ignores a missing directive."""
    import shutil
    repo = tmp_path / "repo"
    # copy ONLY the tree check.sh inspects (observability/) -- the .git dir of
    # the live checkout may contain unreadable object files and copytree dies
    # on them; check.sh resolves EXTRAS/ROOT from its own path so this copy is
    # a self-contained checkout for the static checks.
    shutil.copytree(EXTRAS.parents[1] / "observability", repo / "observability")
    tpl = repo / "observability" / "extras" / "relay" / "nginx.conf.template"
    original = tpl.read_text()

    # (a) remove proxy_pass
    tpl.write_text(original.replace("proxy_pass", "# proxy_pass disabled"))
    r = _run_check(["--static"], checkout=repo)
    assert r.returncode == 1, (
        f"removing proxy_pass must FAIL the static check, got rc={r.returncode}:\n{r.stdout}")
    assert "proxy_pass" in r.stdout

    # (b) remove return 404
    tpl.write_text(original.replace("return 404;", "# return 404 removed;"))
    r = _run_check(["--static"], checkout=repo)
    assert r.returncode == 1, (
        f"removing return 404 must FAIL the static check, got rc={r.returncode}:\n{r.stdout}")
    assert "404" in r.stdout


def _write_drift_live(tmp_path, names):
    f = tmp_path / "live-metrics.txt"
    f.write_text("\n".join(names) + "\n")
    return f


def test_check_drift_live_zero_on_prometheus_client_output(tmp_path):
    """The drift path THROUGH check.sh must consume metric_drift.compare --
    the same import the live mode uses -- and give zero drift on a realistic
    prometheus_client scrape (fingerprints + _created/_bucket twins).
    MUTATION caught: patching `from metric_drift import compare` inside
    check.sh (its inline-code import is what live mode executes)."""
    live = []
    for ln in _fingerprint().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        name = ln.split()[0]
        live += [name, name + "_created"]
        if "(hist)" in ln:
            live += [name + s for s in ("_bucket", "_count", "_sum", "_created")]
    f = _write_drift_live(tmp_path, live)
    r = _run_check([f"--drift-live={f}"])
    assert r.returncode == 0, f"drift check must be clean:\n{r.stdout}{r.stderr}"
    assert "NOT live any more" not in r.stdout, "no false missing"
    assert "NEW live metric" not in r.stdout, "no false NEW on _created twins"


def test_check_drift_live_detects_real_drift(tmp_path):
    """The script path must still REPORT real drift (WARN lines + rc 0, as
    the drift comparison is informative)."""
    f = _write_drift_live(tmp_path, ["vllm:gone_from_live_total", "vllm:brand_new_total"])
    r = _run_check([f"--drift-live={f}"])
    assert r.returncode == 0
    assert "NEW live metric not in the fingerprint" in r.stdout
    assert "NOT live any more" in r.stdout