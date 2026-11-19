# SPDX-License-Identifier: Apache-2.0
"""r9 P1-1/P1-2: KVP_BUILD_GPU_TESTS=1 needs --gpus all + nvidia runtime
pre-flight; the GPU step must not count a module-skip as success; and
provenance.json must record the boolean gpu_regression_run.

Reproduced (P1-1): the docker run block never gained --gpus, so with the flag
the container started GPU-less, test_cache_server.py module-skipped, pytest
returned rc=0 '1 skipped' and the step wrote the FALSE gpu_regression_run=true.
Reproduced (P1-2): the provenance.json heredoc had NO gpu_regression_run field
at all (the docs claimed it does).
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
PIN_FILE = ROOT / "vendor" / "pinned-base-image.env"

DOCKER_STUB = '''#!/usr/bin/env bash
cmd="${1:-}"; shift || true
LOG="${KVP_TEST_DOCKER_LOG:-/tmp}/docker-args.log"
echo "$cmd $*" >> "$LOG"
case "$cmd" in
  run)
    if [ -n "$KVP_TEST_RUN_OUT" ] && [ -f "$KVP_TEST_RUN_OUT" ]; then cat "$KVP_TEST_RUN_OUT"; fi
    exit ${KVP_TEST_RUN_RC:-0} ;;
  inspect) echo "sha256:$(printf 'a%.0s' $(seq 64))"; exit 0 ;;
  info) if [ "${KVP_TEST_NO_NVIDIA:-0}" = "1" ]; then printf 'Runtimes: io.containerd.runc.v2\n'; else printf 'Runtimes: io.containerd.runc.v2 nvidia\n'; fi; exit 0 ;;
  *) exit 0 ;;
esac
'''


def _stub_env(tmp_path: pathlib.Path) -> tuple:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "docker").write_text(DOCKER_STUB)
    (bin_dir / "docker").chmod(0o755)
    # git stub: the fixture "LMCache" tree is not a real git repo
    (bin_dir / "git").write_text(
        "#!/usr/bin/env bash\ncase \"$*\" in\n"
        "  *'rev-parse HEAD'*) echo 07f1c6bb642f4772606a36eab730ae009ddce08f ;;\n"
        "  *'merge-base --is-ancestor'*) exit 0 ;;\n"
        "  *'apply --reverse --check'*) exit 1 ;;\n"
        "  *'apply --check'*|*'apply '*) exit 0 ;;\n"
        "  *'diff --cached'*) echo stubdiff ;;\n"
        "  *'ls-files'*) exit 0 ;;\n"
        "  *) exit 0 ;;\nesac\n")
    (bin_dir / "git").chmod(0o755)
    log_dir = tmp_path / "docker-log"
    log_dir.mkdir(exist_ok=True)
    build_dir = tmp_path / "build"
    (build_dir / "LMCache" / ".git").mkdir(parents=True)
    (build_dir / "LMCache" / "requirements").mkdir()
    # r15: the static import gate needs the five CPU-gate files to exist
    # (fixtures adapt to the gate -- the gate is not weakened)
    for _rel in ("tests/v1/test_lmcache_mp_connector.py",
                 "tests/v1/test_vllm_mp_adapter.py",
                 "tests/v1/multiprocess/test_lmcache_driven_transfer_skip.py",
                 "tests/v1/multiprocess/test_native_state_lookup.py",
                 "tests/v1/multiprocess/test_l1_owner_completion.py"):
        _p = build_dir / "LMCache" / _rel
        _p.parent.mkdir(parents=True, exist_ok=True)
        _p.write_text("def test_ok():\n    assert True\n")
    pdir = tmp_path / "patches"
    pdir.mkdir()
    (pdir / "4136-mp-retrieve-abort.patch").write_text("a\n")
    (pdir / "5972-incomplete-kv-objects.patch").write_text("b\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # a real zip carrying a cuda_ops .so -- passes check_wheel_native
    import zipfile
    whl_name = "lmcache-0.5.8rc1.post48+kvp.abc.whl"
    (build_dir / "wheels").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(build_dir / "wheels" / whl_name, "w") as z:
        z.writestr("lmcache/cuda_ops_stub.so", b"\x00")
        z.writestr("lmcache/lmcache_native_stub.so", b"\x00")
    # WHEEL_OUT is OUT_DIR/wheels -- the host phase looks there after the run
    (out_dir / "wheels").mkdir()
    import shutil
    shutil.copy(build_dir / "wheels" / whl_name, out_dir / "wheels" / whl_name)
    (build_dir / "provenance.json").write_text(json.dumps({"wheel_version": "0.5.8rc1.post48+kvp.x"}))
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_TEST_DOCKER_LOG=str(log_dir),
               KVP_TEST_SCRIPTS_DIR=str(ROOT / "scripts"),
               KVP_BUILD_DIR=str(build_dir), KVP_PATCH_DIR=str(pdir),
               KVP_OUT_DIR=str(out_dir),
               KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
               KVP_LIVE_DIR=str(out_dir / "live"),
               KVP_PIN_FILE=str(PIN_FILE))
    for var in ("IMAGE", "KVP_LMC_IMAGE", "KVP_BASE_IMAGE"):
        env.pop(var, None)
    return env, log_dir


def test_gpus_flag_added_when_requested(tmp_path):
    """KVP_BUILD_GPU_TESTS=1 -> docker run MUST carry --gpus all; without the
    flag it must NOT (red before: the flag never reached docker run)."""
    env, log_dir = _stub_env(tmp_path)
    env["KVP_BUILD_GPU_TESTS"] = "1"
    subprocess.run(["bash", str(BUILD_LMC)], capture_output=True, text=True,
                   timeout=120, env=env, cwd=str(tmp_path))
    args = (log_dir / "docker-args.log").read_text()
    assert "--gpus all" in args, f"--gpus all missing from docker run:\n{args[-500:]}"
    # and without the flag it stays absent
    run2 = tmp_path / "run2"
    run2.mkdir(exist_ok=True)
    env2, log_dir2 = _stub_env(run2)
    subprocess.run(["bash", str(BUILD_LMC)], capture_output=True, text=True,
                   timeout=120, env=env2, cwd=str(run2))
    args2 = (log_dir2 / "docker-args.log").read_text()
    assert "--gpus" not in args2, f"--gpus must be absent without the flag:\n{args2[-500:]}"


def test_no_nvidia_runtime_refuses_before_container(tmp_path):
    """Flag set + docker without nvidia runtime -> clear refusal BEFORE docker run."""
    env, log_dir = _stub_env(tmp_path)
    env["KVP_BUILD_GPU_TESTS"] = "1"
    env["KVP_TEST_NO_NVIDIA"] = "1"   # the stub reports no nvidia runtime
    r = subprocess.run(["bash", str(BUILD_LMC)], capture_output=True, text=True,
                       timeout=120, env=env, cwd=str(tmp_path))
    assert r.returncode != 0, "no nvidia runtime must refuse"
    out = (r.stdout + r.stderr).lower()
    assert "nvidia" in out, f"refusal must name nvidia runtime:\n{out[-500:]}"
    args = (log_dir / "docker-args.log").read_text()
    assert "run" not in args, f"the container must NOT start:\n{args[-300:]}"


def test_gpu_step_skip_is_not_success(tmp_path):
    """In the GPU step a module-level skip ('1 skipped') must NOT produce
    gpu_regression_run=true (red before: rc=0 alone counted as success)."""
    import shutil
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                 "sed", "awk", "tr", "cut", "find", "head", "wc", "mkdir",
                 "rm", "cp", "ls", "date", "basename", "dirname", "unzip", "sort", "df"):
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    out = tmp_path / "pyout.txt"
    out.write_text("5 passed in 0.05s\n")   # CPU gate: clean
    gpu_out = tmp_path / "gpuout.txt"
    gpu_out.write_text("1 skipped in 0.01s\nSKIPPED [1] tests/v1/multiprocess/test_cache_server.py:50: requires available CUDA runtime\n")
    ts = tmp_path / "torchstub" / "torch"
    ts.mkdir(parents=True)
    (ts / "__init__.py").write_text(
        "class _C:\n    @staticmethod\n    def is_available(): return True\n"
        "    @staticmethod\n    def device_count(): return 1\n"
        "    @staticmethod\n    def get_device_capability(i): return (12, 0)\n"
        "cuda = _C()\n__version__ = '2.4.0'\nclass version:\n    cuda = '13.0'\n")
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-P\" ]; then shift; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pip\" ]; then\n"
        "  case \"$*\" in *wheel*)\n"
        f"    /usr/bin/python3 -c \"import zipfile; z=zipfile.ZipFile('{tmp_path}/kvp-wheels/lmcache-0.5.8rc1.post48+kvp.abc.whl','w'); z.writestr('lmcache/__init__.py', b'#'); z.writestr('lmcache/cuda_ops_stub.so', b''); z.writestr('lmcache/lmcache_native_stub.so', b'')\" ;;\n"
        "  esac\n"
        "  exit 0\nfi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pytest\" ]; then\n"
        "  case \"$*\" in\n"
        f"    *test_cache_server.py*) cat \"{gpu_out}\" ;;\n"
        f"    *) cat \"{out}\" ;;\n"
        "  esac\n  exit 0\nfi\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'Python 3.12.3'; exit 0; fi\n"
        "if [ \"$1\" = \"-c\" ]; then\n  case \"$2\" in\n"
        "    *torch.version.cuda*) echo '13.0' ;;\n"
        "    *is_available*) echo 'True' ;;\n"
        "    *device_count*) echo '1' ;;\n"
        "    *__version__*) echo '2.4.0' ;;\n"
        "    *get_device_capability*) echo '(12, 0)' ;;\n"
        "    *md.version*) echo 'x' ;;\n"
        "    *re.search*|*zipfile*) exec /usr/bin/python3 \"$@\" ;;\n"
        "    *) echo '' ;;\n  esac\n  exit 0\nfi\n"
        "if [ \"$1\" = \"-\" ] || [ \"$1\" = \"-P\" ]; then cat > /dev/null; exit 0; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pip\" ]; then shift 2; exec pip \"$@\"; fi\n"
        "if [ \"$1\" = \"-m\" ]; then exit 0; fi\n"
        "exec /usr/bin/python3 \"$@\"\n")
    (bin_dir / "python3").chmod(0o755)
    for tool, body in [
        ("git", "#!/usr/bin/env bash\ncase \"$*\" in *ls-files*) exit 0 ;; esac\nexit 0\n"),
        ("apt-get", "#!/usr/bin/env bash\nexit 0\n"),
        ("nvcc", "#!/usr/bin/env bash\necho 'Cuda 13.0'\n"),
        ("g++", "#!/usr/bin/env bash\necho g++\n"),
        ("ninja", "#!/usr/bin/env bash\necho ninja\n"),
        ("cuobjdump", "#!/usr/bin/env bash\necho 'Fatbin elf code:\n  arch = sm_120'\n"),
        ("pip", "#!/usr/bin/env bash\nexit 0\n"),
        ("chown", "#!/usr/bin/env bash\nexit 0\n"),
    ]:
        p = bin_dir / tool
        p.write_text(body)
        p.chmod(0o755)
    for sub in ("kvp-work", "kvp-wheels", "src"):
        (tmp_path / sub).mkdir(exist_ok=True)
    # r11 C10: staging copies tests/ + pytest.ini from $LMC_SRC into testroot
    _t1 = tmp_path / "src" / "tests" / "v1"
    _t1.mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "tests" / "__init__.py").write_text("")
    (_t1 / "__init__.py").write_text("")
    (_t1 / "test_stub.py").write_text("def test_stub():\n    assert True\n")
    (_t1 / "multiprocess").mkdir(exist_ok=True)
    (_t1 / "multiprocess" / "__init__.py").write_text("")
    for _n in ("test_lmcache_mp_connector.py", "test_vllm_mp_adapter.py"):
        (_t1 / _n).write_text("def test_x():\n    assert True\n")
    for _n in ("test_lmcache_driven_transfer_skip.py", "test_native_state_lookup.py",
               "test_l1_owner_completion.py", "test_cache_server.py"):
        (_t1 / "multiprocess" / _n).write_text("def test_x():\n    assert True\n")
    (tmp_path / "src" / "pytest.ini").write_text("[pytest]\n")
    # r10: the phase verifies the wheel content before the GPU step -- provide
    # a complete one (both native extensions).
    import zipfile
    with zipfile.ZipFile(
        tmp_path / "kvp-wheels" / "lmcache-0.5.8rc1.post48+kvp.abc.whl", "w"
    ) as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
        z.writestr("lmcache/cuda_ops_stub.so", b"\x00")
        z.writestr("lmcache/lmcache_native_stub.so", b"\x00")
    env = dict(os.environ, PATH=str(bin_dir),
               KVP_BUILD_SKIP_TESTS="0", KVP_BUILD_GPU_TESTS="1",
               KVP_HOST_UID="1000", KVP_HOST_GID="1000",
               KVP_SCRIPTS_DIR=str(ROOT / "scripts"),
               KVP_IMPL_DIR=str(ROOT / "implementation"),
               KVP_LMC_SRC=str(tmp_path / "src"),
               KVP_WORK_DIR=str(tmp_path / "kvp-work"),
               KVP_WHEELS_DIR=str(tmp_path / "kvp-wheels"))
    r = subprocess.run(["bash", str(INCONTAINER)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    assert r.returncode != 0, (
        f"a module-skip in the GPU step must refuse the build:\n{r.stdout[-500:]}"
    )
    assert "REGRESSION FAILURE" in (r.stdout + r.stderr) or "did NOT run" in (r.stdout + r.stderr)


def test_provenance_has_boolean_gpu_regression_run(tmp_path):
    """provenance.json must carry boolean gpu_regression_run (json.loads),
    false by default without a GPU step (red before: field absent entirely)."""
    env, log_dir = _stub_env(tmp_path)
    # host phase sources identity.env with GPU_REGRESSION_RUN=false (default)
    r = subprocess.run(["bash", str(BUILD_LMC)], capture_output=True, text=True,
                       timeout=120, env=env, cwd=str(tmp_path))
    prov = tmp_path / "out" / "provenance.json"
    assert prov.is_file(), f"provenance.json missing:\n{r.stdout[-400:]}\n{r.stderr[-400:]}"
    data = json.loads(prov.read_text())
    assert "gpu_regression_run" in data, f"gpu_regression_run key missing: {data.keys()}"
    assert isinstance(data["gpu_regression_run"], bool), (
        f"gpu_regression_run must be a JSON boolean, got {data['gpu_regression_run']!r}"
    )
    assert data["gpu_regression_run"] is False, "no GPU step run -> false"
