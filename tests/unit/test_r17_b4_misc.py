# SPDX-License-Identifier: Apache-2.0
"""r17 B4: two small but real defects.

B4.1 -- vendor/patches/lmcache/SOURCES.md: the blank line after the last
table row and after a heading was eaten (GFM turns a paragraph directly
after a table row INTO a table row). The test asserts the GFM structure:
after every table (last `|` row) and after every heading there MUST be a
blank line.

B4.2 -- C4 (build dependencies): the constraints file is grepped from the
baseline freeze with `^(torch|vllm|numpy|triton)`; when NOTHING matches the
file is empty and pip is free to touch the engine stack during the build-
deps install (only C14 notices, 10 minutes later). The block must print the
pins and refuse with `BUILD REFUSED: no engine pins found in baseline
freeze` BEFORE any install (same first-20-lines diagnostic as C14). The
C4 block is EXTRACTED from _incontainer_build.sh by its markers, never a
copy.
"""
from __future__ import annotations
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCES = ROOT / "vendor" / "patches" / "lmcache" / "SOURCES.md"
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"


def test_sources_blank_lines():
    lines = SOURCES.read_text().splitlines()
    problems = []
    for i, ln in enumerate(lines[:-1]):
        nxt = lines[i + 1]
        if ln.startswith("|"):
            # a table row followed by a non-blank NON-table line becomes a
            # table row in GFM -- must be separated by a blank line
            if nxt.strip() and not nxt.startswith("|"):
                problems.append(
                    f"{SOURCES}:{i + 2}: table row not followed by a blank "
                    f"line (next: {nxt[:50]!r})")
        if re.match(r"^#{1,6} ", ln):
            if nxt.strip():
                problems.append(
                    f"{SOURCES}:{i + 2}: heading not followed by a blank "
                    f"line (next: {nxt[:50]!r})")
    assert not problems, "GFM structure broken:\n" + "\n".join(problems)


def _c4_block() -> str:
    text = INCONTAINER.read_text()
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if "# ---- C4" in ln)
    end = next(i for i, ln in enumerate(lines) if i > start and "# ---- C5" in ln)
    return "\n".join(lines[start + 1:end])


def _run_c4(tmp_path: pathlib.Path, freeze_body: str):
    """Run the REAL C4 block with a pip shim that records every install
    invocation (a refusal must happen BEFORE any install)."""
    work = tmp_path / "kvp-work"
    work.mkdir()
    (work / "image-freeze.before.txt").write_text(freeze_body)
    lmc_src = tmp_path / "src"
    (lmc_src / "requirements").mkdir(parents=True)
    (lmc_src / "requirements" / "build.txt").write_text("ninja\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pip").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{tmp_path / "pip-calls.txt"}"\n'
        "exit 0\n")
    (bin_dir / "pip").chmod(0o755)
    driver = tmp_path / "c4-driver.sh"
    driver.write_text("".join([
        "set -Eeuo pipefail\n",
        f'KVP_WORK="{work}"\n',
        f'LMC_SRC="{lmc_src}"\n',
        _c4_block() + "\n",
        "echo C4-DONE\n",
    ]))
    env = {"PATH": str(bin_dir) + ":" + "/usr/bin:/bin"}
    return subprocess.run(["bash", str(driver)], capture_output=True,
                          text=True, timeout=30, env=env, cwd=str(tmp_path))


def test_c4_empty_constraints_refused_before_install(tmp_path):
    """An empty match (no engine pins in the freeze) must refuse BEFORE the
    build-deps install, with the same first-20-lines diagnostic as C14."""
    freeze = "aiohttp==3.9.5\nsetuptools==75.1.0\nlmcache==0.5.7\n"
    r = _run_c4(tmp_path, freeze)
    assert r.returncode != 0, (
        f"empty constraints must be refused (rc={r.returncode}):\n{r.stdout}")
    out = r.stdout + r.stderr
    assert "BUILD REFUSED" in out, out
    assert "no engine pins found in baseline freeze" in out, out
    assert "first 20 lines of the baseline freeze" in out, out
    assert "setuptools==75.1.0" in out, (
        f"the refusal must show the baseline head:\n{out}")
    # the refusal must happen BEFORE any pip install
    assert not (tmp_path / "pip-calls.txt").exists(), (
        "pip install must NOT run when the constraints are empty")
    assert "C4-DONE" not in r.stdout, "the block must stop at the refusal"


def test_c4_nonempty_constraints_printed_and_install_runs(tmp_path):
    """A non-empty match -> the pins are printed and the install runs (the
    lines themselves are NOT changed -- only printed)."""
    freeze = ("torch==2.14.0+cu130\nvllm==0.9.1\nnumpy==2.1.0\ntriton==3.4.0\n"
              "aiohttp==3.9.5\n")
    r = _run_c4(tmp_path, freeze)
    assert r.returncode == 0, (
        f"a non-empty match must pass:\n{r.stdout}\n{r.stderr}")
    assert "build constraints (4 lines)" in r.stdout, r.stdout
    assert "torch==2.14.0+cu130" in r.stdout and "triton==3.4.0" in r.stdout, (
        f"the pins must be printed:\n{r.stdout}")
    assert "aiohttp" not in r.stdout, (
        f"non-engine lines must not be printed as constraints:\n{r.stdout}")
    assert (tmp_path / "pip-calls.txt").exists(), "the install must run"
    assert "C4-DONE" in r.stdout, r.stdout