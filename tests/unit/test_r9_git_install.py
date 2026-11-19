# SPDX-License-Identifier: Apache-2.0
"""r9 P0: the vLLM image has NO git -- the in-container phase must install it
via apt BEFORE `git config`, and the wheel must keep git-tracked non-py files.

Reproduced on the operator machine: _incontainer_build.sh died rc=127 on
line 'git config ...' (git: command not found) before anything else ran.
"""
from __future__ import annotations
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"


def _harness(tmp_path: pathlib.Path, git_installs: bool = True):
    """Image-like PATH: everything except git; apt-get 'installs' git (creates
    the stub) and logs argv -- exactly how the real image behaves."""
    import shutil
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    # base utilities the phase needs (like the real image provides)
    for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                 "echo", "sed", "awk", "tr", "cut", "find", "head", "wc",
                 "mkdir", "rm", "cp", "ls", "date", "basename", "dirname",
                 "unzip", "sort", "df"):
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    apt_log = tmp_path / "apt.log"
    git_log = tmp_path / "git.log"
    py_out = tmp_path / "pytest-out.txt"
    py_out.write_text("6 passed in 0.05s\n")
    ts = tmp_path / "torchstub" / "torch"
    ts.mkdir(parents=True, exist_ok=True)
    (ts / "__init__.py").write_text(
        "class _C:\n"
        "    @staticmethod\n"
        "    def is_available(): return False\n"
        "    @staticmethod\n"
        "    def device_count(): return 0\n"
        "    @staticmethod\n"
        "    def get_device_capability(i): return (12, 0)\n"
        "cuda = _C()\n__version__ = '2.4.0'\n"
        "class version:\n    cuda = '13.0'\n")
    (bin_dir / "apt-get").write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$@\" >> \"{apt_log}\"\n"
        "case \"$*\" in\n"
        "  *update*) exit 0 ;;\n"
        "  *'install'*'git'*)\n"
        + (f"    printf '#!/usr/bin/env bash\\necho \"$@\" >> \"{git_log}\"\\ncase \"$*\" in *ls-files*) exit 0 ;; *--version*) echo gitver ;; esac\\nexit 0\\n' > \"{bin_dir}/git\"\n    chmod +x \"{bin_dir}/git\"\n    exit 0 ;;\n"
           if git_installs else
           "    echo 'E: Unable to locate package git' >&2; exit 100 ;;\n")
        + "  *) exit 0 ;;\n"
        "esac\n")
    (bin_dir / "apt-get").chmod(0o755)
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pytest\" ]; then\n"
        f"  cat \"{py_out}\"; exit 0\nfi\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'Python 3.12.3'; exit 0; fi\n"
        "if [ \"$1\" = \"-c\" ]; then\n  case \"$2\" in\n"
        "    *torch.version.cuda*) echo '13.0' ;;\n"
        "    *is_available*) echo 'False' ;;\n"
        "    *device_count*) echo '0' ;;\n"
        "    *__version__*) echo '2.4.0' ;;\n"
        "    *get_device_capability*) echo '(12, 0)' ;;\n"
        "    *md.version*) echo 'x' ;;\n"
        "    *re.search*|*zipfile*) exec /usr/bin/python3 \"$@\" ;;\n"
        "    *) echo '' ;;\n  esac\n  exit 0\nfi\n"
        "if [ \"$1\" = \"-P\" ]; then case \"$*\" in *pytest*) cat \"" + str(py_out) + "\"; exit 0 ;; esac; cat > /dev/null; exit 0; fi\n"
        "if [ \"$1\" = \"-\" ]; then cat > /dev/null; exit 0; fi\n"
        "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pip\" ]; then shift 2; exec pip \"$@\"; fi\n"
        "if [ \"$1\" = \"-m\" ]; then exit 0; fi\n"
        "exec /usr/bin/python3 \"$@\"\n")
    (bin_dir / "python3").chmod(0o755)
    for tool in ("git", "nvcc", "pip", "chown"):
        pass  # git deliberately absent until apt installs it
    (bin_dir / "nvcc").write_text("#!/usr/bin/env bash\necho 'Cuda 13.0'\n")
    (bin_dir / "nvcc").chmod(0o755)
    for _t in ("g++", "ninja"):
        (bin_dir / _t).write_text("#!/usr/bin/env bash\necho " + _t + "\n")
        (bin_dir / _t).chmod(0o755)
    (bin_dir / "cuobjdump").write_text("#!/usr/bin/env bash\necho 'Fatbin elf code:\\n  arch = sm_120'\n")
    (bin_dir / "cuobjdump").chmod(0o755)
    (bin_dir / "pip").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "pip|$*|PP=${{PYTHONPATH:-}}" >> "{apt_log}"\n'
        "  case \"$*\" in *wheel*)\n"
        f"    /usr/bin/python3 -c \"import zipfile; z=zipfile.ZipFile('{tmp_path}/kvp-wheels/lmcache-0.5.8rc1.post48+kvp.abc.whl','w'); z.writestr('lmcache/__init__.py', b'#'); z.writestr('lmcache/cuda_ops_stub.so', b''); z.writestr('lmcache/lmcache_native_stub.so', b'')\" ;;\n"
        "  esac\n"
        "exit 0\n")
    (bin_dir / "pip").chmod(0o755)
    (bin_dir / "chown").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "chown").chmod(0o755)
    return bin_dir, apt_log, git_log


def _run(tmp_path: pathlib.Path, git_installs: bool = True):
    bin_dir, apt_log, git_log = _harness(tmp_path, git_installs)
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
    (tmp_path / "src" / ".git").mkdir(exist_ok=True)
    # r10: the phase now builds+checks a wheel -- provide one with BOTH native
    # extensions so the run reaches the git-config point we assert below.
    import zipfile
    with zipfile.ZipFile(
        tmp_path / "kvp-wheels" / "lmcache-0.5.8rc1.post48+kvp.abc.whl", "w"
    ) as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
        z.writestr("lmcache/cuda_ops_stub.so", b"\x00")
        z.writestr("lmcache/lmcache_native_stub.so", b"\x00")
    env = dict(os.environ, PATH=str(bin_dir),
               KVP_BUILD_SKIP_TESTS="0", KVP_BUILD_GPU_TESTS="0",
               KVP_HOST_UID="1000", KVP_HOST_GID="1000",
               KVP_SCRIPTS_DIR=str(ROOT / "scripts"),
               KVP_IMPL_DIR=str(ROOT / "implementation"),
               KVP_LMC_SRC=str(tmp_path / "src"),
               KVP_WORK_DIR=str(tmp_path / "kvp-work"),
               KVP_WHEELS_DIR=str(tmp_path / "kvp-wheels"))
    r = subprocess.run(["bash", str(INCONTAINER)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    return r, apt_log, git_log


def test_git_is_installed_before_first_use(tmp_path):
    """Image WITHOUT git: the phase must install it via apt and only then call
    `git config` (red before the fix: rc=127 'git: command not found')."""
    r, apt_log, git_log = _run(tmp_path, git_installs=True)
    assert r.returncode == 0, (
        f"the phase must complete in a git-less image:\n{r.stdout[-600:]}\n{r.stderr[-600:]}"
    )
    apt = apt_log.read_text()
    assert "install" in apt and " git" in apt.replace("install", ""), (
        f"apt-get install git must be called:\n{apt}"
    )
    git = git_log.read_text()
    assert "config" in git, "git config must run AFTER the install"
    # ordering: the apt install line precedes the git config call
    # (the pip stub shares the log -- filter its lines out)
    apt_lines = [ln for ln in apt.splitlines() if not ln.startswith("pip|")]
    first_install = next(i for i, ln in enumerate(apt_lines) if "install" in ln and "git" in ln)
    assert first_install >= 0
    assert "update" in apt_lines[0], (
        f"apt-get update must come first among apt calls:\n{apt_lines[:5]}"
    )


def test_missing_git_package_refuses_loudly(tmp_path):
    """apt cannot provide git -> BUILD REFUSED naming 'git', rc != 0."""
    r, apt_log, _ = _run(tmp_path, git_installs=False)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "BUILD REFUSED" in out and "git" in out, (
        f"the refusal must name the git package:\n{out[-600:]}"
    )