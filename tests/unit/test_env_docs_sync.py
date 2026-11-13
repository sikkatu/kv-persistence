"""Spec 11.3 round 88: every KVP_OBS_* env var the code understands MUST be
documented in the wiki Configuration page (EN/RU/ZH), and the wiki must not
document variables the code does not know about. Not substring grep -- we
EXTRACT both sets (regex over real sources) and compare them, so adding a
variable to code without docs (or vice versa) fails loudly.

Sources scanned for code side:
  scripts/extras-up.sh, scripts/start-all.sh, scripts/start-observability.sh
  observability/docker-compose.override.yml
Docs side:
  the checked-out wiki repo (sikkatu/kv-persistence.wiki) -- if not next to
  this repo (CI vs developer layout), the test SKIPs with a clear reason.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]

CODE_FILES = [
    KV / "scripts" / "extras-up.sh",
    KV / "scripts" / "start-all.sh",
    KV / "scripts" / "start-observability.sh",
    KV / "observability" / "docker-compose.override.yml",
]

WIKI_CANDIDATES = [
    KV.parent / "kvp.wiki",                 # dev layout: sibling checkout
    KV.parent / "kv-persistence.wiki",      # alt name
    KV / "kvp.wiki",
    Path.home() / "kvp.wiki",
    Path("/home/review/kvp.wiki"),          # local review layout
    Path("/home/work/kvp-wiki"),            # local review layout (alt)
]


def _wiki_dir() -> Path | None:
    import os
    override = os.environ.get("KVP_WIKI_DIR")
    if override and (Path(override) / "Configuration.md").is_file():
        return Path(override)
    for c in WIKI_CANDIDATES:
        if (c / "Configuration.md").is_file() or (c / "Configuration.ru.md").is_file():
            return c
    return None


def _code_vars() -> set[str]:
    """KVP_OBS_* names referenced by the code (env references like
    ${KVP_OBS_GPU:-...} and bare $KVP_OBS_*)."""
    pat = re.compile(r"\bKVP_OBS_[A-Z0-9_]+")
    found: set[str] = set()
    for f in CODE_FILES:
        if not f.is_file():
            continue
        found |= set(pat.findall(f.read_text()))
    return found


def _doc_vars() -> set[str]:
    wiki = _wiki_dir()
    if wiki is None:
        pytest.skip("SKIP: kv-persistence.wiki checkout not found next to this repo -- "
                    "env-var <-> Configuration sync checked on the operator stand only")
    found: set[str] = set()
    for fn in ("Configuration.md", "Configuration.ru.md", "Configuration.zh.md"):
        p = wiki / fn
        if p.is_file():
            found |= set(re.findall(r"\bKVP_OBS_[A-Z0-9_]+", p.read_text()))
    return found


def test_env_vars_and_wiki_stay_in_sync():
    """Code-side and docs-side KVP_OBS_* sets must match EXACTLY (spec 11.3).
    MUTATION caught: adding KVP_OBS_NEW=... to extras-up.sh without a
    Configuration entry, or documenting KVP_OBS_GHOST with no implementation."""
    code = _code_vars()
    doc = _doc_vars()
    if not code:
        pytest.fail("no KVP_OBS_* variables found in code -- scanning is broken")
    assert code == doc, (
        f"KVP_OBS_* mismatch between code and wiki Configuration:\n"
        f"  in code but NOT documented: {sorted(code - doc)}\n"
        f"  documented but NOT in code: {sorted(doc - code)}"
    )


def test_all_three_locales_cover_every_var():
    """Every KVP_OBS_* variable is documented in ALL THREE Configuration
    locales (EN/RU/ZH) -- a ru-only or en-only entry is a silent divergence
    for non-English readers."""
    wiki = _wiki_dir()
    if wiki is None:
        pytest.skip("SKIP: wiki checkout not found")
    code = _code_vars()
    for fn in ("Configuration.md", "Configuration.ru.md", "Configuration.zh.md"):
        p = wiki / fn
        assert p.is_file(), f"{fn} missing in wiki"
        locale_vars = set(re.findall(r"\bKVP_OBS_[A-Z0-9_]+", p.read_text()))
        missing = code - locale_vars
        assert not missing, (
            f"{fn} does not document {sorted(missing)}"
        )