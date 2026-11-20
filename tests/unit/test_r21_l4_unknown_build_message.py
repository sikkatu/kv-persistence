# SPDX-License-Identifier: Apache-2.0
"""r21 L4: the "LMCache build is unknown" message must name the REAL cause
and the REAL fix.

Facts (review): `cli model-name` printed "could not be read from
/etc/kvp-lmcache-identity.json" (a path INSIDE the image -- on the host it
does not exist) and the launcher hinted `setup-identity.sh`, which does NOT
fix this cause (the build identity comes from the RUNNING image, not from
identity.env).

Contract (r21 L4):
- the IdentityError text names the real action: launch via
  scripts/start-all.sh (it resolves the image and exports the build) or run
  bash scripts/build-image.sh. No /etc path, no setup-identity hint.
- verify-engine-identity.sh prints the lmcache_build HINT with the same
  actions and does NOT recommend setup-identity.sh for this field.
"""
from __future__ import annotations
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
sys.path.insert(0, str(IMPL))

BUILD = "0.5.8rc1.post48+kvp.bbbbbbbbbbbb"


def _full_identity_env() -> str:
    """A COMPLETE identity.env (all pins filled) -- so validate() reaches
    the lmcache_build check instead of failing earlier."""
    return "\n".join([
        "KVP_MODEL_ID=XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        f"KVP_MODEL_REVISION={'a' * 40}",
        "KVP_TOKENIZER_NAME=XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        f"KVP_TOKENIZER_REVISION={'b' * 40}",
        "KVP_KV_CACHE_DTYPE=fp16",
        "KVP_BLOCK_SIZE=16",
        "KVP_NUM_KV_HEADS=8",
        "KVP_HEAD_DIM=128",
        "KVP_TP_SIZE=4",
        "KVP_CONNECTOR=LMCacheMPConnector",
        "KVP_CONNECTOR_VERSION=lmcache-0.5.x@pin",
        "KVP_MAX_MODEL_LEN=1048576",
        f"KVP_CHAT_TEMPLATE_DIGEST={'c' * 64}",
    ])


def test_unknown_build_message_names_real_fix(tmp_path):
    """> Without the resolver and without a live/ record, the error must be
    exactly the "build is unknown" text: launch via start-all.sh or run
    build-image.sh. No /etc path, no setup-identity.sh hint."""
    env_file = tmp_path / "identity.env"
    env_file.write_text(_full_identity_env())
    code = (
        "import sys; sys.path.insert(0, {impl!r})\n"
        "from kvpersist.identity import identity_from_env_files, IdentityError\n"
        "try:\n"
        "    i = identity_from_env_files({env!r})\n"
        "    i.namespace()\n"
        "    print('NO ERROR')\n"
        "except IdentityError as e:\n"
        "    print('GOT:', str(e))\n"
    ).format(impl=str(IMPL), env=str(env_file))
    env = dict(os.environ)
    for var in ("KVP_LMCACHE_BUILD", "KVP_LMCACHE_IDENTITY",
                "KVP_IMAGE_IDENTITY", "KVP_LIVE_DIR", "LIVE_DIR"):
        env.pop(var, None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr[-500:]
    got = out.stdout.strip()
    assert got.startswith("GOT:"), f"the build must be unknown:\n{got}"
    assert "the LMCache build is unknown" in got, (
        f"the message must name the real cause:\n{got}")
    assert "scripts/start-all.sh" in got, (
        f"the message must name the real fix (start-all.sh resolves the "
        f"image and exports the build):\n{got}")
    assert "bash scripts/build-image.sh" in got, (
        f"the message must name the rebuild action:\n{got}")
    # the WRONG fixes must be GONE
    assert "/etc/kvp-lmcache-identity.json" not in got, (
        f"the in-image path is meaningless on the host:\n{got}")
    assert "setup-identity.sh" not in got, (
        f"setup-identity.sh does not fix the build identity:\n{got}")


def test_gate_lmcache_build_hint_skips_setup_identity(tmp_path):
    """> verify-engine-identity.sh: a lmcache_build mismatch prints the
    HINT with start-all.sh / build-image.sh and does NOT recommend
    setup-identity.sh for this field."""
    gate = ROOT / "scripts" / "verify-engine-identity.sh"
    src = gate.read_text()
    # the lmcache_build hint block must exist and name the real actions
    assert 'HINT (lmcache_build)' in src, "the gate must print a lmcache_build hint"
    start = src.index('if "lmcache_build" in _bad_labels:')
    end = src.index("else:", start)
    block = src[start:end]
    assert "scripts/start-all.sh" in block, (
        "the lmcache_build hint must name start-all.sh")
    assert "build-image.sh" in block, (
        "the lmcache_build hint must name build-image.sh")
    assert "setup-identity.sh" not in block, (
        "the lmcache_build hint must NOT recommend setup-identity.sh")