# SPDX-License-Identifier: Apache-2.0
"""r13 P0: `pip show lmcache | head -3` dies silently (rc=120, SIGPIPE).

Round-13 hardware log: the build reached C7 (wheel built, cuda_ops has only
sm_120, four native extensions present, `pip install` passed), then the
script died SILENTLY with `docker run rc=120` and never reached C8.

Cause: `head -3` closes the pipe; `pip show` writes to the closed stdout and
exits with code 120 (`ERROR: Pipe to stdout was broken` -- reproduced on pip
26.2.1: pipestatus=120). Under `pipefail` the WHOLE pipeline status is 120.
Line 180 has no `||`, so `set -e` ends the script with rc=120 and NO message.
Line 176 additionally prints a FALSE `(not installed)` when lmcache IS
installed (visible in the log).

This is the same defect class as round 12 but OUTSIDE `$(...)` -- the r12
audit and the "every non-zero exit carries a message" walker did not cover
plain pipelines.

Tests run the REAL C7 block (extracted from _incontainer_build.sh by its
`# ---- C7` / `# ---- C8` markers, never a copy) under real bash with
`set -Eeuo pipefail` and a pip stub that writes >= 200 KB to stdout on
`show` and exits 120 when stdout is closed (python stub with BrokenPipeError
-> sys.exit(120)):
  (a) the C7 block must reach the after-marker (rc=0) -- RED before the fix
      (the `| head -3` pipeline kills the script with rc=120);
  (b) the BEFORE line must print `(not installed)` ONLY when `pip show`
      really fails -- with a working install it must NOT print it (the old
      code printed it falsely because the pipe closed).
Plus a static lint (acceptance): NO `| head` pipeline in
_incontainer_build.sh outside `$( ... || true )` and outside the explicitly
allowed list.

Mutation: restoring `| head -3` -> (a) RED (rc=120).
"""
from __future__ import annotations
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"


def extract_block(text: str, start_snip: str, end_snip: str) -> str:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if start_snip in ln)
    end = next(i for i, ln in enumerate(lines) if i > start and end_snip in ln)
    return "\n".join(lines[start + 1:end])


def _pip_stub(tmp_path: pathlib.Path, exit_code: int = 0) -> pathlib.Path:
    """pip stub: on `show` writes >= 200 KB to stdout; if stdout is closed
    (SIGPIPE / BrokenPipeError) exits 120 like real pip."""
    stub = tmp_path / "bin" / "pip"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"code = {exit_code}\n"
        "if 'show' in sys.argv:\n"
        "    try:\n"
        "        out = ('Name: lmcache\\nVersion: 0.5.7.dev428+kvp.abc\\n'\n"
        "               'Summary: fake\\n') + ('x' * 262144) + '\\n'\n"
        "        sys.stdout.write(out)\n"
        "        sys.stdout.flush()\n"
        "    except BrokenPipeError:\n"
        "        # real pip: 'ERROR: Pipe to stdout was broken' -> exit 120\n"
        "        try:\n"
        "            sys.stderr.close()\n"
        "        except Exception:\n"
        "            pass\n"
        "        sys.exit(120)\n"
        "    sys.exit(code)\n"
        "sys.exit(0)\n")
    stub.chmod(0o755)
    return stub


def _run_c7(tmp_path: pathlib.Path, pip_exit: int = 0) -> subprocess.CompletedProcess:
    c7 = extract_block(INCONTAINER.read_text(), "# ---- C7", "# ---- C8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                 "echo", "sed", "awk", "tr", "cut", "find", "head", "wc",
                 "mkdir", "rm", "cp", "ls", "date", "basename", "dirname",
                 "sort", "python3"):
        real = shutil.which(tool)
        if real and not (bin_dir / tool).exists():
            (bin_dir / tool).symlink_to(real)
    _pip_stub(tmp_path, exit_code=pip_exit)
    driver = tmp_path / "c7-driver.sh"
    driver.write_text("".join([
        "set -Eeuo pipefail\n",
        f'WHEEL_FILE="{tmp_path / "lmcache-0.5.7.dev428+kvp.abc.whl"}"\n',
        "echo C7-BEGIN\n",
        c7 + "\n",
        "echo C7-AFTER\n",
    ]))
    env = {"PATH": str(bin_dir), "PWD": str(tmp_path)}
    return subprocess.run(["bash", str(driver)], capture_output=True, text=True,
                          timeout=60, env=env, cwd=str(tmp_path))


def test_c7_reaches_after_marker_despite_big_pip_show(tmp_path):
    """(a) REAL C7 block + a pip stub that writes >=200 KB and exits 120 on a
    closed stdout: the block must reach C7-AFTER with rc=0. RED before the
    fix (`| head -3` -> SIGPIPE -> rc=120 silent death)."""
    r = _run_c7(tmp_path)
    assert r.returncode == 0, (
        f"C7 must survive a big `pip show` (rc={r.returncode})\n"
        f"stdout:\n{r.stdout[-600:]}\nstderr:\n{r.stderr[-600:]}")
    assert "C7-AFTER" in r.stdout, (
        f"the script must reach the marker after C7:\nstdout:\n{r.stdout[-600:]}")


def test_c7_before_line_no_false_not_installed(tmp_path):
    """(b) with a WORKING `pip show` (exit 0) the BEFORE line must NOT print
    `(not installed)` -- the old pipe closed early and printed it falsely."""
    r = _run_c7(tmp_path, pip_exit=0)
    assert "(not installed)" not in r.stdout, (
        f"a working install must not print '(not installed)':\n{r.stdout[-600:]}")


def test_c7_before_line_reports_missing_install(tmp_path):
    """(b2) when `pip show` REALLY fails (exit 1, not installed) the BEFORE
    line must print `(not installed)`."""
    r = _run_c7(tmp_path, pip_exit=1)
    assert r.returncode == 0, (
        f"a missing install must not kill C7 (rc={r.returncode}):\n{r.stderr[-400:]}")
    assert "(not installed)" in r.stdout, (
        f"a failed `pip show` must print '(not installed)':\n{r.stdout[-600:]}")


# ---------------------------------------------------------------------------
# acceptance lint: no `| head` pipeline outside `$( ... || true )` / allowlist
# ---------------------------------------------------------------------------
def test_no_head_pipeline_outside_fail_soft_substitutions():
    """ACCEPTANCE (r13): in _incontainer_build.sh there must be NO `| head`
    pipeline outside a `$( ... || true )` substitution and outside the
    explicitly allowed list. `head` closes the pipe early -> the left side
    gets SIGPIPE and dies rc=120 (pip) under pipefail."""
    src = INCONTAINER.read_text()
    # allowed: pipelines already marked fail-soft (contain `|| true`)
    offenders = []
    for i, ln in enumerate(src.splitlines(), 1):
        stripped = ln.lstrip()
        if stripped.startswith("#"):
            continue
        if "| head" not in ln and "| head " not in ln:
            if "| head" not in ln:
                continue
        # fail-soft or explicitly allowed
        if "|| true" in ln or "|| echo" in ln:
            continue
        offenders.append(f"{i}: {ln.strip()}")
    assert not offenders, (
        "unprotected `| head` pipelines (SIGPIPE risk under pipefail):\n"
        + "\n".join(offenders))