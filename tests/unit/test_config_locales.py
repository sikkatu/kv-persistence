"""Config locales must stay parallel across languages.

Like recipes' tp4.env.example (en/.ru/.zh): the localized files translate the
COMMENTS ONLY. Every variable name, its value and its order must match the
English reference exactly, so switching language can never change behaviour.
"""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2] / "configs"
PAIRS = [
    ROOT / "generic" / "cache.env",
    ROOT / "generic" / "lmcache-server.env",
    ROOT / "mimo-v2.6-pro" / "identity.env",
]

ASSIGN = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _pairs(text: str):
    out = []
    for line in text.splitlines():
        m = ASSIGN.match(line)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def test_locale_files_exist():
    for ref in PAIRS:
        for ext in (".ru", ".zh"):
            loc = Path(str(ref) + ext)
            assert loc.is_file(), f"{loc} is missing (localized knob reference)"


def test_locales_have_identical_variables_and_values():
    for ref_path in PAIRS:
        ref = _pairs(ref_path.read_text())
        assert ref, f"{ref_path}: no variables found"
        for ext in (".ru", ".zh"):
            loc = Path(str(ref_path) + ext)
            got = _pairs(loc.read_text())
            assert [n for n, _ in got] == [n for n, _ in ref], (
                f"{loc}: variables differ from the reference\n"
                f"  ref: {[n for n, _ in ref]}\n  loc: {[n for n, _ in got]}"
            )
            assert got == ref, (
                f"{loc}: values differ from the reference\n"
                f"  ref: {ref}\n  loc: {got}"
            )


def test_no_credentials_in_configs():
    pat = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD)\s*=\s*[A-Za-z0-9]{8}")
    for ref_path in PAIRS:
        for ext in ("", ".ru", ".zh"):
            p = Path(str(ref_path) + ext)
            for line in p.read_text().splitlines():
                assert not pat.search(line), f"{p.name}: possible credential: {line}"