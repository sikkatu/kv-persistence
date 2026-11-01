"""Unit tests: the USE_LOSSLESS_SERDE switch resolves identically in bash and
Python.

The cache-identity namespace embeds the effective L2 codec (l2_serde). If the
shell launcher folded `USE_LOSSLESS_SERDE=1` into `LMCACHE_L2_SERDE=zstd` but
the Python identity loader still hashed the namespace as "none", two
deployments sharing a cache root would cross-read each other's chunks. These
tests drive BOTH resolvers on the same inputs and assert they agree.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation"))

from kvpersist.identity import IdentityError, identity_from_env_files  # noqa: E402


def _shell_resolve(use: str) -> str:
    env = dict(os.environ)
    env.pop("LMCACHE_L2_SERDE", None)
    if use:
        env["USE_LOSSLESS_SERDE"] = use
    else:
        env.pop("USE_LOSSLESS_SERDE", None)
    out = subprocess.run(
        ["bash", "-c",
         f'. "{ROOT}/scripts/env.sh"; _kvp_load_env "{ROOT}"; '
         'printf %s "$LMCACHE_L2_SERDE"'],
        capture_output=True, text=True, env=env,
    )
    if out.returncode != 0:
        raise AssertionError(f"env.sh resolve failed: {out.stderr}")
    return out.stdout.strip()


def _python_resolve(use: str) -> str:
    for k in ("LMCACHE_L2_SERDE", "KVP_L2_SERDE", "USE_LOSSLESS_SERDE"):
        os.environ.pop(k, None)
    if use:
        os.environ["USE_LOSSLESS_SERDE"] = use
    ident = identity_from_env_files(
        str(ROOT / "configs/generic/lmcache-server.env")
    )
    return ident.l2_serde


@pytest.mark.parametrize(
    "use,expected",
    [
        ("", "none"),
        ("0", "none"),
        ("none", "none"),
        ("1", "zstd"),
        ("yes", "zstd"),
        ("true", "zstd"),
        ("zstd", "zstd"),
        ("lz4", "lz4"),
    ],
)
def test_shell_and_python_agree_on_lossless_switch(use, expected):
    assert _shell_resolve(use) == expected
    assert _python_resolve(use) == expected


def test_python_refuses_lossy_switch_value():
    for k in ("LMCACHE_L2_SERDE", "KVP_L2_SERDE"):
        os.environ.pop(k, None)
    os.environ["USE_LOSSLESS_SERDE"] = "fp8"
    try:
        with pytest.raises(IdentityError):
            identity_from_env_files(
                str(ROOT / "configs/generic/lmcache-server.env")
            )
    finally:
        os.environ.pop("USE_LOSSLESS_SERDE", None)


def test_switch_changes_the_namespace():
    """Different codec == different namespace (safe miss, never a wrong read)."""
    from kvpersist.identity import CacheIdentity

    base = dict(
        model_id="m", model_revision="a" * 40, tokenizer_name="t",
        tokenizer_revision="b" * 40, kv_cache_dtype="bf16", block_size=16,
        num_kv_heads=8, head_dim=128, tp_size=4, connector="LMCacheMPConnector",
        connector_version="v1", max_model_len=1048576,
        chat_template_digest="c" * 64,
    )
    n_none = CacheIdentity(**base, l2_serde="none").namespace()
    n_zstd = CacheIdentity(**base, l2_serde="zstd").namespace()
    n_lz4 = CacheIdentity(**base, l2_serde="lz4").namespace()
    assert n_none != n_zstd != n_lz4
    assert n_none != n_lz4