"""Cache identity and compatibility namespaces.

Every persistent KV entry must belong to a namespace derived from ALL runtime
parameters that materially affect KV values. Two deployments may share KV data
only when every identity field matches exactly. Any missing or uncertain field
yields a cache MISS (IdentityError -> caller falls back to recompute).

The namespace is a SHA-256 over canonical JSON of the identity fields, so it is
stable across restarts, hosts and Python versions.

HOW THE NAMESPACE ISOLATES REAL LMCache STORAGE (verified against the pinned
LMCache 0.5.6 source and a live store/lookup run -- see
tests/integration/test_l2_persistence.py::test_identity_namespace_isolates_lmcache_storage):

  * The LMCache MP cache key (IPCCacheServerKey -> ObjectKey) carries
    ``model_name``; the L2 file name of the ``nixl_store_dynamic`` adapter is
    derived from it: ``<safe_model_name>_<kv_rank>_<group>_<chunkhash>.bin``.
  * Therefore a namespace is made effective for STORAGE -- not just for local
    labels -- by embedding it into the ``model_name`` the engine/connector
    registers with (``CacheIdentity.lmcache_model_name()``). Changing ANY
    identity field changes the namespace, which changes ``model_name``, which
    changes the ObjectKey and the L2 file name: a lookup under the new identity
    cannot see the old objects (proven at the LMCache API level: LOOKUP returns
    0/N chunks where the original identity returns N/N).

REMAINING LIMITATION (review P1-3, RESOLVED at the disk-key level in round 15):
``cache_salt`` (the per-user field of ObjectKey) is part of the key's
equality/hash but upstream ``nixl_store_dynamic`` does NOT put it into the L2
file name (the fs/s3/bigtable adapters do include it). After a restart, a
secondary (disk) lookup of the same tokens under a DIFFERENT salt found the
file written under the original salt. Measured live: store under salt=tenantA,
lookup under salt=tenantB -> 4/4 chunks returned from L2.

FIX IN THIS REPO: `kvpersist/salt_filename_patch.py` appends the salt to the
L2 file name at the disk-key level. All launchers (scripts/start-cache.sh,
systemd/kvp-lmcache-server) start the server through that patch, so the two
salts map to DIFFERENT files and cross-salt reads MISS. Verified live:
store salt=tenantA -> lookup salt=tenantB = 0/4 (test
test_cache_salt_isolates_l2_storage). When running the server WITHOUT the
patch (a bare `lmcache server`), do NOT rely on cache_salt for isolation:
put any per-tenant discriminator into the ``model_name`` itself
(``lmcache_model_name(extra_discriminator=...)``) or use a separate L2 cache
root per tenant.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from typing import Any, Dict

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")
# ObjectKey.model_name must not contain '@' (LMCache uses it as a field
# separator); '/' is allowed and is rendered as '--' in L2 file names.
_LMC_MODEL_NAME_FORBIDDEN = "@"


class IdentityError(Exception):
    """Identity incomplete or uncertain -> caller must treat as cache miss."""


@dataclasses.dataclass(frozen=True)
class CacheIdentity:
    """Compatibility identity of one serving configuration.

    Required fields are mandatory: if any is empty the identity is *uncertain*
    and namespace() raises, which callers translate into a cache miss.
    """

    model_id: str
    model_revision: str          # full 40-hex commit SHA of the weights
    tokenizer_name: str
    tokenizer_revision: str      # full 40-hex revision or tokenizer file digest
    kv_cache_dtype: str          # effective dtype, "auto" must be resolved first
    block_size: int              # KV block size in tokens
    num_kv_heads: int
    head_dim: int
    tp_size: int
    connector: str               # e.g. "LMCacheMPConnector"
    connector_version: str       # pinned lmcache/vllm build identity
    max_model_len: int
    chat_template_digest: str    # sha256 of the applied chat template, 64 hex
    namespace_hint: str = ""     # optional user label, does not affect identity
    # Optional advanced-identity fields (review: cover every KV-affecting knob).
    # Empty string == not applicable (treated as "none") -- NOT an error.
    lora_adapters: str = ""      # comma-separated active LoRA adapter ids + revisions
    multimodal_components: str = ""  # vision/audio encoder ids + revisions if served
    rope_config_digest: str = "" # sha256 of RoPE scaling/theta/section config (64 hex)
    speculative_config: str = "" # MTP / draft-model / speculative settings if active
    # L2 serializer/deserializer (LMCACHE_L2_SERDE: none|zstd|lz4). Part of the
    # identity on purpose (review round 13): the L2 file name does NOT depend on
    # the serde, so two deployments sharing a cache root with DIFFERENT serde
    # would read each other's files with the wrong deserializer. Changing the
    # serde must therefore change the namespace (-> cache miss, never a corrupt
    # read). `""`, `"none"` and `"raw"` all mean the uncompressed mode and are
    # normalized to ONE namespace in `canonical()`; `"zstd"`/`"lz4"` are
    # distinct. Lossless codecs only (kv-persistence is lossless-by-contract;
    # lossy codecs are refused at startup).
    l2_serde: str = ""

    def validate(self) -> None:
        if not self.model_id:
            raise IdentityError("model_id is empty")
        if not _SHA40.match(self.model_revision or ""):
            raise IdentityError(
                f"model_revision must be a full 40-hex commit SHA, got {self.model_revision!r}"
            )
        if not self.tokenizer_name:
            raise IdentityError("tokenizer_name is empty")
        if not self.tokenizer_revision:
            raise IdentityError("tokenizer_revision is empty")
        if not self.kv_cache_dtype:
            raise IdentityError("kv_cache_dtype empty (unresolved dtype is uncertain -> miss)")
        for name in ("block_size", "num_kv_heads", "head_dim", "tp_size", "max_model_len"):
            v = getattr(self, name)
            if not isinstance(v, int) or v <= 0:
                raise IdentityError(f"{name} must be a positive int, got {v!r}")
        if not self.connector:
            raise IdentityError("connector is empty")
        if not self.connector_version:
            raise IdentityError("connector_version is empty")
        if not _SHA64.match(self.chat_template_digest or ""):
            raise IdentityError("chat_template_digest must be 64 hex chars (sha256)")
        # Advanced fields: when non-empty, digest-like fields must be 64 hex.
        for name in ("rope_config_digest",):
            v = getattr(self, name)
            if v and not _SHA64.match(v):
                raise IdentityError(f"{name} must be 64 hex chars when set (sha256)")

    def canonical(self) -> Dict[str, Any]:
        self.validate()
        # namespace_hint is a human label only: it must NEVER change compatibility
        out = {}
        for f in sorted(self.__dataclass_fields__.values(), key=lambda x: x.name):
            if f.name == "namespace_hint":
                continue
            v = getattr(self, f.name)
            # Review fix: `l2_serde=""` and `l2_serde="none"` mean the SAME
            # codec (raw, no transform) but used to hash into DIFFERENT
            # namespaces. Normalize so they are interchangeable; "zstd"/"lz4"
            # stay distinct. Unknown codec strings are kept verbatim (validate
            # elsewhere refuses lossy ones at startup).
            if f.name == "l2_serde" and (v or "").strip().lower() in ("", "none", "raw"):
                v = "none"
            out[f.name] = v
        return out

    def namespace(self) -> str:
        """Stable namespace usable as a directory name / cache key prefix."""
        payload = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return "kvp-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def short_fingerprint(self) -> str:
        return self.namespace().replace("kvp-", "", 1)

    def lmcache_model_name(self, base_model: str = "",
                           extra_discriminator: str = "") -> str:
        """The LMCache ``model_name`` that enforces this identity at STORAGE level.
        LMCache derives both the ObjectKey and the L2 file name from
        ``model_name`` (nixl_store_dynamic: ``<safe_model_name>_<kv_rank>_<group>
        _<chunkhash>[_<salt>].bin`` -- the salt suffix comes from
        ``kvpersist.salt_filename_patch``), so embedding the namespace here is
        what actually separates incompatible configurations on disk and in the
        API.
        Args:
            base_model: Human-readable model label (e.g. the served model name).
                May be empty; the namespace alone is already unique.
            extra_discriminator: Optional per-tenant/per-user label to embed.
                Prefer THIS (or a separate L2 cache root) for isolation between
                tenants. ``cache_salt`` isolation works only when the server is
                started through ``kvpersist.salt_filename_patch`` (all shipped
                launchers do); a bare ``lmcache server`` ignores the salt in the
                file name (see module docstring), so the discriminator in
                ``model_name`` is the portable guarantee.
                Must not contain ``/`` or ``@``: the L2 file name maps ``/`` to
                ``--``, so ``tenant/a`` and ``tenant--a`` would collide.
        Returns:
            ``"<base>/<namespace>[/extra_discriminator]"`` with the namespace's
            ``kvp-`` prefix stripped for brevity. The result is ``@``-free and
            ``\\x00``-free to satisfy the ObjectKey invariants (LMCache uses
            ``@`` as a field separator in serialized keys).
        """
        disc = extra_discriminator.strip("/")
        if any(c in disc for c in ("/", "@", "\\")):
            raise IdentityError(
                "extra_discriminator must not contain '/', '@' or '\\\\' "
                f"(got {extra_discriminator!r}): the L2 file name maps '/' to "
                "'--', so 'tenant/a' and 'tenant--a' would collide on disk")
        parts = [p for p in (base_model.strip("/"), self.short_fingerprint(),
                             disc) if p]
        name = "/".join(parts)
        if _LMC_MODEL_NAME_FORBIDDEN in name or "\x00" in name:
            raise IdentityError(
                f"lmcache model_name must not contain '@' or NUL (got {name!r})")
        return name


def _strip_quotes(value: str) -> str:
    """Strip one layer of shell quoting so `KVP_X="abc"` == `KVP_X=abc`."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _read_env_config(path: str) -> Dict[str, str]:
    """Parse a KEY=VALUE env file the way the bash loader does (quotes stripped)."""
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                line = line.removeprefix("export ").strip()
                if "=" not in line:
                    continue
                name, val = line.split("=", 1)
                name = name.strip()
                # Review fix (round 30): comment cut at " #" AND "\t#" --
                # identical to config._parse_env_file and env.sh _kvp_parse_line.
                for _sep in (" #", "\t#"):
                    if _sep in val:
                        val = val.split(_sep, 1)[0]
                val = val.strip()
                # Review fix (round 31/32): the name must be an ASCII
                # identifier -- python isidentifier() accepts Unicode
                # (КЛЮЧ=value passes) while bash rejects it, so use the same
                # explicit ASCII rule as config._parse_env_file.
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                    continue
                if name:
                    # Review fix (round 33): expand ${HOME}/$HOME exactly like
                    # config._parse_env_file and env.sh -- ${HOME} first.
                    v = _strip_quotes(val)
                    v = v.replace("${HOME}", os.path.expanduser("~"))
                    v = v.replace("$HOME", os.path.expanduser("~"))
                    out[name] = v
    except OSError:
        pass
    return out


def identity_from_env_files(*paths: str) -> "CacheIdentity":
    """Build a CacheIdentity by layering env files (later files win per key).

    Used by the launchers to compute the LMCache model_name from the SAME
    values the identity gate verifies, so the storage-level namespace and the
    verified pins can never drift apart.

    Precedence (review round 14): the process environment WINS over the files,
    matching `scripts/env.sh` (`_kvp_load_env`) and `kvpersist.config.load_config`.
    Without this, `LMCACHE_L2_SERDE=zstd` in the env with `none` in the file gave
    the file's namespace while `start-cache.sh` actually started the server
    with zstd -- data written under one serde read back with the other.
    """
    vals: Dict[str, str] = {}
    for p in paths:
        vals.update(_read_env_config(p))
    import os as _os
    for k in list(vals):
        if k in _os.environ:
            vals[k] = _os.environ[k]
    # Operator switches may live ONLY in the environment (they are commented
    # out in the shipped configs). Without this a `USE_LOSSLESS_SERDE=1` or
    # `KVP_L2_SERDE=zstd` export would be invisible to the namespace while
    # being honoured by the launcher -- two deployments sharing a cache root
    # would then cross-read each other's chunks.
    for k in ("LMCACHE_L2_SERDE", "KVP_L2_SERDE", "USE_LOSSLESS_SERDE"):
        if k in _os.environ:
            vals[k] = _os.environ[k]
    # Review fix (round 28, item 2): the KVP_L2_SERDE PIN in identity.env(.local)
    # must reach the namespace -- otherwise a zstd pin silently hashes as
    # "none" while the gate accepts it (chunks land under the wrong namespace).
    # It is already in `vals` from the file layers; the fold below must prefer
    # it over LMCACHE_L2_SERDE from the config files (env still wins).
    def get(name: str, default: str = "") -> str:
        return vals.get(name, default)
    # Fold the operator switch USE_LOSSLESS_SERDE into the effective
    # LMCACHE_L2_SERDE, exactly like scripts/env.sh `_kvp_resolve_l2_serde`:
    # the namespace must reflect the codec the server ACTUALLY stores, not
    # just the config file. A `USE_LOSSLESS_SERDE=1` launch stores zstd
    # chunks -- if the namespace were still hashed as "none" here, two
    # deployments sharing a cache root would cross-read each other's data.
    #
    # Precedence (the documented "env wins" contract, matching env.sh):
    #   1. LMCACHE_L2_SERDE / KVP_L2_SERDE from the ENVIRONMENT (always wins)
    #   2. USE_LOSSLESS_SERDE (operator shorthand, only when (1) is empty)
    #   3. LMCACHE_L2_SERDE from the config files
    #   4. "none"
    import os as _os2

    env_serde = (
        _os2.environ.get("LMCACHE_L2_SERDE") or _os2.environ.get("KVP_L2_SERDE") or ""
    ).strip()
    use_lossless = get("USE_LOSSLESS_SERDE").strip().lower()
    # Round 48: ONE semantic table shared by env.sh / _fold_serde.
    # "" (unset) = NO switch -> fall through to configured/default;
    # "0|no|false|none" = EXPLICIT raw opt-out -> "none".
    if use_lossless in ("1", "yes", "true", "zstd"):
        switch_serde = "zstd"
    elif use_lossless == "lz4":
        switch_serde = "lz4"
    elif use_lossless in ("0", "no", "false", "none"):
        switch_serde = "none"
    elif use_lossless == "":
        switch_serde = ""
    else:
        raise IdentityError(
            f"USE_LOSSLESS_SERDE={use_lossless!r} not understood "
            "(use 1 / zstd / lz4 / 0)"
        )
    # Review fix (round 28): the KVP_L2_SERDE PIN (identity.env[.local]) wins
    # over the LMCACHE_L2_SERDE config value -- the pin is the deployment's
    # declared codec and must reach the namespace, or zstd chunks land under
    # the "none" namespace while the gate accepts the pin. Env values still
    # win (they overwrote `vals` above). Precedence: env (LMCACHE_L2_SERDE or
    # KVP_L2_SERDE exported) > USE_LOSSLESS_SERDE switch > KVP_L2_SERDE pin >
    # LMCACHE_L2_SERDE file > none.
    if env_serde:
        serde = env_serde.strip().lower()
    elif switch_serde:
        serde = switch_serde
    else:
        _env_pin = _os.environ.get("KVP_L2_SERDE", "").strip()
        # Round 48 (user): the DEFAULT codec is lossless zstd -- matches
        # scripts/env.sh so the namespace is identical in bash and Python
        # with NO exports. Explicit none/raw stays byte-exact raw.
        serde = (get("KVP_L2_SERDE") or _env_pin or get("LMCACHE_L2_SERDE") or "zstd").strip().lower()
    # Advanced identity fields (review fix): these ARE read from the config
    # files / environment -- they were declared on the dataclass but never
    # populated, so two deployments differing only in LoRA / RoPE / multimodal
    # / speculative settings silently shared one namespace. Optional: empty
    # means "not applicable" (the dataclass default), a non-empty value
    # changes the namespace.
    rope_digest = get("KVP_ROPE_CONFIG_DIGEST")
    if rope_digest and not _SHA64.match(rope_digest):
        raise IdentityError(
            f"KVP_ROPE_CONFIG_DIGEST must be 64 hex chars (sha256), got {rope_digest!r}"
        )
    return CacheIdentity(
        model_id=get("KVP_MODEL_ID"),
        model_revision=get("KVP_MODEL_REVISION"),
        tokenizer_name=get("KVP_TOKENIZER_NAME"),
        tokenizer_revision=get("KVP_TOKENIZER_REVISION"),
        kv_cache_dtype=get("KVP_KV_CACHE_DTYPE"),
        block_size=int(get("KVP_BLOCK_SIZE") or 0),
        num_kv_heads=int(get("KVP_NUM_KV_HEADS") or 0),
        head_dim=int(get("KVP_HEAD_DIM") or 0),
        tp_size=int(get("KVP_TP_SIZE") or 0),
        connector=get("KVP_CONNECTOR"),
        connector_version=get("KVP_CONNECTOR_VERSION"),
        max_model_len=int(get("KVP_MAX_MODEL_LEN") or 0),
        chat_template_digest=get("KVP_CHAT_TEMPLATE_DIGEST"),
        namespace_hint=get("KVP_NAMESPACE_HINT"),
        lora_adapters=get("KVP_LORA_ADAPTERS"),
        multimodal_components=get("KVP_MULTIMODAL_COMPONENTS"),
        rope_config_digest=rope_digest,
        speculative_config=get("KVP_SPECULATIVE_CONFIG"),
        l2_serde=serde,
    )


def namespace_from_config(cfg: Dict[str, Any]) -> str:
    """Build a namespace from a config dict. Missing/empty values are uncertain."""
    fields = {f.name for f in dataclasses.fields(CacheIdentity)}
    unknown = set(cfg) - fields
    if unknown:
        raise IdentityError(f"unknown identity fields: {sorted(unknown)}")
    # Optional fields (default "") are NOT required: their absence means
    # "not applicable", which the dataclass already encodes. Only fields with
    # no sensible default must be present (review: the optional advanced
    # identity fields lora_adapters / multimodal_components /
    # rope_config_digest / speculative_config must not be demanded).
    optional = {"namespace_hint", "lora_adapters", "multimodal_components",
                "rope_config_digest", "speculative_config", "l2_serde"}
    required = fields - optional
    missing = required - set(cfg)
    if missing:
        raise IdentityError(f"missing identity fields (uncertain -> miss): {sorted(missing)}")
    return CacheIdentity(**cfg).namespace()