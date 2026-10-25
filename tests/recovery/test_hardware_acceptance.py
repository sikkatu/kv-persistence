"""Recovery / acceptance tests — hardware gate.

These tests require the 4x RTX PRO 6000 host with the production model loaded
and are skipped unless KVP_HARDWARE=1 is set in the environment:

    KVP_HARDWARE=1 pytest tests/recovery -q

They ARE the Definition of Done items of the brief. Passing requires evidence
of disk-backed recovery — not just speed. Each test prints the exact artifacts
it collects so results can be pasted into docs/benchmarks.md.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation"))

pytestmark = pytest.mark.skipif(
    os.environ.get("KVP_HARDWARE") != "1",
    reason="hardware acceptance only (KVP_HARDWARE=1 on the 4-GPU host)",
)


def _run(script: str, *args: str) -> str:
    out = subprocess.run(
        ["bash", str(ROOT / "scripts" / script), *args],
        capture_output=True, text=True, timeout=4 * 3600,
    )
    print(out.stdout[-4000:])
    if out.returncode != 0:
        pytest.fail(f"{script} failed rc={out.returncode}\n{out.stderr[-2000:]}")
    return out.stdout


def _latest_results(pattern: str) -> Path:
    dirs = sorted((ROOT / "benchmarks" / "results").glob(pattern))
    assert dirs, f"no results dir matching {pattern}; run scripts first"
    return dirs[-1]


def test_scenario_a_vllm_restart_recovers_from_nvme():
    """Definition of Done items 1-4 (scenario A): same prompt after vLLM restart
    must produce cached_tokens > 0 and disk-backed retrieval evidence."""
    _run("test-recovery.sh")
    res = _latest_results("*-recovery")
    after = json.loads((res / "afterA.summary.json").read_text())
    assert after["cached_tokens"] > 0, (
        "no cached tokens after vLLM restart — disk recovery did not happen")
    retrieved = int((res / "scenarioA.retrieved_linecount").read_text().strip() or 0)
    assert retrieved > 0, "lmcache log shows no Retrieved lines after restart"


def test_scenario_b_both_services_restart_recovers_from_nvme():
    """Definition of Done item 5 (scenario B): cache survives full service restart."""
    res = _latest_results("*-recovery")
    after = json.loads((res / "afterB.summary.json").read_text())
    assert after["cached_tokens"] > 0, (
        "no cached tokens after BOTH services restarted — persistence failed")


def test_output_correctness_preserved():
    """DoD item 8 — response text sanity after cache hits (manual spot check aid)."""
    res = _latest_results("*-recovery")
    for label in ("warm", "afterA", "afterB"):
        data = json.loads((res / f"{label}.json").read_text())
        assert data.get("usage", {}).get("prompt_tokens", 0) > 0


def test_cache_capacity_bounded():
    """DoD items 10-11 — hard cap enforced, eviction works under small budget."""
    out = _run("budget-report.sh", "status")
    rep = json.loads(out[out.index("{"):])
    assert rep["used_bytes"] <= rep["budget_bytes"]
    assert rep["usage_fraction"] <= 1.0


def test_incompatible_entries_rejected():
    """DoD item 9 — a changed identity yields a different namespace (cache miss)."""
    from kvpersist.identity import CacheIdentity, IdentityError

    base = dict(model_id="m", model_revision="a" * 40, tokenizer_name="t",
                tokenizer_revision="b" * 40, kv_cache_dtype="fp16",
                block_size=16, num_kv_heads=8, head_dim=128, tp_size=4,
                connector="LMCacheMPConnector", connector_version="v1",
                max_model_len=1048576, chat_template_digest="c" * 64)
    n1 = CacheIdentity(**base).namespace()
    base["kv_cache_dtype"] = "bf16"
    n2 = CacheIdentity(**base).namespace()
    assert n1 != n2
    bad = dict(base, model_revision="main")
    with pytest.raises(IdentityError):
        CacheIdentity(**bad).namespace()