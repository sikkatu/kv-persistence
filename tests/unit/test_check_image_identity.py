# SPDX-License-Identifier: Apache-2.0
"""r2 P0-1: the image about to launch MUST carry the custom LMCache identity.

After the obsolete downstream race-guard removal (recipes 799d634), a stock
vLLM image silently runs upstream LMCache WITHOUT #4136/#5972. The launchers
must refuse it. These tests pin the pin-format contract (digest OR bare image
ID) and the label-verification logic (missing labels -> refuse, drifted patch
sha -> refuse, matching labels -> accept).
"""

from __future__ import annotations

# Standard
import hashlib
import json
import pathlib
import subprocess
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
PATCH_DIR = ROOT / "vendor" / "patches" / "lmcache"
PY = sys.executable

sys.path.insert(0, str(IMPL))

from kvpersist import check_image_identity as cii  # noqa: E402

HEX64 = "a" * 64
GOOD_SPEC = f"kvp-lmcache:latest@sha256:{HEX64}"
GOOD_ID_SPEC = f"sha256:{HEX64}"


def _good_labels() -> dict:
    return {
        cii.LABEL_BASE: "vllm/vllm-openai:nightly-8e2beaa3@sha256:" + "b" * 64,
        cii.LABEL_4136: hashlib.sha256(
            (PATCH_DIR / "4136-mp-retrieve-abort.patch").read_bytes()
        ).hexdigest(),
        cii.LABEL_5972: hashlib.sha256(
            (PATCH_DIR / "5972-incomplete-kv-objects.patch").read_bytes()
        ).hexdigest(),
    }


# ---- pin-format contract ----------------------------------------------------
def test_pin_format_accepts_digest_and_image_id():
    for spec in (GOOD_SPEC, GOOD_ID_SPEC):
        ok, msg = cii.is_pinned_image_spec(spec)
        assert ok, f"{spec} must be accepted: {msg}"


def test_pin_format_rejects_floating_tags():
    ok, msg = cii.is_pinned_image_spec("")
    assert not ok and "empty" in msg, "empty spec must be rejected with an 'empty' hint"
    for spec in ("kvp-lmcache:latest", "vllm/vllm-openai:nightly-8e2beaa3", "sha256:too-short"):
        ok, msg = cii.is_pinned_image_spec(spec)
        assert not ok, f"{spec!r} must be rejected"
        assert "pinned by digest" in msg


# ---- label verification (docker mocked) ------------------------------------
def _with_labels(labels: dict | None):
    def fake_read(image: str):
        return labels
    return mock.patch.object(cii, "read_image_labels", fake_read)


def test_accepts_fully_labeled_custom_image():
    with _with_labels(_good_labels()):
        ok, msgs = cii.verify_custom_image(GOOD_ID_SPEC, PATCH_DIR)
    assert ok, msgs


def test_refuses_stock_image_without_labels():
    with _with_labels({}):  # stock vllm-openai has none of our labels
        ok, msgs = cii.verify_custom_image(GOOD_SPEC, PATCH_DIR)
    assert not ok
    assert any("NOT the custom LMCache" in m for m in msgs), msgs


def test_refuses_when_docker_cannot_inspect():
    with _with_labels(None):
        ok, msgs = cii.verify_custom_image(GOOD_SPEC, PATCH_DIR)
    assert not ok
    assert any("docker cannot inspect" in m for m in msgs)


def test_refuses_drifted_patch_sha():
    labels = _good_labels()
    labels[cii.LABEL_5972] = "0" * 64
    with _with_labels(labels):
        ok, msgs = cii.verify_custom_image(GOOD_SPEC, PATCH_DIR)
    assert not ok
    assert any("5972-incomplete-kv-objects.patch" in m for m in msgs)


def test_refuses_drifted_base_label():
    labels = _good_labels()
    base = labels[cii.LABEL_BASE]
    with _with_labels(labels):
        ok, msgs = cii.verify_custom_image(GOOD_SPEC, PATCH_DIR, expect_base="other@sha256:" + "c" * 64)
    assert not ok
    assert any("base label" in m for m in msgs)


def test_rejects_bad_format_before_touching_docker():
    ok, msgs = cii.verify_custom_image("kvp-lmcache:latest", PATCH_DIR)
    assert not ok
    assert any("pinned by digest" in m for m in msgs)


# ---- CLI contract used by env.sh / launchers --------------------------------
def test_cli_exit4_on_stock_image(tmp_path):
    stub = tmp_path / "bin"
    stub.mkdir()
    docker = stub / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "# stock image: Labels == null\n"
        "echo null\n"
    )
    docker.chmod(0o755)
    r = subprocess.run(
        [PY, "-m", "kvpersist.check_image_identity", GOOD_ID_SPEC, str(PATCH_DIR)],
        capture_output=True, text=True,
        env={"PYTHONPATH": str(IMPL), "PATH": f"{stub}:/usr/bin:/bin"},
        timeout=30,
    )
    assert r.returncode == 4, r.stdout + r.stderr
    assert "MISMATCH" in r.stderr


def test_cli_exit0_on_custom_image(tmp_path):
    stub = tmp_path / "bin"
    stub.mkdir()
    docker = stub / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f"echo '{json.dumps(_good_labels())}'\n"
    )
    docker.chmod(0o755)
    r = subprocess.run(
        [PY, "-m", "kvpersist.check_image_identity", GOOD_ID_SPEC, str(PATCH_DIR)],
        capture_output=True, text=True,
        env={"PYTHONPATH": str(IMPL), "PATH": f"{stub}:/usr/bin:/bin"},
        timeout=30,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "verified" in r.stdout


# ---- launcher wiring (static contract) -------------------------------------
def test_launchers_require_custom_image_check():
    for sh in ("run-tp4-hotsplit.sh", "run-tp4-uva.sh"):
        src = (ROOT.parent / "recipes" / "recipes" / "rtx-pro-6000" /
               "mimo-v2.6-pro-hotsplit" / "scripts" / sh).read_text()
        assert ("check_image_identity" in src or "_kvp_require_custom_image" in src
                or "verify-image-custom.sh" in src), (
            f"{sh} must verify the custom LMCache image before launching"
        )


def test_start_cache_requires_custom_image_check():
    src = (ROOT / "scripts" / "start-cache.sh").read_text()
    assert "check_image_identity" in src or "_kvp_require_custom_image" in src or \
           "verify-image-custom.sh" in src, (
        "start-cache.sh must verify the custom LMCache image in docker mode"
    )


def test_start_cache_defaults_to_docker_mode():
    """r2 P0-1 (review): the shared MP cache server must default to the custom
    derivative image -- defaulting to `host` silently ran upstream LMCache
    WITHOUT the #4136/#5972 fixes."""
    src = (ROOT / "scripts" / "start-cache.sh").read_text()
    assert 'KVP_LAUNCH_MODE="${KVP_LAUNCH_MODE:-docker}"' in src, (
        "start-cache.sh must default KVP_LAUNCH_MODE to docker (custom image), not host"
    )