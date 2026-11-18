# SPDX-License-Identifier: Apache-2.0
"""r3 P1-1: build-lmcache.sh must not swallow the pytest exit code.

The old in-container test phase did `pytest ... || true` then `grep -qE
'^FAILED|error'` -- two problems:
  * any word 'error' in output triggered a false failure, and
  * a collection crash / segfault WITHOUT those words sailed through as success.
The rc is the only authoritative signal. Must be: `set +e; pytest ...; rc=$?;
set -e`, refuse at rc != 0, and count skips from the summary line / --junitxml,
never `grep -c '^SKIPPED'`.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-lmcache.sh"
# r6-7 A-1: the pytest phase lives in the in-container file
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"


def test_pytest_rc_is_captured_not_swallowed():
    src = INCONTAINER.read_text()
    assert "|| true" not in _pytest_block(src), (
        "the `pytest ... || true` line must be gone -- it swallows the exit code"
    )
    assert "rc=$?" in src, "pytest's exit code must be captured into rc"
    assert any(needle in src for needle in ('rc != 0', '"$rc" != "0"', "rc -ne 0")), (
        "a non-zero pytest rc must abort the build"
    )


def test_skips_counted_from_summary_not_grep_skipped():
    src = INCONTAINER.read_text()
    assert "grep -cE '^SKIPPED'" not in src and "grep -c '^SKIPPED'" not in src, (
        "skip counts must come from --junitxml or the summary line, not grep of SKIPPED rows"
    )
    assert "junitxml" in src or "passed" in src, (
        "a --junitxml report or a summary-line parse must exist for skip accounting"
    )


def test_mutation_reintroducing_or_true_is_red():
    """Mutation detector: putting back `|| true` must trip the wiring check."""
    import re
    mutant = re.sub(
        r"^(\s*python3 -m pytest .*)$",
        r"\1 || true",
        INCONTAINER.read_text(),
        flags=re.M,
    )
    assert "|| true" in _pytest_block(mutant), (
        "mutation failed to reintroduce the || true escape"
    )
    with pytest.raises(AssertionError):
        _assert_no_swallow(mutant)


def _pytest_block(src: str) -> str:
    """Extract just the pytest invocation + surrounding flow (rough cut)."""
    lines = src.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if "python3 -m pytest" in ln:
            return "".join(lines[max(0, i - 2) : i + 6])
    return ""


def _assert_no_swallow(src: str) -> None:
    assert "|| true" not in _pytest_block(src), (
        "the `pytest ... || true` line must be gone -- it swallows the exit code"
    )
    assert "rc=$?" in src, "pytest's exit code must be captured into rc"