# SPDX-License-Identifier: Apache-2.0
"""r14 P0: the installed-wheel check must go by SUBSTANCE, not by the
"site-packages" substring.

Hardware log 19.11 12:52: C8 FALSELY refused a correctly installed wheel --
the vLLM image is Debian/Ubuntu layout and pip installs into
/usr/local/lib/python3.12/dist-packages, but the check demanded
"site-packages" inside __file__. The recipe launchers already knew about
dist-packages; the build scripts did not.

These tests build REAL fake installed lmcache distributions (with
lmcache-*.dist-info/METADATA and Python stub modules -- .so stubs cannot be
imported) under REAL `python3 -P`:
  (a) .../site-packages layout -> passes;
  (b) .../dist-packages layout (as in the image) -> passes (RED before the
      fix: the substring check refused);
  (c) package in a SOURCE tree (LMC_SRC/lmcache, no dist-info in that root)
      -> BUILD REFUSED;
  (d) metadata in one dir, module loaded from another -> BUILD REFUSED.
The same four scenarios run through the pytest plugin (real
`pytest -p kvp_assert_installed`, exit code 4 on refusal).

Mutation: restoring the `"site-packages" not in f` substring check ->
(b) RED.
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
PY = sys.executable


def _make_installed(tmp_path: pathlib.Path, layout: str,
                    dist_dir_name: str = "lmcache-0.5.7.dev428.dist-info") -> pathlib.Path:
    """Fake installed lmcache distribution under <tmp>/<layout>:
    lmcache/{__init__,cuda_ops,lmcache_native}.py + dist-info/METADATA."""
    root = tmp_path / layout
    pkg = root / "lmcache"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("MARK = 'INSTALLED'\n")
    (pkg / "cuda_ops.py").write_text("X = 1\n")
    (pkg / "lmcache_native.py").write_text("Y = 1\n")
    dinfo = root / dist_dir_name
    dinfo.mkdir()
    (dinfo / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: lmcache\nVersion: 0.5.7.dev428\n")
    (dinfo / "RECORD").write_text("")
    (dinfo / "INSTALLER").write_text("pip\n")
    return root


def _make_source_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """Source tree: LMC_SRC/lmcache WITHOUT any dist-info in LMC_SRC."""
    src = tmp_path / "LMCache"
    pkg = src / "lmcache"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("MARK = 'SOURCE'\n")
    (pkg / "cuda_ops.py").write_text("X = 1\n")
    (pkg / "lmcache_native.py").write_text("Y = 1\n")
    return src


CHECK_SNIPPET = """\
import sys
import lmcache
import lmcache.cuda_ops
import lmcache.lmcache_native
from kvp_assert_installed import assert_installed
mods = (lmcache, sys.modules["lmcache.cuda_ops"], sys.modules["lmcache.lmcache_native"])
try:
    assert_installed(mods, source_roots=[{src_root!r}])
except RuntimeError as e:
    print(str(e), file=sys.stderr)
    raise SystemExit(1)
for mod in mods:
    print(f"OK: {{mod.__name__}} -> {{mod.__file__}}")
"""


def _run_check(tmp_path: pathlib.Path, site_dir: pathlib.Path,
               src_root: pathlib.Path, extra_path: pathlib.Path | None = None
               ) -> subprocess.CompletedProcess:
    """REAL `python3 -P` running the C8-style check against a fake install."""
    code = CHECK_SNIPPET.format(src_root=str(src_root))
    pp = [str(SCRIPTS), str(site_dir)]
    if extra_path is not None:
        pp.append(str(extra_path))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(pp))
    return subprocess.run([PY, "-P", "-c", code], capture_output=True,
                          text=True, timeout=60, env=env, cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# (a)/(b): both installed layouts must pass
# ---------------------------------------------------------------------------
def test_installed_site_packages_layout_passes(tmp_path):
    site = _make_installed(tmp_path, "site-packages")
    src = _make_source_tree(tmp_path)
    r = _run_check(tmp_path, site, src)
    assert r.returncode == 0, (
        f"site-packages layout must pass:\n{r.stdout}\n{r.stderr}")
    assert "OK: lmcache ->" in r.stdout, r.stdout


def test_installed_dist_packages_layout_passes(tmp_path):
    """(b) THE hardware layout: /usr/local/lib/python3.12/dist-packages style.
    RED before the fix (substring check refused)."""
    site = _make_installed(tmp_path, os.path.join("usr", "local", "lib",
                                                  "python3.12", "dist-packages"))
    src = _make_source_tree(tmp_path)
    r = _run_check(tmp_path, site, src)
    assert r.returncode == 0, (
        f"dist-packages layout (the vLLM image layout) must pass:\n"
        f"{r.stdout}\n{r.stderr}")
    assert "OK: lmcache ->" in r.stdout, r.stdout


# ---------------------------------------------------------------------------
# (c): source tree without dist-info -> refused
# ---------------------------------------------------------------------------
def test_source_tree_refused(tmp_path):
    src = _make_source_tree(tmp_path)
    # no installed distribution anywhere: import lmcache resolves to the source
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SCRIPTS), str(src)]))
    code = CHECK_SNIPPET.format(src_root=str(src))
    r = subprocess.run([PY, "-P", "-c", code], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    assert r.returncode != 0, f"source tree must be refused:\n{r.stdout}"
    assert "BUILD REFUSED" in r.stderr, (
        f"the refusal must say BUILD REFUSED:\n{r.stdout}\n{r.stderr}")


# ---------------------------------------------------------------------------
# (d): metadata in one dir, module from another -> refused
# ---------------------------------------------------------------------------
def test_metadata_and_module_mismatch_refused(tmp_path):
    meta_dir = tmp_path / "meta-root"
    meta_dir.mkdir()
    dinfo = meta_dir / "lmcache-0.5.7.dev428.dist-info"
    dinfo.mkdir()
    (dinfo / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: lmcache\nVersion: 0.5.7.dev428\n")
    (dinfo / "RECORD").write_text("")
    # package lives elsewhere (source tree), NOT under meta_dir
    src = _make_source_tree(tmp_path)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(SCRIPTS), str(meta_dir), str(src)]))
    code = CHECK_SNIPPET.format(src_root=str(tmp_path / "elsewhere"))
    r = subprocess.run([PY, "-P", "-c", code], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    assert r.returncode != 0, (
        f"metadata/module mismatch must be refused:\n{r.stdout}")
    assert "BUILD REFUSED" in r.stderr, (
        f"the refusal must say BUILD REFUSED:\n{r.stdout}\n{r.stderr}")


# ---------------------------------------------------------------------------
# the same scenarios through the REAL pytest plugin
# ---------------------------------------------------------------------------
def _run_plugin(tmp_path: pathlib.Path, site_dir: pathlib.Path,
                src_root: pathlib.Path, cwd: pathlib.Path,
                extra_path: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    testroot = tmp_path / "testroot"
    if not testroot.is_dir():
        testroot.mkdir()
        (testroot / "test_ok.py").write_text("def test_ok():\n    assert True\n")
        (testroot / "pytest.ini").write_text("[pytest]\n")
    pp = [str(SCRIPTS), str(site_dir)]
    if extra_path is not None:
        pp.append(str(extra_path))
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(pp),
               KVP_LMC_SRC=str(src_root))
    # run FROM testroot so `test_ok.py` resolves (cwd stays the caller's
    # only when it already holds the test file)
    run_cwd = testroot if not (cwd / "test_ok.py").is_file() else cwd
    return subprocess.run(
        [PY, "-P", "-m", "pytest", "-p", "kvp_assert_installed", "-q",
         "test_ok.py"], capture_output=True, text=True, timeout=60,
        env=env, cwd=str(run_cwd))


def test_plugin_passes_dist_packages_layout(tmp_path):
    site = _make_installed(tmp_path, os.path.join("usr", "local", "lib",
                                                  "python3.12", "dist-packages"))
    src = _make_source_tree(tmp_path)
    r = _run_plugin(tmp_path, site, src, cwd=tmp_path)
    assert r.returncode == 0, (
        f"plugin must pass the dist-packages layout:\n{r.stdout}\n{r.stderr}")
    assert "kvp_assert_installed" in r.stdout, r.stdout


def test_plugin_passes_site_packages_layout(tmp_path):
    site = _make_installed(tmp_path, "site-packages")
    src = _make_source_tree(tmp_path)
    r = _run_plugin(tmp_path, site, src, cwd=tmp_path)
    assert r.returncode == 0, (
        f"plugin must pass the site-packages layout:\n{r.stdout}\n{r.stderr}")


def test_plugin_refuses_source_tree(tmp_path):
    src = _make_source_tree(tmp_path)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SCRIPTS), str(src)]))
    testroot = tmp_path / "testroot"
    testroot.mkdir()
    (testroot / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (testroot / "pytest.ini").write_text("[pytest]\n")
    r = subprocess.run(
        [PY, "-P", "-m", "pytest", "-p", "kvp_assert_installed", "-q",
         "test_ok.py"], capture_output=True, text=True, timeout=60,
        env=env, cwd=str(src))
    assert r.returncode == 4, (
        f"plugin must exit 4 on the SOURCE tree, got rc={r.returncode}:\n"
        f"{r.stdout}\n{r.stderr}")
    assert "BUILD REFUSED" in (r.stdout + r.stderr), (
        f"the refusal must say BUILD REFUSED:\n{r.stdout}\n{r.stderr}")


def test_plugin_refuses_metadata_mismatch(tmp_path):
    meta_dir = tmp_path / "meta-root"
    meta_dir.mkdir()
    dinfo = meta_dir / "lmcache-0.5.7.dev428.dist-info"
    dinfo.mkdir()
    (dinfo / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: lmcache\nVersion: 0.5.7.dev428\n")
    (dinfo / "RECORD").write_text("")
    src = _make_source_tree(tmp_path)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(SCRIPTS), str(meta_dir), str(src)]),
        KVP_LMC_SRC=str(tmp_path / "elsewhere"))
    testroot = tmp_path / "testroot"
    testroot.mkdir()
    (testroot / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (testroot / "pytest.ini").write_text("[pytest]\n")
    r = subprocess.run(
        [PY, "-P", "-m", "pytest", "-p", "kvp_assert_installed", "-q",
         "test_ok.py"], capture_output=True, text=True, timeout=60,
        env=env, cwd=str(tmp_path))
    assert r.returncode == 4, (
        f"plugin must exit 4 on metadata/module mismatch, got rc={r.returncode}:\n"
        f"{r.stdout}\n{r.stderr}")
    assert "BUILD REFUSED" in (r.stdout + r.stderr), r.stdout + r.stderr


# ---------------------------------------------------------------------------
# (e) editable-like: the distribution root INSIDE source_roots -> refused
# (r15 P2.2: the missing test for the source-tree protection -- a mutation
# `if False:` on that branch left every test green before)
# ---------------------------------------------------------------------------
def test_distribution_inside_source_roots_refused(tmp_path):
    """The installed lmcache distribution lives under the checkout itself
    (editable-like: <checkout>/site-packages/lmcache) and source_roots covers
    the checkout -> BUILD REFUSED."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    site = _make_installed(checkout, "site-packages")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SCRIPTS), str(site)]))
    code = CHECK_SNIPPET.format(src_root=str(checkout))
    r = subprocess.run([PY, "-P", "-c", code], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    assert r.returncode != 0, (
        f"a distribution inside source_roots must be refused:\n{r.stdout}")
    assert "BUILD REFUSED" in r.stderr, (
        f"the refusal must say BUILD REFUSED:\n{r.stdout}\n{r.stderr}")
    assert "lives inside the source tree" in r.stderr, r.stderr


def test_plugin_refuses_distribution_inside_source_roots(tmp_path):
    """The same editable-like case through the REAL pytest plugin: the
    distribution root under KVP_LMC_SRC -> exit 4."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    site = _make_installed(checkout, "site-packages")
    src = _make_source_tree(tmp_path)
    r = _run_plugin(tmp_path, site, src_root=checkout, cwd=tmp_path)
    assert r.returncode == 4, (
        f"plugin must exit 4 when the distribution lives inside the source "
        f"tree, got rc={r.returncode}:\n{r.stdout}\n{r.stderr}")
    assert "BUILD REFUSED" in (r.stdout + r.stderr), (
        f"the refusal must say BUILD REFUSED:\n{r.stdout}\n{r.stderr}")


# ---------------------------------------------------------------------------
# static gates: no substring checks left; C8 uses the shared function
# ---------------------------------------------------------------------------
def test_no_site_packages_substring_check_left():
    import re
    pat = re.compile(r'"site-packages"\s*(not\s+)?in')
    hits = []
    for base in (ROOT / "scripts", ROOT / "implementation"):
        for f in base.rglob("*"):
            if not f.is_file() or f.suffix not in (".sh", ".py"):
                continue
            if "__pycache__" in str(f):
                continue
            for i, ln in enumerate(f.read_text().splitlines(), 1):
                if pat.search(ln):
                    hits.append(f"{f}:{i}: {ln.strip()}")
    assert not hits, (
        "the substring criterion is still present:\n" + "\n".join(hits))


def test_c8_uses_the_shared_assert_installed():
    """C8 must import assert_installed from the shared module (one criterion
    for the heredoc and the plugin), not keep a private copy."""
    src = INCONTAINER.read_text()
    assert "from kvp_assert_installed import assert_installed" in src, (
        "C8 must use the shared assert_installed()")
    assert 'assert_installed(mods' in src, "C8 must call assert_installed(mods...)"