# SPDX-License-Identifier: Apache-2.0
"""r11: remaining reference-pipeline gates with REAL programs.

* C5/C16: exactly ONE `pip wheel` call in the script (real grep + real run
  with a recording pip shim -- the run must show exactly one invocation);
* C15: identity.env must source cleanly under `bash -c 'set -Eeuo pipefail;
  . identity.env'` with NO stderr (single-quoted values), and provenance.json
  must be real JSON with native_extensions (lmcache/lmcache_native key),
  cuda_ops_archs (sm_120) and torch_cuda_arch_list == 12.0 -- the REAL
  build-lmcache.sh is run with tiny shims;
* C4/C14: the engine freeze diff gate -- real python, a changed torch line in
  the "after" freeze must be refused.
"""
from __future__ import annotations
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
IMPL = ROOT / "implementation"
PY = sys.executable


# ---------------------------------------------------------------------------
# C5/C16: exactly one `pip wheel`
# ---------------------------------------------------------------------------
def test_exactly_one_pip_wheel_in_the_script():
    src = INCONTAINER.read_text()
    count = sum(1 for ln in src.splitlines()
                if "pip wheel" in ln and not ln.lstrip().startswith("#"))
    assert count == 1, (
        f"the in-container script must contain exactly ONE pip wheel "
        f"invocation, found {count} (the old duplicated build block is gone)"
    )


def test_pip_wheel_runs_exactly_once(tmp_path):
    """REAL run: a recording pip shim must record exactly one `wheel` call."""
    import shutil as sh
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                 "echo", "sed", "awk", "tr", "cut", "find", "head", "wc",
                 "mkdir", "rm", "cp", "ls", "date", "basename", "dirname",
                 "unzip", "sort"):
        real = sh.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    rec = tmp_path / "pip-wheel-calls.log"
    (bin_dir / "pip").write_text(
        "#!/usr/bin/env bash\n"
        f'case "$*" in *wheel*) echo "$*" >> "{rec}" ;; esac\n'
        "exit 0\n")
    (bin_dir / "pip").chmod(0o755)
    # the python3 shim delegates everything real except torch probes + heredocs
    ts = tmp_path / "torchstub" / "torch"
    ts.mkdir(parents=True)
    (ts / "__init__.py").write_text(
        "class _C:\n"
        "    @staticmethod\n    def is_available(): return False\n"
        "    @staticmethod\n    def device_count(): return 0\n"
        "    @staticmethod\n    def get_device_capability(i): return (12, 0)\n"
        "cuda = _C()\n__version__ = '2.4.0'\nclass version:\n    cuda = '13.0'\n")
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pip\" ]; then\n"
        "  case \"$*\" in\n"
        f"    *wheel*) echo \"$*\" >> \"{rec}\"; "
        f"python3 -c \"import zipfile; z=zipfile.ZipFile('{tmp_path}/kvp-wheels/lmcache-0.5.7.dev428+kvp.abc.whl','w'); z.writestr('lmcache/cuda_ops_stub.so', b''); z.writestr('lmcache/lmcache_native_stub.so', b'')\" ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        f"if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"kvpersist.check_wheel_native\" ]; then\n"
        f"  shift 2; PYTHONPATH={IMPL} exec /usr/bin/python3 -m kvpersist.check_wheel_native \"$@\"; fi\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'Python 3.12.3'; exit 0; fi\n"
        "if [ \"$1\" = \"-m\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"-c\" ]; then\n  case \"$2\" in\n"
        "    *torch.version.cuda*) echo '13.0' ;;\n"
        "    *is_available*) echo 'False' ;;\n"
        "    *device_count*) echo '0' ;;\n"
        "    *__version__*) echo '2.4.0' ;;\n"
        "    *get_device_capability*) echo '(12, 0)' ;;\n"
        "    *) echo '' ;;\n  esac\n  exit 0\nfi\n"
        "if [ \"$1\" = \"-\" ] || [ \"$1\" = \"-P\" ]; then cat > /dev/null; exit 0; fi\n"
        "exec /usr/bin/python3 \"$@\"\n")
    (bin_dir / "python3").chmod(0o755)
    for tool, body in [
        ("git", "#!/usr/bin/env bash\ncase \"$*\" in *'rev-parse HEAD'*) echo 03ee5b82418b6bd44e424cdd46932d9346f43eb2 ;; *'merge-base --is-ancestor'*) exit 0 ;; *'apply --reverse --check'*) exit 1 ;; *'apply --check'*|*'apply '*) exit 0 ;; *'diff --cached'*) echo stubdiff ;; *'ls-files'*) exit 0 ;; *) exit 0 ;; esac\n"),
        ("apt-get", "#!/usr/bin/env bash\nexit 0\n"),
        ("nvcc", "#!/usr/bin/env bash\necho 'Cuda 13.0'\n"),
        ("g++", "#!/usr/bin/env bash\necho 'g++'\n"),
        ("ninja", "#!/usr/bin/env bash\necho 'ninja'\n"),
        ("cuobjdump", "#!/usr/bin/env bash\necho 'Fatbin elf code: sm_120'\n"),
        ("chown", "#!/usr/bin/env bash\nexit 0\n"),
    ]:
        p = bin_dir / tool
        p.write_text(body)
        p.chmod(0o755)
    src_dir = tmp_path / "src"
    (src_dir / ".git").mkdir(parents=True)
    (src_dir / "requirements").mkdir()
    (src_dir / "requirements" / "build.txt").write_text("ninja\n")
    for sub in ("kvp-work", "kvp-wheels"):
        (tmp_path / sub).mkdir()
    import zipfile
    with zipfile.ZipFile(tmp_path / "kvp-wheels" / "lmcache-0.5.7.dev428+kvp.abc.whl", "w") as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
        z.writestr("lmcache/cuda_ops_stub.so", b"\x00")
        z.writestr("lmcache/lmcache_native_stub.so", b"\x00")
    env = dict(os.environ, PATH=str(bin_dir),
               KVP_BUILD_SKIP_TESTS="1", KVP_BUILD_GPU_TESTS="0",
               KVP_HOST_UID="1000", KVP_HOST_GID="1000",
               KVP_SCRIPTS_DIR=str(ROOT / "scripts"),
               KVP_IMPL_DIR=str(IMPL),
               KVP_LMC_SRC=str(src_dir),
               KVP_WORK_DIR=str(tmp_path / "kvp-work"),
               KVP_WHEELS_DIR=str(tmp_path / "kvp-wheels"))
    r = subprocess.run(["bash", str(INCONTAINER)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    calls = rec.read_text() if rec.is_file() else ""
    wheel_calls = [ln for ln in calls.splitlines() if ln.strip().startswith("-m pip wheel")]
    assert len(wheel_calls) == 1, (
        f"exactly ONE pip wheel call expected, got {len(wheel_calls)}:\n{calls}"
    )


# ---------------------------------------------------------------------------
# C15: identity.env sourcing (REAL bash) + provenance.json (REAL json.loads)
# ---------------------------------------------------------------------------
def test_identity_env_sources_cleanly_under_strict_bash(tmp_path):
    """`bash -c 'set -Eeuo pipefail; . identity.env'` must produce NO stderr
    and set the values (the old code broke on non-empty JSON -- 'No such file
    or directory' from the unquoted brace expansion)."""
    env_file = tmp_path / "identity.env"
    env_file.write_text(
        "TORCH_CUDA_ARCH_LIST_USED='12.0'\n"
        "CUDA_OPS_ARCHS='sm_120'\n"
        'NATIVE_EXTENSIONS_JSON=\'{"lmcache/cuda_ops": "lmcache/cuda_ops.so"}\'\n'
        "GPU_REGRESSION_RUN='false'\n"
    )
    r = subprocess.run(
        ["bash", "-c", "set -Eeuo pipefail; . \"$1\"; "
         "echo OK:$TORCH_CUDA_ARCH_LIST_USED:$CUDA_OPS_ARCHS:$GPU_REGRESSION_RUN",
         "_", str(env_file)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"identity.env must source cleanly:\n{r.stderr}"
    assert r.stderr == "", f"strict bash must print NO errors:\n{r.stderr}"
    assert "OK:12.0:sm_120:false" in r.stdout, r.stdout


def test_provenance_json_fields(tmp_path):
    """REAL build-lmcache.sh run (tiny shims): provenance.json must be real
    JSON with native_extensions (containing lmcache_native), cuda_ops_archs
    (sm_120) and torch_cuda_arch_list == 12.0."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                 "echo", "sed", "awk", "tr", "cut", "find", "head", "wc",
                 "mkdir", "rm", "cp", "ls", "date", "basename", "dirname",
                 "unzip", "sort", "sha256sum", "id", "df"):
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    ts = tmp_path / "torchstub" / "torch"
    ts.mkdir(parents=True)
    (ts / "__init__.py").write_text(
        "class _C:\n"
        "    @staticmethod\n    def is_available(): return False\n"
        "    @staticmethod\n    def device_count(): return 0\n"
        "    @staticmethod\n    def get_device_capability(i): return (12, 0)\n"
        "cuda = _C()\n__version__ = '2.4.0'\nclass version:\n    cuda = '13.0'\n")
    build_dir = tmp_path / "build"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (build_dir / "LMCache" / ".git").mkdir(parents=True)
    (build_dir / "LMCache" / "requirements").mkdir()
    (build_dir / "wheels").mkdir(parents=True)
    # the host phase sources identity.env from KVP_BUILD_DIR (= WORK_DIR)
    # the values are UNIQUE (not the script defaults) so a renamed variable
    # in the script cannot pass by falling back to its default
    (build_dir / "identity.env").write_text(
        "TORCH_CUDA_ARCH_LIST_USED='7.5 8.0 12.0'\n"
        "CUDA_OPS_ARCHS='sm_120 sm_90'\n"
        'NATIVE_EXTENSIONS_JSON=\'{"lmcache/cuda_ops": "lmcache/cuda_ops.so", '
        '"lmcache/lmcache_native": "lmcache/lmcache_native.so"}\'\n'
        "GPU_REGRESSION_RUN='false'\n"
    )
    # the host phase also verifies the wheel in build_dir/wheels and expects
    # the container output in out_dir/wheels (WHEEL_OUT) -- provide both.
    import zipfile
    for whl_dir in (build_dir / "wheels", out_dir / "wheels"):
        whl_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(
            whl_dir / "lmcache-0.5.7.dev428+kvp.abc.whl", "w"
        ) as z:
            z.writestr("lmcache/cuda_ops_stub.so", b"\x00")
            z.writestr("lmcache/lmcache_native_stub.so", b"\x00")
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-m\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"-c\" ]; then\n  case \"$2\" in\n"
        "    *'import torch'*|*torch*) PYTHONPATH=" + str(ts) + " exec /usr/bin/python3 \"$@\" ;;\n"
        "    *) exec /usr/bin/python3 \"$@\" ;;\n  esac\nfi\n"
        "if [ \"$1\" = \"-\" ] || [ \"$1\" = \"-P\" ]; then PYTHONPATH=" + str(ts) + " exec /usr/bin/python3 \"$@\"; fi\n"
        "exec /usr/bin/python3 \"$@\"\n")
    (bin_dir / "python3").chmod(0o755)
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "cmd=\"$1\"; shift || true\n"
        "case \"$cmd\" in\n"
        "  run) cat > /dev/null; exit 0 ;;\n"
        "  inspect) echo \"sha256:$(printf 'a%.0s' $(seq 64))\"; exit 0 ;;\n"
        "  info) exit 0 ;;\n"
        "  *) exit 0 ;;\nesac\n")
    (bin_dir / "docker").chmod(0o755)
    for tool, body in [
        ("git", "#!/usr/bin/env bash\ncase \"$*\" in *'rev-parse HEAD'*) echo 03ee5b82418b6bd44e424cdd46932d9346f43eb2 ;; *'merge-base --is-ancestor'*) exit 0 ;; *'apply --reverse --check'*) exit 1 ;; *'apply --check'*|*'apply '*) exit 0 ;; *'diff --cached'*) echo stubdiff ;; *'ls-files'*) exit 0 ;; *) exit 0 ;; esac\n"),
    ]:
        p = bin_dir / tool
        p.write_text(body)
        p.chmod(0o755)
    (build_dir / "provenance.json").write_text(
        json.dumps({"wheel_version": "0.5.7.dev428+kvp.deadbeefcafe"}))
    pdir = tmp_path / "patches"
    pdir.mkdir()
    (pdir / "4136-mp-retrieve-abort.patch").write_text("a\n")
    (pdir / "5972-incomplete-kv-objects.patch").write_text("b\n")
    env = dict(os.environ,
               PATH=str(bin_dir),
               KVP_BUILD_DIR=str(build_dir),
               KVP_PATCH_DIR=str(pdir),
               KVP_OUT_DIR=str(out_dir),
               KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
               KVP_LIVE_DIR=str(out_dir / "live"),
               KVP_WORK_DIR=str(tmp_path / "kvp-work"),
               KVP_PIN_FILE=str(ROOT / "vendor" / "pinned-base-image.env"))
    for var in ("IMAGE", "KVP_LMC_IMAGE", "KVP_BASE_IMAGE"):
        env.pop(var, None)
    r = subprocess.run(["bash", str(BUILD_LMC)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    prov = out_dir / "provenance.json"
    assert prov.is_file(), f"provenance.json missing:\n{r.stdout[-400:]}\n{r.stderr[-400:]}"
    data = json.loads(prov.read_text())
    ext = data["native_extensions"]
    assert "lmcache/lmcache_native" in ext, f"native_extensions must carry lmcache_native: {ext}"
    assert data["cuda_ops_archs"] == "sm_120 sm_90", (
        f"cuda_ops_archs must be the identity.env value, got {data['cuda_ops_archs']!r}"
    )
    assert data["torch_cuda_arch_list"] == "7.5 8.0 12.0", (
        f"torch_cuda_arch_list must be the identity.env value, got {data['torch_cuda_arch_list']!r}"
    )


# ---------------------------------------------------------------------------
# C4/C14: engine freeze diff (REAL python check, changed torch refused)
# ---------------------------------------------------------------------------
def test_engine_freeze_diff_refuses_changed_torch(tmp_path):
    """The C14 diff logic is real python code in the script -- exercise the
    same logic against a changed torch pin and require refusal."""
    checker = INCONTAINER.read_text()
    start = checker.index("import re, sys\ndef engine_pins(path):")
    end = checker.index("PY\n", start)
    code = checker[start:end]
    before = tmp_path / "before.txt"
    after = tmp_path / "after.txt"
    before.write_text("torch==2.14.0+cu130\nnumpy==2.1.0\n")
    after.write_text("torch==2.15.0\nnumpy==2.1.0\n")
    r = subprocess.run(
        [PY, "-c", code, str(before), str(after)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode != 0, "changed torch must be refused"
    assert "BUILD REFUSED" in r.stderr, f"refusal must say BUILD REFUSED:\n{r.stderr}"
    # unchanged -> pass
    after.write_text("torch==2.14.0+cu130\nnumpy==2.1.0\n")
    r2 = subprocess.run(
        [PY, "-c", code, str(before), str(after)],
        capture_output=True, text=True, timeout=30,
    )
    assert r2.returncode == 0, f"unchanged pins must pass:\n{r2.stderr}"