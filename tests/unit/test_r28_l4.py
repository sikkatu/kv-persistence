# SPDX-License-Identifier: Apache-2.0
"""r28 L4: the six mutants that survived the r27 green run.

Each test here kills exactly one surviving mutant (a production edit after
which all r27 tests stayed green):
1. `KVP_SAME_UID=${KVP_SAME_UID:-1}` -> `:-0` (the default is root!) --
   run with KVP_SAME_UID UNSET and assert `--user <uid>:<gid>`;
2. `SERVER_GID=$(id -g)` -> `$(id -u)` -- stub id answers uid=1000 gid=1001
   and the run must carry `--user 1000:1001`;
3. dropping the GID comparison in the K3 server-UID check --
   Config.User=1000:0 against engine 1000:1000 must REFUSE naming both;
4. the `sudo chown -R` hint hardcoded to 0:0 -- with uid=1000 gid=1001 the
   printed command must be exactly `sudo chown -R 1000:1001 <root>`;
5. dropping `CONSUMER_USER=${CONSUMER_USER:-$PRODUCER_USER}` -- run with
   PRODUCER_USER set and CONSUMER_USER UNSET, the consumer container must
   still get `--user 1000:1000`;
6. `test-recovery.sh` output truncated to `head -25` -- covered by
   test_r28_diagnostics.py::test_l2_recovery_branches_bounded.
"""
from __future__ import annotations
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START_CACHE = ROOT / "scripts" / "start-cache.sh"
ENV_SH = ROOT / "scripts" / "env.sh"
IPC_TEST = ROOT / "scripts" / "ipc-test.sh"

DOCKER_STUB = '''#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "${1:-}" in
  image)
    case "${2:-}" in
      inspect) echo "sha256:" ; exit 0 ;;
    esac ;;
  run)
    echo "RUN $*" >> "$STUB_LOG"
    exit 0 ;;
  inspect)
    case "$*" in
      *Config.User*) printf '%s' "${STUB_DOCKER_USER-}" ;;
      *State.Running*) echo true ;;
      *State.Status*) echo running ;;
      *) echo "" ;;
    esac
    exit 0 ;;
  exec) echo "pong"; exit 0 ;;
  rm|ps|stop) exit 0 ;;
esac
exit 0
'''

ID_STUB = '''#!/usr/bin/env bash
case "${1:-}" in
  -u) echo "${STUB_UID:-%d}" ;;
  -g) echo "${STUB_GID:-%d}" ;;
  -un) echo "tester" ;;
  *) echo "tester" ;;
esac
''' % (os.getuid(), os.getgid())


def _write_stub(bin_dir, name, text):
    p = bin_dir / name
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _start_cache(tmp_path, extra_env=None, same_uid=None):
    """Run the REAL start-cache.sh with stub docker/id. same_uid=None means
    KVP_SAME_UID is NOT set in the environment (the L4.1 contract)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub_log = tmp_path / "stub.log"
    _write_stub(bin_dir, "docker", DOCKER_STUB)
    _write_stub(bin_dir, "id", ID_STUB)
    # K2's ownership probe must never fire here (the marker is created by the
    # test process itself); the r27 T2 tests cover K2 with their own stub find.
    _write_stub(bin_dir, "find", "#!/usr/bin/env bash\nexit 0\n")
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    _write_stub(bin_dir, "python3",
                "#!/usr/bin/env bash\n"
                "case \"$*\" in\n"
                "  *server*--help*) echo '--http-host --http-port --otlp-endpoint' ;;\n"
                "  *kvpersist.launch*server*) exit 0 ;;\n"
                "  *check_codec_available*) exit 0 ;;\n"
                "  *check_image_identity*)\n"
                "    echo 'KVP_WHEEL_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb'\n"
                "    exit 0 ;;\n"
                f"  *) exec \"{real_py}\" \"$@\" ;;\n"
                "esac\n")
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    (live / "image-identity.env").write_text(
        "KVP_LMC_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb\n")
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               STUB_LOG=str(stub_log),
               KVP_LAUNCH_MODE="docker",
               KVP_CACHE_ROOT=str(cache_root),
               KVP_CACHE_BUDGET_GB="1",
               KVP_LIVE_DIR=str(live),
               LIVE_DIR=str(live),
               KVP_SKIP_IPC_TEST="1",
               LMCACHE_L2_SERDE="none",
               KVP_SHM_DIR=str(tmp_path / "shm"),
               KVP_SKIP_IMAGE_CHECK="1",
               KVP_SKIP_LMC_IDENTITY="1")
    (tmp_path / "shm").mkdir(exist_ok=True)
    for var in ("IMAGE", "DOCKER_IMAGE", "KVP_LMCACHE_BUILD"):
        env.pop(var, None)
    env.pop("KVP_SAME_UID", None)  # L4.1: the variable must be UNSET
    if same_uid is not None:
        env["KVP_SAME_UID"] = same_uid
    env.setdefault("STUB_UID", "1000")
    env.setdefault("STUB_GID", "1000")
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(["bash", str(START_CACHE)], capture_output=True,
                       text=True, timeout=90, env=env, cwd=str(tmp_path))
    return r, stub_log, cache_root


def _server_run_line(stub_log):
    if not stub_log.exists():
        return ""
    for line in stub_log.read_text().splitlines():
        if line.startswith("RUN ") and "kvpersist.launch" in line and "server" in line \
                and "--help" not in line:
            return line
    return ""


def test_l4_1_default_is_operator_uid_not_root(tmp_path):
    """> L4.1: KVP_SAME_UID UNSET -> the default is same-UID: `--user
    <uid>:<gid>`, never `--user 0:0`. MUTATION: `:-1` -> `:-0` (the default
    silently becomes root)."""
    r, stub_log, _ = _start_cache(tmp_path, same_uid=None)
    line = _server_run_line(stub_log)
    assert line, f"the server must launch:\n{(r.stdout + r.stderr)[-800:]}"
    assert "--user 1000:1000" in line, (
        f"the UNSET default must be the operator UID (1000:1000 from the id stub):\n{line}")
    assert "--user 0:0" not in line, (
        f"the default must NOT be root:\n{line}")


def test_l4_2_gid_comes_from_id_g(tmp_path):
    """> L4.2: SERVER_GID must come from `id -g` (stub: uid=1000 gid=1001)
    -> `--user 1000:1001`. MUTATION: `$(id -g)` -> `$(id -u)` (both become
    1000)."""
    r, stub_log, _ = _start_cache(tmp_path,
                                  extra_env={"STUB_UID": "1000",
                                             "STUB_GID": "1001"})
    line = _server_run_line(stub_log)
    assert line, f"the server must launch:\n{(r.stdout + r.stderr)[-800:]}"
    assert "--user 1000:1001" in line, (
        f"the GID must come from id -g (1001, not 1000):\n{line}")


# ---------------------------------------------------------------------------
# L4.3: the GID comparison in the K3 server-UID check
# ---------------------------------------------------------------------------
def test_l4_3_gid_mismatch_refuses(tmp_path):
    """> L4.3: Config.User=1000:0 against engine 1000:1000 (_same=1) -> REFUSE
    naming BOTH pairs. MUTATION: dropping the GID comparison (only UIDs
    compared -> 1000==1000 would pass)."""
    import socket
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "docker", (
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *Config.User*) echo '1000:0' ;;\n"
        "  *State.Running*) echo true ;;\n"
        "  *State.Status*) echo running ;;\n"
        "  *) echo '' ;;\n"
        "esac\n"
        "exit 0\n"))
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    _write_stub(bin_dir, "python3",
                "#!/usr/bin/env bash\n"
                "case \"$*\" in\n"
                "  *mp_ping*) echo 'rpc: pong'; exit 0 ;;\n"
                f"  *) exec \"{real_py}\" \"$@\" ;;\n"
                "esac\n")
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_SKIP_IPC_TEST="1",
               KVP_CACHE_CONTAINER="lmcache-kvp-server")
    script = (
        f'. "{ENV_SH}" && '
        f'_kvp_pre_flights "{ROOT}" 127.0.0.1 {port} '
        f'"{tmp_path}/croot" "img@sha256:x" 1000 1000 1 docker')
    try:
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=60, env=env, cwd=str(tmp_path))
    finally:
        srv.close()
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a GID mismatch must refuse:\n{out[-1200:]}"
    assert "1000:0" in out and "1000:1000" in out, (
        f"the refusal must name BOTH pairs:\n{out[-1200:]}")


# ---------------------------------------------------------------------------
# L4.4: the exact `sudo chown` command text
# ---------------------------------------------------------------------------
def test_l4_4_chown_command_text_is_exact(tmp_path):
    """> L4.4: with uid=1000 gid=1001 the printed fix command must be exactly
    `sudo chown -R 1000:1001 <root>`. MUTATION: hardcoding 0:0 in the hint."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "find", (
        "#!/usr/bin/env bash\n"
        "echo \"$1/leftover-root-file\"\n"
        "exit 0\n"))
    _write_stub(bin_dir, "docker", DOCKER_STUB)
    _write_stub(bin_dir, "id", ID_STUB)
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    _write_stub(bin_dir, "python3",
                "#!/usr/bin/env bash\n"
                "case \"$*\" in\n"
                "  *server*--help*) echo '--http-host --http-port --otlp-endpoint' ;;\n"
                "  *kvpersist.launch*server*) exit 0 ;;\n"
                "  *check_codec_available*) exit 0 ;;\n"
                "  *check_image_identity*)\n"
                "    echo 'KVP_WHEEL_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb'\n"
                "    exit 0 ;;\n"
                f"  *) exec \"{real_py}\" \"$@\" ;;\n"
                "esac\n")
    live = tmp_path / "live"
    live.mkdir()
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    (live / "image-identity.env").write_text(
        "KVP_LMC_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb\n")
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "leftover-root-file").write_text("x")
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               STUB_LOG=str(tmp_path / "stub.log"),
               KVP_LAUNCH_MODE="docker",
               KVP_CACHE_ROOT=str(cache_root),
               KVP_CACHE_BUDGET_GB="1",
               KVP_LIVE_DIR=str(live),
               LIVE_DIR=str(live),
               KVP_SKIP_IPC_TEST="1",
               LMCACHE_L2_SERDE="none",
               KVP_SHM_DIR=str(tmp_path / "shm"),
               KVP_SKIP_IMAGE_CHECK="1",
               KVP_SKIP_LMC_IDENTITY="1",
               STUB_UID="1000",
               STUB_GID="1001")
    (tmp_path / "shm").mkdir()
    for var in ("IMAGE", "DOCKER_IMAGE", "KVP_LMCACHE_BUILD", "KVP_SAME_UID"):
        env.pop(var, None)
    r = subprocess.run(["bash", str(START_CACHE)], capture_output=True,
                       text=True, timeout=90, env=env, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a foreign leftover must refuse:\n{out[-800:]}"
    assert f"sudo chown -R 1000:1001 {cache_root}" in out, (
        f"the exact fix command must print:\n{out[-800:]}")


# ---------------------------------------------------------------------------
# L4.5: CONSUMER_USER defaults to PRODUCER_USER
# ---------------------------------------------------------------------------
def test_l4_5_consumer_user_defaults_to_producer_user(tmp_path):
    """> L4.5: PRODUCER_USER=1000:1000 set, CONSUMER_USER UNSET -> the consumer
    container still gets `--user 1000:1000`. MUTATION: dropping
    `CONSUMER_USER=${CONSUMER_USER:-$PRODUCER_USER}`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    itd = tmp_path / "ipc-test"
    _write_stub(bin_dir, "docker", f'''#!/usr/bin/env bash
echo "$*" >> "{argv_log}"
case "${{1:-}}" in
  rm) exit 0 ;;
  logs) echo "producer: torch 2.5, python 3.12, pid=1111, IPC handle written"; exit 0 ;;
  run)
    case "$*" in
      *consumer.py*) echo "consumer: torch 2.5, python 3.12, pid=2222, sum=1 expected=1"; exit 0 ;;
      *producer.py*) touch "{itd}/handle.pkl"; exit 0 ;;
    esac
    exit 0 ;;
esac
exit 0
''')
    host_py = bin_dir / "hostpy"
    host_py.write_text("#!/usr/bin/env bash\nexit 0\n")
    host_py.chmod(host_py.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               IMAGE="img@sha256:" + "a" * 64,
               CONSUMER="container",
               HOST_PY=str(host_py),
               IPC_TEST_DIR=str(itd),
               IPC_CONTAINER="kvp-ipc-test-stub",
               PRODUCER_USER="1000:1000",
               GPU_IDX="0")
    env.pop("CONSUMER_USER", None)  # the L4.5 contract: UNSET
    r = subprocess.run(["bash", str(IPC_TEST)], capture_output=True,
                       text=True, timeout=60, env=env, cwd=str(tmp_path))
    lines = argv_log.read_text().splitlines()
    cons = [l for l in lines if "consumer.py" in l]
    assert cons, f"the container consumer must launch:\n{r.stdout}{r.stderr}"
    assert "--user 1000:1000" in cons[0], (
        f"CONSUMER_USER must default to PRODUCER_USER:\n{cons[0]}")


# ---------------------------------------------------------------------------
# D1: the observability network wiring
# ---------------------------------------------------------------------------
def _start_cache_otlp(tmp_path, collector_running=True,
                      explicit_endpoint=None):
    """Run the REAL start-cache.sh (docker mode) with stub docker whose
    inspect answers obs_otel_collector state and its network; return the
    recorded server docker-run line + the output."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_log = tmp_path / "stub.log"
    _write_stub(bin_dir, "find", "#!/usr/bin/env bash\nexit 0\n")
    _write_stub(bin_dir, "id", ID_STUB)
    _write_stub(bin_dir, "docker", f'''#!/usr/bin/env bash
echo "$*" >> "{stub_log}"
case "${{1:-}}" in
  image)
    case "${{2:-}}" in
      inspect) echo "sha256:" ; exit 0 ;;
    esac ;;
  run)
    case "$*" in
      *server*--help*) echo "--http-host --http-port --otlp-endpoint --enable-tracing"; exit 0 ;;
    esac
    echo "RUN $*" >> "{stub_log}"
    exit 0 ;;
  inspect)
    case "$*" in
      *obs_otel_collector*)
        case "$*" in
          *State.Running*) echo "{"true" if collector_running else "false"}" ;;
          *NetworkSettings*) echo "observability_default" ;;
          *) echo "" ;;
        esac ;;
      *Config.User*) echo "1000:1000" ;;
      *State.Running*) echo true ;;
      *State.Status*) echo running ;;
      *) echo "" ;;
    esac
    exit 0 ;;
  exec) echo "pong"; exit 0 ;;
  rm|ps|stop) exit 0 ;;
esac
exit 0
''')
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    _write_stub(bin_dir, "python3",
                "#!/usr/bin/env bash\n"
                "case \"$*\" in\n"
                "  *server*--help*) echo '--http-host --http-port --otlp-endpoint' ;;\n"
                "  *kvpersist.launch*server*) exit 0 ;;\n"
                "  *check_codec_available*) exit 0 ;;\n"
                "  *check_image_identity*)\n"
                "    echo 'KVP_WHEEL_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb'\n"
                "    exit 0 ;;\n"
                f"  *) exec \"{real_py}\" \"$@\" ;;\n"
                "esac\n")
    live = tmp_path / "live"
    live.mkdir()
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    (live / "image-identity.env").write_text(
        "KVP_LMC_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb\n")
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               STUB_LOG=str(stub_log),
               KVP_LAUNCH_MODE="docker",
               KVP_CACHE_ROOT=str(cache_root),
               KVP_CACHE_BUDGET_GB="1",
               KVP_LIVE_DIR=str(live),
               LIVE_DIR=str(live),
               KVP_SKIP_IPC_TEST="1",
               LMCACHE_L2_SERDE="none",
               KVP_SHM_DIR=str(tmp_path / "shm"),
               KVP_SKIP_IMAGE_CHECK="1",
               KVP_SKIP_LMC_IDENTITY="1",
               STUB_UID="1000",
               STUB_GID="1000")
    (tmp_path / "shm").mkdir()
    for var in ("IMAGE", "DOCKER_IMAGE", "KVP_LMCACHE_BUILD",
                "KVP_SAME_UID", "KVP_OTLP_ENDPOINT", "LMCACHE_OTLP_ENDPOINT",
                "USE_OBSERVABILITY"):
        env.pop(var, None)
    if explicit_endpoint is not None:
        env["KVP_OTLP_ENDPOINT"] = explicit_endpoint
    r = subprocess.run(["bash", str(START_CACHE)], capture_output=True,
                       text=True, timeout=90, env=env, cwd=str(tmp_path))
    line = ""
    for l in stub_log.read_text().splitlines():
        if l.startswith("RUN ") and "kvpersist.launch" in l and "server" in l \
                and "--help" not in l:
            line = l
    return r, line


def test_d1_collector_running_joins_the_obs_network(tmp_path):
    """> D1: obs_otel_collector running -> the server docker run carries
    `--network observability_default` and the endpoint
    `http://obs_otel_collector:4317`, and NO `host.docker.internal`.
    MUTATIONS: restoring the host.docker.internal rewrite / dropping
    --network."""
    r, line = _start_cache_otlp(tmp_path, collector_running=True)
    assert line, f"the server must launch:\n{(r.stdout + r.stderr)[-800:]}"
    assert "--network observability_default" in line, (
        f"the server must join the collector's network:\n{line}")
    assert "http://obs_otel_collector:4317" in line, (
        f"the endpoint must be the collector's service address:\n{line}")
    assert "host.docker.internal" not in line, (
        f"the broken rewrite must be gone:\n{line}")


def test_d1_no_collector_no_network_no_otlp(tmp_path):
    """> D1: no collector -> NO --network and NO --otlp-endpoint (metrics
    off), with the 'not detected' line in the output."""
    r, line = _start_cache_otlp(tmp_path, collector_running=False)
    out = r.stdout + r.stderr
    assert line, f"the server must launch:\n{out[-800:]}"
    assert "--network" not in line, f"no network without a collector:\n{line}"
    assert "--otlp-endpoint" not in line, f"no OTLP without a collector:\n{line}"
    assert "not detected" in out, f"the honest note must print:\n{out[-800:]}"


def test_d1_explicit_endpoint_never_rewritten(tmp_path):
    """> D1: an explicit KVP_OTLP_ENDPOINT is used AS-IS (never rewritten),
    even with a running collector -- including a localhost one (the old code
    rewrote localhost -> host.docker.internal). MUTATION: rewriting the
    explicit endpoint."""
    r, line = _start_cache_otlp(tmp_path, collector_running=True,
                                explicit_endpoint="http://localhost:4317")
    assert line, f"the server must launch:\n{(r.stdout + r.stderr)[-800:]}"
    assert "http://localhost:4317" in line, (
        f"the explicit endpoint must be used as-is:\n{line}")
    assert "host.docker.internal" not in line, f"no rewrite:\n{line}"
    assert "obs_otel_collector" not in line, (
        f"the explicit endpoint must win over the collector address:\n{line}")