# SPDX-License-Identifier: Apache-2.0
"""r11 C10/C11: layout gate with the REAL pytest (no argv-logging stubs).

Reference-pipeline rule: every gate is covered by a test that runs the real
program on a toy layout. Here: SOURCE lmcache (no native) in the staging
tree vs INSTALLED lmcache (with lmcache_native) in a fake site-packages;
the exact command the build script uses must see INSTALLED and the plugin
must refuse the SOURCE case with exit 4.

Mutations documented in the tests: running from the source dir or without
`-P` / without the staging (a lmcache/ dir next to the tests) makes the run
see SOURCE -- the tests assert the shipped command does not.
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
SCRIPTS = ROOT / "scripts"
PY = sys.executable


def pytest_cmd(testroot: pathlib.Path, files: list[str]) -> list[str]:
    """The EXACT command form _incontainer_build.sh runs (C11). Keep in sync
    with the script -- the wiring test below fails if they diverge."""
    return [PY, "-P", "-m", "pytest", "-p", "kvp_assert_installed",
            "-q", "-rs", *files]


def _layout(tmp_path: pathlib.Path) -> tuple:
    """Toy layout: src/lmcache = SOURCE (no native), site-packages/lmcache =
    INSTALLED (with cuda_ops + lmcache_native), src/tests = package tests."""
    src = tmp_path / "src"
    site = tmp_path / "site-packages"
    (src / "lmcache").mkdir(parents=True)
    (src / "lmcache" / "__init__.py").write_text("MARK = 'SOURCE'\n")
    (site / "lmcache").mkdir(parents=True)
    (site / "lmcache" / "__init__.py").write_text("MARK = 'INSTALLED'\n")
    (site / "lmcache" / "cuda_ops.py").write_text("X = 1\n")
    (site / "lmcache" / "lmcache_native.py").write_text("Y = 1\n")
    (src / "tests" / "v1").mkdir(parents=True)
    for pkg in (src / "tests", src / "tests" / "v1"):
        (pkg / "__init__.py").write_text("")
    (src / "tests" / "v1" / "test_mark.py").write_text(
        "def test_mark():\n"
        "    import lmcache\n"
        "    import lmcache.cuda_ops  # noqa: F401\n"
        "    import lmcache.lmcache_native  # noqa: F401\n"
        "    assert lmcache.MARK == 'INSTALLED', f'saw {lmcache.MARK}'\n"
    )
    (src / "pytest.ini").write_text("[pytest]\n")
    return src, site


def test_layout_run_sees_installed_not_source(tmp_path):
    """The shipped command on the toy layout must exercise INSTALLED (real
    pytest; red before r11: tests ran against the source tree)."""
    src, site = _layout(tmp_path)
    testroot = tmp_path / "testroot"
    testroot.mkdir()
    shutil.copytree(src / "tests", testroot / "tests")
    shutil.copy(src / "pytest.ini", testroot / "pytest.ini")
    env = dict(os.environ, PYTHONPATH=f"{SCRIPTS}:{site}")
    r = subprocess.run(
        pytest_cmd(testroot, ["tests/v1/test_mark.py"]),
        capture_output=True, text=True, timeout=60, env=env, cwd=str(testroot),
    )
    assert r.returncode == 0, (
        f"shipped command must see INSTALLED:\n{r.stdout}\n{r.stderr}"
    )
    assert "site-packages" in r.stdout, "plugin must print the site-packages __file__"


def test_plugin_refuses_source_tree_with_exit4(tmp_path):
    """The plugin must abort with rc=4 when lmcache resolves to the SOURCE
    tree (lmcache/ next to tests, no -P trick) -- real pytest, real plugin."""
    src, site = _layout(tmp_path)
    # staging WITHOUT copying tests away: run from src (SOURCE on sys.path)
    env = dict(os.environ, PYTHONPATH=f"{SCRIPTS}")
    r = subprocess.run(
        [PY, "-m", "pytest", "-p", "kvp_assert_installed", "-q",
         "tests/v1/test_mark.py"],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(src),
    )
    assert r.returncode == 4, (
        f"plugin must exit 4 on SOURCE lmcache, got rc={r.returncode}:\n{r.stdout}\n{r.stderr}"
    )
    assert "BUILD REFUSED" in (r.stdout + r.stderr), (
        "the refusal must say BUILD REFUSED"
    )


def test_script_runs_the_exact_command(tmp_path):
    """Wiring: every pytest invocation in the script is the staged form
    (python3 -P -m pytest -p kvp_assert_installed) -- a run WITHOUT -P would
    silently collect the SOURCE tree (mutation: dropping -P must fail)."""
    src = INCONTAINER.read_text()
    expected = "python3 -P -m pytest -p kvp_assert_installed"
    n_runs = [ln for ln in src.splitlines()
              if "-m pytest" in ln and not ln.lstrip().startswith("#")]
    assert n_runs, "no pytest invocation found"
    for ln in n_runs:
        assert expected in ln, (
            f"every pytest run must use the staged form {expected!r}:\n{ln}"
        )
    assert "PYTHONPATH=\"$KVP_SCRIPTS\"" in src, (
        "the script must pass the plugin dir on PYTHONPATH"
    )