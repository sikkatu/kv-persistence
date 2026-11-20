# SPDX-License-Identifier: Apache-2.0
"""r21 L6: a bare tag in IMAGE gets a clear refusal, not the misleading
"base pin mismatch, rebuild" advice.

Fact (review): `IMAGE=kvp-lmcache:latest` was treated as a stock base pin
and the resolver answered "custom image was built FROM a different base,
rebuild" -- useless advice: the user simply used the wrong FORMAT.

Contract (r21 L6): an IMAGE without a digest is refused with:
  "IMAGE must be 'repo:tag@sha256:<64hex>' (base pin) or 'sha256:<id>'
   (custom), bare tag is not accepted, take $(cat live/image)".
No base-pin-mismatch wording for this case.

Test (behavioural, the REAL env.sh resolver + docker stub from the r20 K1
harness): IMAGE=kvp-lmcache:latest -> rc != 0, the bare-tag text is present,
the base-pin-mismatch text is GONE.
"""
from __future__ import annotations
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _run_resolver(tmp_path: pathlib.Path, image: str | None):
    """REAL _kvp_resolve_runtime_image from env.sh with a minimal fixture."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for s in ("env.sh",):
        (ROOT / "scripts" / s).read_text()
        (repo / "scripts" / s).write_text((ROOT / "scripts" / s).read_text())
    for d in ("implementation", "vendor", "configs"):
        (repo / d).symlink_to(ROOT / d)
    live = repo / "live"
    live.mkdir()
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    script = f"""
set -u
. "{repo}/scripts/env.sh"
KVP_LIVE_DIR="{live}"
IMAGE="${{IMAGE:-}}"
_kvp_resolve_runtime_image "{repo}" 0
rc=$?
echo "RC=$rc"
echo "RESOLVED_IMAGE=${{IMAGE:-<unset>}}"
"""
    env = dict(os.environ, KVP_LIVE_DIR=str(live))
    for var in ("IMAGE", "KVP_LMCACHE_BUILD", "KVP_SKIP_IMAGE_CHECK",
                "KVP_SKIP_LMC_IDENTITY", "KVP_LMCACHE_IDENTITY"):
        env.pop(var, None)
    if image is not None:
        env["IMAGE"] = image
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=60, env=env, cwd=str(repo))


def test_bare_tag_refused_with_clear_message(tmp_path):
    """> IMAGE=kvp-lmcache:latest -> rc != 0, the bare-tag refusal names the
    accepted formats and $(cat live/image); NO 'base pin mismatch' advice."""
    r = _run_resolver(tmp_path, "kvp-lmcache:latest")
    out = r.stdout + r.stderr
    assert "RC=1" in r.stdout, (
        f"a bare tag must be refused:\n{out[-1200:]}")
    assert "bare tag is NOT accepted" in out or "bare tag" in out, (
        f"the refusal must say the bare tag is not accepted:\n{out[-1200:]}")
    assert "repo:tag@sha256:" in out, (
        f"the refusal must name the accepted base-pin format:\n{out[-1200:]}")
    assert "sha256:" in out and "cat" in out, (
        f"the refusal must name the custom format and $(cat live/image):\n{out[-1200:]}")
    # the misleading advice must be GONE
    assert "base pin mismatch" not in out, (
        f"the base-pin-mismatch advice must NOT fire for a format error:\n{out[-1200:]}")
    assert "built FROM a different base" not in out, (
        f"the rebuild advice must NOT fire for a format error:\n{out[-1200:]}")


def test_pinned_stock_still_resolves(tmp_path):
    """> A properly pinned stock IMAGE (repo:tag@sha256:<hex>) must NOT hit
    the bare-tag refusal -- the K1 base-pin path stays intact."""
    import json
    import hashlib
    real_patch_dir = ROOT / "vendor" / "patches" / "lmcache"

    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    labels = json.dumps({
        "org.kvp.lmc-base": ("vllm/vllm-openai:nightly-8e2beaa3bff595577a069d49d"
                             "96bde9074294ec2@sha256:167ccd8696bc09f1b63301185134"
                             "e04b883623f8adcbd8e3a0ec3140a193abcf"),
        "org.kvp.lmc-patch-4136": _sha(real_patch_dir / "4136-mp-retrieve-abort.patch"),
        "org.kvp.lmc-patch-5972": _sha(real_patch_dir / "5972-incomplete-kv-objects.patch"),
    })
    wv = "0.5.8rc1.post48+kvp.60dce759ea93"
    ident = json.dumps({
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr4136_patch_sha256": _sha(real_patch_dir / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(real_patch_dir / "5972-incomplete-kv-objects.patch"),
        "wheel": f"lmcache-{wv}-cp312-cp312-linux_x86_64.whl",
        "wheel_sha256": "a" * 64,
        "wheel_version": wv,
    })
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "env.sh").write_text((ROOT / "scripts" / "env.sh").read_text())
    for d in ("implementation", "vendor", "configs"):
        (repo / d).symlink_to(ROOT / d)
    live = repo / "live"
    live.mkdir()
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(f"""#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
_all="$*"
case "$cmd" in
  image)
    sub="${{1:-}}"; shift || true
    case "$sub" in
      inspect) echo '{labels}' ;;
    esac ;;
  run)
    _entry=""
    while [ "$#" -gt 0 ]; do case "$1" in
      --entrypoint) _entry="$2"; shift 2 ;;
      *) shift ;;
    esac; done
    case "$_entry" in
      cat) echo '{ident}' ;;
      python3) echo "{wv}" ;;
    esac ;;
  inspect) echo "sha256:{'c' * 64}" ;;
  *) exit 0 ;;
esac
""")
    (bin_dir / "docker").chmod(0o755)
    stock = ("vllm/vllm-openai:nightly-8e2beaa3bff595577a069d49d"
             "96bde9074294ec2@sha256:167ccd8696bc09f1b63301185134"
             "e04b883623f8adcbd8e3a0ec3140a193abcf")
    script = f"""
set -u
. "{repo}/scripts/env.sh"
KVP_LIVE_DIR="{live}"
IMAGE="{stock}"
_kvp_resolve_runtime_image "{repo}" 0
rc=$?
echo "RC=$rc"
echo "RESOLVED_IMAGE=${{IMAGE:-<unset>}}"
"""
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_LIVE_DIR=str(live))
    for var in ("KVP_LMCACHE_BUILD", "KVP_SKIP_IMAGE_CHECK",
                "KVP_SKIP_LMC_IDENTITY", "KVP_LMCACHE_IDENTITY"):
        env.pop(var, None)
    r = subprocess.run(["bash", "-c", script], capture_output=True,
                       text=True, timeout=60, env=env, cwd=str(repo))
    out = r.stdout + r.stderr
    assert "RC=0" in r.stdout, (
        f"a properly pinned stock base must RESOLVE (K1 contract):\n{out[-1200:]}")
    assert "bare tag" not in out, (
        f"the bare-tag refusal must NOT fire for a pinned stock:\n{out[-1200:]}")