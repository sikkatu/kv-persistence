# SPDX-License-Identifier: Apache-2.0
"""r29 N3: kill the mutants that survived the r28 green run.

M18. launch.py: `if serde_alloc_patch.kvp_serde_configured():` -> `if False:`
     the L6 fix could be switched off silently. Test: run the REAL
     python3 -m kvpersist.launch server --l2-adapter ... against a fake
     `lmcache` package (cli/main prints patched=<bool>); three cases:
     kvp-serde -> installed + patched=True; no serde -> not installed;
     mutated method -> WARNING + patched=False, rc 0.
M19/M20. test-recovery.sh: the kvp_scan_l2_failures call removed in
     scenario A / scenario B (and the pre call). Each scenario's real
     fragment is EXECUTED (not grep'ed) with stub functions: the harness
     feeds a log with a store failure and expects
     `FAIL: <tag>: L2 store FAILING` with the right tag (scenario pre / A / B).
M24/M25. diagnostics fields estimate_by_layout / need -> ?
     covered by test_r28_serde_alloc.py::test_l6_diag_exact_values_deterministic_pair.
"""
from __future__ import annotations
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = ROOT / "scripts" / "test-recovery.sh"
ENV_SH = ROOT / "scripts" / "env.sh"
FIXTURE = ROOT / "tests" / "fixtures" / "lmcache_alloc_temp_buffers.py.txt"


# ---------------------------------------------------------------------------
# M18: the real kvpersist.launch must install the patch on kvp-* serde
# ---------------------------------------------------------------------------
FAKE_CLI_MAIN = (
    "import sys\n"
    "def main():\n"
    "    try:\n"
    "        from lmcache.v1.distributed.l2_adapters.serde_wrapper import SerdeL2AdapterWrapper\n"
    "        from kvpersist.serde_alloc_patch import _patched_alloc\n"
    "        patched = SerdeL2AdapterWrapper._alloc_temp_buffers is _patched_alloc\n"
    "    except Exception:\n"
    "        patched = False\n"
    "    print('patched=%s' % patched)\n"
    "    return 0\n"
)


def _run_launch(tmp_path, adapter_json, mutate_method=False):
    """Run the REAL `python3 -m kvpersist.launch server --l2-adapter <json>`
    with a fake lmcache package carrying the FIXTURE method."""
    fixture = FIXTURE.read_text()
    if mutate_method:
        fixture = fixture.replace("shape_0 = objects[0].get_shapes()",
                                  "shape_0 = objects[9].get_shapes()")
    pkgroot = tmp_path / "pkgroot"
    pkg = pkgroot / "lmcache"   # must be importable AS `lmcache`
    for sub in ("cli", "v1", "v1/distributed", "v1/distributed/l2_adapters"):
        (pkg / sub).mkdir(parents=True, exist_ok=True)
        (pkg / sub / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "cli" / "main.py").write_text(FAKE_CLI_MAIN)
    (pkg / "v1" / "distributed" / "l2_adapters" / "serde_wrapper.py").write_text(
        "from __future__ import annotations\n"
        "class _L1Error:\n"
        "    SUCCESS = 0\n"
        "L1Error = _L1Error\n"
        "_L1_WRITE_TAG = 'tag'\n"
        "class MemoryLayoutDesc:\n"
        "    def __init__(self, shapes, dtypes):\n"
        "        self.shapes = shapes\n"
        "        self.dtypes = dtypes\n"
        "def make_temp_key(k):\n"
        "    return k\n"
        "def serialized_layout_desc(layout, serde):\n"
        "    return layout\n"
        "class SerdeL2AdapterWrapper:\n"
        + fixture
    )
    # a matching adapter file so kvp_serde_configured and the codec gate pass
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    env = dict(os.environ,
               PYTHONPATH=str(pkgroot) + os.pathsep + str(ROOT / "implementation"),
               KVP_CACHE_ROOT=str(cache_root),
               KVP_SKIP_IPC_TEST="1")
    for var in ("LMCACHE_L2_SERDE", "KVP_L2_SERDE"):
        env.pop(var, None)
    r = subprocess.run(
        ["python3", "-m", "kvpersist.launch", "server",
         "--l2-adapter", adapter_json],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(tmp_path))
    return r


def test_m18_kvp_serde_installs_the_patch(tmp_path):
    """> M18: a kvp-raw adapter -> the launch prints 'serde temp-buffer patch
    installed' and patched=True. MUTATION: `if False:` around the install
    (neither line prints)."""
    r = _run_launch(tmp_path, '{"type": "nvme", "serde": {"type": "kvp-raw"}}')
    out = r.stdout + r.stderr
    assert "serde temp-buffer patch installed" in out, out[-800:]
    assert "patched=True" in out, out[-800:]


def test_m18_no_serde_does_not_install(tmp_path):
    """> M18: an adapter WITHOUT serde -> no install line, patched=False."""
    r = _run_launch(tmp_path, '{"type": "nvme"}')
    out = r.stdout + r.stderr
    assert "serde temp-buffer patch installed" not in out, out[-800:]
    assert "patched=False" in out, out[-800:]


def test_m18_mutated_source_warns_and_continues(tmp_path):
    """> M18 + N5b: a mutated upstream method -> loud WARNING, patched=False,
    rc 0 (the launch continues -- L7 catches the store failures)."""
    r = _run_launch(tmp_path, '{"type": "nvme", "serde": {"type": "kvp-raw"}}',
                    mutate_method=True)
    out = r.stdout + r.stderr
    assert "WARNING: serde temp-buffer patch NOT installed" in out, out[-800:]
    assert "patched=False" in out, out[-800:]
    assert r.returncode == 0, f"the launch must continue after the warning:\n{out[-800:]}"


# ---------------------------------------------------------------------------
# M19/M20: the three kvp_scan_l2_failures call sites in test-recovery.sh
# ---------------------------------------------------------------------------
def _extract_callsites():
    """Return [(label, fragment)] for the three scan calls in the real
    test-recovery.sh, labelled by the scenario tag in the fail message."""
    src = RECOVERY.read_text()
    sites = []
    for m in re.finditer(
            r'kvp_scan_l2_failures "\$LOG" "\$([A-Za-z_0-9]+)" \|\| fail "([^"]+)"',
            src):
        sites.append((m.group(2), m.group(0)))
    return sites


def _run_callsite(call_line):
    """Execute the REAL call line against a stub kvp_scan_l2_failures (which
    fails like a real scan on a store-failure log) and a stub fail()."""
    script = (
        'kvp_scan_l2_failures(){ return 1; }\n'
        'fail(){ echo "FAIL: $*"; exit 7; }\n'
        'LOG=fake.log\n'
        + call_line.replace('"$LOG"', '"fake.log"')
    )
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=30)


def test_m19_m20_all_three_scenarios_call_the_scanner():
    """> M19/M20: the scanner is called in scenario pre, A AND B with the
    right failure tags. Each REAL call line is executed: removing one makes
    its scenario missing -> red. MUTATIONS: removing the pre / A / B call."""
    sites = _extract_callsites()
    labels = [s[0] for s in sites]
    assert len(sites) == 3, (
        f"the scanner must be called in exactly 3 scenarios (pre, A, B), "
        f"found {len(sites)}: {labels}")
    for label, call_line in sites:
        r = _run_callsite(call_line)
        out = r.stdout + r.stderr
        assert r.returncode == 7, (
            f"[{label}] the fail must fire on a failing scan:\n{out}")
        assert f"FAIL: {label}" in out, (
            f"[{label}] wrong failure tag:\n{out}")
    joined = " ".join(labels)
    assert "scenario pre" in joined, f"the pre call must exist: {labels}"
    assert any(l.startswith("A:") for l in labels), f"the A call must exist: {labels}"
    assert any(l.startswith("B:") for l in labels), f"the B call must exist: {labels}"


# ---------------------------------------------------------------------------
# N7: the D1 tail of start-observability.sh must never flip its exit code
# ---------------------------------------------------------------------------
def _run_obs_tail(tmp_path, cache_net, srv_net="observability_default"):
    """Execute the REAL D1 tail of start-observability.sh under the REAL
    `set -Eeuo pipefail` header (extracted from the file itself) with a
    docker stub. cache_net/srv_net control the two `docker inspect` answers
    (empty string = container absent -> inspect exits 1)."""
    src = (ROOT / "scripts" / "start-observability.sh").read_text()
    # the real strict-mode header from the file, not hand-written in the test
    header = next(line for line in src.splitlines() if line.startswith("set -Eeuo pipefail"))
    start = src.index("# r28 D1: the network the monitoring stack needs")
    tail = src[start:]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *State.Running*obs_otel_collector*) echo true ;;\n'
        f'  *NetworkSettings*obs_otel_collector*) echo {srv_net} ;;\n'
        f'  *NetworkSettings*lmcache-kvp-server*) if [ -n "{cache_net}" ]; then echo "{cache_net}"; else exit 1; fi ;;\n'
        '  *inspect*) [ -n "' + cache_net + '" ] || exit 1 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
    script = header + "\n" + tail
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=30, env=env, cwd=str(tmp_path))


def test_n7_no_cache_container_is_not_an_error(tmp_path):
    """> N7: the cache container is absent at step 1/3 (the NORMAL start-all
    order) -> rc 0, no NOTE, NO silent death. MUTATION: drop `|| true`
    (pipefail kills the script, rc != 0)."""
    r = _run_obs_tail(tmp_path, cache_net="")
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"a missing cache container must NOT fail the script:\n{out[-800:]}")
    assert "NOTE: the cache server was started before observability" not in out


def test_n7_other_network_prints_note_and_stays_zero(tmp_path):
    """> N7: cache container in another network -> the NOTE prints and the
    script still exits 0."""
    r = _run_obs_tail(tmp_path, cache_net="other_net", srv_net="observability_default")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-800:]
    assert "NOTE: the cache server was started before observability" in out, out[-800:]


def test_n7_same_network_is_silent(tmp_path):
    """> N7: cache container in the SAME network -> no NOTE, rc 0."""
    r = _run_obs_tail(tmp_path, cache_net="observability_default",
                      srv_net="observability_default")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-800:]
    assert "NOTE: the cache server was started before observability" not in out


# ---------------------------------------------------------------------------
# N4 second half: SIGPIPE -- the selector must read the WHOLE input
# ---------------------------------------------------------------------------
def test_n4_sigpipe_no_silent_death_on_huge_noise(tmp_path):
    """> N4: a traceback block followed by 5 MB of DEBUG noise (way past the
    64 KB pipe buffer) under the REAL `set -Eeuo pipefail` -> the traceback
    prints, the script reaches the end. MUTATION: `exit` in the selector awk
    (tail dies with SIGPIPE, set -e kills the script silently)."""
    frames = "".join(
        f"  File x.py, line {100 + i}, in frame_{i}\n    do_step_{i}()\n"
        for i in range(60))
    exc = ("torch.AcceleratorError: CUDA error: mapping of buffer object failed\n"
           "Returning 205 (CUDA_ERROR_MAP_FAILED) from cuIpcOpenEventHandle\n")
    block = ("[2026-11-22 08:03:25,100] LMCache ERROR Error handling operation register_kv_cache\n"
             "Traceback (most recent call last):\n" + frames + exc)
    noise = "".join(
        f"[2026-11-22 08:04:{i % 60:02d}] LMCache DEBUG skipping eviction\n"
        for i in range(70000))
    logf = tmp_path / "server.log"
    with open(logf, "w") as fh:
        fh.write(block)
        fh.write(noise)
    # the EXACT selector shape of test-recovery.sh + the REAL
    # _kvp_bound_block from env.sh, under the REAL strict-mode header
    header = next(
        line for line in (ROOT / "scripts" / "test-recovery.sh").read_text().splitlines()
        if line.startswith("set -Eeuo pipefail"))
    # the REAL selector program from test-recovery.sh (mutation target: the
    # `exit` in it must make THIS test red)
    src = (ROOT / "scripts" / "test-recovery.sh").read_text()
    awks = src.index("_srverr_trace=")
    awk_start = src.index("awk '", awks) + 5
    awk_end = src.index("' | _kvp_bound_block", awk_start)
    selector = "awk '" + src[awk_start:awk_end] + "'"
    script = (
        header + "\n"
        f'. "{ENV_SH}"\n'
        f'LOG="{logf}"\n'
        f'_srverr_trace="$(tail -c +1 "$LOG" | {selector} | _kvp_bound_block 80 20)"\n'
        'printf "%s\\n" "$_srverr_trace"\n'
        'echo "REACHED-THE-END"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True,
                       text=True, timeout=120, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert "REACHED-THE-END" in out, (
        f"SIGPIPE must NOT silently kill the script (rc={r.returncode}):\n{out[-800:]}")
    assert "Returning 205" in out, f"the last exception line must print:\n{out[-800:]}"
    assert "lines skipped" in out, f"the limiter marker must show:\n{out[-800:]}"


def test_n6c_selfcheck_line_is_signed(tmp_path):
    """> N6c: `serde_alloc_patch --selfcheck` on a module carrying the FIXTURE
    source (so the patch installs) prints the SIGNED line exactly
    'serde_alloc_patch selfcheck: OK' -- never a bare OK.
    MUTATION: bare `print("OK")`."""
    # build the fake lmcache package from the fixture so install() succeeds
    fixture = FIXTURE.read_text()
    pkgroot = tmp_path / "pkgroot"
    pkg = pkgroot / "lmcache"
    for sub in ("v1", "v1/distributed", "v1/distributed/l2_adapters"):
        (pkg / sub).mkdir(parents=True, exist_ok=True)
        (pkg / sub / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "v1" / "distributed" / "l2_adapters" / "serde_wrapper.py").write_text(
        "from __future__ import annotations\n"
        "class _L1Error:\n    SUCCESS = 0\nL1Error = _L1Error\n"
        "_L1_WRITE_TAG = 'tag'\n"
        "class MemoryLayoutDesc:\n"
        "    def __init__(self, shapes, dtypes):\n"
        "        self.shapes = shapes\n        self.dtypes = dtypes\n"
        "def make_temp_key(k):\n    return k\n"
        "def serialized_layout_desc(layout, serde):\n    return layout\n"
        "class SerdeL2AdapterWrapper:\n" + fixture)
    env = dict(os.environ,
               PYTHONPATH=str(pkgroot) + os.pathsep + str(ROOT / "implementation"),
               KVP_SKIP_IPC_TEST="1")
    r = subprocess.run(["python3", "-m", "kvpersist.serde_alloc_patch",
                        "--selfcheck"], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert "serde_alloc_patch selfcheck: OK" in out, (
        f"the signed OK line must print:\n{out}")
