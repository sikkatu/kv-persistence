# SPDX-License-Identifier: Apache-2.0
"""r16 P2: the C14 engine-freeze gate must NOT be blind.

Hidden defect (prechecked 19.11): `engine_pins` matched only `name==ver`
lines. If the vLLM nightly image reports `vllm @ file:///...` (direct
reference, a real pip-freeze format), the line matches nothing, BOTH sides
come out empty and the "engine unchanged" check passes on ANY input. We do
not know which format our image uses -- the gate must handle BOTH and must
REFUSE when it cannot see torch/vllm in the baseline.

Tests run the REAL C14 python block extracted from _incontainer_build.sh
(the heredoc body between its markers, never a copy):
  (a) both freeze formats parsed (`name==ver` + `name @ url`);
  (b) empty/unrecognised baseline -> `BUILD REFUSED: engine pins not found
      in baseline freeze (format not recognised)` with the first 20 lines;
  (c) changed torch version -> refused, message carries names AND versions;
  (d) unchanged -> passes, prints the compared pins.
Mutation: dropping the torch/vllm presence requirement -> (b) RED.
"""
from __future__ import annotations
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
PY = sys.executable


def _c14_code() -> str:
    text = INCONTAINER.read_text()
    start = text.index("import re, sys\n\ndef _norm(name):")
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
    before = "torch==2.14.0+cu130\nvllm @ file:///opt/vllm-a\nnumpy==2.1.0\n"
    after = "torch==2.14.0+cu130\nvllm @ file:///opt/vllm-B\nnumpy==2.1.0\n"
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
    before = "torch==2.14.0+cu130\nvllm==0.9.1\nnumpy==2.1.0\n"
    after = "torch==2.15.0\nvllm==0.9.1\nnumpy==2.1.0\n"
    r = _run(tmp_path, before, after)
    assert r.returncode != 0, "changed torch must be refused"
    assert "BUILD REFUSED" in r.stderr, r.stderr
    assert "2.14.0+cu130" in r.stderr and "2.15.0" in r.stderr, (
        f"the refusal must carry both versions:\n{r.stderr}")


def test_unchanged_passes_and_prints_pins(tmp_path):
    """(d) unchanged engine -> passes and prints the compared pins (names and
    versions), not just a number."""
    freeze = ("torch==2.14.0+cu130\nvllm==0.9.1\nnumpy==2.1.0\ntriton==3.4.0\n")
    r = _run(tmp_path, freeze, freeze)
    assert r.returncode == 0, f"unchanged engine must pass:\n{r.stderr}"
    assert "engine identity OK" in r.stdout, r.stdout
    assert "torch==2.14.0+cu130" in r.stdout and "vllm==0.9.1" in r.stdout, (
        f"the OK line must list the compared pins:\n{r.stdout}")


def test_pep503_name_normalisation(tmp_path):
    """Name variants (case, dots) must not evade the diff."""
    before = "Torch==2.14.0\nvLLM==0.9.1\n"
    after = "torch==2.15.0\nvllm==0.9.1\n"
    r = _run(tmp_path, before, after)
    assert r.returncode != 0, (
        f"case variants must map to the same pin (PEP 503):\n{r.stdout}")
    assert "BUILD REFUSED" in r.stderr, r.stderr