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
    assert make_identity(kv_cache_dtype="bfloat16").namespace() != base
    assert make_identity(tp_size=2).namespace() != base
    assert make_identity(chat_template_digest="d" * 64).namespace() != base


# ---------------------------------------------------------------------------
# lmcache_model_name(): the mechanism that makes the namespace effective at
# the LMCache STORAGE level (ObjectKey.model_name -> L2 filename).
# ---------------------------------------------------------------------------

def test_lmcache_model_name_embeds_namespace():
    ident = make_identity()
    name = ident.lmcache_model_name("mimo-pro")
    assert ident.short_fingerprint() in name, \
        "the namespace fingerprint must be part of the LMCache model_name"
    assert name.startswith("mimo-pro/"), "base model label must be the prefix"


def test_lmcache_model_name_changes_with_identity():
    """Changing ANY identity field must change the LMCache model_name."""
    base = make_identity().lmcache_model_name("mimo-pro")
    for over in ({"model_revision": "d" * 40}, {"kv_cache_dtype": "bfloat16"},
                 {"tp_size": 2}, {"block_size": 32}, {"num_kv_heads": 16},
                 {"head_dim": 64}, {"max_model_len": 65536},
                 {"chat_template_digest": "f" * 64},
                 {"connector_version": "0.6.0"},
                 {"model_id": "Other"}, {"tokenizer_name": "oth"},
                 {"tokenizer_revision": "e" * 40}):
        name = make_identity(**over).lmcache_model_name("mimo-pro")
        assert name != base, f"identity change {over} must change model_name"


def test_lmcache_model_name_hint_does_not_change():
    """namespace_hint is a human label: it must NOT affect the LMCache name."""
    a = make_identity(namespace_hint="alpha").lmcache_model_name("mimo-pro")
    b = make_identity(namespace_hint="beta").lmcache_model_name("mimo-pro")
    assert a == b


def test_lmcache_model_name_rejects_at_sign():
    """ObjectKey.model_name must not contain '@' (LMCache field separator)."""
    with pytest.raises(IdentityError):
        make_identity().lmcache_model_name("bad@name")


def test_lmcache_model_name_extra_discriminator():
    """extra_discriminator allows per-tenant isolation inside model_name."""
    ident = make_identity()
    plain = ident.lmcache_model_name("mimo-pro")
    tenant_a = ident.lmcache_model_name("mimo-pro", extra_discriminator="tenantA")
    tenant_b = ident.lmcache_model_name("mimo-pro", extra_discriminator="tenantB")
    assert plain != tenant_a
    assert plain != tenant_b
    assert tenant_a != tenant_b
    assert "tenantA" in tenant_a and "tenantB" in tenant_b


def test_lmcache_model_name_rejects_colliding_discriminators():
    """`tenant/a` and `tenant--a` map to the SAME L2 filename ('/' -> '--').

    The discriminator must be rejected up front, otherwise two tenants would
    collide on disk despite different names (review round 13). Note the '/'
    used as the separator between base/namespace/discriminator is fine -- only
    a '/' INSIDE the discriminator would collide after the adapter maps it."""
    ident = make_identity()
    with pytest.raises(IdentityError):
        ident.lmcache_model_name("mimo-pro", extra_discriminator="tenant/a")
    with pytest.raises(IdentityError):
        ident.lmcache_model_name("mimo-pro", extra_discriminator="tenant@a")
    with pytest.raises(IdentityError):
        ident.lmcache_model_name("mimo-pro", extra_discriminator="ten\\ant")
    name = ident.lmcache_model_name("mimo-pro", extra_discriminator="tenantA")
    assert name.endswith("/tenantA")


def test_lmcache_model_name_rejects_nul():
    """NUL must be rejected (ObjectKey invariant), even if it reaches base_model."""
    with pytest.raises(IdentityError):
        make_identity().lmcache_model_name("bad\x00name")


def test_namespace_from_config_optional_fields_not_required():
    """lora/multimodal/rope/speculative/l2_serde are optional, not demanded."""
    cfg = make_identity().canonical()
    for field in ("lora_adapters", "multimodal_components",
                  "rope_config_digest", "speculative_config", "l2_serde",
                  "namespace_hint"):
        cfg.pop(field, None)
    # must NOT raise
    namespace_from_config(cfg)


def test_l2_serde_part_of_identity():
    """Changing the L2 codec must change the namespace (the file name does not
    encode the serde, so sharing a root with a different serde would read data
    with the wrong deserializer -- review round 13). Review fix: "" / "none" /
    "raw" all mean the SAME uncompressed mode and must share ONE namespace;
    "zstd"/"lz4" must differ."""
    base = make_identity().namespace()
    assert make_identity(l2_serde="none").namespace() == base, (
        '"" and "none" are the same codec and MUST hash to the same namespace'
    )
    assert make_identity(l2_serde="raw").namespace() == base
    assert make_identity(l2_serde="").namespace() == base
    assert make_identity(l2_serde="zstd").namespace() != base
    assert make_identity(l2_serde="lz4").namespace() != base
    assert make_identity(l2_serde="zstd").namespace() != make_identity(
        l2_serde="lz4"
    ).namespace()


def test_advanced_identity_fields_split_the_namespace():
    """Review fix: lora_adapters / multimodal_components / rope_config_digest /
    speculative_config must change namespace() AND lmcache_model_name() --
    two deployments differing only in one of them must NOT share cached KV."""
    base = make_identity()
    for field, value in (
        ("lora_adapters", "lora-a@rev1"),
        ("multimodal_components", "vision-enc@rev2"),
        ("rope_config_digest", "c" * 64),
        ("speculative_config", "mtp-draft@rev3"),
    ):
        other = make_identity(**{field: value})
        assert other.namespace() != base.namespace(), (
            f"{field} must split the namespace"
        )
        assert other.lmcache_model_name("mimo") != base.lmcache_model_name("mimo"), (
            f"{field} must split the LMCache model_name"
        )


def test_advanced_identity_fields_loaded_from_config(tmp_path, monkeypatch):
    """The loader must READ these fields from the config files -- before the
    fix they stayed empty no matter what the operator set (review P1)."""
    for k in ("KVP_LORA_ADAPTERS", "KVP_MULTIMODAL_COMPONENTS",
              "KVP_ROPE_CONFIG_DIGEST", "KVP_SPECULATIVE_CONFIG",
              "LMCACHE_L2_SERDE", "USE_LOSSLESS_SERDE"):
        monkeypatch.delenv(k, raising=False)
    tmp = tmp_path / "identity.env"
    tmp.write_text(
        "KVP_MODEL_ID=m\n"
        "KVP_MODEL_REVISION=" + "a" * 40 + "\n"
        "KVP_TOKENIZER_NAME=t\n"
        "KVP_TOKENIZER_REVISION=" + "b" * 40 + "\n"
        "KVP_KV_CACHE_DTYPE=bfloat16\n"
        "KVP_BLOCK_SIZE=16\n"
        "KVP_NUM_KV_HEADS=8\n"
        "KVP_HEAD_DIM=128\n"
        "KVP_TP_SIZE=4\n"
        "KVP_CONNECTOR=LMCacheMPConnector\n"
        "KVP_CONNECTOR_VERSION=v1\n"
        "KVP_MAX_MODEL_LEN=1024\n"
        "KVP_CHAT_TEMPLATE_DIGEST=" + "c" * 64 + "\n"
        "KVP_LORA_ADAPTERS=lora-a@rev1\n"
        "KVP_MULTIMODAL_COMPONENTS=vision-enc@rev2\n"
        "KVP_ROPE_CONFIG_DIGEST=" + "d" * 64 + "\n"
        "KVP_SPECULATIVE_CONFIG=mtp-draft@rev3\n"
    )
    from kvpersist.identity import identity_from_env_files

    ident = identity_from_env_files(str(tmp))
    assert ident.lora_adapters == "lora-a@rev1"
    assert ident.multimodal_components == "vision-enc@rev2"
    assert ident.rope_config_digest == "d" * 64
    assert ident.speculative_config == "mtp-draft@rev3"
    # and each of them is in the namespace payload
    canon = ident.canonical()
    assert canon["lora_adapters"] == "lora-a@rev1"
    assert canon["speculative_config"] == "mtp-draft@rev3"


def test_rope_digest_must_be_well_formed(tmp_path, monkeypatch):
    for k in ("LMCACHE_L2_SERDE", "USE_LOSSLESS_SERDE"):
        monkeypatch.delenv(k, raising=False)
    tmp = tmp_path / "identity.env"
    tmp.write_text(
        "KVP_MODEL_ID=m\n"
        "KVP_MODEL_REVISION=" + "a" * 40 + "\n"
        "KVP_TOKENIZER_NAME=t\n"
        "KVP_TOKENIZER_REVISION=" + "b" * 40 + "\n"
        "KVP_KV_CACHE_DTYPE=bfloat16\n"
        "KVP_BLOCK_SIZE=16\n"
        "KVP_NUM_KV_HEADS=8\n"
        "KVP_HEAD_DIM=128\n"
        "KVP_TP_SIZE=4\n"
        "KVP_CONNECTOR=LMCacheMPConnector\n"
        "KVP_CONNECTOR_VERSION=v1\n"
        "KVP_MAX_MODEL_LEN=1024\n"
        "KVP_CHAT_TEMPLATE_DIGEST=" + "c" * 64 + "\n"
        "KVP_ROPE_CONFIG_DIGEST=not-a-sha\n"
    )
    from kvpersist.identity import IdentityError, identity_from_env_files

    try:
        identity_from_env_files(str(tmp))
        raise AssertionError("malformed KVP_ROPE_CONFIG_DIGEST must be refused")
    except IdentityError:
        pass


def test_identity_from_env_files_env_wins(tmp_path, monkeypatch):
    """`LMCACHE_L2_SERDE` in the process env must WIN over the file value --
    otherwise `model-name` reports the file's namespace while the server
    actually runs with the env serde (review round 14)."""
    from kvpersist.identity import identity_from_env_files
    f = tmp_path / "identity.env"
    f.write_text(
        "KVP_MODEL_ID=x\n"
        "KVP_MODEL_REVISION=" + "a" * 40 + "\n"
        "KVP_TOKENIZER_NAME=t\n"
        "KVP_TOKENIZER_REVISION=" + "b" * 40 + "\n"
        "KVP_KV_CACHE_DTYPE=fp16\n"
        "KVP_BLOCK_SIZE=16\n"
        "KVP_NUM_KV_HEADS=8\n"
        "KVP_HEAD_DIM=128\n"
        "KVP_TP_SIZE=4\n"
        "KVP_CONNECTOR=LMCacheMPConnector\n"
        "KVP_CONNECTOR_VERSION=v\n"
        "KVP_MAX_MODEL_LEN=1024\n"
        "KVP_CHAT_TEMPLATE_DIGEST=" + "c" * 64 + "\n"
        "LMCACHE_L2_SERDE=none\n"
    )
    monkeypatch.delenv("LMCACHE_L2_SERDE", raising=False)
    ident_file = identity_from_env_files(str(f))
    assert ident_file.l2_serde == "none"
    monkeypatch.setenv("LMCACHE_L2_SERDE", "fp8")
    ident_env = identity_from_env_files(str(f))
    assert ident_env.l2_serde == "fp8", "process env must win over the file"
    assert ident_env.namespace() != ident_file.namespace(), \
        "env-vs-file serde difference must change the namespace"