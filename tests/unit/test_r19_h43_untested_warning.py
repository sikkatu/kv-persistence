# SPDX-License-Identifier: Apache-2.0
"""r19 H4.3: verify-engine-identity.sh must LOUDLY warn about an untested
LMCache wheel.

When the image was baked with KVP_ALLOW_UNTESTED_WHEEL=1, the identity file
carries `untested_wheel: "untested-wheel"` -- the wheel never ran the test
gates. That is a deliberate operator mode (no refusal), but verify-engine-
identity.sh and the launch must SEE it: before r19 the marker was invisible
and the image passed the gate silently.

Fix: verify-engine-identity.sh prints `WARNING: this image carries an
UNTESTED LMCache wheel` when the baked identity has a NON-EMPTY
`untested_wheel`; an empty field or a missing field (old images) prints
NOTHING.

Test (the REAL script, docker stub serving the identity JSON):
  - untested_wheel="untested-wheel" -> the WARNING line is present;
  - untested_wheel="" -> no warning;
  - the field absent -> no warning.
Mutation: dropping the warning -> (a) RED.
"""
from __future__ import annotations
import hashlib
import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "verify-engine-identity.sh"
PATCH_DIR = ROOT / "vendor" / "patches" / "lmcache"


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _identity_json(untested: str | None) -> str:
    """A baked identity matching THIS repo's real patches (the gate verifies
    them via kvpersist.lmc_identity), with the untested marker as given."""
    ident = {
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr4136_patch_sha256": _sha(PATCH_DIR / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(PATCH_DIR / "5972-incomplete-kv-objects.patch"),
        "wheel": "lmcache-0.5.8rc1.post48+kvp.60dce759ea93-cp312-cp312-linux_x86_64.whl",
        "wheel_sha256": "a" * 64,
        "wheel_version": "0.5.8rc1.post48+kvp.60dce759ea93",
    }
    if untested is not None:
        ident["untested_wheel"] = untested
    return json.dumps(ident)


def _run_gate(tmp_path: pathlib.Path, untested: str | None):
    """REAL verify-engine-identity.sh with a docker stub that serves the
    identity JSON (exec cat) and empty container args (inspect)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    body = tmp_path / "ident.json"
    body.write_text(_identity_json(untested))
    (bin_dir / "docker").write_text(f"""#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
case "$cmd" in
  exec) cat "{body}" ;;
  inspect) echo "[]" ;;
  *) exit 0 ;;
esac
""")
    (bin_dir / "docker").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH','')}")
    for var in ("MODEL_DIR", "KVP_MODEL_CONFIG", "KVP_SKIP_LMC_IDENTITY"):
        env.pop(var, None)
    return subprocess.run(["bash", str(GATE), "test-container"],
                          capture_output=True, text=True, timeout=60,
                          env=env, cwd=str(tmp_path))


def test_untested_wheel_marker_prints_warning(tmp_path):
    """(a) a non-empty untested_wheel -> the loud WARNING."""
    r = _run_gate(tmp_path, untested="untested-wheel")
    out = r.stdout + r.stderr
    assert "WARNING: this image carries an UNTESTED LMCache wheel" in out, (
        f"the gate must warn about an untested wheel:\n{out[-1500:]}")
    assert "UNTESTED" in out, out[-800:]


def test_empty_marker_prints_no_warning(tmp_path):
    """(b) an empty untested_wheel field -> no warning."""
    r = _run_gate(tmp_path, untested="")
    out = r.stdout + r.stderr
    assert "UNTESTED LMCache wheel" not in out, (
        f"an empty marker must stay silent:\n{out[-1500:]}")


def test_missing_marker_prints_no_warning(tmp_path):
    """(c) the field absent (old images) -> no warning."""
    r = _run_gate(tmp_path, untested=None)
    out = r.stdout + r.stderr
    assert "UNTESTED LMCache wheel" not in out, (
        f"a missing marker must stay silent (old image):\n{out[-1500:]}")


def test_mutation_no_warning_is_red(tmp_path):
    """Mutation detector: the script must contain the warning line (dropping
    it makes (a) RED)."""
    src = GATE.read_text()
    assert "UNTESTED LMCache wheel" in src, (
        "verify-engine-identity.sh must carry the untested-wheel warning")
    # the warning must be conditional on the marker, not unconditional
    assert "_untested" in src, (
        "the warning must be conditional on the untested_wheel field")