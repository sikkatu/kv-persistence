# SPDX-License-Identifier: Apache-2.0
"""r8: GPU-integration test must not gate a GPU-less build; root-owned build
artifacts must be chowned back to the host user even on failure; GPU_CC in
identity.env must be plain `12.0` (a tuple breaks `source` in bash).

Reproduced: scripts/_incontainer_build.sh ran six regression files and
required skipped == 0. The build container runs WITHOUT --gpus, and
tests/v1/multiprocess/test_cache_server.py does
`pytest.skip("requires available CUDA runtime", allow_module_level=True)`
-- 'N passed, 1 skipped' trips the gate, so a production build WITHOUT
KVP_BUILD_SKIP_TESTS=1 can never finish.

Rule: stubs must not forgive what the real command does not forgive.
"""
from __future__ import annotations
import os
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"

PYTEST_OUT_CPU_OK = "......                                                           [100%]\n6 passed in 0.05s\n"
PYTEST_OUT_CACHE_SERVER_SKIP = (
    ".....                                                            [100%]\n"
    "5 passed, 1 skipped in 0.05s\n"
    "SKIPPED [1] tests/v1/multiprocess/test_cache_server.py:50: requires available CUDA runtime\n"
)
PYTEST_OUT_GATE_FILE_SKIP = (
    ".....                                                            [100%]\n"
    "5 passed, 1 skipped in 0.05s\n"
    "SKIPPED [1] tests/v1/test_vllm_mp_adapter.py:483: could not import 'lmcache.integration.vllm.lmcache_mp_connector'\n"
)


def _make_stubs(tmp_path: pathlib.Path, pytest_out: str, pytest_rc: int = 0,
                gpu_out: str | None = None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    out_file = tmp_path / "pytest-out.txt"
    out_file.write_text(pytest_out)
    gpu_file = tmp_path / "pytest-gpu-out.txt"
    gpu_file.write_text(gpu_out if gpu_out is not None else pytest_out)
    chown_log = tmp_path / "chown.log"
    # torch stub so the REAL identity-heredoc python code runs unmodified
    ts = tmp_path / "torchstub"
    (ts / "torch").mkdir(parents=True, exist_ok=True)
    (ts / "torch" / "__init__.py").write_text(
        "class _C:\n"
        "    @staticmethod\n"
        "    def is_available(): return False\n"
        "    @staticmethod\n"
        "    def device_count(): return 0\n"
        "    @staticmethod\n"
        "    def get_device_capability(i): return (12, 0)\n"
        "cuda = _C()\n"
        "__version__ = '2.4.0'\n"
        "class version:\n    cuda = '13.0'\n"
    )
    ident_env = tmp_path / "kvp-work" / "identity.env"
    py_stub = (
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-P\" ]; then shift; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pip\" ]; then\n"
"  case \"$*\" in *wheel*)\n"
        f"    /usr/bin/python3 -c \"import zipfile; z=zipfile.ZipFile('{tmp_path}/kvp-wheels/lmcache-0.5.7.dev428+kvp.abc.whl','w'); z.writestr('lmcache/__init__.py', b'#'); z.writestr('lmcache/cuda_ops_stub.so', b''); z.writestr('lmcache/lmcache_native_stub.so', b'')\" ;;\n"
        "  esac\n"
        "  exit 0\nfi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pytest\" ]; then\n"
        # honest pytest model: the module-level CUDA skip of
        # test_cache_server.py happens ONLY when that file is collected.
        "  case \"$*\" in\n"
        "    *test_cache_server.py*)\n"
        f"      cat \"{gpu_file}\" ;;\n"
        "    *)\n"
        f"      cat \"{out_file}\" ;;\n"
        "  esac\n"
        f"  exit {pytest_rc}\n"
        "fi\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'Python 3.12.3'; exit 0; fi\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  case \"$2\" in\n"
        "    *torch.version.cuda*) echo '13.0' ;;\n"
        "    *is_available*) echo 'False' ;;\n"
        "    *device_count*) echo '0' ;;\n"
        "    *__version__*) echo '2.4.0' ;;\n"
        "    *get_device_capability*) echo '(12, 0)' ;;\n"
        "    *md.version*) echo '0.5.7.dev428+kvp.abc' ;;\n"
        # r12: the C6c block discovers/extracts the cuda_ops .so via python3
        # zipfile (no unzip dependency) -- these probes must run for real.
        "    *re.search*|*zipfile*) exec /usr/bin/python3 \"$@\" ;;\n"
        "    *) echo '' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        f"if [ \"$1\" = \"-\" ]; then body=$(cat); case \"$body\" in *GPU_CC=*) printf '%s\\n' \"$body\" | PYTHONPATH={ts} /usr/bin/python3 - > \"{ident_env}\" ;; *) : ;; esac; exit 0; fi\n"
        # r10: the import-check heredoc runs as `python3 -P -` -- it must see
        # site-packages modules; the stub accepts it and prints the proof lines.
        "if [ \"$1\" = \"-P\" ]; then cat > /dev/null; echo 'lmcache.__file__ = /usr/lib/python3/site-packages/lmcache/__init__.py'; echo 'lmcache.cuda_ops.__file__ = /usr/lib/python3/site-packages/lmcache/cuda_ops.so'; echo 'lmcache.lmcache_native.__file__ = /usr/lib/python3/site-packages/lmcache/lmcache_native.so'; echo '[in-container] import-check OK (all native modules from site-packages)'; exit 0; fi\n"
        "if [ \"$1\" = \"-m\" ]; then exit 0; fi\n"
        "exec /usr/bin/python3 \"$@\"\n"
    )
    (bin_dir / "python3").write_text(py_stub)
    (bin_dir / "python3").chmod(0o755)
    for tool, body in [
        ("git", "#!/usr/bin/env bash\nexit 0\n"),
        ("apt-get", "#!/usr/bin/env bash\nexit 0\n"),
        ("pip", "#!/usr/bin/env bash\nexit 0\n"),
        ("nvcc", "#!/usr/bin/env bash\necho 'Cuda compilation tools, release 13.0, V13.0.61'\n"),
        ("g++", "#!/usr/bin/env bash\necho g++\n"),
        ("ninja", "#!/usr/bin/env bash\necho ninja\n"),
        ("cuobjdump", "#!/usr/bin/env bash\necho 'Fatbin elf code:\\n  arch = sm_120'\n"),
        ("chown", f'#!/usr/bin/env bash\necho "$@" >> "{chown_log}"\nexit 0\n'),
    ]:
        p = bin_dir / tool
        p.write_text(body)
        p.chmod(0o755)
    return bin_dir, chown_log


def _run_incontainer(tmp_path: pathlib.Path, pytest_out: str,
                     pytest_rc: int = 0, extra_env=None, gpu_out: str | None = None):
    bin_dir, chown_log = _make_stubs(tmp_path, pytest_out, pytest_rc, gpu_out=gpu_out)
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
    # r10: the phase verifies the wheel content before the tests -- provide a
    # complete one (both native extensions).
    import zipfile
    with zipfile.ZipFile(
        tmp_path / "kvp-wheels" / "lmcache-0.5.7.dev428+kvp.abc.whl", "w"
    ) as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
        z.writestr("lmcache/cuda_ops_stub.so", b"\x00")
        z.writestr("lmcache/lmcache_native_stub.so", b"\x00")
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_BUILD_SKIP_TESTS="0",
               KVP_BUILD_GPU_TESTS="0",
               KVP_HOST_UID="1000", KVP_HOST_GID="1000",
               KVP_SCRIPTS_DIR=str(ROOT / "scripts"),
               KVP_LMC_SRC=str(tmp_path / "src"),
               KVP_WORK_DIR=str(tmp_path / "kvp-work"),
               KVP_WHEELS_DIR=str(tmp_path / "kvp-wheels"))
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        ["bash", str(INCONTAINER)], capture_output=True, text=True,
        timeout=120, env=env, cwd=str(tmp_path),
    )
    return r, chown_log


# ---------------------------------------------------------------------------
# P0
# ---------------------------------------------------------------------------
def test_cache_server_skip_passes_the_gate(tmp_path):
    """The module-level CUDA skip of test_cache_server.py must NOT gate the
    build -- the five CPU gate files run WITHOUT that file and pass
    (red before the fix: the old single six-file run collected it and the
    skipped!=0 gate aborted the build)."""
    r, _ = _run_incontainer(tmp_path, PYTEST_OUT_CPU_OK,
                            gpu_out=PYTEST_OUT_CACHE_SERVER_SKIP)
    assert r.returncode == 0, (
        f"a skip of test_cache_server.py must not gate the build:\n"
        f"{r.stdout[-800:]}\n{r.stderr[-800:]}"
    )


def test_skip_in_five_cpu_files_still_aborts(tmp_path):
    """A skip in one of the FIVE mandatory CPU files must still abort the
    build (the gate must not become toothless)."""
    r, _ = _run_incontainer(tmp_path, PYTEST_OUT_GATE_FILE_SKIP)
    assert r.returncode != 0, "a skip in a mandatory CPU test must abort"
    out = r.stdout + r.stderr
    assert "SKIPPED" in out and "test_vllm_mp_adapter" in out, (
        f"the error must list the SKIPPED lines from -rs:\n{out[-800:]}"
    )


def test_gpu_tests_run_only_when_requested(tmp_path):
    """KVP_BUILD_GPU_TESTS=1 runs test_cache_server.py in a separate GPU step
    (the log must say so); with 0 the step is skipped with a clear message."""
    r, _ = _run_incontainer(tmp_path, PYTEST_OUT_CPU_OK, extra_env={"KVP_BUILD_GPU_TESTS": "1"})
    assert "gpu" in (r.stdout + r.stderr).lower(), (
        "the GPU regression step must be announced in the log"
    )
    r2, _ = _run_incontainer(tmp_path, PYTEST_OUT_CPU_OK)
    assert "skip" in (r2.stdout + r2.stderr).lower() or "gpu" in (r2.stdout + r2.stderr).lower(), (
        "the skipped GPU step must be announced in the log"
    )


# ---------------------------------------------------------------------------
# P2-1
# ---------------------------------------------------------------------------
def test_chown_runs_even_on_pytest_failure(tmp_path):
    """Even when pytest fails, the trap must chown the mounted dirs back to
    the host user (root-owned artifacts block a re-run)."""
    r, chown_log = _run_incontainer(tmp_path, "1 failed in 0.05s", pytest_rc=1)
    assert r.returncode != 0
    log = chown_log.read_text() if chown_log.is_file() else ""
    assert "1000:1000" in log, (
        f"chown back to KVP_HOST_UID:GID must run on failure too:\n{log}"
    )
    for d in (str(tmp_path / "src"), str(tmp_path / "kvp-wheels"), str(tmp_path / "kvp-work")):
        assert d in log, f"chown must cover {d}:\n{log}"


def test_chown_runs_on_success(tmp_path):
    r, chown_log = _run_incontainer(tmp_path, PYTEST_OUT_CPU_OK)
    log = chown_log.read_text() if chown_log.is_file() else ""
    assert "1000:1000" in log, f"chown must run after a successful build too:\n{log}"


# ---------------------------------------------------------------------------
# P2-2
# ---------------------------------------------------------------------------
def test_gpu_cc_identity_format_is_shell_safe(tmp_path):
    """identity.env must hold GPU_CC=12.0 (a tuple `(12, 0)` breaks `source`
    in bash and `${GPU_CC:-unknown}` yields '12,')."""
    r, _ = _run_incontainer(tmp_path, PYTEST_OUT_CPU_OK)
    identity = tmp_path / "kvp-work" / "identity.env"
    text = identity.read_text() if identity.is_file() else r.stdout
    m = re.search(r"^GPU_CC=(.*)$", text, re.M)
    assert m, f"identity.env must carry GPU_CC:\n{text}"
    val = m.group(1).strip()
    assert re.fullmatch(r"[0-9]+\.[0-9]+|n/a", val), (
        f"GPU_CC must be plain '12.0' or 'n/a', got {val!r}"
    )


def test_build_lmcache_passes_host_uid_gid():
    """build-lmcache.sh must forward -e KVP_HOST_UID/GID to the container."""
    src = BUILD_LMC.read_text()
    assert "KVP_HOST_UID" in src and "KVP_HOST_GID" in src, (
        "build-lmcache.sh must pass the host uid/gid for the chown step"
    )