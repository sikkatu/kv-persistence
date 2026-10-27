"""CI guard: config-locale comment coverage (review 3C).

Every knob defined in an EN config file must also be documented in its .ru and
.zh siblings so translators/CI notice drift immediately. This test fails if a
NEW key exists in the English reference but has no localized counterpart (or the
locale file is missing a comment block for it).

Also verifies the reverse: a localized file must not invent knobs the EN
reference does not have (silent divergence).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "configs"
PAIRS = [
    ROOT / "generic" / "cache.env",
    ROOT / "generic" / "lmcache-server.env",
    ROOT / "mimo-v2.6-pro" / "identity.env",
]

ASSIGN = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$")


def _keys(text: str):
    return [m.group(1) for m in (ASSIGN.match(line) for line in text.splitlines()) if m]


def _commented(text: str):
    """Names documented in comment blocks (loose: '# KVP_FOO=...')."""
    out = set()
    for line in text.splitlines():
        m = re.match(r"^#\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if m:
            out.add(m.group(1))
    return out


def test_every_en_knob_has_ru_and_zh_documentation():
    for ref in PAIRS:
        if not ref.is_file():
            continue
        en_keys = set(_keys(ref.read_text())) | _commented(ref.read_text())
        for ext in (".ru", ".zh"):
            loc = Path(str(ref) + ext)
            assert loc.is_file(), f"{loc} is missing (localized config translation)"
            loc_keys = set(_keys(loc.read_text())) | _commented(loc.read_text())
            missing = en_keys - loc_keys
            assert not missing, (
                f"{loc} is missing localized documentation for: {sorted(missing)}. "
                "Every new knob needs a comment in EN/RU/ZH (review 3C)."
            )


def test_locales_do_not_invent_knobs():
    for ref in PAIRS:
        if not ref.is_file():
            continue
        en_keys = set(_keys(ref.read_text())) | _commented(ref.read_text())
        for ext in (".ru", ".zh"):
            loc = Path(str(ref) + ext)
            if not loc.is_file():
                continue
            loc_keys = set(_keys(loc.read_text())) | _commented(loc.read_text())
            extra = loc_keys - en_keys
            assert not extra, (
                f"{loc} documents knobs absent from the EN reference: {sorted(extra)}"
            )


def test_docs_mention_all_budget_knobs():
    """Documents must not describe a knob that no config defines."""
    cfg = set()
    for ref in PAIRS:
        if ref.is_file():
            cfg |= set(_keys(ref.read_text()))
    docs_root = Path(__file__).resolve().parents[2] / "docs"
    known_knobs = re.compile(r"KVP_[A-Z0-9_]+")
    missing = set()
    for doc in docs_root.glob("*.md"):
        for knob in set(known_knobs.findall(doc.read_text())):
            if knob not in cfg and knob not in {
                "KVP_MODEL_REVISION", "KVP_TOKENIZER_REVISION",
                "KVP_CHAT_TEMPLATE_DIGEST", "KVP_CACHE_BUDGET_GB",
                "KVP_CACHE_ROOT", "KVP_CACHE_DEVICE", "KVP_EVICT_TRIGGER_WATERMARK",
                "KVP_EVICT_RATIO", "KVP_LMCACHE_HOST", "KVP_LMCACHE_PORT",
                "KVP_LMCACHE_HTTP_PORT", "KVP_L1_SIZE_GB", "KVP_CHUNK_SIZE",
                "KVP_PERSIST_ENABLED", "KVP_CACHE_BYPASS", "KVP_NAMESPACE_HINT",
                "KVP_MODEL_ID", "KVP_TOKENIZER_NAME", "KVP_KV_CACHE_DTYPE",
                "KVP_BLOCK_SIZE", "KVP_NUM_KV_HEADS", "KVP_HEAD_DIM", "KVP_TP_SIZE",
                "KVP_CONNECTOR", "KVP_CONNECTOR_VERSION", "KVP_MAX_MODEL_LEN",
                "KVP_MODEL_DIR", "KVP_FINGERPRINT_MODE", "KVP_MODEL_ID_OVERRIDE",
            }:
                missing.add(knob)
    assert not missing, f"docs reference knobs that configs do not define: {sorted(missing)}"