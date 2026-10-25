"""Unit tests: cache identity / compatibility namespaces. No GPU, no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "implementation"))

import pytest

from kvpersist.identity import CacheIdentity, IdentityError, namespace_from_config


def make_identity(**over):
    base = dict(
        model_id="XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        model_revision="a" * 40,
        tokenizer_name="XiaomiMiMo/MiMo-V2.6-Pro-MOPD",
        tokenizer_revision="b" * 40,
        kv_cache_dtype="fp16",
        block_size=16,
        num_kv_heads=8,
        head_dim=128,
        tp_size=4,
        connector="LMCacheMPConnector",
        connector_version="lmcache-0.5.x@pin",
        max_model_len=1048576,
        chat_template_digest="c" * 64,
    )
    base.update(over)
    return CacheIdentity(**base)


def test_namespace_is_stable():
    a, b = make_identity(), make_identity()
    assert a.namespace() == b.namespace()
    assert a.namespace().startswith("kvp-")


def test_namespace_changes_with_each_field():
    base = make_identity().namespace()
    for field in (
        "model_id", "model_revision", "tokenizer_name", "tokenizer_revision",
        "kv_cache_dtype", "tp_size", "block_size", "num_kv_heads", "head_dim",
        "connector", "connector_version", "max_model_len", "chat_template_digest",
    ):
        over = {}
        if field in ("model_revision", "tokenizer_revision", "chat_template_digest"):
            over[field] = {"model_revision": "d" * 40,
                           "tokenizer_revision": "d" * 40,
                           "chat_template_digest": "d" * 64}[field]
        elif isinstance(getattr(make_identity(), field), int):
            over[field] = getattr(make_identity(), field) + 1
        else:
            over[field] = getattr(make_identity(), field) + "-changed"
        assert make_identity(**over).namespace() != base, f"{field} must affect namespace"


def test_namespace_hint_does_not_affect_identity():
    assert make_identity(namespace_hint="psi").namespace() == make_identity().namespace()


@pytest.mark.parametrize(
    "over",
    [
        {"model_revision": "main"},
        {"model_revision": "abc"},
        {"model_revision": "A" * 40},  # uppercase is not accepted: canonical is lowercase hex
        {"chat_template_digest": "nothex" * 10},
        {"kv_cache_dtype": ""},
        {"tp_size": 0},
        {"block_size": -1},
        {"model_id": ""},
        {"connector_version": ""},
    ],
)
def test_uncertain_identity_raises(over):
    with pytest.raises(IdentityError):
        make_identity(**over).namespace()


def test_namespace_from_config_missing_fields_raise():
    with pytest.raises(IdentityError):
        namespace_from_config({"model_id": "x"})


def test_namespace_from_config_unknown_field_raises():
    cfg = make_identity().canonical()
    cfg["surprise"] = 1
    with pytest.raises(IdentityError):
        namespace_from_config(cfg)


def test_namespace_from_config_roundtrip():
    cfg = make_identity().canonical()
    assert namespace_from_config(cfg) == make_identity().namespace()


def test_incompatible_entries_rejected():
    """Changed identity => different namespace, never a wrong hit."""
    base = make_identity().namespace()
    assert make_identity(kv_cache_dtype="bf16").namespace() != base
    assert make_identity(tp_size=2).namespace() != base
    assert make_identity(chat_template_digest="d" * 64).namespace() != base