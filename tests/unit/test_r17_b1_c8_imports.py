# SPDX-License-Identifier: Apache-2.0
"""r17 B1: the C8 import-check command must run AS WRITTEN in the scripts.

Hardware-blocking defect (r17 review): both in-container scripts ran

    PYTHONPATH="$KVP_SCRIPTS" ... python3 -P -m kvpersist.gate_common check-imports

but the `kvpersist` package lives in implementation/ (mounted at /kvp-impl
inside the container) and was NOT on PYTHONPATH -> "No module named
'kvpersist'" on the FIRST command of the container phase. The tests missed
it because the python3 shim in test_r12_c14_after_tests.py set
PYTHONPATH={IMPL} ITSELF and stubbed `check-imports` away.

Rule (r17): every container-phase command that calls a Python module from
this repo must at least once run in a test EXACTLY as written in the script
(extracted by marker) with the caller's environment passed through as-is and
only the container-visible directories available.

Tests here extract the REAL C8 command line from BOTH scripts by marker and
run it as-is against a FAKE INSTALLED lmcache distribution placed in a
dist-packages-like directory OUTSIDE the source tree (added to sys.path the
way `site` does it, via the PYTHONUSERBASE user-site mechanism -- the caller's
environment is passed through unchanged, no PYTHONPATH rewriting):
  (a) _incontainer_build.sh C8 -> rc=0, "import-check OK";
  (b) _rerun_incontainer.sh C8 -> rc=0, "import-check OK";
  (c) an installed distribution whose root lives INSIDE the source tree ->
      refusal (the existing assert_installed logic must survive).
Mutation: dropping `:$KVP_IMPL` from either script -> (a)/(b) RED.
"""
from __future__ import annotations
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
RERUN_INCONTAINER = ROOT / "scripts" / "_rerun_incontainer.sh"
SCRIPTS = ROOT / "scripts"
IMPL = ROOT / "implementation"


def _c8_command(script: pathlib.Path) -> str:
    """The FULL C8 command line as written in the script (marker-based
    extraction -- never a copy). The command may span several lines (line
    continuations are joined)."""
    lines = script.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if "kvpersist.gate_common check-imports" in ln
                 and not ln.lstrip().startswith("#"))
    # gather the command: walk up over any leading env assignments that are
    # continued onto this line, then join trailing backslash continuations
    i = start
    while i > 0 and re.match(r"^\s*[A-Z_]+=", lines[i - 1]) \
            and lines[i - 1].rstrip().endswith("\\"):
        i -= 1
    cmd_parts = [lines[i]]
    while cmd_parts[-1].rstrip().endswith("\\"):
        i += 1
        cmd_parts.append(lines[i])
    cmd = " ".join(p.rstrip().rstrip("\\").strip() for p in cmd_parts)
    assert "kvpersist.gate_common check-imports" in cmd
    return cmd


def _fake_installed_lmcache(dist_dir: pathlib.Path) -> None:
    """A fake INSTALLED lmcache distribution in `dist_dir` (a
    dist-packages-like directory), with empty modules cuda_ops,
    lmcache_native, integration/vllm/lmcache_mp_connector and
    v1/multiprocess/modules/lmcache_driven_transfer plus a .dist-info."""
    pkg = dist_dir / "lmcache"
    for sub in ("cuda_ops", "lmcache_native",
                "integration/vllm/lmcache_mp_connector",
                "v1/multiprocess/modules/lmcache_driven_transfer"):
        leaf = sub.rsplit("/", 1)[-1]
        d = pkg / sub.rsplit("/", 1)[0] if "/" in sub else pkg
        d.mkdir(parents=True, exist_ok=True)
        (d / (leaf + ".py")).write_text("")
    # every package dir needs an __init__.py
    for p in pkg.rglob("*"):
        if p.is_dir():
            (p / "__init__.py").touch()
    (pkg / "__init__.py").write_text("")
    di = dist_dir / "lmcache-0.5.8.dist-info"
    di.mkdir(parents=True, exist_ok=True)
    (di / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: lmcache\nVersion: 0.5.8\n")
    (di / "RECORD").write_text("")


def _py_version() -> str:
    """The version of the `python3` the C8 command will invoke (the
    user-site dir name must match THAT interpreter, not the test's)."""
    r = subprocess.run(
        ["python3", "-c",
         "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _run_c8(script: pathlib.Path, tmp_path: pathlib.Path,
            dist_inside_source_tree: bool = False) -> subprocess.CompletedProcess:
    cmd = _c8_command(script)
    ver = _py_version()
    userbase = tmp_path / "userbase"
    dist_dir = userbase / "lib" / f"python{ver}" / "site-packages"
    dist_dir.mkdir(parents=True, exist_ok=True)
    _fake_installed_lmcache(dist_dir)
    if dist_inside_source_tree:
        # the INSTALLED distribution root lives inside the source tree
        # (like an editable install): assert_installed must refuse it
        lmc_src = tmp_path  # parent of userbase -> dist_root under source root
    else:
        lmc_src = tmp_path / "src"
    lmc_src.mkdir(exist_ok=True)
    env = dict(os.environ)  # caller's environment passed through AS-IS
    env.update({
        # user-site puts the fake installed distribution into sys.path the
        # way `site` does -- no PYTHONPATH rewriting by the harness
        "PYTHONUSERBASE": str(userbase),
        "KVP_SCRIPTS": str(SCRIPTS),
        "KVP_IMPL": str(IMPL),
        "KVP_LMC_SRC": str(lmc_src),
    })
    driver = tmp_path / "c8-driver.sh"
    driver.write_text("".join([
        "set -Eeuo pipefail\n",
        'die() { echo "ERROR: $*" >&2; exit 1; }\n',
        # script-local variables the C8 command references (as the
        # in-container scripts define them)
        f'LMC_SRC="{lmc_src}"\n',
        f'KVP_WORK="{tmp_path / "work"}"\n',
        cmd + "\n",
    ]))
    return subprocess.run(["bash", str(driver)], capture_output=True,
                          text=True, timeout=30, env=env,
                          cwd=str(lmc_src))


def test_c8_command_from_build_script_runs_as_written(tmp_path):
    """(a) the REAL C8 command of _incontainer_build.sh, executed as
    written, must pass against a fake installed distribution (mutation:
    dropping `:$KVP_IMPL` -> RED with "No module named 'kvpersist'")."""
    r = _run_c8(INCONTAINER, tmp_path)
    assert r.returncode == 0, (
        f"C8 as written in _incontainer_build.sh must pass (rc={r.returncode})\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")
    assert "import-check OK" in r.stdout, r.stdout


def test_c8_command_from_rerun_script_runs_as_written(tmp_path):
    """(b) the REAL C8 command of _rerun_incontainer.sh, executed as
    written, must pass the same way (mutation: dropping `:$KVP_IMPL` ->
    RED)."""
    r = _run_c8(RERUN_INCONTAINER, tmp_path)
    assert r.returncode == 0, (
        f"C8 as written in _rerun_incontainer.sh must pass (rc={r.returncode})\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")
    assert "import-check OK" in r.stdout, r.stdout


def test_distribution_inside_source_tree_refused(tmp_path):
    """(c) an installed distribution whose root lives INSIDE the source tree
    must be refused by the existing assert_installed logic (the test must
    not weaken it)."""
    r = _run_c8(INCONTAINER, tmp_path, dist_inside_source_tree=True)
    assert r.returncode != 0, (
        f"a source-tree distribution must be refused:\n{r.stdout}\n{r.stderr}")
    out = r.stdout + r.stderr
    assert "BUILD REFUSED" in out and "source tree" in out, (
        f"the refusal must name the source tree:\n{out}")