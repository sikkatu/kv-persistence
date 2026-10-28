"""Cache identity and compatibility namespaces.

Every persistent KV entry must belong to a namespace derived from ALL runtime
parameters that materially affect KV values. Two deployments may share KV data
only when every identity field matches exactly. Any missing or uncertain field
yields a cache MISS (IdentityError -> caller falls back to recompute).

The namespace is a SHA-256 over canonical JSON of the identity fields, so it is
stable across restarts, hosts and Python versions.

LIMITATION (review P1-3): this namespace is a LOCAL policy-layer concept --
KV-persistence computes it from identity fields, but the actual L2 keys LMCache
uses internally for store/retrieve are LMCache's own (derived from its
connector's KV-chunk identity, NOT directly from this namespace). Until an
end-to-end test drives real LMCache store/retrieve calls and proves that
changing EACH identity field causes a cache MISS at the LMCache API level (not
just at the namespace computation level), this namespace should be treated as
a best-effort layer for local policy/labels, NOT as a proven guarantee of
cross-config isolation at the LMCache storage-key level. See
docs/final-report.md known-limitations for the required hardware test.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Dict

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")


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
        return {
            f.name: getattr(self, f.name)
            for f in sorted(self.__dataclass_fields__.values(), key=lambda x: x.name)
            if f.name != "namespace_hint"
        }

    def namespace(self) -> str:
        """Stable namespace usable as a directory name / cache key prefix."""
        payload = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return "kvp-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def short_fingerprint(self) -> str:
        return self.namespace().replace("kvp-", "", 1)


def namespace_from_config(cfg: Dict[str, Any]) -> str:
    """Build a namespace from a config dict. Missing/empty values are uncertain."""
    fields = {f.name for f in dataclasses.fields(CacheIdentity)}
    unknown = set(cfg) - fields
    if unknown:
        raise IdentityError(f"unknown identity fields: {sorted(unknown)}")
    required = fields - {"namespace_hint"}
    missing = required - set(cfg)
    if missing:
        raise IdentityError(f"missing identity fields (uncertain -> miss): {sorted(missing)}")
    return CacheIdentity(**cfg).namespace()