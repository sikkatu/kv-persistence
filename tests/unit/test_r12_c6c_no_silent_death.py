# SPDX-License-Identifier: Apache-2.0
"""r12 P1: C6c must never die SILENTLY under `set -Eeuo pipefail`.

Round-12 review defects in the C6c block:
  1. `_so="$(unzip -l "$WHEEL_FILE" | awk ...)"` -- without `unzip` in the
     image the pipeline dies rc=127 (pipefail) and the script exits with NO
     message at all (only the chown-back line and the host's docker rc).
  2. `CUDA_OPS_ARCHS="$(cuobjdump ... | grep -o ...)"` -- when cuobjdump finds
     nothing (or dies with stderr hidden), `grep -o` returns rc=1 and the
     script dies silently -- instead of the loud
     `BUILD REFUSED: ... does NOT contain sm_120` the gate exists for.

Tests run the REAL C6c block (extracted from _incontainer_build.sh by its
`# ---- C6c` / `# ---- C7` markers, never a copy) under real bash with
`set -Eeuo pipefail` and stub programs on PATH:
  (a) PATH without `unzip` -> the block must still work (rc=0, sm_120 found);
  (b) cuobjdump stub printing NOTHING -> loud `BUILD REFUSED` on stderr
      (`cuobjdump produced no architectures` + the `cuobjdump --list-elf`
      output), not silence;
  (c) cuobjdump stub printing `sm_89` -> `BUILD REFUSED ... does NOT contain
      sm_120`.

Plus the acceptance gate: EVERY refusal scenario of the REAL script (the ones
exercised by the r11 real-program tests) must exit with a message containing
`BUILD REFUSED` or `REGRESSION FAILURE` on stderr -- a non-zero exit with no
message fails the test.

Mutation: bringing `unzip` back -> scenario (a) is RED again (rc=127).
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
IMPL = ROOT / "implementation"


def extract_block(text: str, start_snip: str, end_snip: str) -> str:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if start_snip in ln)
    end = next(i for i, ln in enumerate(lines) if i > start and end_snip in ln)
    return "\n".join(lines[start + 1:end])


def _c6c(tmp_path: pathlib.Path) -> str:
    """The REAL C6c block + the cuda_ops .so discovery via python3 zipfile
    (r12: unzip is not guaranteed in the image) and the export line."""
    return extract_block(INCONTAINER.read_text(), "# ---- C6c", "# ---- C7")


def _fake_wheel(tmp_path: pathlib.Path) -> pathlib.Path:
    import zipfile
    wheel = tmp_path / "lmcache-0.5.7.dev428+kvp.abc.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
        z.writestr("lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so", b"\x7fELF")
        z.writestr("lmcache/lmcache_native.cpython-312-x86_64-linux-gnu.so", b"\x7fELF")
    return wheel


def _bin_with(tmp_path: pathlib.Path, cuobjdump_body: str,
              include_unzip: bool = False) -> pathlib.Path:
    """Stub PATH dir: real coreutils + stub cuobjdump (+ optional real unzip).
    python3 is the REAL one -- the fixed C6c discovers the .so via python3
    zipfile, so no unzip is needed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tools = ["bash", "cat", "grep", "tail", "mktemp", "chmod", "printf", "echo",
             "sed", "awk", "tr", "cut", "find", "head", "wc", "mkdir", "rm",
             "cp", "ls", "date", "basename", "dirname", "sort", "python3"]
    if include_unzip:
        tools.append("unzip")
    for tool in tools:
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    cu = bin_dir / "cuobjdump"
    cu.write_text("#!/usr/bin/env bash\n" + cuobjdump_body)
    cu.chmod(0o755)
    return bin_dir


def _run_c6c(tmp_path: pathlib.Path, bin_dir: pathlib.Path,
             wheel: pathlib.Path) -> subprocess.CompletedProcess:
    c6c = _c6c(tmp_path)
    driver = tmp_path / "c6c-driver.sh"
    driver.write_text("".join([
        "set -Eeuo pipefail\n",
        f'WHEEL_FILE="{wheel}"\n',
        c6c + "\n",
        'echo "RESULT:$CUDA_OPS_ARCHS"\n',
    ]))
    env = {"PATH": str(bin_dir), "PWD": str(tmp_path)}
    return subprocess.run(["bash", str(driver)], capture_output=True,
                          text=True, timeout=60, env=env, cwd=str(tmp_path))


def test_c6c_works_without_unzip(tmp_path):
    """(a) the real C6c must find the .so WITHOUT unzip (python3 zipfile)
    and pass the sm_120 gate. Before the fix this died rc=127 (command not
    found under pipefail) with NO message."""
    wheel = _fake_wheel(tmp_path)
    bin_dir = _bin_with(tmp_path, "echo 'Fatbin elf code:\n  arch = sm_120'\n",
                        include_unzip=False)
    assert not (bin_dir / "unzip").exists(), "test bug: unzip must NOT be on PATH"
    r = _run_c6c(tmp_path, bin_dir, wheel)
    assert r.returncode == 0, (
        f"C6c must pass WITHOUT unzip on PATH (rc={r.returncode})\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}")
    assert "RESULT:sm_120" in r.stdout, r.stdout


def test_c6c_empty_cuobjdump_refuses_loudly(tmp_path):
    """(b) cuobjdump producing NOTHING must give `BUILD REFUSED:
    cuobjdump produced no architectures` on stderr -- and the raw
    `cuobjdump --list-elf` output must be shown. Before the fix: silent
    rc=1 exit."""
    wheel = _fake_wheel(tmp_path)
    bin_dir = _bin_with(tmp_path, "exit 0\n")
    r = _run_c6c(tmp_path, bin_dir, wheel)
    assert r.returncode != 0, f"empty cuobjdump must refuse:\n{r.stdout}"
    assert "BUILD REFUSED" in r.stderr, (
        f"the refusal MUST carry a message, got silence:\nstderr={r.stderr!r}")
    assert "cuobjdump produced no architectures" in r.stderr, r.stderr
    assert "--list-elf" in r.stderr, (
        f"the refusal must show the raw cuobjdump output:\n{r.stderr}")


def test_c6c_sm89_only_refuses_loudly(tmp_path):
    """(c) an arch list without sm_120 must refuse with the documented
    `does NOT contain sm_120` message."""
    wheel = _fake_wheel(tmp_path)
    bin_dir = _bin_with(tmp_path, "echo 'Fatbin elf code:\n  arch = sm_89'\n")
    r = _run_c6c(tmp_path, bin_dir, wheel)
    assert r.returncode != 0, f"sm_89-only must refuse:\n{r.stdout}"
    assert "BUILD REFUSED" in r.stderr, f"no message:\n{r.stderr!r}"
    assert "does NOT contain sm_120" in r.stderr, r.stderr


def test_every_refusal_scenario_prints_a_message(tmp_path):
    """ACCEPTANCE: no gate of the REAL script may exit non-zero WITHOUT a
    `BUILD REFUSED` / `REGRESSION FAILURE` message on stderr. Scenarios are
    the real refusal paths exercised by the r11 real-program tests, driven
    through the whole _incontainer_build.sh with tiny shims."""
    # --- shared stub layout (same shape as the r11 real-run fixtures) ---
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                 "echo", "sed", "awk", "tr", "cut", "find", "head", "wc",
                 "mkdir", "rm", "cp", "ls", "date", "basename", "dirname",
                 "sort", "python3", "sha256sum", "id", "df"):
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

    def python3_stub(body_extra: str) -> str:
        return "".join([
            "#!/usr/bin/env bash\n",
            body_extra,
            'if [ "$1" = "--version" ]; then echo \'Python 3.12.3\'; exit 0; fi\n',
            'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit 0; fi\n',
            f'if [ "$1" = "-m" ] && [ "$2" = "kvpersist.check_wheel_native" ]; then shift 2; PYTHONPATH={IMPL} exec /usr/bin/python3 -m kvpersist.check_wheel_native "$@"; fi\n',
            'if [ "$1" = "-m" ]; then exit 0; fi\n',
            'if [ "$1" = "-c" ]; then\n  case "$2" in\n',
            '    *torch.version.cuda*) echo \'13.0\' ;;\n',
            '    *is_available*) echo \'False\' ;;\n',
            '    *device_count*) echo \'0\' ;;\n',
            '    *__version__*) echo \'2.4.0\' ;;\n',
            '    *get_device_capability*) echo \'(12, 0)\' ;;\n',
            f'    *importlib*|*md.version*|*re.search*) PYTHONPATH={ts.parent} exec /usr/bin/python3 "$@" ;;\n',
            '    *) echo \'\' ;;\n  esac\n  exit 0\nfi\n',
            f'if [ "$1" = "-" ] || [ "$1" = "-P" ]; then PYTHONPATH={ts.parent} exec /usr/bin/python3 "$@"; fi\n',
            f'PYTHONPATH={ts.parent} exec /usr/bin/python3 "$@"\n',
        ])

    # helper: run the whole script and return (rc, stderr)
    # NOTE (r12): NEVER symlink python3 here and then write_text over it --
    # write_text follows the symlink and would overwrite the SYSTEM python3.12
    # with this stub (destroying the interpreter + infinite exec recursion).
    # The stub is a plain file shadowing python3 on PATH.
    def run_scenario(name: str, tool_overrides: dict,
                     env_extra: dict | None = None,
                     wheel_files: tuple = ("lmcache/cuda_ops_stub.so",
                                           "lmcache/lmcache_native_stub.so")):
        import zipfile
        sbin = tmp_path / name
        sbin.mkdir()
        for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                     "echo", "sed", "awk", "tr", "cut", "find", "head", "wc",
                     "mkdir", "rm", "cp", "ls", "date", "basename", "dirname",
                     "sort", "sha256sum", "id", "df"):
            real = shutil.which(tool)
            if real:
                (sbin / tool).symlink_to(real)
        py3 = sbin / "python3"
        py3.write_text(python3_stub(""))
        py3.chmod(0o755)
        defaults = {
            "apt-get": "#!/usr/bin/env bash\nexit 0\n",
            "git": "#!/usr/bin/env bash\ncase \"$*\" in *'ls-files'*) exit 0 ;; esac\nexit 0\n",
            "nvcc": "#!/usr/bin/env bash\necho 'Cuda 13.0'\n",
            "g++": "#!/usr/bin/env bash\necho g++\n",
            "ninja": "#!/usr/bin/env bash\necho ninja\n",
            "cuobjdump": "#!/usr/bin/env bash\necho 'Fatbin elf code:\n  arch = sm_120'\n",
            "chown": "#!/usr/bin/env bash\nexit 0\n",
            "pip": "#!/usr/bin/env bash\nexit 0\n",
        }
        defaults.update(tool_overrides)
        for tool, body in defaults.items():
            p = sbin / tool
            p.write_text(body)
            p.chmod(0o755)
        work = tmp_path / f"{name}-work"
        wheels = tmp_path / f"{name}-wheels"
        src = tmp_path / f"{name}-src"
        for d in (work, wheels, src):
            d.mkdir()
        (src / ".git").mkdir()
        (src / "requirements").mkdir()
        (src / "requirements" / "build.txt").write_text("ninja\n")
        with zipfile.ZipFile(wheels / "lmcache-0.5.7.dev428+kvp.abc.whl", "w") as z:
            z.writestr("lmcache/__init__.py", b"# stub\n")
            for f in wheel_files:
                z.writestr(f, b"\x00")
        env = dict(os.environ, PATH=str(sbin),
                   KVP_BUILD_SKIP_TESTS="1", KVP_BUILD_GPU_TESTS="0",
                   KVP_HOST_UID="1000", KVP_HOST_GID="1000",
                   KVP_SCRIPTS_DIR=str(ROOT / "scripts"),
                   KVP_IMPL_DIR=str(IMPL),
                   KVP_LMC_SRC=str(src),
                   KVP_WORK_DIR=str(work),
                   KVP_WHEELS_DIR=str(wheels))
        if env_extra:
            env.update(env_extra)
        r = subprocess.run(["bash", str(INCONTAINER)], capture_output=True,
                           text=True, timeout=120, env=env, cwd=str(tmp_path))
        return r

    scenarios = {
        # C1: apt-get update fails -> "BUILD REFUSED: 'apt-get update' failed"
        "apt_update_fails": (
            {"apt-get": "#!/usr/bin/env bash\ncase \"$*\" in update*) exit 1 ;; esac\nexit 0\n"}, {}),
        # C1: package install fails -> "BUILD REFUSED: package ... unavailable"
        "pkg_install_fails": (
            {"apt-get": "#!/usr/bin/env bash\ncase \"$*\" in install*) exit 1 ;; esac\nexit 0\n"}, {}),
        # C3: no nvcc -> "BUILD REFUSED: nvcc not found"
        "nvcc_missing": (
            {"nvcc": "#!/usr/bin/env bash\nexit 127\n"}, {}),
        # C5: stub pip wheel leaves NO wheel -> "BUILD REFUSED: expected exactly one wheel"
        "no_wheel": (
            {"pip": "#!/usr/bin/env bash\nexit 0\n"}, {}),
        # C6b: wheel without lmcache_native -> "BUILD REFUSED: the wheel lacks required native extensions"
        "wheel_missing_native": (
            {}, {"KVP_BUILD_SKIP_TESTS": "0"}),
        # C6c: cuobjdump empty -> "BUILD REFUSED: cuobjdump produced no architectures"
        "cuobjdump_empty": (
            {"cuobjdump": "#!/usr/bin/env bash\nexit 0\n"}, {}),
        # C6c: wrong arch -> "BUILD REFUSED ... does NOT contain sm_120"
        "wrong_arch": (
            {"cuobjdump": "#!/usr/bin/env bash\necho 'Fatbin elf code:\n  arch = sm_89'\n"}, {}),
    }
    for name, (overrides, env_extra) in scenarios.items():
        # wheel_missing_native: drop lmcache_native from the wheel files
        wf = (("lmcache/cuda_ops_stub.so",) if name == "wheel_missing_native"
              else ("lmcache/cuda_ops_stub.so", "lmcache/lmcache_native_stub.so"))
        r = run_scenario(name, overrides, env_extra, wheel_files=wf)
        assert r.returncode != 0, f"{name}: expected a refusal, got rc=0"
        msg = r.stderr
        assert ("BUILD REFUSED" in msg) or ("REGRESSION FAILURE" in msg), (
            f"{name}: non-zero exit WITHOUT a refusal message -- silent death.\n"
            f"stdout:\n{r.stdout}\nstderr:\n{r.stderr!r}")