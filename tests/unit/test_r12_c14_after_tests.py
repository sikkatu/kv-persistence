# SPDX-License-Identifier: Apache-2.0
"""r12 P2: the engine freeze diff (C14) must run AFTER the test-deps install
(C9-C13) and also when KVP_BUILD_SKIP_TESTS=1.

Round-12 review defect: the C14 block stood BEFORE C9 (the `pip install
pytest` step), so the pytest install itself was never checked by the diff.
The reference pipeline has C14 after C13 (before C15), and it must run even
when tests are skipped.

Tests drive the REAL whole _incontainer_build.sh with a pip shim whose
`freeze` reports the CURRENT numpy version from a state file:
  (a) tests ON: installing pytest mutates numpy in the freeze state -> C14
      must refuse (`BUILD REFUSED: engine packages changed`). Before the fix
      the diff ran BEFORE the install, so the run passed -- RED.
  (b) tests OFF (SKIP_TESTS=1): installing the wheel mutates numpy -> C14
      must STILL refuse (it must not be inside the tests-only branch).
  (c) no mutation -> the run must pass (positive control).

Mutation: moving C14 back before C9 -> (a) RED (run passes, no refusal).
"""
from __future__ import annotations
import json
import os
import pathlib
import shutil
import subprocess
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
IMPL = ROOT / "implementation"


def _run(tmp_path: pathlib.Path, skip_tests: str, mutate_on: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
                 "echo", "sed", "awk", "tr", "cut", "find", "head", "wc",
                 "mkdir", "rm", "cp", "ls", "date", "basename", "dirname",
                 "sort", "sha256sum", "id", "df"):
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

    freeze_state = tmp_path / "freeze-state.json"
    freeze_state.write_text(json.dumps({"numpy": "2.1.0"}))

    # layout dirs + wheel fixture BEFORE the shims (the pip shim recreates
    # the wheel after C5's rm and must know its path)
    work = tmp_path / "kvp-work"
    wheels = tmp_path / "kvp-wheels"
    src = tmp_path / "src"
    for d in (work, wheels, src):
        d.mkdir()
    (src / ".git").mkdir()
    (src / "requirements").mkdir()
    (src / "requirements" / "build.txt").write_text("ninja\n")
    (src / "tests" / "v1" / "multiprocess").mkdir(parents=True)
    (src / "pytest.ini").write_text("[pytest]\n")
    for f in ("tests/v1/test_lmcache_mp_connector.py",
              "tests/v1/test_vllm_mp_adapter.py",
              "tests/v1/multiprocess/test_lmcache_driven_transfer_skip.py",
              "tests/v1/multiprocess/test_native_state_lookup.py",
              "tests/v1/multiprocess/test_l1_owner_completion.py"):
        p = src / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def test_ok():\n    assert True\n")

    pip_body = "".join([
        "#!/usr/bin/env bash\n",
        f'state="{freeze_state}"\n',
        f'whl="{wheels / "lmcache-0.5.8rc1.post48+kvp.abc.whl"}"\n',
        'case "$*" in\n',
        # Unique markers, checked in order (the tmp_path contains 'pytest' in
        # its name under pytest -- generic masks matched EVERY install):
        # C7 wheel install uses --force-reinstall; C9 installs the spec
        # 'pytest>=7.0,<9.1' (mask: pytest AND 9.1 -- unique to that call).
        f"  *force-reinstall*) [ \"{mutate_on}\" = \"wheel-install\" ] && /usr/bin/python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); d['numpy']='2.2.0'; json.dump(d, open(sys.argv[1],'w'))\" \"$state\" ;;\n",
        f"  *pytest*9.1*) [ \"{mutate_on}\" = \"pytest\" ] && /usr/bin/python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); d['numpy']='2.2.0'; json.dump(d, open(sys.argv[1],'w'))\" \"$state\" ;;\n",
        "  *freeze*) /usr/bin/python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); print('torch==2.14.0+cu130'); print('numpy=='+d['numpy']); print('vllm==0.9.1'); print('triton==3.4.0')\" \"$state\" ;;\n",
        # C5 runs `rm -f *.whl` first -- the shim recreates the wheel like the
        # real `pip wheel` would (r11 fixtures do the same). The wheel case is
        # LAST: the kvp-wheels path also contains 'wheel' (install must not
        # land here).
        "  *wheel*) /usr/bin/python3 -c \"import sys, zipfile; z = zipfile.ZipFile(sys.argv[1], 'w'); z.writestr('lmcache/__init__.py', b'# stub'); z.writestr('lmcache/cuda_ops_stub.so', b'\\x00'); z.writestr('lmcache/lmcache_native_stub.so', b'\\x00'); z.close()\" \"$whl\" ;;\n",
        "esac\n",
        "exit 0\n",
    ])
    (bin_dir / "pip").write_text(pip_body)
    (bin_dir / "pip").chmod(0o755)

    (bin_dir / "python3").write_text("".join([
        "#!/usr/bin/env bash\n",
        # r12: strip -P first so `python3 -P -m pytest` and `python3 -P -` are
        # routed like their non-P forms (the old order hung on a stdin read).
        'if [ "${1:-}" = "-P" ]; then shift; fi\n',
        'if [ "$1" = "--version" ]; then echo \'Python 3.12.3\'; exit 0; fi\n',
        # C5 runs `python3 -m pip wheel` -- recreate the wheel after C5's rm
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n',
        '  case "$*" in *wheel*) /usr/bin/python3 -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1], \'w\'); z.writestr(\'lmcache/__init__.py\', b\'# stub\'); z.writestr(\'lmcache/cuda_ops_stub.so\', b\'\\x00\'); z.writestr(\'lmcache/lmcache_native_stub.so\', b\'\\x00\'); z.close()" ',
        f'"{wheels / "lmcache-0.5.8rc1.post48+kvp.abc.whl"}" ;; esac\n',
        '  exit 0\n',
        'fi\n',
        f'if [ "$1" = "-m" ] && [ "$2" = "kvpersist.check_wheel_native" ]; then shift 2; PYTHONPATH={IMPL} exec /usr/bin/python3 -m kvpersist.check_wheel_native "$@"; fi\n',
        # r16 P3: the shared gate module runs for real EXCEPT check-imports (the
        # fixtures have no installed lmcache -- import-check was stubbed the
        # same way before, when it was a heredoc)
        f'if [ "$1" = "-m" ] && [ "$2" = "kvpersist.gate_common" ]; then\n',
        '  shift 2\n',
        '  if [ "${1:-}" = "check-imports" ]; then\n',
        '    echo "[in-container] import-check OK (all native modules under the installed distribution location)"; exit 0\n',
        '  fi\n',
        f'  PYTHONPATH={IMPL} exec /usr/bin/python3 -m kvpersist.gate_common "$@"\n',
        'fi\n',
        'if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then\n',
        # r16 P1: the C12 gate counts from the --junitxml report -- the stub
        # must produce one (5 passed) like the real pytest does
        '  _xml=""\n',
        '  for _a in "$@"; do case "$_a" in --junitxml=*) _xml="${_a#--junitxml=}" ;; esac; done\n',
        '  if [ -n "$_xml" ]; then printf \'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="stub" tests="5" failures="0" errors="0" skipped="0"></testsuite></testsuites>\\n\' > "$_xml"; fi\n',
        '  echo "5 passed in 0.05s"; exit 0\n',
        'fi\n',
        'if [ "$1" = "-m" ]; then exit 0; fi\n',
        'if [ "$1" = "-c" ]; then\n  case "$2" in\n',
        '    *torch.version.cuda*) echo \'13.0\' ;;\n',
        '    *is_available*) echo \'False\' ;;\n',
        '    *device_count*) echo \'0\' ;;\n',
        '    *__version__*) echo \'2.4.0\' ;;\n',
        '    *get_device_capability*) echo \'(12, 0)\' ;;\n',
        f'    *importlib*|*md.version*|*re.search*|*zipfile*) PYTHONPATH={ts.parent} exec /usr/bin/python3 "$@" ;;\n',
        '    *) echo \'\' ;;\n  esac\n  exit 0\nfi\n',
        # heredoc body (`python3 -`): the import-check heredoc (C8) is faked
        # as success; C6a/C14/C15 heredocs run for real with the torch stub.
        'if [ "$1" = "-" ]; then\n',
        '  data="$(cat)"\n',
        '  if printf \'%s\' "$data" | grep -q \'import lmcache\'; then\n',
        '    echo "[in-container] import-check OK (all native modules from site-packages)"; exit 0\n',
        '  fi\n',
        '  shift\n',
        f'  printf \'%s\' "$data" | PYTHONPATH={ts.parent} /usr/bin/python3 - "$@"\n',
        '  exit $?\n',
        'fi\n',
        f'PYTHONPATH={ts.parent} exec /usr/bin/python3 "$@"\n',
    ]))
    (bin_dir / "python3").chmod(0o755)

    with zipfile.ZipFile(wheels / "lmcache-0.5.8rc1.post48+kvp.abc.whl", "w") as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
        z.writestr("lmcache/cuda_ops_stub.so", b"\x00")
        z.writestr("lmcache/lmcache_native_stub.so", b"\x00")

    for tool, body in [
        ("apt-get", "#!/usr/bin/env bash\nexit 0\n"),
        ("git", "#!/usr/bin/env bash\ncase \"$*\" in *'ls-files'*) exit 0 ;; esac\nexit 0\n"),
        ("nvcc", "#!/usr/bin/env bash\necho 'Cuda 13.0'\n"),
        ("g++", "#!/usr/bin/env bash\necho g++\n"),
        ("ninja", "#!/usr/bin/env bash\necho ninja\n"),
        ("cuobjdump", "#!/usr/bin/env bash\necho 'Fatbin elf code:\n  arch = sm_120'\n"),
        ("chown", "#!/usr/bin/env bash\nexit 0\n"),
    ]:
        p = bin_dir / tool
        p.write_text(body)
        p.chmod(0o755)

    env = dict(os.environ, PATH=str(bin_dir),
               KVP_BUILD_SKIP_TESTS=skip_tests, KVP_BUILD_GPU_TESTS="0",
               KVP_HOST_UID="1000", KVP_HOST_GID="1000",
               KVP_SCRIPTS_DIR=str(ROOT / "scripts"),
               KVP_IMPL_DIR=str(IMPL),
               KVP_LMC_SRC=str(src),
               KVP_WORK_DIR=str(work),
               KVP_WHEELS_DIR=str(wheels))
    return subprocess.run(["bash", str(INCONTAINER)], capture_output=True,
                          text=True, timeout=120, env=env, cwd=str(tmp_path))


def test_c14_runs_after_pytest_install_and_detects_mutation(tmp_path):
    """(a) tests ON: the pip shim mutates numpy at the pytest install ->
    C14 must refuse. RED before the fix (the diff predated the install)."""
    r = _run(tmp_path, skip_tests="0", mutate_on="pytest")
    assert r.returncode != 0, (
        f"pytest install changed numpy -- C14 must refuse (rc={r.returncode})\n"
        f"stdout:\n{r.stdout[-1500:]}\nstderr:\n{r.stderr[-1500:]}")
    assert "BUILD REFUSED" in r.stderr, f"no refusal message:\n{r.stderr[-800:]}"
    assert "engine packages changed" in r.stderr, r.stderr[-800:]


def test_c14_runs_even_when_tests_are_skipped(tmp_path):
    """(b) tests OFF: the pip shim mutates numpy at the wheel install ->
    C14 must STILL refuse (must not live inside the tests-only branch)."""
    r = _run(tmp_path, skip_tests="1", mutate_on="wheel-install")
    assert r.returncode != 0, (
        f"wheel install changed numpy -- C14 must run at SKIP_TESTS=1 "
        f"(rc={r.returncode})\nstdout:\n{r.stdout[-1500:]}\nstderr:\n{r.stderr[-1500:]}")
    assert "BUILD REFUSED" in r.stderr, f"no refusal message:\n{r.stderr[-800:]}"
    assert "engine packages changed" in r.stderr, r.stderr[-800:]


def test_c14_positive_control_no_mutation(tmp_path):
    """(c) positive control: no mutation -> the whole run must pass and the
    engine identity line must appear."""
    r = _run(tmp_path, skip_tests="0", mutate_on="")
    assert r.returncode == 0, (
        f"clean run must pass (rc={r.returncode})\n"
        f"stdout:\n{r.stdout[-1500:]}\nstderr:\n{r.stderr[-1500:]}")
    assert "engine identity OK" in r.stdout, r.stdout[-800:]