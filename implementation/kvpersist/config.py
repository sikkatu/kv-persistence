"""Single source of configuration loading for all kv-persistence scripts.

Review finding: start-cache.sh used plain '.' sourcing while preflight.sh used
a hand-rolled seeder — two load mechanisms with different env-priority
semantics. This module is the ONE implementation: env wins over files, then
.env and .local, with the same parsing everywhere. Every script must call
load_config() instead of sourcing env files directly.

Also resolves the LMCACHE_* vs KVP_LMCACHE_* duplication (review finding):
cache.env holds the KVP_ names (docs-facing); lmcache-server.env holds the
LMCACHE_* names actually used by `lmcache server`. When both define the same
knob, the effective value must agree -- verify() enforces it and reports a
config split instead of silently letting the user change one side.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import re

# Review fix (round 32): the strict env-file format allows ONLY ASCII
# identifiers -- python str.isidentifier() accepts Unicode (КЛЮЧ=value
# passes) while bash rejects it. One rule for all parsers.
_ASCII_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(Exception):
    """Malformed configuration or conflicting duplicate knobs."""


# knob mapping: KVP_ name in cache.env  ->  LMCACHE_ name in lmcache-server.env
# These are the same setting expressed in the two files (the review's duplicate).
_KNOB_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("KVP_LMCACHE_HOST", "LMCACHE_HOST"),
    ("KVP_LMCACHE_PORT", "LMCACHE_PORT"),
    ("KVP_LMCACHE_HTTP_PORT", "LMCACHE_HTTP_PORT"),
    ("KVP_L1_SIZE_GB", "LMCACHE_L1_SIZE_GB"),
    ("KVP_CHUNK_SIZE", "LMCACHE_CHUNK_SIZE"),
    ("KVP_EVICT_TRIGGER_WATERMARK", "LMCACHE_EVICTION_TRIGGER_WATERMARK"),
    ("KVP_EVICT_RATIO", "LMCACHE_EVICTION_RATIO"),
)


# Round 28: a knob may be spelled LMCACHE_* (lmcache-server.env) OR KVP_*
# (cache.env). They are ONE setting. Conflicts must be resolved by LAYER ORDER
# (process env > .local files > base files), NOT by "alias always wins" --
# cache.env ships active KVP_* defaults, and those must not override an
# operator's LMCACHE_* edit in lmcache-server.env.local.
_ALIAS_MAP = {
    "LMCACHE_HOST": "KVP_LMCACHE_HOST",
    "LMCACHE_PORT": "KVP_LMCACHE_PORT",
    "LMCACHE_HTTP_PORT": "KVP_LMCACHE_HTTP_PORT",
    "LMCACHE_L1_SIZE_GB": "KVP_L1_SIZE_GB",
    "LMCACHE_CHUNK_SIZE": "KVP_CHUNK_SIZE",
    "LMCACHE_EVICTION_TRIGGER_WATERMARK": "KVP_EVICT_TRIGGER_WATERMARK",
    "LMCACHE_EVICTION_RATIO": "KVP_EVICT_RATIO",
    "LMCACHE_L2_SERDE": "KVP_L2_SERDE",
}

def resolve_layered(repo_root: str) -> Dict[str, str]:
    """Merge every env file by LAYER (base first, .local last) with BOTH
    spellings of a knob folded into ONE name (the LMCACHE_* spelling).
    The LAST layer to set either spelling wins -- identical semantics to
    scripts/env.sh _kvp_seed (which exports in this order).
    """
    root = Path(repo_root)
    gen = root / "configs" / "generic"
    layers = [
        gen / "cache.env",
        gen / "lmcache-server.env",
        gen / "cache.env.local",
        gen / "lmcache-server.env.local",
    ]
    for d in sorted(root.glob("configs/*")):
        if d.name != "generic":
            layers.insert(2, d / "identity.env")
            layers.append(d / "identity.env.local")
    out: Dict[str, str] = {}
    for p in layers:
        for k, v in _parse_env_file(p).items():
            out[k] = v
    # fold aliases into the canonical LMCACHE_* spelling by layer order:
    # a later assignment to EITHER spelling replaces the folded value
    folded: Dict[str, str] = {}
    for p in layers:
        vals = _parse_env_file(p)
        for k, v in vals.items():
            canonical = k
            for lm, kv in _ALIAS_MAP.items():
                if k == kv:
                    canonical = lm
            folded[canonical] = v
    # preserve non-alias keys as-is
    for k, v in out.items():
        if k not in _ALIAS_MAP.values() and k not in _ALIAS_MAP:
            folded.setdefault(k, v)
    return folded

def _parse_env_file(path: Path) -> Dict[str, str]:
    """Parse KEY=VALUE env file. MUST match scripts/env.sh `_kvp_parse_line`."""
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        name, val = line.split("=", 1)
        name = name.strip()
        # Comment cut: a ` #` (whitespace + hash) starts a trailing comment --
        # the same rule as bash `${_val%% #*}`. (A `#` INSIDE a value with no
        # preceding whitespace is part of the value.)
        for sep in (" #", "\t#"):
            if sep in val:
                val = val.split(sep, 1)[0]
        val = val.strip()
        # Quote stripping: ONE matching outer pair (single or double), same as
        # the bash loader's `_kvp_unquote`. Review round 13: the old code did
        # .strip('"').strip("'") which only removes *outermost* characters and
        # mis-parsed values like "a'b".
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        val = val.replace("$HOME", os.path.expanduser("~")).replace("${HOME}", os.path.expanduser("~"))
        if name and _ASCII_IDENT.match(name):
            out[name] = val
    return out


def read_env_file(path: str) -> Dict[str, str]:
    """Public wrapper around the ONE env-file parser shared by load_config,
    the identity gate (verify-engine-identity.sh) and the launchers.

    Handles `export KEY=...`, trailing ` #` comments, one matching outer quote
    pair and $HOME / ${HOME} expansion -- byte-for-byte the rules of
    scripts/env.sh `_kvp_parse_line` and kvpersist/config.py `_parse_env_file`.
    Missing files yield an empty dict (never an error).
    """
    return _parse_env_file(Path(path))

def load_config(repo_root: str, *, env_wins: bool = True, model_config: Optional[str] = None) -> Dict[str, str]:
    """Merge store env files in order. Precedence (env_wins=True):

    1. existing process environment (always wins -- documented contract)
    2. identity.env.local
    3. lmcache-server.env.local
    4. cache.env.local
    5. identity.env
    6. lmcache-server.env
    7. cache.env

    Files are applied in the order below and a LATER file overrides an
    EARLIER one for the same key. This is EXACTLY the order the bash loader
    seeds them in `scripts/env.sh` `_kvp_load_env`:
    cache.env -> lmcache-server.env -> identity.env -> cache.env.local ->
    lmcache-server.env.local -> identity.env.local. (Review round 13: an
    earlier revision put the .local files BEFORE some base files here, so a
    key set in both cache.env.local and lmcache-server.env resolved to the
    lmcache-server.env value in Python but to the cache.env.local value in
    bash. The two loaders now agree byte-for-byte on precedence.)
    env_wins=False returns the file values only (used for consistency checks).
    """
    root = Path(repo_root)
    mc = model_config if model_config else _resolve_model_config(root)
    order = [
        root / "configs/generic/cache.env",
        root / "configs/generic/lmcache-server.env",
        root / f"configs/{mc}/identity.env",
        root / "configs/generic/cache.env.local",
        root / "configs/generic/lmcache-server.env.local",
        root / f"configs/{mc}/identity.env.local",
    ]
    merged: Dict[str, str] = {}
    for p in order:
        # Later file wins for every key (identical to the bash seeder).
        merged.update(_parse_env_file(p))
    if env_wins:
        # Review fix (round 28): "env wins over files" means an EXPORTED
        # operator value is visible even when NO file defines that key (the
        # KVP_* aliases are commented out in cache.env by default now). Also
        # fold an exported KVP_* alias into the canonical LMCACHE_* spelling
        # so the namespace and the server see ONE value.
        for k in os.environ:
            if k.startswith("KVP_") or k.startswith("LMCACHE_") or k == "USE_LOSSLESS_SERDE":
                merged[k] = os.environ[k]
        for _lm, _kv in _ALIAS_MAP.items():
            if _kv in os.environ:
                merged[_lm] = os.environ[_kv]
    return merged


def _resolve_model_config(root: Path) -> str:
    """Single source of truth for which model config directory to use.

    Resolution order (first non-empty wins):
      1. KVP_MODEL_CONFIG environment variable.
      2. The single directory under configs/ that contains identity.env
         (besides 'generic'). If MULTIPLE such directories exist, raise -- an
         ambiguous layout must never silently pick one (review: no silent
         fallback to mimo-v2.6-pro).
    """
    import os as _os
    env = _os.environ.get("KVP_MODEL_CONFIG", "").strip()
    if env:
        return env
    candidates = [
        d.name for d in (root / "configs").iterdir()
        if d.is_dir() and d.name != "generic"
        and (d / "identity.env").is_file()
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        # No model-specific directory (e.g. a generic-only deployment). The
        # loader tolerates missing files; this is NOT a silent fallback to a
        # model -- it simply means "no model identity file exists here".
        return "generic"
    raise ConfigError(
        f"multiple model config directories found: {sorted(candidates)}; "
        "set KVP_MODEL_CONFIG explicitly to choose one (no silent default)"
    )


def verify(repo_root: str) -> List[str]:
    """Return a list of conflict messages (empty = consistent)."""
    merged = load_config(repo_root, env_wins=False)
    conflicts: List[str] = []
    for kv, lm in _KNOB_PAIRS:
        if kv in merged and lm in merged and merged[kv] != merged[lm]:
            conflicts.append(
                f"{kv}={merged[kv]!r} (cache.env) does not match "
                f"{lm}={merged[lm]!r} (lmcache-server.env) -- same knob, two "
                f"files; pick ONE value in both or clear the duplicate"
            )
    return conflicts


def export_env(merged: Dict[str, str], *, only_missing: bool = True) -> None:
    """Export mapping into os.environ (for shell-emit helpers). only_missing
    preserves the env-wins contract."""
    for k, v in merged.items():
        if only_missing and k in os.environ:
            continue
        os.environ[k] = v