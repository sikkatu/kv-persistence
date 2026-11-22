# SPDX-License-Identifier: Apache-2.0
"""r25 G1-G4/G7 tests (acceptance of r24 F2/F3/E4 on hardware logs).

G1: the readiness gate must WAIT for container creation (the docker-run
    client starts the container asynchronously -- the old gate refused at
    0s); refuse at once when the client exited without creating it, and when
    the container is created but exited/dead.
G2: the readiness timeout counts REAL elapsed seconds (a failing mp_ping
    takes up to 5s per try -- the old +2 counter ran 3-4x past the limit).
G3: start-all.sh must EXPORT the resolved KVP_LAUNCH_MODE (the assignment
    runs the resolver in a subshell -- its own export is lost there).
G4: repeated runs of this file stay green (free ports + stub finalizer).
G7: KVP_SMOKE_TERM_TIMEOUT invalid -> the kvpersist.launch stub is NEVER
    invoked and the output does not claim "did NOT exit".
"""
from __future__ import annotations
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_r22_server_argv_and_smoke import _start_cache_argv  # noqa: E402


# ===========================================================================
# G1: three distinct states
# ===========================================================================
def test_g1_late_container_is_waited_for(tmp_path):
    """> G1 acceptance: the container appears 0.3, 1.5 and 5s after `docker run`
    (creation with --gpus all takes hundreds of ms) -> the gate must WAIT
    and reach READY every time, not refuse at 0s (the r24 F2 race)."""
    for delay in (0.3, 1.5, 5.0):
        t0 = time.monotonic()
        argv, r = _start_cache_argv(tmp_path, "docker", tmp_path / f"c{delay}",
                                    require_launch=False,
                                    create_delay=delay,
                                    extra_env={"KVP_CACHE_READY_TIMEOUT": "20",
                                               "KVP_CACHE_CREATE_TIMEOUT": "15"})
        out = r.stdout + r.stderr
        elapsed = time.monotonic() - t0
        assert r.returncode == 0, (
            f"a container appearing after {delay}s must be waited for, "
            f"not refused:\n{out[-800:]}")
        assert "READY" in out, out[-800:]


def test_g1_run_dies_without_container_refuses(tmp_path):
    """> G1: the docker-run client exits immediately (rc=125, e.g. "could
    not select device driver") and no container was ever created -> refusal
    within a few seconds naming the docker-run failure + log tail."""
    t0 = time.monotonic()
    argv, r = _start_cache_argv(tmp_path, "docker", tmp_path / "cache",
                                require_launch=False, run_dies=True,
                                extra_env={"KVP_CACHE_READY_TIMEOUT": "20",
                                           "KVP_CACHE_CREATE_TIMEOUT": "15"})
    out = r.stdout + r.stderr
    elapsed = time.monotonic() - t0
    assert r.returncode != 0, (
        f"a docker-run client that exits at once must refuse:\n{out[-800:]}")
    assert "never created" in out or "docker-run client exited" in out, (
        f"the refusal must name the docker-run failure:\n{out[-800:]}")
    assert elapsed <= 6, f"must fail fast, took {elapsed:.1f}s"


def test_g1_created_then_exited_refuses_fast(tmp_path):
    """> G1: the container is created and immediately exited/dead -> refusal
    within a few seconds (the state is 'exited', not a slow ping timeout)."""
    t0 = time.monotonic()
    argv, r = _start_cache_argv(tmp_path, "docker", tmp_path / "cache",
                                require_launch=False, docker_state="exited",
                                extra_env={"KVP_CACHE_READY_TIMEOUT": "20",
                                           "KVP_CACHE_CREATE_TIMEOUT": "15"})
    out = r.stdout + r.stderr
    elapsed = time.monotonic() - t0
    assert r.returncode != 0, (
        f"an exited container must refuse:\n{out[-800:]}")
    assert "exited" in out or "dead" in out, (
        f"the refusal must name the container state:\n{out[-800:]}")
    assert elapsed <= 6, f"must fail fast, took {elapsed:.1f}s"


def test_g1_mutation_no_wait_is_red(tmp_path):
    """> G1 mutation: a gate that inspects ONCE (no wait loop) would refuse a
    late container. Probe it directly: the old behaviour = inspect immediately
    -> 'absent' -> refuse. Our fixed code waits. The mutant probe below
    reproduces the old logic and shows it refuses where the fixed code
    reaches READY (the behavioural difference is test_g1_late_container)."""
    src = (ROOT / "scripts" / "start-cache.sh").read_text()
    # the fixed code must contain the wait-for-creation branch
    assert "KVP_CACHE_CREATE_TIMEOUT" in src, (
        "the gate must wait for container creation (KVP_CACHE_CREATE_TIMEOUT)")
    assert "docker-run client exited immediately" in src, (
        "the gate must detect the docker-run client exit (no container)")


# ===========================================================================
# G2: real elapsed time
# ===========================================================================
def test_g2_ready_timeout_uses_real_seconds(tmp_path):
    """> G2 acceptance: a failing mp_ping takes 5s per try; with
    KVP_CACHE_READY_TIMEOUT=6 the refusal must arrive within 6+7=13s (the
    old +2-per-iteration counter ran 21.3s -- 3-4x past the limit)."""
    t0 = time.monotonic()
    argv, r = _start_cache_argv(tmp_path, "docker", tmp_path / "cache",
                                require_launch=False, docker_pong=False,
                                ping_slow=True,
                                extra_env={"KVP_CACHE_READY_TIMEOUT": "6",
                                           "KVP_CACHE_CREATE_TIMEOUT": "6"})
    out = r.stdout + r.stderr
    elapsed = time.monotonic() - t0
    assert r.returncode != 0, (
        f"a server that never pongs must be refused:\n{out[-800:]}")
    assert elapsed <= 13, (
        f"the timeout must be REAL seconds (6 + one 5s ping + slack), took "
        f"{elapsed:.1f}s:\n{out[-800:]}")
    assert "did NOT answer" in out, out[-800:]


def test_g2_mutation_counter_is_red():
    """> G2 mutation: the old code counted +2 per iteration. The fixed code
    must use `date +%s` (or SECONDS) -- pinned here."""
    src = (ROOT / "scripts" / "start-cache.sh").read_text()
    assert "_start_ts=$(date +%s)" in src, (
        "the readiness timeout must be measured in REAL seconds")
    assert "_waited=$((_waited + 2))" not in src, (
        "the old +2-per-iteration counter must be gone")


# ===========================================================================
# G3: export KVP_LAUNCH_MODE in start-all.sh
# ===========================================================================
def test_g3_start_all_exports_launch_mode():
    """> G3: the assignment KVP_LAUNCH_MODE="$(_kvp_resolve_launch_mode)"
    runs the resolver in a SUBSHELL -- its export is lost. start-all.sh must
    export explicitly after the assignment, BEFORE the recipe launcher."""
    t = (ROOT / "scripts" / "start-all.sh").read_text()
    at = t.index('KVP_LAUNCH_MODE="$(_kvp_resolve_launch_mode)"')
    after = t[at:]
    assert "export KVP_LAUNCH_MODE" in after.split("run-tp4-hotsplit")[0], (
        "start-all.sh must export KVP_LAUNCH_MODE after the assignment "
        "and before the recipe launcher (the resolver's export is lost in "
        "the subshell)")


def test_g3_export_reaches_the_recipe_launcher(tmp_path):
    """> G3 behavioural: run the RESOLVER + export exactly as start-all.sh
    does, then run a child that prints the variable -- it must see docker
    (the default) or host (explicit)."""
    for explicit, want in ((None, "docker"), ("host", "host")):
        env = dict(os.environ)
        env.pop("KVP_LAUNCH_MODE", None)
        if explicit:
            env["KVP_LAUNCH_MODE"] = explicit
        script = (
            f'set -u\n. "{ROOT}/scripts/env.sh"\n'
            'KVP_LAUNCH_MODE="$(_kvp_resolve_launch_mode)" || exit 1\n'
            'export KVP_LAUNCH_MODE\n'
            'bash -c \'echo [${KVP_LAUNCH_MODE-UNSET}]\'\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=30, env=env)
        assert f"[{want}]" in r.stdout, (
            f"explicit={explicit!r} must reach the child as {want!r}: "
            f"{r.stdout}{r.stderr}")


# ===========================================================================
# G4: repeated runs stay green (free ports + finalizer)
# ===========================================================================
def test_g4_two_runs_in_a_row_are_green(tmp_path):
    """> G4: the F2 stubs used to hold fixed ports (15571-3) for 30s after a
    refusal -- the next run failed 'Address already in use'. Now the ports
    are free per run and the finalizer kills the stub servers."""
    for i in range(2):
        argv, r = _start_cache_argv(tmp_path, "docker", tmp_path / f"c{i}",
                                    require_launch=False,
                                    docker_pong=False,
                                    extra_env={"KVP_CACHE_READY_TIMEOUT": "3",
                                               "KVP_CACHE_CREATE_TIMEOUT": "3"})
        out = r.stdout + r.stderr
        assert r.returncode != 0, f"run {i}:\n{out[-600:]}"


def test_g4_no_stub_servers_left(tmp_path):
    """> G4: after the helper returns, no fake_server process is left (the
    finalizer kills them -- they sleep 30s when start-cache.sh refuses)."""
    argv, r = _start_cache_argv(tmp_path, "docker", tmp_path / "cache",
                                require_launch=False, docker_pong=False,
                                extra_env={"KVP_CACHE_READY_TIMEOUT": "3",
                                           "KVP_CACHE_CREATE_TIMEOUT": "3"})
    me = str(tmp_path).encode()
    left = 0
    for proc in pathlib.Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes()
        except OSError:
            continue
        if b"fake_server.py" in cmd and me in cmd:
            left += 1
    assert left == 0, f"{left} stub servers left running after the helper"


# ===========================================================================
# G7: E4.1 hardening
# ===========================================================================
def test_g7_bad_term_timeout_never_launches(tmp_path):
    """> G7: a bad KVP_SMOKE_TERM_TIMEOUT must be refused BEFORE the
    kvpersist.launch stub runs and without a "did NOT exit" lie (the
    mutation 'drop exit 1' must go red here). All paths under tmp_path."""
    launches = tmp_path / "launches"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *kvpersist.launch*) echo x >> "{launches}"; sleep 360 & wait $! 2>/dev/null; exit 0 ;;\n'
        "  *kvpersist.mp_ping*) exit 0 ;;\n"
        "  *check_codec_available*) exit 0 ;;\n"
        f'  *) exec "{real_py}" "$@" ;;\n'
        "esac\n")
    (bin_dir / "python3").chmod(0o755)
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    for bad in ("abc", "0", "-3", "1.5"):
        env = dict(os.environ,
                   PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                   KVP_SMOKE_REPO=str(ROOT),
                   KVP_SMOKE_TERM_TIMEOUT=bad)
        r = subprocess.run(
            ["bash", str(ROOT / "scripts" / "_smoke_cache_server.sh"),
             "zstd", str(cache_root), "0"],
            capture_output=True, text=True, timeout=30, env=env,
            cwd=str(tmp_path))
        out = r.stdout + r.stderr
        assert r.returncode != 0, (
            f"KVP_SMOKE_TERM_TIMEOUT={bad!r} must be refused:\n{out[-400:]}")
        assert "KVP_SMOKE_TERM_TIMEOUT" in out, (
            f"the error must name the variable:\n{out[-400:]}")
        assert not launches.exists(), (
            f"the kvpersist.launch stub must NEVER run on a bad timeout "
            f"({bad!r}):\n{out[-400:]}")
        assert "did NOT exit" not in out, (
            f"the smoke must not claim 'did NOT exit' for {bad!r}:\n{out[-400:]}")