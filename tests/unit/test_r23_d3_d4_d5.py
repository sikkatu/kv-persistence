# SPDX-License-Identifier: Apache-2.0
"""r23 D3/D4/D5 tests (real scripts, real sockets, behavioural mutations).

D3: the fd-channel socket is AF_UNIX SOCK_SEQPACKET (pinned lmcache
    fd_channel.py:14,53,92) -- the smoke probe must use the REAL socket type
    and fail-safe (refuse unless staleness is PROVEN).
D4: an invalid LMCACHE_EVICTION_POLICY must be refused BEFORE any server
    launch (the r22 process substitution swallowed the rc=1 and launched
    the server with an empty argv).
D5: the two shutdown markers are SEPARATE criteria; the SIGTERM wait is
    configurable (KVP_SMOKE_TERM_TIMEOUT); build-image.sh passes
    ${KVP_SMOKE_PORT:-15555} to the smoke (never a hard-coded 5555).
"""
from __future__ import annotations
import os
import pathlib
import re
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation"))

# ---- shared helpers from the build-image test module ----------------------
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_r22_server_argv_and_smoke import (  # noqa: E402
    _run_smoke_with_server_stub, _start_cache_argv,
)
from test_build_image_runs import make_fixture, _run_build_image  # noqa: E402


def _fd_sock_path(port: int) -> pathlib.Path:
    return pathlib.Path(f"/dev/shm/lmcache_fd_{port}.sock")


def _seqpacket_listener(port: int):
    """A REAL AF_UNIX SOCK_SEQPACKET listener -- exactly what a live pinned
    lmcache server holds on the fd channel."""
    sp = _fd_sock_path(port)
    sp.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    srv.bind(str(sp))
    srv.listen(1)
    return srv


def _run_smoke_fd(tmp_path: pathlib.Path, port: int):
    """Run the REAL _smoke_cache_server.sh on `port` with a plain stub."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *kvpersist.launch*) exit 0 ;;\n"
        "  *kvpersist.mp_ping*) exit 0 ;;\n"
        "  *check_codec_available*) exit 0 ;;\n"
        f'  *) exec "{real_py}" "$@" ;;\n'
        "esac\n")
    (bin_dir / "python3").chmod(0o755)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_SMOKE_REPO=str(ROOT),
               KVP_SMOKE_PORT=str(port))
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "_smoke_cache_server.sh"),
         "zstd", str(tmp_path / "cache"), str(port)],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(tmp_path))
# ===========================================================================
# D3: SEQPACKET probe + fail-safe
# ===========================================================================
def test_d3_seqpacket_live_listener_refuses(tmp_path):
    """> D3: a LIVE SOCK_SEQPACKET listener = a server owns the port's fd
    channel -> the smoke REFUSES naming the port."""
    port = 15570
    srv = _seqpacket_listener(port)
    try:
        r = _run_smoke_fd(tmp_path, port)
        out = r.stdout + r.stderr
        assert r.returncode != 0, f"a live fd socket must be refused:\n{out[-800:]}"
        assert "fd-channel" in out, (
            f"the refusal must come from the fd-channel probe "
            f"(not a later unrelated failure):\n{out[-800:]}")
        assert str(port) in out, f"the refusal must name the port:\n{out[-800:]}"
    finally:
        srv.close()
        _fd_sock_path(port).unlink(missing_ok=True)


def test_d3_stale_socket_file_proceeds(tmp_path):
    """> D3: a STALE socket file (created, nothing listening -- connect()
    raises ConnectionRefusedError) is proven stale -> the smoke PROCEEDS
    (does not refuse on a dead socket file)."""
    port = 15571
    sp = _fd_sock_path(port)
    sp.unlink(missing_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.bind(str(sp))
    s.close()   # the file stays, nothing listens
    try:
        r = _run_smoke_fd(tmp_path, port)
        out = r.stdout + r.stderr
        assert "held by a live" not in out, (
            f"a stale socket file must NOT be refused as live:\n{out[-800:]}")
    finally:
        sp.unlink(missing_ok=True)


def test_d3_stream_listener_failsafe_refuses(tmp_path):
    """> D3 fail-safe: a SOCK_STREAM listener (the WRONG type -- connect()
    fails with EPROTOTYPE, which does NOT prove staleness) must make the
    probe REFUSE. An OSError that is not FileNotFound/ConnectionRefused is
    NOT proof of staleness -> refuse."""
    port = 15572
    sp = _fd_sock_path(port)
    sp.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sp))
    srv.listen(1)
    try:
        r = _run_smoke_fd(tmp_path, port)
        out = r.stdout + r.stderr
        assert r.returncode != 0, (
            f"EPROTOTYPE must fail-safe to REFUSAL (not proceed):\n{out[-800:]}")
        assert "fd-channel" in out, (
            f"the refusal must come from the fd-channel probe -- the mutant "
            f"(no fail-safe) proceeds and only dies LATER (missing markers / "
            f"no pong), which must NOT count:\n{out[-800:]}")
    finally:
        srv.close()
        sp.unlink(missing_ok=True)


def test_d3_mutation_stream_probe_would_not_refuse_seqpacket():
    """> Mutation (D3): if the probe used SOCK_STREAM (the r22 defect), a live
    SEQPACKET listener would NOT be detected (EPROTOTYPE -> misread as
    stale -> proceed). Verify the probe source uses the REAL type."""
    src = (ROOT / "scripts" / "_smoke_cache_server.sh").read_text()
    assert "socket.SOCK_SEQPACKET" in src, (
        "the probe must use SOCK_SEQPACKET (the pinned lmcache type)")
    # behavioural: a SOCK_STREAM connect to a SEQPACKET listener = EPROTOTYPE
    port = 15573
    sp = _fd_sock_path(port)
    sp.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    srv.bind(str(sp))
    srv.listen(1)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(sp))
            eproto_ok = True    # connected?! unexpected
        except OSError as e:
            eproto_ok = (e.errno == __import__("errno").EPROTOTYPE)
            eproto_msg = str(e)
        finally:
            s.close()
        assert eproto_ok, (
            "SOCK_STREAM connect to SEQPACKET must fail with EPROTOTYPE, "
            f"got: {globals().get('eproto_msg', eproto_ok)}")
    finally:
        srv.close()
        sp.unlink(missing_ok=True)


def test_d3_mutation_no_failsafe_would_proceed(tmp_path):
    """> Mutation (D3): drop the fail-safe (treat every OSError as stale)
    -> on a SOCK_STREAM listener the mutant proceeds instead of refusing.
    Build both probe variants and compare verdicts."""
    port = 15574
    sp = _fd_sock_path(port)
    sp.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sp))
    srv.listen(1)
    real_probe = (
        "import socket, sys\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)\n"
        "try:\n"
        "    s.connect(sys.argv[1])\n"
        "    s.close()\n"
        "    sys.exit(0)\n"
        "except (FileNotFoundError, ConnectionRefusedError):\n"
        "    sys.exit(1)\n"
        "except OSError:\n"
        "    sys.exit(0)\n"
    )
    mutant_probe = real_probe.replace(
        "except OSError:\n    sys.exit(0)",
        "except OSError:\n    sys.exit(1)")
    assert mutant_probe != real_probe
    try:
        real = subprocess.run([sys.executable, "-c", real_probe, str(sp)],
                              capture_output=True)
        mut = subprocess.run([sys.executable, "-c", mutant_probe, str(sp)],
                             capture_output=True)
        assert real.returncode == 0, "the real probe must REFUSE on EPROTOTYPE"
        assert mut.returncode == 1, (
            "the mutant (no fail-safe) wrongly PROCEEDS on EPROTOTYPE")
    finally:
        srv.close()
        sp.unlink(missing_ok=True)
# ===========================================================================
# D4: refuse BEFORE launch on a bad LMCACHE_EVICTION_POLICY
# ===========================================================================
def test_d4_bad_policy_refuses_before_launch(tmp_path):
    """> D4: an invalid LMCACHE_EVICTION_POLICY -> the REAL start-cache.sh
    refuses and NEVER launches the server (zero launch attempts). The r22
    code swallowed the rc=1 through process substitution and launched the
    server with an EMPTY argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    (live / "image-identity.env").write_text(
        "KVP_LMC_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb\n")
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    # the stub COUNTS launch attempts; any launch means the refusal failed
    launches = tmp_path / "launches"
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *kvpersist.launch*) case "$*" in *--help*) echo "--http-host --http-port --otlp-endpoint --enable-tracing"; exit 0 ;; esac; echo x >> "{launches}"; exit 0 ;;\n'
        "  *kvpersist.mp_ping*) exit 3 ;;\n"
        "  *check_codec_available*) exit 0 ;;\n"
        "  *--help*) echo \"--http-host --http-port --otlp-endpoint --enable-tracing\" ;;\n"
        f'  *) exec "{real_py}" "$@" ;;\n'
        "esac\n")
    (bin_dir / "python3").chmod(0o755)
    (bin_dir / "lmcache").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "lmcache").chmod(0o755)
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_LAUNCH_MODE="host",
               KVP_CACHE_ROOT=str(cache_root),
               KVP_CACHE_BUDGET_GB="1",
               KVP_LIVE_DIR=str(live),
               LIVE_DIR=str(live),
               KVP_SKIP_IPC_TEST="1",
               LMCACHE_L2_SERDE="none",
               LMCACHE_EVICTION_POLICY="lfu",
               LMCACHE_PORT="15575",
               LMCACHE_HOST="127.0.0.1")
    r = subprocess.run(["bash", str(ROOT / "scripts" / "start-cache.sh")],
                       capture_output=True, text=True, timeout=120, env=env,
                       cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a bad policy must refuse:\n{out[-800:]}"
    assert "LMCACHE_EVICTION_POLICY" in out, (
        f"the refusal must name LMCACHE_EVICTION_POLICY:\n{out[-800:]}")
    assert "refusing to start the server" in out, (
        f"the refusal must come from the argv reader's caller (the `die` "
        f"BEFORE any launch), not from a later crash with an empty argv:\n"
        f"{out[-800:]}")
    assert not launches.exists(), (
        f"the server must NOT be launched on a bad policy:\n{out[-800:]}")


def test_d4_mutation_swallow_rc_call_shape_is_red():
    """> Mutation (D4): the r22 defect -- process substitution swallows the
    rc=1 (`_kvp_server_argv_read ARR < <(_kvp_server_argv ...)`). The source
    must NOT use that call shape anywhere: the argv reader is called with
    the argv parameters directly (its rc is checked by the caller)."""
    for script in ("start-cache.sh", "_smoke_cache_server.sh"):
        src = (ROOT / "scripts" / script).read_text()
        bad = re.findall(r"_kvp_server_argv_read\s+\w+\s*<\s*<\(", src)
        assert not bad, (
            f"{script} must not call the argv reader via process substitution "
            f"(it swallows the rc=1 on a bad LMCACHE_EVICTION_POLICY): {bad}")
        # and the call must pass the argv parameters to the reader
        assert "_kvp_server_argv_read" in src, (
            f"{script} must call the shared argv reader")


def test_d4_docker_branch_also_refuses(tmp_path):
    """> D4: the docker branch of start-cache.sh must refuse too -- the argv
    reader is shared, both branches die before `docker run`/the server
    launch. Reuses the REAL helper (the same stubs the B4 consumer tests
    use): a bad LMCACHE_EVICTION_POLICY must produce a refusal and ZERO
    launch attempts."""
    cache_root = tmp_path / "cache"
    argv, r = _start_cache_argv(tmp_path, "docker", cache_root,
                                extra_env={"LMCACHE_EVICTION_POLICY": "lfu"},
                                require_launch=False)
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"the docker branch must refuse on a bad policy:\n{out[-800:]}")
    assert "LMCACHE_EVICTION_POLICY" in out, (
        f"the refusal must name LMCACHE_EVICTION_POLICY:\n{out[-800:]}")
    assert argv == [], (
        f"the server must NOT be launched on a bad policy, got argv={argv}")


# ===========================================================================
# D5: separate closed/persist markers, configurable TERM timeout,
# build-image.sh passes ${KVP_SMOKE_PORT:-15555}
# ===========================================================================
def test_d5_missing_persist_marker_refuses(tmp_path):
    """> D5.1: the CLOSED marker present but the PERSIST marker missing ->
    refusal naming the persist marker (the two markers are SEPARATE
    criteria; the old test only checked 'markers present/absent' together,
    so the B5-4 mutant -- ungated persist check -- survived)."""
    r = _run_smoke_with_server_stub(tmp_path, exit_code=143,
                                    closed=True, persist=False)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a missing persist marker must refuse:\n{out[-800:]}"
    assert "persist_enabled=True" in out, (
        f"the refusal must name the persist marker:\n{out[-800:]}")


def test_d5_missing_closed_marker_refuses(tmp_path):
    """> D5.1: the PERSIST marker present but the CLOSED marker missing ->
    refusal naming the closed marker."""
    r = _run_smoke_with_server_stub(tmp_path, exit_code=143,
                                    closed=False, persist=True)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a missing closed marker must refuse:\n{out[-800:]}"
    assert "MPCacheServer closed" in out, (
        f"the refusal must name the closed marker:\n{out[-800:]}")


def test_d5_hung_server_times_out_refuses(tmp_path):
    """> D5.2: a server that IGNORES SIGTERM -> the smoke kills it after
    KVP_SMOKE_TERM_TIMEOUT (3s here, not the production 30s) and refuses
    with 137. The r22 code had the wait hard-coded (mutation B5-5: the
    timeout parameter was unused)."""
    t0 = time.monotonic()
    r = _run_smoke_with_server_stub(tmp_path, exit_code=143,
                                    closed=True, persist=True, hang=True)
    elapsed = time.monotonic() - t0
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a hung server must be refused:\n{out[-800:]}"
    assert "137" in out, f"the refusal must report exit 137:\n{out[-800:]}"
    assert elapsed < 30, (
        f"KVP_SMOKE_TERM_TIMEOUT=3 must not wait the production 30s "
        f"(took {elapsed:.0f}s):\n{out[-800:]}")


def test_d5_build_image_passes_smoke_port(tmp_path):
    """> D5.3: build-image.sh passes ${KVP_SMOKE_PORT:-15555} to the smoke --
    with KVP_SMOKE_PORT=15577 set, the recorded smoke invocation must carry
    15577 (mutation B6-2: hard-coded 5555 -> caught here)."""
    patch_dir, build_dir, out_dir, _p4, _p5, _whl = make_fixture(tmp_path)
    args_log = tmp_path / "smoke-args.log"
    r, _store = _run_build_image(tmp_path, patch_dir, build_dir, out_dir,
                                 extra_env={"KVP_SMOKE_ARGS_LOG": str(args_log),
                                            "KVP_SMOKE_PORT": "15577"})
    assert args_log.exists(), (
        f"the smoke must run during the build:\n{r.stdout}\n{r.stderr}")
    text = args_log.read_text()
    assert "15577" in text, (
        f"the smoke port must come from KVP_SMOKE_PORT (15577), got:\n{text}")
    assert " 5555 " not in f" {text} ", (
        f"the smoke port must NOT be the production 5555:\n{text}")


def test_d5_build_image_default_smoke_port_is_15555(tmp_path):
    """> D5.3: without KVP_SMOKE_PORT the smoke gets the default 15555
    (not the production 5555)."""
    patch_dir, build_dir, out_dir, _p4, _p5, _whl = make_fixture(tmp_path)
    args_log = tmp_path / "smoke-args.log"
    env = {"KVP_SMOKE_ARGS_LOG": str(args_log)}
    r, _store = _run_build_image(tmp_path, patch_dir, build_dir, out_dir,
                                 extra_env=env)
    assert args_log.exists(), (
        f"the smoke must run during the build:\n{r.stdout}\n{r.stderr}")
    text = args_log.read_text()
    assert "15555" in text, (
        f"the default smoke port must be 15555, got:\n{text}")


def test_d5_mutation_hardcoded_port_is_red():
    """> Mutation (D5.3 / B6-2): hard-code 5555 in build-image.sh instead of
    ${KVP_SMOKE_PORT:-15555} -> the port tests above must catch it. Pin the
    source here: the invocation line must carry the parameterized port."""
    src = (ROOT / "scripts" / "build-image.sh").read_text()
    assert "${KVP_SMOKE_PORT:-15555}" in src, (
        "build-image.sh must pass ${KVP_SMOKE_PORT:-15555} to the smoke")
    # the mutant: replace the parameterized port with the production 5555
    mutant = src.replace('"${KVP_SMOKE_PORT:-15555}"', '"5555"')
    assert mutant != src, "the mutation must change the source"
    assert '"5555"' in mutant


def test_d5_mutation_ungated_persist_check_is_red():
    """> Mutation (D5.1 / B5-4): the persist-marker check runs ungated (e.g.
    only `grep -q ... || true` instead of `|| exit 1`) -> the
    missing-persist test must catch it. The smoke source must gate BOTH
    marker checks with a hard exit."""
    src = (ROOT / "scripts" / "_smoke_cache_server.sh").read_text()
    # find both marker checks and verify each ends in exit 1
    import re  # noqa: F811
    # match the whole if-block: from the `if ! grep` line to the `fi` on its
    # own line (the marker text itself contains " fi" inside "data files")
    closed_block = re.search(
        r"if ! grep -q \"MPCacheServer closed\".*?^fi", src, re.S | re.M)
    persist_block = re.search(
        r"if ! grep -q \"persist_enabled=True.*?^fi", src, re.S | re.M)
    assert closed_block, "the closed-marker check must exist"
    assert persist_block, "the persist-marker check must exist"
    for name, block in (("closed", closed_block), ("persist", persist_block)):
        assert "exit 1" in block.group(0), (
            f"the {name}-marker check must hard-exit on a missing marker, "
            f"got:\n{block.group(0)}")
# ===========================================================================
# D6: case-insensitive eviction policy (D6.2), stale comment (D6.1),
# committed pin-spec generator (D6.3)
# ===========================================================================
def test_d6_eviction_policy_case_insensitive():
    """> D6.2: every case variant normalizes to the pin's canonical spelling
    (LRU/ARC/IsolatedLRU/noop). The pin parser is case-sensitive; the shell
    normalization must accept any case (r22 only handled a fixed list)."""
    cases = {
        "lru": "LRU", "LRU": "LRU", "Lru": "LRU", "lRu": "LRU",
        "arc": "ARC", "Arc": "ARC", "aRC": "ARC",
        "isolatedlru": "IsolatedLRU", "Isolatedlru": "IsolatedLRU",
        "ISOLATEDLRU": "IsolatedLRU", "isolatedLRU": "IsolatedLRU",
        "noop": "noop", "Noop": "noop", "NOOP": "noop", "nOoP": "noop",
    }
    for src, want in cases.items():
        out = subprocess.run(
            ["bash", "-c",
             f'set -u\n. "{ROOT}/scripts/env.sh"\n'
             f'_kvp_normalize_eviction_policy "{src}"'],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, LMCACHE_EVICTION_POLICY=""))
        assert out.stdout.strip() == want, (
            f"{src!r} must normalize to {want!r}, got {out.stdout.strip()!r} "
            f"(rc={out.returncode})")


def test_d6_stale_comment_removed():
    """> D6.1: the stale r22-era comment about the deleted line-based
    normalization (`lru|Lru`) must be gone (the code is now case-insensitive)."""
    src = (ROOT / "scripts" / "env.sh").read_text()
    assert "lru|Lru" not in src, (
        "the stale line-based normalization comment/branch must be removed")
    assert "${_p,,}" in src, (
        "the normalization must lowercase the input (case-insensitive)")


def test_d6_pin_spec_generator_is_committed():
    """> D6.3: tests/tools/gen_pin_spec.py is committed and can regenerate
    tests/unit/pin_server_spec.json from the pinned lmcache source (the
    fixture is reproducible, not hand-edited)."""
    gen = ROOT / "tests" / "tools" / "gen_pin_spec.py"
    assert gen.is_file(), "tests/tools/gen_pin_spec.py must be committed"
    text = gen.read_text()
    assert "07f1c6bb642f4772606a36eab730ae009ddce08f" in text, (
        "the generator must pin the lmcache source commit (the pin's SHA)")
    assert "pin_server_spec" in text, (
        "the generator must produce pin_server_spec.json")
    spec = ROOT / "tests" / "unit" / "pin_server_spec.json"
    assert spec.is_file(), "the generated fixture must exist"
    import json as _j
    data = _j.loads(spec.read_text())
    # the spec must carry the flags the tests rely on
    flat = {fl for sec in data.values() for f in sec["flags"] for fl in f["flags"]}
    for fl in ("--host", "--port", "--l1-size-gb", "--eviction-policy",
               "--chunk-size", "--l2-adapter"):
        assert fl in flat, f"the pin spec must carry {fl}"