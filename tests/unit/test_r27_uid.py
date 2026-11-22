# SPDX-License-Identifier: Apache-2.0
"""Round 27 review: the cache server must run under the SAME UID as the engine.

Hardware review proved CUDA IPC between the two containers only opens when
BOTH sides share ONE UID (1000+1000 PASS, root+1000 FAIL MAP_FAILED,
--pid host is NOT needed). Defects covered:
K1: start-cache.sh docker run must carry --user <uid>:<gid> (KVP_SAME_UID=1)
    and HOME=/tmp/kvp-server-home (+XDG/TRITON); KVP_SAME_UID=0 -> --user 0:0.
K2: leftover ROOT-owned objects in $ROOT_PATH or a foreign
    /dev/shm/lmcache_fd_<port>.sock must REFUSE before docker run with the
    exact one-shot fix command (never auto-run anything as root).
K3: _kvp_pre_flights (docker mode) must read the RUNNING server's
    docker Config.User and refuse BEFORE the engine start when it does not
    match the engine UID (empty/0/0:0/root all mean root).

All behavioural: stub docker + stub id in PATH write argv to a file; no real
paths, no fixed ports (H3/H4 lesson). IPC_TEST_DIR/IPC_CONTAINER/KVP_SHM_DIR
keep the tests away from /tmp/kvp-ipc-test and /dev/shm.
"""
from __future__ import annotations
import os
import pathlib
import stat
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "implementation"))

START_CACHE = ROOT / "scripts" / "start-cache.sh"

# ---------------------------------------------------------------------------
# stub docker: records argv; inspect answers Config.User / State.Running
# ---------------------------------------------------------------------------
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
  logs) echo "producer: torch 2.5, python 3.12, pid=${STUB_PROD_PID:-1234}, IPC handle written"; exit 0 ;;
  rm|ps|stop) exit 0 ;;
esac
exit 0
'''

ID_STUB = f'''#!/usr/bin/env bash
case "${{1:-}}" in
  -u) echo "${{STUB_UID:-{os.getuid()}}}" ;;
  -g) echo "${{STUB_GID:-{os.getgid()}}}" ;;
  -un) echo "tester" ;;
  *) echo "tester" ;;
esac
'''


def _write_stub(bin_dir: pathlib.Path, name: str, text: str):
    p = bin_dir / name
    p.write_text(text)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _start_cache(tmp_path, extra_env=None, same_uid="1", cache_root=None):
    """Run the REAL start-cache.sh with stub docker/id and an isolated cache
    root + live dir. Returns (returncode, stdout+stderr, stub_log path)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub_log = tmp_path / "stub.log"
    _write_stub(bin_dir, "docker", DOCKER_STUB)
    _write_stub(bin_dir, "id", ID_STUB)
    # start-cache probes `python3 -m kvpersist.launch server --help` and the
    # codec check -- answer everything with the real python.
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    _write_stub(bin_dir, "python3", f'''#!/usr/bin/env bash
case "$*" in
  *server*--help*) echo "--http-host --http-port --otlp-endpoint" ;;
  *kvpersist.launch*server*) exit 0 ;;
  *check_codec_available*) exit 0 ;;
  *check_image_identity*)
    echo "KVP_WHEEL_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb"
    exit 0 ;;
  *) exec "{real_py}" "$@" ;;
esac
''')
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    (live / "image-identity.env").write_text(
        "KVP_LMC_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb\n")
    cache_root = cache_root or (tmp_path / "cache")
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
               KVP_SAME_UID=same_uid,
               LMCACHE_L2_SERDE="none",
               KVP_SHM_DIR=str(tmp_path / "shm"))
    (tmp_path / "shm").mkdir(exist_ok=True)
    for var in ("IMAGE", "DOCKER_IMAGE", "KVP_LMCACHE_BUILD"):
        env.pop(var, None)
    # r2 add-on gates need a REAL image identity -- opt out exactly like the
    # other stub-docker tests do (test_engine_delegation, r2 fixtures).
    env["KVP_SKIP_IMAGE_CHECK"] = "1"
    env["KVP_SKIP_LMC_IDENTITY"] = "1"
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(["bash", str(START_CACHE)], capture_output=True,
                       text=True, timeout=90, env=env, cwd=str(tmp_path))
    return r, stub_log, cache_root


def _server_run_line(stub_log: pathlib.Path) -> str:
    """The docker run line that launches the SERVER (kvpersist.launch server)."""
    if not stub_log.exists():
        return ""
    for line in stub_log.read_text().splitlines():
        if line.startswith("RUN ") and "kvpersist.launch" in line and "server" in line \
                and "--help" not in line:
            return line
    return ""


# ===========================================================================
# T1 (K1): --user matches KVP_SAME_UID
# ===========================================================================
def test_t1_same_uid_carries_user_and_home(tmp_path):
    """> T1: KVP_SAME_UID=1 -> the server docker run has --user <uid>:<gid> and
    HOME=/tmp/kvp-server-home (the operator user has no passwd home inside
    the container). MUTATION caught: dropping --user."""
    r, stub_log, _ = _start_cache(tmp_path, same_uid="1")
    out = r.stdout + r.stderr
    line = _server_run_line(stub_log)
    assert line, f"server must be launched:\n{out[-800:]}"
    uid = os.getuid()
    gid = os.getgid()
    assert f"--user {uid}:{gid}" in line, (
        f"KVP_SAME_UID=1 must pass --user {uid}:{gid}: {line}")
    assert "HOME=/tmp/kvp-server-home" in line, line
    assert "XDG_CACHE_HOME=/tmp/kvp-server-home/.cache" in line, line
    assert "TRITON_CACHE_DIR=/tmp/kvp-server-home/triton" in line, line


def test_t1_root_fallback_is_explicit_root(tmp_path):
    """> T1: KVP_SAME_UID=0 -> the server docker run has --user 0:0 (the
    explicit root fallback, both sides root). MUTATION caught: always using
    the operator UID."""
    r, stub_log, _ = _start_cache(tmp_path, same_uid="0")
    line = _server_run_line(stub_log)
    assert line, f"server must be launched:\n{(r.stdout + r.stderr)[-800:]}"
    assert "--user 0:0" in line, line


def test_t1_note_only_in_root_mode(tmp_path):
    """> T1: the 'files written by ROOT' note is gone for KVP_SAME_UID=1 and
    kept for KVP_SAME_UID=0."""
    r, _, _ = _start_cache(tmp_path, same_uid="1")
    assert "written by ROOT" not in (r.stdout + r.stderr), (
        "the ROOT note must not print for same-UID")
    r, _, _ = _start_cache(tmp_path, same_uid="0")
    assert "written by ROOT" in (r.stdout + r.stderr), (
        "the ROOT note must stay for the root fallback")


# ===========================================================================
# T2 (K2): leftover root state refuses with exact commands
# ===========================================================================
def test_t2_foreign_cache_root_objects_refuse_with_chown(tmp_path):
    """> T2: a file in $ROOT_PATH owned by another uid -> refusal BEFORE docker
    run naming `sudo chown -R`. The test fakes ownership via a stub `find`
    that reports one foreign object (a real chown is impossible without
    root, the stub stands in for the find predicate). MUTATION caught:
    dropping the ownership check."""
    bin_dir = tmp_path / "bin"
    # start-cache runs `find "$ROOT_PATH" -mindepth 1 ! -uid N -print -quit`:
    # a stub `find` reporting one hit simulates a root-owned leftover.
    r, stub_log, cache_root = _start_cache(tmp_path, extra_env={
        "STUB_FIND_HIT": "1"})
    # Without a real foreign owner the find returns nothing -> the launch
    # proceeds. Now re-run with a stub find that DOES report a leftover.
    bin_dir = tmp_path / "bin"
    _write_stub(bin_dir, "find", '''#!/usr/bin/env bash
if [ -n "${STUB_FIND_HIT:-}" ]; then echo "$1/leftover-root-file"; exit 0; fi
exit 0
''')
    (cache_root / "leftover-root-file").write_text("x")
    stub_log.unlink(missing_ok=True)  # only the SECOND run must be recorded
    r2, stub_log2, _ = _start_cache(tmp_path, extra_env={"STUB_FIND_HIT": "1"})
    out = r2.stdout + r2.stderr
    assert r2.returncode != 0, f"a foreign-UID leftover must refuse:\n{out[-800:]}"
    uid, gid = os.getuid(), os.getgid()
    assert f"sudo chown -R {uid}:{gid}" in out, out[-800:]
    assert "leftover-root-file" in out, out[-800:]
    assert not _server_run_line(stub_log2), (
        "the refusal must happen BEFORE the server docker run")


def test_t2_foreign_shm_socket_refuses_with_rm(tmp_path):
    """> T2: a leftover /dev/shm (KVP_SHM_DIR) socket of a foreign owner ->
    refusal naming `sudo rm -f`. The socket is a plain file in the test's own
    KVP_SHM_DIR; the stub `stat` reports the foreign uid."""
    r0, _, cache_root = _start_cache(tmp_path)
    shm = tmp_path / "shm"
    # discover the port the run used: KVP_CACHE_ROOT's server would use
    # LMCACHE_PORT -- start-cache default 5555 unless env sets it.
    port = os.environ.get("LMCACHE_PORT", "5555")
    sock = shm / f"lmcache_fd_{port}.sock"
    sock.write_text("")
    bin_dir = tmp_path / "bin"
    _write_stub(bin_dir, "stat", '''#!/usr/bin/env bash
# report a FOREIGN owner for any path
echo "999"
''')
    # need the port start-cache uses: pass LMCACHE_PORT explicitly
    (tmp_path / "stub.log").unlink(missing_ok=True)  # only the refusal run counts
    r, stub_log, _ = _start_cache(tmp_path, extra_env={
        "LMCACHE_PORT": port, "LMCACHE_HOST": "127.0.0.1"})
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a foreign shm socket must refuse:\n{out[-800:]}"
    assert f"sudo rm -f {sock}" in out, out[-800:]
    assert not _server_run_line(stub_log), (
        "the refusal must happen BEFORE the server docker run")


# ===========================================================================
# T3 (K3): pre-flight compares the RUNNING server's UID
# ===========================================================================
def _pre_flights(root_dir, tmp_path, same="1", docker_user=""):
    """Run the REAL _kvp_pre_flights (docker mode) with stub docker and a
    REAL listening socket (the TCP reachability probe is bash /dev/tcp)."""
    import socket
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    bin_dir = tmp_path / "pfbin"
    bin_dir.mkdir(exist_ok=True)
    stub_log = tmp_path / "pfstub.log"
    _write_stub(bin_dir, "docker", DOCKER_STUB)
    _write_stub(bin_dir, "id", ID_STUB)
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    _write_stub(bin_dir, "python3", f'''#!/usr/bin/env bash
case "$*" in
  *mp_ping*) echo "rpc: pong"; exit 0 ;;
  *) exec "{real_py}" "$@" ;;
esac
''')
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               STUB_LOG=str(stub_log),
               STUB_DOCKER_USER=docker_user,
               KVP_SKIP_IPC_TEST="1",
               KVP_CACHE_CONTAINER="lmcache-kvp-server")
    script = (
        f'. "{ROOT}/scripts/env.sh" && '
        f'_kvp_pre_flights "{root_dir}" 127.0.0.1 {port} '
        f'"{tmp_path}/croot" "img@sha256:x" 1000 1000 {same} docker'
    )
    try:
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=60, env=env, cwd=str(tmp_path))
    finally:
        srv.close()
    return r, stub_log


def test_t3_root_server_vs_engine_1000_refuses(tmp_path):
    """> T3: the running server has empty Config.User (= root) while the engine
    runs as 1000:1000 (_same=1) -> refusal naming BOTH UIDs.
    MUTATION caught: inverting the comparison or not normalizing an empty
    Config.User."""
    r, _ = _pre_flights(ROOT, tmp_path, same="1", docker_user="")
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a root server vs engine 1000 must refuse:\n{out[-1200:]}"
    assert "runs as UID 0:0" in out, out[-1200:]
    assert "1000:1000" in out, f"the refusal must name the engine UID:\n{out[-1200:]}"


def test_t3_matching_uid_passes(tmp_path):
    """> T3: Config.User 1000:1000, engine 1000:1000 (_same=1) -> passes."""
    r, _ = _pre_flights(ROOT, tmp_path, same="1", docker_user="1000:1000")
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"matching UIDs must pass:\n{out[-1200:]}"
    assert "UID matches the engine" in out, out[-1200:]


def test_t3_root_mode_expects_root_server(tmp_path):
    """> T3: _same=0 (both sides root) -- empty Config.User (root server) passes,
    a 1000:1000 server refuses. MUTATION caught: hard-coding the operator UID."""
    r, _ = _pre_flights(ROOT, tmp_path, same="0", docker_user="")
    assert r.returncode == 0, (r.stdout + r.stderr)[-1200:]
    r, _ = _pre_flights(ROOT, tmp_path, same="0", docker_user="1000:1000")
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a non-root server in root mode must refuse:\n{out[-1200:]}"
    assert "runs as UID 1000:1000" in out, out[-1200:]


def test_t3_root_variants_normalize(tmp_path):
    """> T3: Config.User values '0', '0:0', 'root' all normalize to root and
    refuse against engine 1000. MUTATION caught: dropping the normalization."""
    for variant in ("0", "0:0", "root"):
        r, _ = _pre_flights(ROOT, tmp_path, same="1", docker_user=variant)
        out = r.stdout + r.stderr
        assert r.returncode != 0, f"'{variant}' must normalize to root:\n{out[-1200:]}"
        assert "runs as UID 0:0" in out, f"'{variant}' -> root:\n{out[-1200:]}"


def test_t3_unparsable_user_is_skipped_not_refused(tmp_path):
    """> T3: a Config.User value that is NOT a uid/gid pair (e.g. a test stub
    answering a status line) is NOT a proven mismatch -> NOTE + skip, never a
    false refusal. MUTATION caught: treating garbage as a UID."""
    r, _ = _pre_flights(ROOT, tmp_path, same="1",
                        docker_user="running exit=0 oom=false")
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"an unparsable Config.User must not refuse:\n{out[-1200:]}"
    assert "UID check is skipped" in out, out[-1200:]