# SPDX-License-Identifier: Apache-2.0
"""r16 P2 + r17 B2: the C14 engine-freeze gate must NOT be blind and must
NOT compare non-engine packages.

r16 hidden defect (prechecked 19.11): `engine_pins` matched only `name==ver`
lines. If the vLLM nightly image reports `vllm @ file:///...` (direct
reference, a real pip-freeze format), the line matches nothing, BOTH sides
come out empty and the "engine unchanged" check passes on ANY input. We do
not know which format our image uses -- the gate must handle BOTH and must
REFUSE when it cannot see torch/vllm in the baseline.

r17 B2 defect (hardware-blocking): the r16 rewrite DROPPED the engine-name
filter -- the gate compared EVERY pip package, and the build itself mutates
the freeze (C7 installs a new lmcache, C9 pytest+pluggy+iniconfig, C4
setuptools-scm ...), so C14 refused EVERY real build with

    BUILD REFUSED: engine packages changed during the build:
      iniconfig: (absent) -> 2.1.0
      lmcache: 0.5.7 -> 0.5.8rc1.post48+kvp...
      ...

The filter (r12 semantics: torch*, vllm*, numpy*, triton* AFTER PEP 503
normalisation -- torchvision, vllm-flash-attn, triton-kernels count) is
restored; the OK line prints only the engine pins.

Tests run the REAL C14 python block extracted from _incontainer_build.sh
(the heredoc body between its markers, never a copy):
  (a) both freeze formats parsed (`name==ver` + `name @ url`);
  (b) empty/unrecognised baseline -> `BUILD REFUSED: engine pins not found
      in baseline freeze (format not recognised)` with the first 20 lines;
  (c) changed torch version -> refused, message carries names AND versions;
  (d) unchanged -> passes, prints the compared pins;
  (e) build noise (lmcache/pytest/pluggy/iniconfig/setuptools-scm appearing
      or changing) -> PASSES;
  (f) changed torchvision (engine prefix) -> refused;
  (g) the OK line never contains pytest/lmcache.
Mutation: dropping the engine filter -> (e) RED.
"""
from __future__ import annotations
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
PY = sys.executable

# the real image freeze is not just the engine: build/test deps live there too
NOISE_BEFORE = ("aiohttp==3.9.5\nsetuptools==75.1.0\nlmcache==0.5.7\n"
                "packaging==24.1\n")
NOISE_AFTER = ("aiohttp==3.9.5\nsetuptools==75.1.0\npackaging==24.1\n"
               "pytest==8.4.2\npluggy==1.6.0\niniconfig==2.1.0\n"
               "setuptools-scm==9.2.0\n"
               "lmcache==0.5.8rc1.post48+kvp.5554e8befbc2\n")


def _c14_code() -> str:
    text = INCONTAINER.read_text()
    start = text.index("import re, sys\n")
    end = text.index("PY\n", start)
    return text[start:end]


def _run(tmp_path: pathlib.Path, before: str, after: str):
    b = tmp_path / "before.txt"
    a = tmp_path / "after.txt"
    b.write_text(before)
    a.write_text(after)
    return subprocess.run([PY, "-c", _c14_code(), str(b), str(a)],
                          capture_output=True, text=True, timeout=30)


def test_both_freeze_formats_are_parsed(tmp_path):
    """(a) `name==ver` AND `name @ url` lines must both land in the pin set;
    a direct-reference vllm that CHANGES between before/after must be
    refused (the old parser was blind to it)."""
    before = NOISE_BEFORE + "torch==2.14.0+cu130\nvllm @ file:///opt/vllm-a\nnumpy==2.1.0\n"
    after = NOISE_AFTER + "torch==2.14.0+cu130\nvllm @ file:///opt/vllm-B\nnumpy==2.1.0\n"
    r = _run(tmp_path, before, after)
    assert r.returncode != 0, (
        f"a changed direct reference must be refused:\n{r.stdout}")
    assert "BUILD REFUSED" in r.stderr, r.stderr
    assert "vllm" in r.stderr and "ref:file:///opt/vllm-B" in r.stderr, (
        f"the refusal must carry names AND versions/refs:\n{r.stderr}")


def test_unrecognised_baseline_is_refused(tmp_path):
    """(b) an empty baseline (or one without torch/vllm) must be refused --
    NOT silently pass (mutation: dropping the presence requirement ->
    this test RED)."""
    for before in ("", "numpy==2.1.0\ntriton==3.4.0\n",
                   "some-package==1.0\n"):
        r = _run(tmp_path, before, "torch==2.14.0\nvllm==0.9.1\n")
        assert r.returncode != 0, (
            f"an unrecognised baseline must be refused, got rc=0 for {before!r}")
        assert "BUILD REFUSED" in r.stderr, r.stderr
        assert "engine pins not found in baseline freeze" in r.stderr, r.stderr
        assert "first 20 lines of the baseline freeze" in r.stderr, (
            f"the refusal must show the baseline head:\n{r.stderr}")


def test_changed_torch_version_refused_with_names_and_versions(tmp_path):
    """(c) a changed torch version -> refused; the message must carry the
    compared names AND versions (not just a count)."""
    before = NOISE_BEFORE + "torch==2.14.0+cu130\nvllm==0.9.1\nnumpy==2.1.0\n"
    after = NOISE_AFTER + "torch==2.15.0\nvllm==0.9.1\nnumpy==2.1.0\n"
    r = _run(tmp_path, before, after)
    assert r.returncode != 0, "changed torch must be refused"
    assert "BUILD REFUSED" in r.stderr, r.stderr
    assert "2.14.0+cu130" in r.stderr and "2.15.0" in r.stderr, (
        f"the refusal must carry both versions:\n{r.stderr}")


def test_unchanged_passes_and_prints_pins(tmp_path):
    """(d) unchanged engine -> passes and prints the compared pins (names and
    versions), not just a number."""
    freeze = ("torch==2.14.0+cu130\nvllm==0.9.1\nnumpy==2.1.0\ntriton==3.4.0\n")
    r = _run(tmp_path, NOISE_BEFORE + freeze, NOISE_AFTER + freeze)
    assert r.returncode == 0, f"unchanged engine must pass:\n{r.stderr}"
    assert "engine identity OK" in r.stdout, r.stdout
    assert "torch==2.14.0+cu130" in r.stdout and "vllm==0.9.1" in r.stdout, (
        f"the OK line must list the compared pins:\n{r.stdout}")


def test_build_noise_is_not_an_engine_change(tmp_path):
    """(e) THE r17 hardware case: the build itself adds/changes pytest,
    pluggy, iniconfig, setuptools-scm and a new lmcache -- with the engine
    untouched the gate must PASS (mutation: dropping the engine filter ->
    this test RED with 'BUILD REFUSED: engine packages changed')."""
    engine = "torch==2.14.0+cu130\nvllm==0.9.1\nnumpy==2.1.0\ntriton==3.4.0\n"
    r = _run(tmp_path, NOISE_BEFORE + engine, NOISE_AFTER + engine)
    assert r.returncode == 0, (
        f"build noise must not refuse the build (rc={r.returncode}):\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")
    assert "engine identity OK" in r.stdout, r.stdout


def test_changed_torchvision_refused(tmp_path):
    """(f) a change in an engine-prefix package (torchvision) must be
    refused -- the filter keeps the r12 prefix semantics."""
    before = NOISE_BEFORE + "torch==2.14.0\ntorchvision==0.19.0\nvllm==0.9.1\n"
    after = NOISE_AFTER + "torch==2.14.0\ntorchvision==0.20.0\nvllm==0.9.1\n"
    r = _run(tmp_path, before, after)
    assert r.returncode != 0, "changed torchvision must be refused"
    assert "BUILD REFUSED" in r.stderr, r.stderr
    assert "torchvision" in r.stderr and "0.19.0" in r.stderr \
        and "0.20.0" in r.stderr, (
        f"the refusal must name torchvision with both versions:\n{r.stderr}")


def test_ok_line_lists_only_engine_packages(tmp_path):
    """(g) the OK line must never contain pytest/lmcache noise names."""
    engine = "torch==2.14.0+cu130\nvllm==0.9.1\nnumpy==2.1.0\ntriton==3.4.0\n"
    r = _run(tmp_path, NOISE_BEFORE + engine, NOISE_AFTER + engine)
    assert r.returncode == 0, r.stderr
    assert "engine identity OK" in r.stdout, r.stdout
    for noise in ("pytest", "lmcache", "pluggy", "iniconfig",
                  "setuptools-scm", "aiohttp"):
        assert noise not in r.stdout, (
            f"the OK line must not list {noise!r}:\n{r.stdout}")


def test_pep503_name_normalisation(tmp_path):
    """Name variants (case, dots) must not evade the diff."""
    before = "Torch==2.14.0\nvLLM==0.9.1\n"
    after = "torch==2.15.0\nvllm==0.9.1\n"
    r = _run(tmp_path, before, after)
    assert r.returncode != 0, (
        f"case variants must map to the same pin (PEP 503):\n{r.stdout}")
    assert "BUILD REFUSED" in r.stderr, r.stderr