# SPDX-License-Identifier: Apache-2.0
"""r10 P0-1: the phase must BUILD the wheel BEFORE any lmcache import;
imports must come from the INSTALLED wheel, not $LMC_SRC; a wheel without
lmcache_native*.so must die at the wheel check, before pytest.

Reproduced on the operator machine: import-check ran before pip wheel and
imported from PYTHONPATH=$LMC_SRC -- but native modules only exist AFTER the
build, so the phase always died ModuleNotFoundError('lmcache.lmcache_native').
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"


def _harness(tmp_path: pathlib.Path, wheel_has_native: bool = True,
             extra_env: dict | None = None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                 "sed", "awk", "tr", "cut", "find", "head", "wc", "mkdir",
                 "rm", "cp", "ls", "date", "basename", "dirname", "df"):
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    call_log = tmp_path / "calls.log"
    ts = tmp_path / "torchstub" / "torch"
    ts.mkdir(parents=True, exist_ok=True)
    (ts / "__init__.py").write_text(
        "class _C:\n"
        "    @staticmethod\n    def is_available(): return False\n"
        "    @staticmethod\n    def device_count(): return 0\n"
        "    @staticmethod\n    def get_device_capability(i): return (12, 0)\n"
        "cuda = _C()\n__version__ = '2.4.0'\nclass version:\n    cuda = '13.0'\n")
    # python3 stub: logs argv + PYTHONPATH, answers probes, runs identity heredocs
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "python3|$*|PP=${{PYTHONPATH:-}}|PWD=$PWD" >> "{call_log}"\n'
        "if [ \"$1\" = \"--version\" ]; then echo 'Python 3.12.3'; exit 0; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pytest\" ]; then\n"
        f"  cat {tmp_path}/pytest-out.txt; exit 0; fi\n"
        "if [ \"${1:-}\" = \"-m\" ] && [ \"${2:-}\" = \"pip\" ]; then\n"
        f"  echo \"python3|$*\" >> \"{call_log}\"\n"
        "  case \"$*\" in *wheel*)\n"
        f"    /usr/bin/python3 -c \"import zipfile; z=zipfile.ZipFile('{tmp_path}/kvp-wheels/lmcache-0.5.7.dev428+kvp.abc.whl','w'); z.writestr('lmcache/__init__.py', b'# stub'); z.writestr('lmcache/cuda_ops_stub.so', b''); z.writestr('lmcache/lmcache_native_stub.so', b'')\" ;;\n"
        "  esac\n"
        "  exit 0\nfi\n"
        # r10: the wheel-native check runs as `python3 -m kvpersist.check_wheel_native`
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"kvpersist.check_wheel_native\" ]; then\n"
        "  shift 2; exec /usr/bin/python3 -m kvpersist.check_wheel_native \"$@\"; fi\n"
        "if [ \"$1\" = \"-m\" ]; then exit 0; fi\n"
        "if [ \"$1\" = \"-c\" ]; then\n  case \"$2\" in\n"
        "    *torch.version.cuda*) echo '13.0' ;;\n"
        "    *is_available*) echo 'False' ;;\n"
        "    *device_count*) echo '0' ;;\n"
        "    *__version__*) echo '2.4.0' ;;\n"
        "    *get_device_capability*) echo '(12, 0)' ;;\n"
        "    *md.version*) echo 'x' ;;\n"
        "    *re.search*|*zipfile*|*importlib*) exec /usr/bin/python3 \"$@\" ;;\n"
        "    *) echo '' ;;\n  esac\n  exit 0\nfi\n"
        # identity heredoc (`python3 -`) and import-check (`python3 -P -`):
        # run for real with torch stub
        "if [ \"$1\" = \"-\" ] || [ \"$1\" = \"-P\" ]; then\n"
        f"  PYTHONPATH={ts} /usr/bin/python3 - > {tmp_path}/identity.env 2>/dev/null; exit 0\n"
        "fi\n"
        "exec /usr/bin/python3 \"$@\"\n")
    (bin_dir / "python3").chmod(0o755)
    (tmp_path / "pytest-out.txt").write_text("5 passed in 0.05s\n")
    # pip stub: `pip wheel` creates a fake wheel; `pip install` logs
    wheel_name = "lmcache-0.5.7.dev428+kvp.abc.whl"
    import zipfile
    whl_path = tmp_path / "kvp-wheels" / wheel_name
    (tmp_path / "kvp-wheels").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(whl_path, "w") as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
        z.writestr("lmcache/cuda_ops_stub.so", b"\x00")
        if wheel_has_native:
            z.writestr("lmcache/lmcache_native_stub.so", b"\x00")
    (bin_dir / "pip").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "pip|$*|PP=${{PYTHONPATH:-}}" >> "{call_log}"\n'
        "exit 0\n")
    (bin_dir / "pip").chmod(0o755)
    for tool, body in [
        ("git", "#!/usr/bin/env bash\ncase \"$*\" in *ls-files*) exit 0 ;; esac\nexit 0\n"),
        ("apt-get", "#!/usr/bin/env bash\nexit 0\n"),
        ("nvcc", "#!/usr/bin/env bash\necho 'Cuda 13.0'\n"),
        ("g++", "#!/usr/bin/env bash\necho g++\n"),
        ("ninja", "#!/usr/bin/env bash\necho ninja\n"),
        ("cuobjdump", "#!/usr/bin/env bash\necho 'Fatbin elf code:\n  arch = sm_120'\n"),
        ("chown", "#!/usr/bin/env bash\nexit 0\n"),
    ]:
        p = bin_dir / tool
        p.write_text(body)
        p.chmod(0o755)
    for sub in ("kvp-work", "kvp-wheels", "src"):
        (tmp_path / sub).mkdir(exist_ok=True)
    env = dict(os.environ, PATH=str(bin_dir),
               KVP_BUILD_SKIP_TESTS="0", KVP_BUILD_GPU_TESTS="0",
               KVP_HOST_UID="1000", KVP_HOST_GID="1000",
               KVP_SCRIPTS_DIR=str(ROOT / "scripts"),
               KVP_IMPL_DIR=str(ROOT / "implementation"),
               KVP_LMC_SRC=str(tmp_path / "src"),
               KVP_WORK_DIR=str(tmp_path / "kvp-work"),
               KVP_WHEELS_DIR=str(tmp_path / "kvp-wheels"))
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(["bash", str(INCONTAINER)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    return r, call_log.read_text() if call_log.is_file() else ""


def test_pip_wheel_before_any_lmcache_import(tmp_path):
    """`pip wheel` must run BEFORE any import-check / pytest (red before:
    import-check preceded the build and died on missing native modules)."""
    r, calls = _harness(tmp_path)
    lines = calls.splitlines()
    wheel_idx = next((i for i, ln in enumerate(lines) if "-m pip wheel" in ln), None)
    install_idx = next((i for i, ln in enumerate(lines)
                        if "pip" in ln and "--force-reinstall" in ln), None)
    import_idx = next((i for i, ln in enumerate(lines)
                       if "import lmcache" in ln), None)
    check_idx = next((i for i, ln in enumerate(lines) if "check_wheel_native" in ln), None)
    pytest_idx = next((i for i, ln in enumerate(lines) if "-m pytest" in ln), None)
    assert wheel_idx is not None, f"pip wheel never ran:\n{calls}"
    # NO lmcache import may run before the build (mutation check: an
    # import-check hoisted above pip wheel turns this red)
    if import_idx is not None:
        assert wheel_idx < import_idx, (
            f"import lmcache must NOT run before pip wheel:\n{calls}"
        )
    if check_idx is not None:
        assert wheel_idx < check_idx, f"pip wheel must precede the wheel check:\n{calls}"
    if install_idx is not None:
        assert wheel_idx < install_idx, f"pip wheel must precede pip install:\n{calls}"
    if pytest_idx is not None:
        assert wheel_idx < pytest_idx, f"pip wheel must precede pytest:\n{calls}"


def test_no_pythonpath_to_source_tree(tmp_path):
    """The phase must NOT export PYTHONPATH=$LMC_SRC -- imports must come from
    the installed wheel (red before: PYTHONPATH pointed at the source tree)."""
    r, calls = _harness(tmp_path)
    for ln in calls.splitlines():
        if "PP=" in ln:
            pp = ln.split("PP=")[1].split("|")[0]
            assert str(tmp_path / "src") not in pp, (
                f"PYTHONPATH must not point at the source tree:\n{ln}"
            )
    src = INCONTAINER.read_text()
    assert "export PYTHONPATH=$LMC_SRC" not in src, (
        "the source tree must not be on PYTHONPATH"
    )


# ---------------------------------------------------------------------------
# P0-2: a wheel without lmcache_native*.so must fail the wheel check (before
# pytest), not silently pass.
# ---------------------------------------------------------------------------
def test_wheel_without_lmcache_native_refused(tmp_path):
    """Fake wheel carrying only cuda_ops*.so (no lmcache_native) -- the phase
    must die at the wheel content check, before pytest (red before: only
    cuda_ops was required; mutation check: restoring cuda_ops-only turns this
    red because the build would proceed past the check)."""
    r, calls = _harness(tmp_path, wheel_has_native=False)
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"a wheel without lmcache_native must be refused:\n{out[-500:]}"
    )
    # the refusal must come from the WHEEL check naming lmcache_native, and
    # pytest must NOT have been reached
    assert "lmcache_native" in out, (
        f"the refusal must name the missing extension:\n{out[-500:]}"
    )
    assert "-m pytest" not in calls, (
        f"the build must die at the wheel check, before pytest:\n{calls}"
    )


# ---------------------------------------------------------------------------
# P1: the project's TORCH_CUDA_ARCH_LIST wins over the image's; the used value
# reaches identity.env.
# ---------------------------------------------------------------------------
def test_project_arch_override_wins_over_image(tmp_path):
    """The image predefines TORCH_CUDA_ARCH_LIST="7.5 8.0 ... 12.0" -- the
    project variable (KVP_TORCH_CUDA_ARCH_LIST) must win (red before: the
    image value won and 7 archs were compiled)."""
    r, calls = _harness(tmp_path, extra_env={
        "TORCH_CUDA_ARCH_LIST": "7.5 8.0 8.6 8.9 9.0 10.0 12.0",
        "KVP_TORCH_CUDA_ARCH_LIST": "12.0",
    })
    out = r.stdout + r.stderr
    assert "overridden" in out.lower(), (
        f"the override must be visible in the log:\n{out[-500:]}"
    )
    # the effective value used by the build must be the project's 12.0
    assert "TORCH_CUDA_ARCH_LIST=12.0" in out, (
        f"the build must use 12.0, not the image list:\n{out[-500:]}"
    )