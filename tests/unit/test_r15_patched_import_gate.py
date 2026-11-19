# SPDX-License-Identifier: Apache-2.0
"""r15 P1: the static import gate for the patched LMCache tree.

Hardware log 19.11 14:57: C11 failed (rc=2) -- `ImportError: cannot import
name 'IPCCacheServerKey' from 'lmcache.v1.multiprocess.custom_types'`. The
5972 patch (taken from the PR head, Oct 27) is SEMANTICALLY stale: upstream
moved IPCCacheServerKey to lmcache.v1.common.custom_types (c4fb19dd, after
the PR head), and the patch still applied cleanly (fuzz) -- "applies
cleanly" does NOT mean "compatible".

The gate (`implementation/kvpersist/check_patched_imports.py`) resolves every
lmcache/tests import in the patched-tree scope against the tree (AST only,
no torch/vllm) and refuses stale names/paths BEFORE the docker compile.

Tests (offline, real Python, synthetic mini-trees -- no LMCache checkout):
  (a) stale import -> refusal with the exact message and rc=1;
  (b) fixed import -> rc=0;
  (c) fallback import inside `try/except ImportError` is NOT flagged;
  (d) PEP 420 namespace package (no __init__.py) is accepted;
  (e) `_proto_gen` and `lmcache.lmcache_*` are exempt;
  (f) empty file list -> refusal (the gate must never silently skip);
plus the wiring check: the gate is called in build-lmcache.sh BEFORE the
docker phase. (The patch-content checks live in test_r15_patch_porting.py.)

Mutations: removing the gate call from build-lmcache.sh -> the wiring test
RED. (The patch-content mutation lives with test_r15_patch_porting.py.)
"""
from __future__ import annotations
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATE = ROOT / "implementation" / "kvpersist" / "check_patched_imports.py"
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
PY = sys.executable


def _run_gate(tree: pathlib.Path, files: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(GATE), str(tree), *files],
                          capture_output=True, text=True, timeout=60)


def _mini_tree(tmp_path: pathlib.Path, test_body: str) -> pathlib.Path:
    """Synthetic mini tree: lmcache/v1/common/custom_types.py (defines
    IPCCacheServerKey), lmcache/v1/multiprocess/custom_types.py (stale
    location, no IPCCacheServerKey) and one test file given by `test_body`."""
    tree = tmp_path / "tree"
    common = tree / "lmcache" / "v1" / "common"
    mp = tree / "lmcache" / "v1" / "multiprocess"
    common.mkdir(parents=True)
    mp.mkdir(parents=True)
    (tree / "lmcache" / "__init__.py").write_text("")
    (tree / "lmcache" / "v1" / "__init__.py").write_text("")
    (common / "__init__.py").write_text("")
    (mp / "__init__.py").write_text("")
    (common / "custom_types.py").write_text(
        "class IPCCacheServerKey:\n    pass\nclass BlockAllocationRecord:\n    pass\n")
    (mp / "custom_types.py").write_text("class BlockAllocationRecord:\n    pass\n")
    tests = tree / "tests" / "v1" / "multiprocess"
    tests.mkdir(parents=True)
    (tree / "tests" / "__init__.py").write_text("")
    (tree / "tests" / "v1" / "__init__.py").write_text("")
    (tests / "__init__.py").write_text("")
    (tests / "test_gate_case.py").write_text(test_body)
    return tree


STALE = (
    "from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey\n"
    "from lmcache.v1.common.custom_types import BlockAllocationRecord\n"
)
FIXED = (
    "from lmcache.v1.common.custom_types import IPCCacheServerKey\n"
    "from lmcache.v1.common.custom_types import BlockAllocationRecord\n"
)


def test_stale_import_refused_with_message(tmp_path):
    """(a) the stale import must be refused with `BUILD REFUSED (stale
    patch/test import)` and rc=1 (RED before r15: C11 died in the 20-minute
    build)."""
    tree = _mini_tree(tmp_path, STALE)
    r = _run_gate(tree, ["tests/v1/multiprocess/test_gate_case.py"])
    assert r.returncode == 1, (
        f"a stale import must be refused (rc={r.returncode}):\n{r.stdout}")
    assert "BUILD REFUSED (stale patch/test import)" in r.stderr, r.stderr
    assert "IPCCacheServerKey" in r.stderr, r.stderr
    assert "test_gate_case.py" in r.stderr, r.stderr


def test_fixed_import_passes(tmp_path):
    """(b) after the fix (the real r15 fix) the gate is clean, rc=0."""
    tree = _mini_tree(tmp_path, FIXED)
    r = _run_gate(tree, ["tests/v1/multiprocess/test_gate_case.py"])
    assert r.returncode == 0, (
        f"the fixed import must pass (rc={r.returncode}):\n{r.stderr}")
    assert r.stderr == "", r.stderr


def test_fallback_import_not_flagged(tmp_path):
    """(c) an import inside try/except ImportError (fallback) is not
    flagged -- those run-time fallbacks are legitimate."""
    body = (
        "try:\n"
        "    from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey\n"
        "except ImportError:\n"
        "    from lmcache.v1.common.custom_types import IPCCacheServerKey\n"
    )
    tree = _mini_tree(tmp_path, body)
    r = _run_gate(tree, ["tests/v1/multiprocess/test_gate_case.py"])
    assert r.returncode == 0, (
        f"fallback imports must not be flagged:\n{r.stderr}")


def test_namespace_package_accepted(tmp_path):
    """(d) a PEP 420 namespace package (dir without __init__.py) resolves."""
    tree = _mini_tree(tmp_path, FIXED)
    ns = tree / "lmcache" / "v1" / "ns_pkg"
    ns.mkdir()
    (ns / "mod.py").write_text("class Helper:\n    pass\n")
    (tree / "tests" / "v1" / "multiprocess" / "test_gate_case.py").write_text(
        FIXED + "from lmcache.v1.ns_pkg.mod import Helper\n")
    r = _run_gate(tree, ["tests/v1/multiprocess/test_gate_case.py"])
    assert r.returncode == 0, (
        f"namespace packages must resolve:\n{r.stderr}")


def test_exempt_modules_and_proto_gen(tmp_path):
    """(e) generated/native modules are exempt: `lmcache.lmcache_*`, and any
    module path containing `_proto_gen`."""
    body = (
        "from lmcache.lmcache_native import make_something\n"
        "from lmcache.cuda_ops import another_thing\n"
        "from lmcache.v1.generated._proto_gen.kv_pb2 import KvMessage\n"
    )
    tree = _mini_tree(tmp_path, body)
    r = _run_gate(tree, ["tests/v1/multiprocess/test_gate_case.py"])
    assert r.returncode == 0, (
        f"exempt modules must not be flagged:\n{r.stderr}")


def test_empty_file_list_refused(tmp_path):
    """(f) an EMPTY file scope must refuse -- the gate must never silently
    skip (a silent skip is exactly how the r14 class of bugs survives)."""
    tree = _mini_tree(tmp_path, FIXED)
    r = subprocess.run([PY, str(GATE), str(tree)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, "an empty file list must be refused"
    assert "BUILD REFUSED" in r.stderr, r.stderr


# ---------------------------------------------------------------------------
# static checks on the build wiring (the patch-content checks live in
# test_r15_patch_porting.py -- r15 P0)
# ---------------------------------------------------------------------------


def test_gate_wired_in_build_before_docker():
    """build-lmcache.sh must run the gate AFTER the patches and BEFORE the
    docker phase (mutation: removing the call -> this test RED)."""
    src = BUILD_LMC.read_text()
    # the REAL gate call and the REAL docker run command, both in CODE (the
    # strings also appear in comments -- ignore those, or a mutated
    # `if false # ... check_patched_imports.py` would still pass)
    gate_at = -1
    docker_at = -1
    pos = 0
    for ln in src.splitlines():
        code = ln.split("#", 1)[0]  # strip inline comments too
        if gate_at == -1 and "check_patched_imports.py" in code:
            gate_at = pos + code.find("check_patched_imports.py")
        if docker_at == -1 and "docker run" in code:
            docker_at = pos
        pos += len(ln) + 1
    assert gate_at != -1, (
        "build-lmcache.sh must CALL check_patched_imports.py (in code, not "
        "only mention it in a comment)")
    assert docker_at != -1, "build-lmcache.sh must have the docker phase"
    assert gate_at < docker_at, (
        "the import gate must run BEFORE the docker phase")
    assert 'BUILD REFUSED (stale patch/test import)' in src, (
        "the refusal message must be carried by the script wiring")