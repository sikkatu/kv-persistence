"""Round 42/80b regression: LMCache version-compatibility pre-flight.

The install run failed at register_kv_caches (300s timeout) because the host
served lmcache 0.5.3 while the pinned vLLM image ships 0.5.6 -- mixed minor
versions break the MP protocol silently. The check lives in the shared
_kvp_pre_flights() (env.sh) now (it was in the deleted kv launcher); this
test drives it directly: the host version comes from the _kvp_python resolver
($HOME/.venvs/lmcache/bin/python), the image version from `docker run --rm
--entrypoint python3` -- both stubbed here.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T
import os
import socket
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _listen():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _setup(tmp_path: Path, host_ver: str, img_ver: str):
    """Stub BOTH version probes:
    * host: $HOME/.venvs/lmcache/bin/python (what _kvp_python resolves);
    * image: a docker stub answering the `--entrypoint python3` probe."""
    home = tmp_path / "home"
    vpy = home / ".venvs" / "lmcache" / "bin" / "python"
    vpy.parent.mkdir(parents=True)
    vpy.write_text(f'#!/usr/bin/env bash\necho "{host_ver}"\n')
    vpy.chmod(vpy.stat().st_mode | stat.S_IEXEC)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\n'
        'case "$*" in\n'
        '  *entrypoint*python3*) echo "' + img_ver + '"; exit 0 ;;\n'
        'esac\nexit 0\n')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return home, bindir


def _run_pre_flights(tmp_path, host_ver, img_ver):
    home, bindir = _setup(tmp_path, host_ver, img_ver)
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    srv, port = _listen()
    try:
        r = subprocess.run(
            ["bash", "-c",
             f'. scripts/env.sh && _kvp_pre_flights "{ROOT}" 127.0.0.1 {port} '
             f'"{cache_root}" "img@sha256:x" 1000 1000 0 host'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(home),
                 "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                 "KVP_SKIP_IPC_TEST": "1",
                 "LMCACHE_L2_SERDE": "zstd"},
            cwd=str(ROOT))
    finally:
        srv.close()
    return r


def test_version_mismatch_blocks_before_the_load(tmp_path):
    """Host 0.5.3 vs image 0.5.6 must abort BEFORE the model load (was:
    register_kv_caches silently timed out after 300s)."""
    r = _run_pre_flights(tmp_path, "0.5.3", "0.5.6")
    combined = r.stdout + r.stderr
    assert r.returncode != 0, (
        "a version mismatch must abort:\n" + combined[-1500:])
    assert "LMCACHE VERSION MISMATCH" in combined, (
        "the refusal must name the version mismatch:\n" + combined[-1500:])
    assert "0.5.3" in combined and "0.5.6" in combined, (
        "the refusal must name BOTH versions:\n" + combined[-1500:])


def test_version_match_passes_the_pre_flight(tmp_path):
    """Matching versions let the launch through the pre-flight."""
    r = _run_pre_flights(tmp_path, "0.5.6", "0.5.6")
    combined = r.stdout + r.stderr
    assert "pre-flight: lmcache version matches" in combined, (
        "the pre-flight must confirm matching versions:\n" + combined[-1500:])


# ---------------------------------------------------------------------------
# r20 K3: docker mode -- the real invariant is "server and engine run ONE
# image", NOT "host-venv version == image version" (the host copy is only
# the mp_ping client there; requirements-lmcache.txt calls it "diagnostic").
# The host-vs-image version check made EVERY docker launch refuse (host
# 0.5.6 vs image 0.5.8rc1.post48+kvp...) with an unrunnable hint
# (`pip install lmcache==...+kvp...` -- no such package on PyPI).
# ---------------------------------------------------------------------------
CACHE_CONTAINER = "lmcache-kvp-server"


def _setup_docker(tmp_path: Path, host_ver: str,
                  srv_img: str, eng_img: str,
                  exec_rc: int = 0, exec_out: str = "RPC OK",
                  container_present: bool = True,
                  img_ver: str = "0.5.8rc1.post48+kvp.60dce759ea93"):
    """A docker stub answering exactly what the r20 K3 checks ask:
    * `docker inspect --format {{.Image}} <cache-container>` -> srv_img
      (empty string when the container is absent);
    * `docker inspect --format {{.Id}} <image>` -> eng_img;
    * `docker exec <cache-container> python3 -m kvpersist.mp_ping ...`
      -> exec_out with exit exec_rc;
    * `docker run --rm --entrypoint python3 <image> -c ...` -> img_ver (the
      IMAGE's version -- NOT the host's: the r17- r20 K3 defect hid behind a
      stub answering the same version on both sides)."""
    home = tmp_path / "home"
    vpy = home / ".venvs" / "lmcache" / "bin" / "python"
    vpy.parent.mkdir(parents=True)
    vpy.write_text(f'#!/usr/bin/env bash\necho "{host_ver}"\n')
    vpy.chmod(vpy.stat().st_mode | stat.S_IEXEC)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # a real listener for the (c0) TCP probe
    srv, port = _listen()
    docker = bindir / "docker"
    log_path = str(tmp_path / "docker.log")
    img_line = srv_img if container_present else ""
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log_path}"\n'
        'cmd="${1:-}"; shift || true\n'
        'case "$cmd" in\n'
        '  inspect)\n'
        '    _fmt=""; _tgt=""\n'
        '    while [ "$#" -gt 0 ]; do case "$1" in\n'
        '      --format) _fmt="$2"; shift 2 ;;\n'
        '      *) _tgt="$1"; shift ;;\n'
        '    esac; done\n'
        '    case "$_fmt" in\n'
        f'      *".Image"*) echo "{img_line}" ;;\n'
        f'      *".Id"*) echo "{eng_img}" ;;\n'
        # any OTHER format (a tag / name query) answers the SAME value for
        # every target -- a code path comparing tags instead of .Id sees NO
        # difference between two builds (the r20 K3 mutation detector)
        '      *) echo "kvp-lmcache:latest" ;;\n'
        '    esac\n'
        '    exit 0 ;;\n'
        '  exec)\n'
        f'    echo "{exec_out}"; exit {exec_rc} ;;\n'
        '  run)\n'
        f'    echo "{img_ver}"; exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return home, bindir, srv, port


def _run_pre_flights_docker(tmp_path, host_ver="0.5.6",
                            srv_img="sha256:" + "a" * 64,
                            eng_img="sha256:" + "a" * 64,
                            exec_rc=0, exec_out="RPC OK",
                            container_present=True,
                            img_ver="0.5.8rc1.post48+kvp.60dce759ea93"):
    home, bindir, srv, port = _setup_docker(
        tmp_path, host_ver, srv_img, eng_img, exec_rc, exec_out,
        container_present, img_ver)
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    try:
        r = subprocess.run(
            ["bash", "-c",
             f'. scripts/env.sh && _kvp_pre_flights "{ROOT}" 127.0.0.1 {port} '
             f'"{cache_root}" "img@sha256:x" 1000 1000 0 docker'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(home),
                 "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                 "KVP_SKIP_IPC_TEST": "1",
                 "KVP_CACHE_CONTAINER": CACHE_CONTAINER,
                 "LMCACHE_L2_SERDE": "none"},
            cwd=str(ROOT))
    finally:
        srv.close()
    return r


def test_docker_mode_same_image_passes_with_host_version_mismatch(tmp_path):
    """THE hardware case: host 0.5.6 vs image 0.5.8rc1.post48+kvp... must
    NOT refuse in docker mode -- the server/engine run ONE image (the same
    id), which is the real invariant. The journal must show the cache
    container inspect and the exec'd RPC ping."""
    r = _run_pre_flights_docker(tmp_path, host_ver="0.5.6",
                                img_ver="0.5.8rc1.post48+kvp.60dce759ea93",
                                srv_img="sha256:" + "a" * 64,
                                eng_img="sha256:" + "a" * 64,
                                exec_rc=0, exec_out="RPC OK")
    combined = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"docker mode with one image must pass despite the host version:\n"
        f"{combined[-1500:]}")
    assert "LMCACHE VERSION MISMATCH" not in combined, (
        f"the host-vs-image version check must NOT run in docker mode:\n"
        f"{combined[-1500:]}")
    assert "same image" in combined, combined[-800:]
    log = (tmp_path / "docker.log").read_text()
    assert "inspect" in log and CACHE_CONTAINER in log, (
        f"the cache container must be inspected:\n{log}")
    assert "exec" in log and "mp_ping" in log, (
        f"the RPC ping must run INSIDE the cache container:\n{log}")


def test_docker_mode_different_images_refused_naming_both(tmp_path):
    """Server on a different image than the engine -> refusal naming BOTH
    ids + the stop-cache/start-cache fix."""
    srv_img = "sha256:" + "b" * 64
    eng_img = "sha256:" + "a" * 64
    r = _run_pre_flights_docker(tmp_path, srv_img=srv_img, eng_img=eng_img)
    combined = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"different images must be refused:\n{combined[-1500:]}")
    assert "DIFFERENT images" in combined, combined[-800:]
    assert "b" * 8 in combined and "a" * 8 in combined, (
        f"the refusal must name BOTH image ids:\n{combined[-1500:]}")
    assert "stop-cache.sh" in combined and "start-cache.sh" in combined, (
        f"the fix must be named:\n{combined[-800:]}")


def test_docker_mode_no_container_but_port_open_skips_with_note(tmp_path):
    """No cache container, TCP port open -> NOTE 'check skipped', no
    refusal."""
    r = _run_pre_flights_docker(tmp_path, container_present=False,
                                srv_img="", eng_img="sha256:" + "a" * 64)
    combined = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"an absent container with an open port must not refuse:\n"
        f"{combined[-1500:]}")
    assert "check skipped" in combined, combined[-800:]


def test_docker_mode_exec_ping_exit3_is_refused(tmp_path):
    """In docker mode mp_ping exit 3 (lmcache not importable INSIDE the
    image) must be a REFUSAL -- 'skipped' there would hide a broken image."""
    r = _run_pre_flights_docker(tmp_path, exec_rc=3,
                                exec_out="lmcache not importable")
    combined = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"exec-ping exit 3 must be refused in docker mode:\n{combined[-1500:]}")
    assert "NOT importable inside the cache container" in combined, (
        combined[-800:])
    assert "skipped" not in combined.split("NOT importable")[0][-200:], (
        "exit 3 must never be reported as an honest skip in docker mode")


def test_host_mode_mismatch_hint_names_the_built_wheel(tmp_path):
    """host mode: the version check stays, but the fix hint must be
    RUNNABLE -- the path of the single *.whl under live/lmcache-build/wheels,
    with --force-reinstall --no-deps, and NO `lmcache==<version>` (such a
    package does not exist on PyPI)."""
    # a real wheel name in the repo's live dir (what build-lmcache.sh leaves)
    live = ROOT / "live" / "lmcache-build" / "wheels"
    created = False
    if not live.exists():
        live.mkdir(parents=True, exist_ok=True)
        created = True
    whl = live / "lmcache-0.5.8rc1.post48+kvp.60dce759ea93-cp312-cp312-linux_x86_64.whl"
    had_whl = whl.exists()
    if not had_whl:
        whl.write_bytes(b"PK\x05\x06stub")
    try:
        home, bindir = _setup(tmp_path, "0.5.3", "0.5.6")
        cache_root = tmp_path / "cache"
        cache_root.mkdir(exist_ok=True)
        srv, port = _listen()
        try:
            r = subprocess.run(
                ["bash", "-c",
                 f'. scripts/env.sh && _kvp_pre_flights "{ROOT}" 127.0.0.1 {port} '
                 f'"{cache_root}" "img@sha256:x" 1000 1000 0 host'],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "HOME": str(home),
                     "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                     "KVP_SKIP_IPC_TEST": "1",
                     "LMCACHE_L2_SERDE": "zstd"},
                cwd=str(ROOT))
        finally:
            srv.close()
        combined = r.stdout + r.stderr
        assert r.returncode != 0, (
            f"host mode with a version mismatch must refuse:\n{combined[-1500:]}")
        assert "LMCACHE VERSION MISMATCH" in combined, combined[-800:]
        assert "--force-reinstall --no-deps" in combined, (
            f"the hint must carry --force-reinstall --no-deps:\n{combined[-1500:]}")
        assert whl.name in combined, (
            f"the hint must name the EXACT built wheel:\n{combined[-1500:]}")
        assert "lmcache==" not in combined, (
            f"the unrunnable `lmcache==<version>` hint must be GONE:\n"
            f"{combined[-1500:]}")
    finally:
        if not had_whl:
            whl.unlink(missing_ok=True)
        if created:
            try:
                live.rmdir()
                (ROOT / "live" / "lmcache-build").rmdir()
            except OSError:
                pass