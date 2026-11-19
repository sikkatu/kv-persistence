# SPDX-License-Identifier: Apache-2.0
"""r16 P4.2: two shell gates that mutation tests previously missed.

The r15 P0' upgrade added an ancestor gate for PR #6608 (`PR6608_MERGE`) and
the r15 P1 wiring added the empty-scope refusal for the import gate
(`GATE_SCOPE` empty -> BUILD REFUSED). A mutation `if false;` on the first
and `true || {` on the second left every test green -- nothing exercised
those shell blocks.

These tests EXTRACT the real blocks from `scripts/build-lmcache.sh` (by their
markers) and run them under real bash with a real mini git repo:
  (1) the #6608 ancestor gate must die when the commit is NOT an ancestor
      (mutation: `if false;` -> this test RED);
  (2) the empty-scope refusal must die with BUILD REFUSED when GATE_SCOPE is
      empty (mutation: `true || {` -> this test RED).
"""
from __future__ import annotations
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"

GIT_ENV = dict(os.environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _git(args, cwd) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       env=GIT_ENV, cwd=str(cwd))
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _extract(text: str, start: str, end: str) -> str:
    lines = text.splitlines()
    s = next(i for i, ln in enumerate(lines) if start in ln)
    e = next(i for i, ln in enumerate(lines) if i > s and end in ln)
    return "\n".join(lines[s:e + 1])


def _mini_repo(tmp_path: pathlib.Path, with_ancestor: bool) -> tuple[pathlib.Path, str, str]:
    """A mini git repo; returns (repo, HEAD sha, PR6608 sha).
    `with_ancestor=True` -> the PR6608 commit is an ancestor of HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "file.txt").write_text("base\n")
    _git(["init", "-q"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "base"], repo)
    base = _git(["rev-parse", "HEAD"], repo)
    if with_ancestor:
        pr = base  # the base commit itself is trivially an ancestor
    else:
        (repo / "file.txt").write_text("base\nother\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-qm", "other"], repo)
        pr = _git(["rev-parse", "HEAD"], repo)  # HEAD is not an ancestor of base... swap
        # make HEAD ahead of pr: pr is an ancestor of HEAD in this branch;
        # to have a NON-ancestor we need pr on a side branch
        _git(["checkout", "-q", "-b", "side"], repo)
        (repo / "file.txt").write_text("base\nside\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-qm", "side"], repo)
        pr = _git(["rev-parse", "HEAD"], repo)
        _git(["checkout", "-q", "master"], repo) if False else _git(["checkout", "-q", "-"], repo)
    head = _git(["rev-parse", "HEAD"], repo)
    return repo, head, pr


def _ancestor_gate_block() -> str:
    text = BUILD_LMC.read_text()
    # the gate comment (NOT the PR6608_MERGE assignment comment above)
    return _extract(text, "#6608 (KVCacheGroup in the MP connector) is required",
                    "note \"PR #6608")


def test_pr6608_ancestor_gate_dies_when_not_ancestor(tmp_path):
    """(1) the real ancestor-gate block must die when PR6608 is NOT an
    ancestor of the pinned commit (mutation `if false;` -> RED)."""
    block = _ancestor_gate_block()
    # sanity: the block really checks the ancestor relation
    assert "merge-base --is-ancestor" in block, (
        "the extracted block must contain the ancestor check")
    repo, head, pr_nonancestor = _mini_repo(tmp_path, with_ancestor=False)
    driver = tmp_path / "ancestor-driver.sh"
    driver.write_text("".join([
        "set -Eeuo pipefail\n",
        "die() { echo \"ERROR: $*\" >&2; exit 1; }\n",
        "note() { echo \"[note] $*\"; }\n",
        f'SRC="{repo}"\n',
        f'LMCACHE_SOURCE_COMMIT="{head}"\n',
        f'PR6608_MERGE="{pr_nonancestor}"\n',
        block + "\n",
        "echo REACHED_END\n",
    ]))
    r = subprocess.run(["bash", str(driver)], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode != 0, (
        f"a non-ancestor PR6608 must abort the build:\n{r.stdout}")
    assert "NOT an ancestor" in r.stderr, r.stderr
    assert "REACHED_END" not in r.stdout, (
        "the gate must stop the script before any later step")


def test_pr6608_ancestor_gate_passes_when_ancestor(tmp_path):
    """(1b) positive control: when the commit IS an ancestor the gate
    passes."""
    block = _ancestor_gate_block()
    repo, head, pr_ancestor = _mini_repo(tmp_path, with_ancestor=True)
    driver = tmp_path / "ancestor-ok.sh"
    driver.write_text("".join([
        "set -Eeuo pipefail\n",
        "die() { echo \"ERROR: $*\" >&2; exit 1; }\n",
        "note() { echo \"[note] $*\"; }\n",
        f'SRC="{repo}"\n',
        f'LMCACHE_SOURCE_COMMIT="{head}"\n',
        f'PR6608_MERGE="{pr_ancestor}"\n',
        block + "\n",
        "echo REACHED_END\n",
    ]))
    r = subprocess.run(["bash", str(driver)], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, f"an ancestor PR6608 must pass:\n{r.stderr}"
    assert "REACHED_END" in r.stdout, r.stdout


def _empty_scope_block() -> str:
    text = BUILD_LMC.read_text()
    lines = text.splitlines()
    s = next(i for i, ln in enumerate(lines) if "[ -n \"$GATE_SCOPE\" ]" in ln)
    # the refusal block runs until the closing brace line
    e = next(i for i, ln in enumerate(lines) if i > s and ln.strip() == "}")
    return "\n".join(lines[s:e + 1])


def test_empty_gate_scope_refused(tmp_path):
    """(2) the real empty-scope block must refuse with BUILD REFUSED when
    GATE_SCOPE is empty (mutation `true || {` -> RED)."""
    block = _empty_scope_block()
    assert "GATE_SCOPE" in block, "the extracted block must test GATE_SCOPE"
    driver = tmp_path / "scope-driver.sh"
    driver.write_text("".join([
        "set -Eeuo pipefail\n",
        'GATE_SCOPE=""\n',
        block + "\n",
        "echo REACHED_END\n",
    ]))
    r = subprocess.run(["bash", str(driver)], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode != 0, "an empty gate scope must be refused"
    out = r.stdout + r.stderr
    assert "BUILD REFUSED" in out, f"the refusal must say BUILD REFUSED:\n{out}"
    assert "gate scope is EMPTY" in out, (
        f"the refusal must name the empty scope:\n{out}")
    assert "REACHED_END" not in r.stdout, (
        "the refusal must stop the script")