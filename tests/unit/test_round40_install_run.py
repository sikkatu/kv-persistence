"""Round 40 regression tests (install-run findings):

1. setup-identity.sh must fill KVP_NUM_KV_HEADS / KVP_HEAD_DIM from the
   model's config.json (template hard-codes head_dim=128; MiMo-V2.6-Pro
   serves 192 -> gate MISMATCH).
2. The gate's MISMATCH message must carry actionable hints for l2_serde
   and KV geometry (not a bare "fix identity.env").
3. start-observability.sh must print CHANGING progress (no identical
   repeated lines) and allow >= 180s for a cold Grafana start.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_setup_identity_fills_kv_geometry():
    """Round 40: the geometry pins must be written from config.json."""
    src = (ROOT / "scripts" / "setup-identity.sh").read_text()
    assert 'setvar("KVP_NUM_KV_HEADS", geo_nkv)' in src, (
        "setup-identity.sh must write KVP_NUM_KV_HEADS from config.json"
    )
    assert 'setvar("KVP_HEAD_DIM", geo_hd)' in src, (
        "setup-identity.sh must write KVP_HEAD_DIM from config.json"
    )
    assert "num_key_value_heads" in src and "head_dim" in src, (
        "the geometry reader must pick num_key_value_heads and head_dim"
    )


def test_gate_mismatch_carries_actionable_hints():
    """Round 40: l2_serde and KV-geometry mismatches must name the fix."""
    src = (ROOT / "scripts" / "verify-engine-identity.sh").read_text()
    assert "HINT (l2_serde)" in src, (
        "the l2_serde mismatch must explain the codec-switch fix"
    )
    assert "HINT (KV geometry)" in src, (
        "the geometry mismatch must point at setup-identity.sh"
    )


def test_observability_progress_is_changing_and_patient():
    """Round 40: the Grafana wait must show changing progress (not an
    identical line every 2s) and allow >= 180s on a cold start."""
    src = (ROOT / "scripts" / "start-observability.sh").read_text()
    assert "LAST_LINE" in src, (
        "the wait loop must suppress identical repeated lines"
    )
    assert "up to 180s" in src, (
        "the cold-start budget must be >= 180s (60s was too short)"
    )
    # the old repeating pattern must be gone
    assert "grafana not ready yet" not in src, (
        "the identical 'grafana not ready yet' line must be gone"
    )