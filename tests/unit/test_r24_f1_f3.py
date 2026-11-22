# SPDX-License-Identifier: Apache-2.0
"""r24 acceptance tests (F1-F3 hardware blockers + E2/E3/E4 test gaps).

F1 (P0): the cache-server docker run needs `--entrypoint python3` -- the
    vllm/vllm-openai image has ENTRYPOINT `vllm`, which rejects
    `-m kvpersist.launch ...` and the container dies at once. Test: a stub
    docker with ENTRYPOINT semantics (vllm rejects non-vllm commands without
    --entrypoint) must keep the "container" alive only with the fix. Plus a
    static audit: every `docker run` running python/bash must set --entrypoint.
F2 (P1): docker readiness = container alive + RPC pong inside (the port check
    only sees the Docker proxy). Tests: dead container -> fast fail with the
    log tail; no pong -> timeout refusal; delayed pong -> READY not before.
F3 (P1): ONE launch-mode resolver, default docker everywhere, refusal on
    bogus values.
E2: prove "refuse before launch" for the docker branch and the smoke.
E3: regression test for the build-image.sh identity-dir fix.
E4.1: KVP_SMOKE_TERM_TIMEOUT validation.
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_r22_server_argv_and_smoke import (  # noqa: E402
    _start_cache_argv, _run_smoke_with_server_stub, _argv_from_file,
)
from test_build_image_runs import make_fixture, _run_build_image  # noqa: E402


# ===========================================================================
# F1: --entrypoint audit + docker-branch launch on an ENTRYPOINT-aware stub
# ===========================================================================
def test_f1_audit_docker_run_needs_entrypoint_for_non_vllm():
    """> F1.2: every `docker run` in scripts/*.sh whose command is NOT vllm
    (i.e. python/bash/-m after the image) must carry `--entrypoint` BEFORE
    the image name. Violation = the container's ENTRYPOINT (`vllm`) swallows
    the command and dies. Static audit: comment lines and echo/printf
    strings are ignored; a docker run line that invokes python/bash/sh/-m
    (or a .py/.sh script path) and has NO --entrypoint is a violation (vllm
    subcommands never run python directly)."""
    violations = []
    for sh in sorted((ROOT / "scripts").glob("*.sh")):
        text = sh.read_text()
        joined = text.replace("\\\n", " ")   # unfold continuation lines
        for line in joined.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue   # comment
            idx = line.find("docker run")
            if idx == -1:
                continue
            head = line[:idx].lower()
            if any(k in head for k in ("echo", "printf", "note", "warn")):
                continue   # prose that merely mentions docker run
            after = line[idx + len("docker run"):]
            if "--entrypoint" in after:
                continue   # already correct
            # no --entrypoint: is a non-vllm command used?
            if re.search(r"\b(python3?|bash|sh)\b|\s-m\s|\.sh\b|\.py\b",
                         after):
                violations.append(
                    f"{sh.name}: docker run without --entrypoint but a "
                    f"non-vllm command (line: {stripped[:120]!r})")
    assert not violations, (
        "every docker run with a non-vllm command must set --entrypoint "
        "(the vllm ENTRYPOINT image rejects it otherwise):\n"
        + "\n".join(violations))


def test_f1_docker_run_with_entrypoint_starts_server(tmp_path):
    """> F1.1+F1.3: the docker branch of start-cache.sh must run the server
    with --entrypoint python3. The stub docker has ENTRYPOINT semantics:
    without --entrypoint it rejects the command like `vllm` does (rc=2,
    "unrecognized arguments") and the "container" never starts. With the fix
    the argv recording happens (the server command is accepted)."""
    cache_root = tmp_path / "cache"
    argv, r = _start_cache_argv(tmp_path, "docker", cache_root)
    out = r.stdout + r.stderr
    assert "unrecognized arguments" not in out, (
        f"the docker run must carry --entrypoint python3 (otherwise the "
        f"vllm ENTRYPOINT swallows the command):\n{out[-800:]}")
    assert argv, f"the server must be launched:\n{out[-800:]}"
    # the recording skips the literal `server` subcommand (like the B4 tests
    # expect) -- the flags follow directly
    assert "--host" in argv and "--port" in argv, (
        f"the server argv must carry the launch flags: {argv[:8]}")
    assert not any(a == "-m" for a in argv), (
        "the docker command must be `--entrypoint python3` + args (no -m)")


# ===========================================================================
# F2: docker readiness = container alive + RPC pong
# ===========================================================================
def test_f2_dead_container_refuses_fast(tmp_path):
    """> F2 + G1: a container that was created and exited immediately ->
    start-cache.sh refuses within seconds (no READY), naming the CONTAINER
    condition, with the log tail."""
    t0 = time.monotonic()
    argv, r = _start_cache_argv(tmp_path, "docker", tmp_path / "cache",
                                require_launch=False, docker_state="exited",
                                extra_env={"KVP_CACHE_READY_TIMEOUT": "6"})
    out = r.stdout + r.stderr
    elapsed = time.monotonic() - t0
    assert r.returncode != 0, (
        f"a dead container must refuse the launch:\n{out[-800:]}")
    assert "container" in out.lower(), (
        f"the refusal must name the CONTAINER condition:\n{out[-800:]}")
    assert elapsed < 30, f"must fail fast, took {elapsed:.0f}s"


def test_f2_no_pong_refuses_by_timeout(tmp_path):
    """> F2: the container is alive but the server never answers mp_ping ->
    refusal by KVP_CACHE_READY_TIMEOUT, naming the SERVER (RPC ping)
    condition; no READY line is printed as success."""
    t0 = time.monotonic()
    argv, r = _start_cache_argv(tmp_path, "docker", tmp_path / "cache",
                                require_launch=False, docker_pong=False,
                                extra_env={"KVP_CACHE_READY_TIMEOUT": "4"})
    out = r.stdout + r.stderr
    elapsed = time.monotonic() - t0
    assert r.returncode != 0, (
        f"a server that never pongs must be refused:\n{out[-800:]}")
    assert "RPC ping" in out or "mp_ping" in out, (
        f"the refusal must name the SERVER pong condition:\n{out[-800:]}")
    assert "READY" not in out, (
        f"READY must not be printed on a timeout:\n{out[-800:]}")


def test_f2_ready_not_before_pong(tmp_path):
    """> F2: with a pong that appears only AFTER several seconds, READY must
    not be printed before the pong (the gate waits for the RPC answer, not
    for the published port)."""
    t0 = time.monotonic()
    argv, r = _start_cache_argv(tmp_path, "docker", tmp_path / "cache",
                                require_launch=False,
                                docker_pong=False, pong_delay=4.0,
                                extra_env={"KVP_CACHE_READY_TIMEOUT": "12"})
    out = r.stdout + r.stderr
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, (
        f"a pong that arrives later must still reach READY:\n{out[-800:]}")
    assert "READY" in out, f"READY must be printed eventually:\n{out[-800:]}"
    assert elapsed >= 3, (
        f"READY must not be printed before the pong (elapsed {elapsed:.1f}s)")


# ===========================================================================
# F3: ONE launch-mode resolver, default docker, refusal on bogus
# ===========================================================================
def _resolve_mode(env: dict | None = None):
    """Run the REAL _kvp_resolve_launch_mode in a shell; return (rc, out)."""
    full = (f'set -u\n. "{ROOT}/scripts/env.sh"\n'
            '_r=$(_kvp_resolve_launch_mode); _rc=$?\n'
            'echo "rc=$_rc RESOLVED=$_r"\n')
    e = dict(os.environ)
    e.pop("KVP_LAUNCH_MODE", None)
    if env:
        e.update(env)
    r = subprocess.run(["bash", "-c", full], capture_output=True,
                       text=True, timeout=30, env=e)
    return r


def test_f3_default_is_docker():
    """> F3: without KVP_LAUNCH_MODE the resolver returns docker (and the
    consumers see docker). The old split (start-cache= docker, start-all/
    pre_flights = host) was the hardware defect."""
    r = _resolve_mode()
    out = r.stdout + r.stderr
    assert "rc=0" in out and "RESOLVED=docker" in out, out


def test_f3_explicit_host_is_honoured():
    """> F3: KVP_LAUNCH_MODE=host stays host."""
    r = _resolve_mode({"KVP_LAUNCH_MODE": "host"})
    out = r.stdout + r.stderr
    assert "rc=0" in out and "RESOLVED=host" in out, out


def test_f3_bogus_mode_refuses():
    """> F3: KVP_LAUNCH_MODE=bogus -> refusal naming the allowed values."""
    r = _resolve_mode({"KVP_LAUNCH_MODE": "bogus"})
    out = r.stdout + r.stderr
    assert "rc=1" in out, (
        f"the resolver must return rc=1 on a bogus mode:\n{out[-500:]}")
    assert "host" in out and "docker" in out, (
        f"the refusal must name the allowed values:\n{out[-500:]}")


def test_f3_resolver_is_used_by_consumers():
    """> F3: start-cache.sh and start-all.sh call the ONE resolver (no
    per-script `:-host` / `:-docker` defaults left)."""
    for name in ("start-cache.sh", "start-all.sh"):
        t = (ROOT / "scripts" / name).read_text()
        assert "_kvp_resolve_launch_mode" in t, (
            f"{name} must use the shared launch-mode resolver")
        assert 'KVP_LAUNCH_MODE:-host' not in t, (
            f"{name} must not default KVP_LAUNCH_MODE to host")
    envsh = (ROOT / "scripts" / "env.sh").read_text()
    assert '_mode="${KVP_LAUNCH_MODE:-docker}"' in envsh, (
        "the resolver must default to docker")
    # pre_flights' own default must be docker too
    assert '_mode="${9:-docker}"' in envsh, (
        "_kvp_pre_flights must default its launch-mode arg to docker")


def test_f3_start_all_exports_mode_before_recipe(tmp_path):
    """> F3: start-all.sh exports the resolved KVP_LAUNCH_MODE BEFORE calling
    the recipe launcher (whose own default is host). The export happens in
    _kvp_resolve_launch_mode; assert the call site order in the script."""
    t = (ROOT / "scripts" / "start-all.sh").read_text()
    resolve_at = t.index("_kvp_resolve_launch_mode")
    recipe_at = t.index("run-tp4-hotsplit.sh")
    assert resolve_at < recipe_at, (
        "start-all.sh must resolve AND export the launch mode before "
        "running the recipe launcher")


# ===========================================================================
# E2: "refuse before launch" for the docker branch and the smoke
# ===========================================================================
def test_e2_docker_branch_bad_policy_zero_docker_run(tmp_path):
    """> E2.1: a bad LMCACHE_EVICTION_POLICY in the docker branch -> refusal
    with "refusing to start the server" and ZERO server `docker run`
    invocations. The stub logs EVERY docker run; the counter must be 0."""
    cache_root = tmp_path / "cache"
    argv, r = _start_cache_argv(tmp_path, "docker", cache_root,
                                require_launch=False,
                                extra_env={"LMCACHE_EVICTION_POLICY": "lfu"})
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"a bad policy must refuse:\n{out[-800:]}"
    assert "refusing to start the server" in out, (
        f"the refusal must come from the argv reader's caller:\n{out[-800:]}")
    # EVERY docker run is logged; count REAL server launches (not the --help probe)
    run_log = tmp_path / "bin_docker" / "docker-run.log"
    launches = 0
    if run_log.exists():
        for line in run_log.read_text().splitlines():
            if "kvpersist.launch" in line and "--help" not in line:
                launches += 1
    assert launches == 0, (
        f"the server must NOT be launched on a bad policy ({launches} "
        f"docker run invocations recorded):\n{run_log.read_text()[-500:]}")


def test_e2_smoke_bad_policy_shipped_config(tmp_path):
    """> E2.2: a SHIPPED config with LMCACHE_EVICTION_POLICY=lfu -> the smoke
    refuses: rc!=0, stderr names the variable + the four allowed values, and
    the kvpersist.launch stub is NEVER invoked. (The env variable alone does
    not influence the smoke -- the shipped config wins; the refusal path is
    the broken shipped config.)"""
    # tmp copy of the repo configs with a bad policy
    import shutil
    repo = tmp_path / "repo"
    (repo / "configs" / "generic").mkdir(parents=True)
    (repo / "configs" / "mimo-v2.6-pro").mkdir(parents=True)
    # copy the real template + cache.env
    real = ROOT / "configs" / "generic"
    for f in real.iterdir():
        if f.name != "lmcache-server.env":
            shutil.copy(f, repo / "configs" / "generic" / f.name)
    # bad shipped config
    bad = (real / "lmcache-server.env").read_text()
    bad = bad.replace("LMCACHE_EVICTION_POLICY=LRU",
                      "LMCACHE_EVICTION_POLICY=lfu")
    (repo / "configs" / "generic" / "lmcache-server.env").write_text(bad)
    # the adapter template must exist
    tpl_src = ROOT / "configs" / "mimo-v2.6-pro" / "l2-adapter.json.in"
    shutil.copy(tpl_src, repo / "configs" / "mimo-v2.6-pro" /
                "l2-adapter.json.in")
    # stub: python3 counts kvpersist.launch invocations
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    launches = tmp_path / "launches"
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *kvpersist.launch*) echo x >> "{launches}"; exit 0 ;;\n'
        "  *kvpersist.mp_ping*) exit 0 ;;\n"
        "  *check_codec_available*) exit 0 ;;\n"
        f'  *) exec "{real_py}" "$@" ;;\n'
        "esac\n")
    (bin_dir / "python3").chmod(0o755)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_SMOKE_REPO=str(repo),
               KVP_SMOKE_PORT="15575",
               KVP_SMOKE_TERM_TIMEOUT="3")
    env.pop("LMCACHE_EVICTION_POLICY", None)
    r = subprocess.run(
        ["bash", str(ROOT / "scripts" / "_smoke_cache_server.sh"),
         "zstd", str(tmp_path / "cache"), "15575"],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"a broken shipped policy must refuse the smoke:\n{out[-800:]}")
    assert "LMCACHE_EVICTION_POLICY" in out, (
        f"the refusal must name the variable:\n{out[-800:]}")
    for choice in ("LRU", "ARC", "IsolatedLRU", "noop"):
        assert choice in out, (
            f"the refusal must name the allowed value {choice}:\n{out[-800:]}")
    assert not launches.exists(), (
        f"the kvpersist.launch stub must NOT be invoked:\n{out[-800:]}")


def test_e2_direct_argv_read_empty_and_malformed():
    """> E2.3 (D4-5): _kvp_server_argv_read with a stubbed _kvp_server_argv
    that prints nothing (rc=0) or prints `foo` (rc=0) -> rc=1 + "empty or
    malformed". Both shapes are swallowed by the OLD process-substitution
    call and only this validation catches them."""
    for payload in ("", "foo"):
        script = (
            "set -u\n"
            f'. "{ROOT}/scripts/env.sh"\n'
            # stub the argv builder: succeed with the given payload
            "_kvp_server_argv() {\n"
            f"  printf '%s' \"{payload}\"\n"
            "  return 0\n"
            "}\n"
            "ARR=()\n"
            "_kvp_server_argv_read ARR h 5555 4 256 /tmp/nonexistent.json \"\"\n"
            "echo rc=$?\n"
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=30, env=dict(os.environ))
        out = r.stdout + r.stderr
        assert "rc=1" in out, (
            f"payload={payload!r} must be refused by the argv reader:\n{out}")
        assert "empty or malformed" in out, (
            f"the refusal must say 'empty or malformed':\n{out}")


# ===========================================================================
# E3: build-image.sh identity-dir regression test
# ===========================================================================
def test_e3_build_image_identity_dir_created(tmp_path):
    """> E3: KVP_IDENTITY_OUT and KVP_LIVE_DIR point at NON-EXISTENT dirs ->
    build-image.sh must still succeed (rc=0), create both, and live/image
    must hold sha256:. The mutation D2-1 (drop the mkdir) makes this RED."""
    patch_dir, build_dir, out_dir, _p4, _p5, _whl = make_fixture(tmp_path)
    missing_identity = tmp_path / "no" / "such" / "dir" / "image-identity.env"
    missing_live = tmp_path / "no" / "such" / "live"
    assert not missing_identity.parent.exists()
    assert not missing_live.exists()
    r, _store = _run_build_image(
        tmp_path, patch_dir, build_dir, out_dir,
        extra_env={"KVP_IDENTITY_OUT": str(missing_identity),
                   "KVP_LIVE_DIR": str(missing_live)})
    assert r.returncode == 0, (
        f"build-image must create the identity dir itself:\n"
        f"{r.stdout[-800:]}\n{r.stderr[-800:]}")
    assert missing_identity.is_file(), (
        f"{missing_identity} must be created")
    assert missing_live.is_dir(), f"{missing_live} must be created"
    image_file = missing_live / "image"
    assert image_file.is_file(), "live/image must be recorded"
    assert image_file.read_text().strip().startswith("sha256:"), (
        f"live/image must hold sha256:..., got {image_file.read_text()!r}")


# ===========================================================================
# E4.1: KVP_SMOKE_TERM_TIMEOUT validation
# ===========================================================================
def test_e4_smoke_term_timeout_must_be_integer():
    """> E4.1: a non-numeric KVP_SMOKE_TERM_TIMEOUT -> clear error + exit 1
    BEFORE anything starts (the old code let `seq` fail silently and then
    lied "did NOT exit within Xs")."""
    for bad in ("abc", "", "0", "-3", "1.5"):
        bin_dir = pathlib.Path("/tmp") / f"e4bin_{abs(hash(bad)) % 10**8}"
        bin_dir.mkdir(exist_ok=True)
        real_py = subprocess.run(["which", "python3"], capture_output=True,
                                 text=True).stdout.strip()
        (bin_dir / "python3").write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            "  *kvpersist.launch*) sleep 360 & wait $! 2>/dev/null; exit 0 ;;\n"
            "  *kvpersist.mp_ping*) exit 0 ;;\n"
            "  *check_codec_available*) exit 0 ;;\n"
            f'  *) exec "{real_py}" "$@" ;;\n'
            "esac\n")
        (bin_dir / "python3").chmod(0o755)
        env = dict(os.environ,
                   PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                   KVP_SMOKE_REPO=str(ROOT),
                   KVP_SMOKE_PORT="15576",
                   KVP_SMOKE_TERM_TIMEOUT=bad)
        r = subprocess.run(
            ["bash", str(ROOT / "scripts" / "_smoke_cache_server.sh"),
             "zstd", "/tmp/e4-cache", "15576"],
            capture_output=True, text=True, timeout=30, env=env,
            cwd="/tmp")
        out = r.stdout + r.stderr
        assert r.returncode != 0, (
            f"KVP_SMOKE_TERM_TIMEOUT={bad!r} must be refused:\n{out[-400:]}")
        assert "KVP_SMOKE_TERM_TIMEOUT" in out, (
            f"the error must name the variable:\n{out[-400:]}")
        shutil.rmtree(bin_dir, ignore_errors=True)