# SPDX-License-Identifier: Apache-2.0
"""r2 P1-2: the custom LMCache build identity is verified in the gate.

The wheel version alone cannot tell the patched build from the upstream
nightly (no-local-version), so the deployed container must carry
/etc/kvp-lmcache-identity.json and the gate must REFUSE (MISMATCH) on any
drift: missing file, missing key, patch sha mismatch, base mismatch.
"""

from __future__ import annotations

# Standard
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
PATCH_DIR = ROOT / "vendor" / "patches" / "lmcache"
PY = sys.executable

sys.path.insert(0, str(IMPL))


def _good_ident(**over) -> dict:
    ident = {
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr6551_status": "in-base",
        "pr4136_patch_sha256": hashlib.sha256(
            (PATCH_DIR / "4136-mp-retrieve-abort.patch").read_bytes()
        ).hexdigest(),
        "pr5972_patch_sha256": hashlib.sha256(
            (PATCH_DIR / "5972-incomplete-kv-objects.patch").read_bytes()
        ).hexdigest(),
        "wheel_sha256": "deadbeef",
    }
    ident.update(over)
    return ident


def test_valid_identity_passes():
    from kvpersist.lmc_identity import verify_identity

    ok, msgs = verify_identity(_good_ident(), PATCH_DIR)
    assert ok, msgs


def test_missing_key_is_mismatch():
    from kvpersist.lmc_identity import verify_identity

    ident = _good_ident()
    del ident["pr4136_patch_sha256"]
    ok, msgs = verify_identity(ident, PATCH_DIR)
    assert not ok, "a missing key must be a MISMATCH, not a silent pass"
    assert any("missing key" in m for m in msgs)


def test_patch_sha_drift_is_mismatch():
    from kvpersist.lmc_identity import verify_identity

    ok, msgs = verify_identity(
        _good_ident(pr5972_patch_sha256="0" * 64), PATCH_DIR
    )
    assert not ok, "a drifted #5972 patch sha must be a MISMATCH"
    assert any("5972-incomplete-kv-objects.patch" in m for m in msgs)


def test_base_drift_is_mismatch_when_expected():
    from kvpersist.lmc_identity import verify_identity

    ok, msgs = verify_identity(
        _good_ident(lmcache_source_commit="f" * 40),
        PATCH_DIR,
        expected_base="07f1c6bb642f4772606a36eab730ae009ddce08f",
    )
    assert not ok
    assert any("base" in m for m in msgs)


# ---- CLI contract used by the gate ------------------------------------------------
def _run_cli(ident: dict, *extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "kvpersist.lmc_identity", "-", str(PATCH_DIR), *extra],
        input=json.dumps(ident),
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(IMPL), "PATH": "/usr/bin:/bin"},
        timeout=30,
    )


def test_cli_exit0_on_valid_identity():
    r = _run_cli(_good_ident())
    assert r.returncode == 0, r.stderr
    assert "identity OK" in r.stdout


def test_cli_exit4_on_drift():
    r = _run_cli(_good_ident(pr4136_patch_sha256="1" * 64))
    assert r.returncode == 4
    assert "MISMATCH" in r.stderr


def test_cli_exit4_on_missing_file_key_via_empty_json():
    r = _run_cli({})
    assert r.returncode == 4
    assert "MISMATCH" in r.stderr


def test_gate_script_contains_identity_check():
    """The engine identity gate must call the identity verifier for a running
    container (container check only -- --check-config-only has no container)."""
    src = (ROOT / "scripts" / "verify-engine-identity.sh").read_text()
    assert "kvpersist.lmc_identity" in src, (
        "verify-engine-identity.sh must verify the custom LMCache build identity"
    )
    assert "/etc/kvp-lmcache-identity.json" in src


def test_build_image_bakes_identity_file():
    """STATIC (supplementary) check -- sitting next to the behavioural
    test_build_image_runs.py, which actually RUNS build-image.sh. A grep of the
    script text alone does not prove the script works (r3 P0-1 point 4:
    reorder/typo bugs only show up at run time)."""
    src = (ROOT / "scripts" / "build-image.sh").read_text()
    assert "kvp-lmcache-identity.json" in src, (
        "build-image.sh must bake the identity file into the image"
    )
    assert "COPY kvp-lmcache-identity.json /etc/kvp-lmcache-identity.json" in src