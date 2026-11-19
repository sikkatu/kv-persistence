# SPDX-License-Identifier: Apache-2.0
"""r16 P1: the C12/C13 summary gates must read the junit report, not the text.

Hardware-prechecked defect (19.11): LMCache's pytest.ini has `log_cli = true`,
so even with `-q` the summary line is decorated:
`================= 5 failed, 150 passed, 101 warnings in 27.55s =================`
and the old `grep -E '^[0-9]+ (passed|failed|error)'` finds NOTHING on a GREEN
run -> "REGRESSION FAILURE: pytest summary not found" on a healthy suite.
Reproduced with the real pytest + that pytest.ini:
`============================== 2 passed in 0.01s ===============================`.

The gate now counts from the --junitxml report (xml.etree): tests, failures,
errors, skipped summed over all <testsuite>; passed = tests - failures -
errors - skipped; rc==0 AND failures==0 AND errors==0 AND skipped==0 AND
passed>0; missing/empty/broken XML -> explicit REGRESSION FAILURE.

Tests run the REAL C12 block extracted from _incontainer_build.sh (markers
`# C12` / `# C13`, never a copy) with REAL pytest in BOTH configurations
(with the LMCache-style `log_cli = true` pytest.ini and without):
  (a) green run -> passes with the script-built summary line;
  (b) run with a skip -> refused, SKIPPED rows printed;
  (c) run with a failure -> refused;
  (d) run without --junitxml -> refused ("junit report missing/unparsable").
Mutation: restoring the text `grep '^[0-9]+ passed'` summary -> (a) RED on the
log_cli configuration.
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
PY = sys.executable


def extract_block(text: str, start_snip: str, end_snip: str) -> str:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if start_snip in ln)
    end = next(i for i, ln in enumerate(lines) if i > start and end_snip in ln)
    return "\n".join(lines[start + 1:end])


def _c12_block() -> str:
    return extract_block(INCONTAINER.read_text(), "# C12", "# C13")


def _make_suite(tmp_path: pathlib.Path, body: str,
                log_cli: bool) -> pathlib.Path:
    """A tiny test suite with the LMCache-style pytest.ini (or none)."""
    d = tmp_path / ("suite_logcli" if log_cli else "suite_plain")
    d.mkdir()
    if log_cli:
        (d / "pytest.ini").write_text("[pytest]\nlog_cli = true\n")
    (d / "test_suite.py").write_text(body)
    return d


GREEN = "def test_a():\n    assert True\ndef test_b():\n    assert True\n"
WITH_SKIP = ("def test_a():\n    assert True\n"
             "def test_s():\n    import pytest\n    pytest.skip('environment')\n")
WITH_FAIL = "def test_a():\n    assert True\ndef test_f():\n    assert 1 == 2\n"


def _run_c12(tmp_path: pathlib.Path, suite: pathlib.Path,
             body: str, with_xml: bool = True,
             env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run REAL pytest in the suite, then execute the REAL C12 block with
    that run's rc and reports (exactly what the build script does)."""
    work = tmp_path / f"work_{suite.name}_{with_xml}_{len(body)}"
    work.mkdir()
    report_xml = work / "pytest-report.xml"
    report_txt = work / "pytest-report.txt"
    cmd = [PY, "-m", "pytest", "-q", "-rs"]
    if with_xml:
        cmd.append(f"--junitxml={report_xml}")
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                         env=env, cwd=str(suite))
    report_txt.write_text(run.stdout + run.stderr)
    driver = work / "c12-driver.sh"
    driver.write_text("".join([
        "set -u\n",
        f'rc="{run.returncode}"\n',
        f'KVP_WORK="{work}"\n',
        f'KVP_IMPL="{ROOT / "implementation"}"\n',
        _c12_block() + "\n",
        "echo C12-DONE\n",
    ]))
    return subprocess.run(["bash", str(driver)], capture_output=True,
                          text=True, timeout=60, cwd=str(suite))


def test_green_run_passes_with_log_cli(tmp_path):
    """(a) THE hardware case: `log_cli = true` decorates the summary -- the
    junit-based gate must pass a green run (RED before: the text grep missed
    the summary and refused)."""
    suite = _make_suite(tmp_path, GREEN, log_cli=True)
    r = _run_c12(tmp_path, suite, GREEN)
    assert r.returncode == 0, (
        f"green run with log_cli must pass:\n{r.stdout}\n{r.stderr}")
    assert "CPU gate PASSED (2 passed, 0 failed, 0 errors, 0 skipped)" in r.stdout, (
        f"the script must build its own summary line:\n{r.stdout}")


def test_green_run_passes_without_log_cli(tmp_path):
    """(a2) the plain configuration passes too (no log_cli)."""
    suite = _make_suite(tmp_path, GREEN, log_cli=False)
    r = _run_c12(tmp_path, suite, GREEN)
    assert r.returncode == 0, (
        f"green run without log_cli must pass:\n{r.stdout}\n{r.stderr}")


def test_skip_is_refused_and_lists_skipped_rows(tmp_path):
    """(b) a run containing a skip must be refused and the SKIPPED rows
    printed (the gate must not become toothless)."""
    for log_cli in (True, False):
        suite = _make_suite(tmp_path, WITH_SKIP, log_cli=log_cli)
        r = _run_c12(tmp_path, suite, WITH_SKIP)
        assert r.returncode != 0, f"a skip must be refused (log_cli={log_cli})"
        assert "REGRESSION FAILURE" in r.stderr, r.stderr
        assert "SKIPPED" in (r.stdout + r.stderr), (
            f"the refusal must list the SKIPPED rows (log_cli={log_cli}):\n"
            f"{r.stdout}\n{r.stderr}")


def test_failure_is_refused(tmp_path):
    """(c) a failing test must be refused (either at the rc gate or at the
    junit failures count)."""
    for log_cli in (True, False):
        suite = _make_suite(tmp_path, WITH_FAIL, log_cli=log_cli)
        r = _run_c12(tmp_path, suite, WITH_FAIL)
        assert r.returncode != 0, f"a failure must be refused (log_cli={log_cli})"
        assert "REGRESSION FAILURE" in r.stderr, r.stderr


def test_missing_junit_report_is_refused(tmp_path):
    """(d) a run WITHOUT --junitxml must be refused explicitly -- the gate
    never silently passes without its report."""
    suite = _make_suite(tmp_path, GREEN, log_cli=True)
    r = _run_c12(tmp_path, suite, GREEN, with_xml=False)
    assert r.returncode != 0, "a missing junit report must be refused"
    assert "REGRESSION FAILURE" in r.stderr, r.stderr
    assert "junit report missing/unparsable" in r.stderr, r.stderr


def test_no_text_summary_grep_left_in_the_gate():
    """Static: the C12/C13 gates must not fall back to a text summary grep
    (mutation: restoring `grep '^[0-9]+ passed'` -> the green log_cli run is
    refused, test (a) RED). Comment lines are skipped -- the r16 comment
    quotes the old grep to explain the defect."""
    src = INCONTAINER.read_text()
    block = extract_block(src, "# C12", "# ---- C14")
    code_lines = [ln for ln in block.splitlines()
                  if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    for bad in ("'^[0-9]+ (passed", "'^[0-9]+ passed",
                "grep -E '^[0-9]+ (passed"):
        assert bad not in code, (
            f"the C12/C13 block must not parse the text summary: found {bad!r}")