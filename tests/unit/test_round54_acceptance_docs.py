"""Round 54: hardware acceptance recorded -- status claims must be honest.

The user's hardware run (2026-11-10) proved restart-recovery on 4x RTX
PRO 6000: scenario A cached_tokens=2560 (memory hit), scenario B
cached_tokens=2560 + L2 read events=1 (disk-backed), 0 hard failures.
The docs must carry the ACCEPTED status with the evidence, and the
machine verdict must stay honest (INCONCLUSIVE on KVP_CACHE_DEVICE).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / name).read_text()


def test_readme_status_records_acceptance():
    for name, needle in (
        ("README.md", "Restart-recovery acceptance PASSED"),
        ("README.ru.md", "ПРОЙДЕНА на целевом GPU-хосте"),
        ("README.zh.md", "硬件验收已通过"),
    ):
        t = _read(name)
        assert needle in t, "%s must record the hardware acceptance" % name
        assert "2560" in t, "%s must cite the cached_tokens evidence" % name


def test_final_reports_status_is_accepted():
    fr = _read("docs/final-report.md")
    assert "ACCEPTED (hardware 2026-11-10)" in fr
    assert "L2 read events = 1" in fr
    assert "KVP_CACHE_DEVICE" in fr, (
        "the report must stay honest about the INCONCLUSIVE machine verdict")
    ru = _read("docs/final-report.ru.md")
    assert "ПРИНЯТО (железо 2026-11-10)" in ru
    zh = _read("docs/final-report.zh.md")
    assert "验收通过（硬件 2026-11-10）" in zh


def test_compatibility_matrix_verified_rows():
    for name in ("docs/compatibility.md", "docs/compatibility.ru.md",
                 "docs/compatibility.zh.md"):
        t = _read(name)
        assert "2026-11-10" in t, "%s must date the hardware verification" % name
        # the restore rows must NOT claim NEEDS TESTING anymore
        for line in t.splitlines():
            if "Restore after" in line or "Восстановление после" in line or "恢复" in line:
                assert "NEEDS TESTING" not in line, (
                    "%s: restore rows must be VERIFIED: %r" % (name, line))


def test_no_stale_partial_status():
    fr = _read("docs/final-report.md")
    assert "Overall status: PARTIAL" not in fr
    ru = _read("docs/final-report.ru.md")
    assert "Общий статус: PARTIAL" not in ru
    zh = _read("docs/final-report.zh.md")
    assert "总状态：PARTIAL" not in zh


def test_changelog_records_round54():
    """Round 54 evidence lives in the git log now (the CHANGELOG keeps only
    the newest 3 rounds, user request, round 56). The policy itself is
    asserted: at most 3 round sections, no verbatim request quotes."""
    import re
    for name in ("CHANGELOG.md", "CHANGELOG.ru.md", "CHANGELOG.zh.md"):
        t = _read(name)
        rounds = re.findall(r"^## (Round|Раунд|第 )", t, re.M)
        assert len(rounds) <= 3, (
            "%s must keep only the newest 3 rounds, found %d" % (name, len(rounds)))
        assert "user:" not in t and "пользователь:" not in t, (
            "%s must not carry verbatim request quotes" % name)