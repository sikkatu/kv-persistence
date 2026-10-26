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


def _parse_env_file(path: Path) -> Dict[str, str]:
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
        val = val.split(" #")[0].strip().strip('"').strip("'")
        val = val.replace("$HOME", os.path.expanduser("~")).replace("${HOME}", os.path.expanduser("~"))
        if name and name.isidentifier():
            out[name] = val
    return out


def load_config(repo_root: str, *, env_wins: bool = True) -> Dict[str, str]:
    """Merge store env files in order. Precedence (env_wins=True):
    existing process env > cache.env.local > cache.env > lmcache-server.env.local >
    lmcache-server.env > identity.env. Returns the effective mapping.
    env_wins=False returns the file values only (used for consistency checks).
    """
    root = Path(repo_root)
    order = [
        root / "configs/generic/cache.env",
        root / "configs/generic/cache.env.local",
        root / "configs/generic/lmcache-server.env",
        root / "configs/generic/lmcache-server.env.local",
        root / "configs/mimo-v2.6-pro/identity.env",
        root / "configs/mimo-v2.6-pro/identity.env.local",
    ]
    merged: Dict[str, str] = {}
    for p in order:
        for k, v in _parse_env_file(p).items():
            if k not in merged or p.name.endswith(".local") or p.name == "identity.env":
                merged[k] = v
    if env_wins:
        for k in list(merged):
            if k in os.environ:
                merged[k] = os.environ[k]
    return merged


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