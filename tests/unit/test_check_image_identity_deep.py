# SPDX-License-Identifier: Apache-2.0
"""r3 P1-3: image verification must prove the wheel is actually INSIDE, not just
that a LABEL claims so.

Review-verified gap: `check_image_identity.py` only read `org.kvp.lmc-*` LABELs
via `docker image inspect`. A LABEL is inherited by ANY derivative and is
forged with one `LABEL` line -- it is not proof the wheel with cuda_ops is
really baked in. Deep verification (no GPU, no model load) must ALSO:
  * `docker run --rm --entrypoint cat <IMAGE> /etc/kvp-lmcache-identity.json`
    and cross-check wheel_sha256 + patch sha's against this repo's patches,
  * `docker run --rm --entrypoint python3 <IMAGE> -c "import importlib.util as
    u; assert u.find_spec('lmcache.cuda_ops')"` -- the compiled extension
    importable WITHOUT launching GPUs/model.
  Any mismatch must abort the launch BEFORE the 15-minute model load.
This file stubs `docker` and covers both success and failure outcomes.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
PATCH_DIR = ROOT / "vendor" / "patches" / "lmcache"
PY = sys.executable
HEX64 = "0" * 64
GOOD_SPEC = f"sha256:{HEX64}"


def _sha256(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _good_labels() -> dict:
    return {
        "org.kvp.lmc-base": f"vllm/vllm-openai:nightly-8e2beaa3@sha256:{'b' * 64}",
        "org.kvp.lmc-patch-4136": _sha256(PATCH_DIR / "4136-mp-retrieve-abort.patch"),
        "org.kvp.lmc-patch-5972": _sha256(PATCH_DIR / "5972-incomplete-kv-objects.patch"),
    }


def _docker_stub(bin_dir: pathlib.Path, labels: dict | None, identity_json: dict | str | None,
                 cuda_ops_spec: bool = True, identity_sha_ok: bool = True,
                 installed_version: str = "0.5.8rc1.post48+kvp.deadbeefcafe"):
    """A stub `docker` that answers each verification sub-command."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    labels = labels if labels is not None else {}
    if isinstance(identity_json, dict):
        ident_text = json.dumps(identity_json)
    else:
        ident_text = identity_json if identity_json is not None else "null"
    cuda_rc = "0" if cuda_ops_spec else "1"
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "cmd=\"$1\"; shift || true\n"
        "if [ \"$cmd\" = \"image\" ] || [ \"$cmd\" = \"inspect\" ]; then\n"
        f"  echo '{json.dumps(labels)}'; exit 0\n"
        "fi\n"
        "if [ \"$cmd\" = \"run\" ]; then\n"
        "  case \"$*\" in\n"
        "    *'/etc/kvp-lmcache-identity.json'*)\n"
        f"      echo '{ident_text}'; exit 0 ;;\n"
        "    *'importlib.util'*)\n"
        f"      exit {cuda_rc} ;;\n"
        "    *'importlib.metadata'*)\n"
        f"      echo '{installed_version}'; exit 0 ;;\n"
        "  esac\n"
        "  echo ''\n"
        "  exit 0\n"
        "fi\n"
        "echo ''\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub


def _run(img: str, tmp_path: pathlib.Path, labels: dict | None, identity_json, cuda_ops_spec=True):
    bin_dir = tmp_path / "bin"
    _docker_stub(bin_dir, labels, identity_json, cuda_ops_spec)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
        PYTHONPATH=str(IMPL),
    )
    return subprocess.run(
        [PY, "-m", "kvpersist.check_image_identity", img, str(PATCH_DIR), "--deep"],
        capture_output=True, text=True, timeout=30, env=env, cwd=str(tmp_path),
    )


def test_deep_refuses_missing_identity_json(tmp_path):
    """LABEL present but no /etc/kvp-lmcache-identity.json -> refuse (LABELs
    alone do NOT prove the wheel is baked in -- any derivative forges a LABEL)."""
    r = _run(GOOD_SPEC, tmp_path, labels=_good_labels(), identity_json=None)
    assert r.returncode != 0, (
        "deep check must refuse when the baked identity json is missing/unreadable"
    )
    assert "identity" in (r.stdout + r.stderr)


def test_deep_verifies_identity_json_and_cuda_ops(tmp_path):
    """Full deep pass: LABEL + identity json (correct patch shas + a real
    '+kvp.' wheel_version) + cuda_ops find_spec + the installed version
    matching the identity -> accept."""
    ident = {
        "pr4136_patch_sha256": _sha256(PATCH_DIR / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha256(PATCH_DIR / "5972-incomplete-kv-objects.patch"),
        "wheel_sha256": "a" * 64,
        "wheel_version": "0.5.8rc1.post48+kvp.deadbeefcafe",
    }
    r = _run(GOOD_SPEC, tmp_path, labels=_good_labels(), identity_json=ident,
             cuda_ops_spec=True)
    assert r.returncode == 0, (
        f"fully-correct deep verification must PASS, got rc={r.returncode}:\n{r.stderr[-800:]}"
    )


def test_deep_refuses_when_cuda_ops_missing(tmp_path):
    """LABELs + identity json fine, but `find_spec('lmcache.cuda_ops')` empty ->
    the wheel lacks the compiled extension -> refuse."""
    ident = {
        "pr4136_patch_sha256": _sha256(PATCH_DIR / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha256(PATCH_DIR / "5972-incomplete-kv-objects.patch"),
        "wheel_sha256": "a" * 64,
    }
    r = _run(GOOD_SPEC, tmp_path, labels=_good_labels(), identity_json=ident,
             cuda_ops_spec=False)
    assert r.returncode != 0, (
        "must refuse when find_spec('lmcache.cuda_ops') reports nothing"
    )
    assert "cuda_ops" in (r.stdout + r.stderr)


def test_deep_refuses_identity_sha_drift(tmp_path):
    """identity json carries WRONG patch shas -> refuse (differs from repo
    patches -- someone rebuilt with different patches but kept old identity)."""
    ident = {
        "pr4136_patch_sha256": "b" * 64,
        "pr5972_patch_sha256": _sha256(PATCH_DIR / "5972-incomplete-kv-objects.patch"),
        "wheel_sha256": "a" * 64,
    }
    r = _run(GOOD_SPEC, tmp_path, labels=_good_labels(), identity_json=ident,
             cuda_ops_spec=True)
    assert r.returncode != 0, (
        "must refuse when the identity json's patch sha differs from repo patches"
    )


def test_deep_requires_identity_json_even_with_label(tmp_path):
    """Forged LABEL alone (identity json 'null') must NOT pass deep check."""
    r = _run(GOOD_SPEC, tmp_path, labels=_good_labels(), identity_json="null",
             cuda_ops_spec=True)
    assert r.returncode != 0, (
        "forged LABEL with a missing/invalid identity json must be refused"
    )