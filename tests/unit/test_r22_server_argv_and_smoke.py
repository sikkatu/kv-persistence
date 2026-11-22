# SPDX-License-Identifier: Apache-2.0
"""r22 B1-B6: the server command the smoke runs must be the command the pin
accepts (hardware log 21 Nov 2026: `--eviction-policy: invalid choice: 'lru'`,
then the multi-line adapter JSON split into argv elements, then exit 143
treated as unclean).

Spec fixture: tests/unit/pin_server_spec.json -- extracted from the PIN
source (07f1c6bb) by gen_pin_spec.py (lmcache/v1/{multiprocess,distributed,
distributed/l2_adapters,mp_observability}/config.py, AST-walked for
add_argument). NEVER hand-typed.

B1: default `LRU`, normalization (`lru`->LRU, `isolatedlru`->IsolatedLRU),
    refusal for unknown values naming the four choices.
B2: mp_ping imports `lmcache.v1.client.transport.factory` (the pin path),
    falls back to the legacy path, code 3 when neither.
B3: the smoke loads the SHIPPED configs (chunk-size 256 etc), not defaults.
B4: argv is NUL-separated, --l2-adapter is a ONE-LINE compact JSON, parsed by
    the pin's argparse spec (action=append) -> exactly 17 elements, no
    unrecognized args, the JSON value equals the rendered file (modulo
    whitespace).
B5: clean-exit criterion: code 0 OR 143 + both shutdown markers
    (`MPCacheServer closed`, `persist_enabled=True, keeping data files on
    disk`). Any other code or missing markers -> refusal.
B6: the smoke uses its own port (15555), refuses when the fd-channel socket
    is held by a live server.
"""
from __future__ import annotations
import argparse
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
IMPL = ROOT / "implementation"
sys.path.insert(0, str(IMPL))
SPEC = json.loads((ROOT / "tests" / "unit" / "pin_server_spec.json").read_text())

# flatten the spec: flag -> {type, choices, default}
FLAGS = {}
for sec, v in SPEC.items():
    for f in v["flags"]:
        for fl in f["flags"]:
            FLAGS[fl] = f


def _envsh_run(script: str, env: dict | None = None, cwd: pathlib.Path | None = None):
    """Run a bash snippet in a shell that sources the REAL env.sh."""
    full = f'set -u\n. "{ROOT}/scripts/env.sh"\n{script}'
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(["bash", "-c", full], capture_output=True,
                          text=True, timeout=60, env=e,
                          cwd=str(cwd or ROOT))


def _parse_like_pin(argv: list[str]):
    """Parse the recorded argv with a parser built FROM THE PIN SPEC:
    --l2-adapter is `action=append` (lmcache/v1/distributed/l2_adapters/
    config.py:443), all other flags use the spec's type/choices. Returns
    (namespace, leftovers) -- leftovers must be EMPTY in every consumer."""
    p = argparse.ArgumentParser(exit_on_error=False)
    for fl, meta in FLAGS.items():
        kw = {}
        if fl == "--l2-adapter":
            kw["action"] = "append"
        if meta.get("choices"):
            kw["choices"] = meta["choices"]
        tname = meta.get("type")
        if tname == "int":
            kw["type"] = int
        elif tname == "float":
            kw["type"] = float
        p.add_argument(fl, **kw)
    try:
        return p.parse_known_args(argv)
    except SystemExit as e:  # argparse calls sys.exit on a bad value
        return argparse.Namespace(), [f"argparse exit {e.code}"]


# ---------------------------------------------------------------------------
# B1: eviction-policy default / normalization / refusal
# ---------------------------------------------------------------------------
def test_b1_eviction_policy_default_is_LRU():
    """> LMCACHE_EVICTION_POLICY unset -> LRU (the pin's canonical spelling)."""
    out = _envsh_run('unset LMCACHE_EVICTION_POLICY\n'
                     'echo "$(_kvp_normalize_eviction_policy "")"',
                     env={"LMCACHE_EVICTION_POLICY": ""})
    assert out.stdout.strip() == "LRU", out.stdout + out.stderr


def test_b1_eviction_policy_normalizes():
    """> lru -> LRU, Lru -> LRU, isolatedlru -> IsolatedLRU, NOOP -> noop."""
    for src, want in (("lru", "LRU"), ("Lru", "LRU"),
                      ("isolatedlru", "IsolatedLRU"), ("NOOP", "noop"),
                      ("LRU", "LRU"), ("ARC", "ARC")):
        out = _envsh_run(f'echo "$(_kvp_normalize_eviction_policy "{src}")"',
                         env={"LMCACHE_EVICTION_POLICY": ""})
        assert out.stdout.strip() == want, f"{src}: {out.stdout!r}"


def test_b1_eviction_policy_unknown_refuses():
    """> bogus -> refusal naming LMCACHE_EVICTION_POLICY and the 4 choices."""
    out = _envsh_run('_kvp_normalize_eviction_policy bogus', env={"LMCACHE_EVICTION_POLICY": ""})
    assert out.returncode != 0, "an unknown policy must be refused"
    err = out.stdout + out.stderr
    assert "LMCACHE_EVICTION_POLICY" in err, err[-500:]
    for choice in ("LRU", "ARC", "IsolatedLRU", "noop"):
        assert choice in err, f"the refusal must name {choice}: {err[-500:]}"


def test_b1_mutation_default_lru_is_red():
    """> Mutation: default back to `lru` -> the LRU test above must catch it."""
    src = (ROOT / "scripts" / "env.sh").read_text()
    assert '${LMCACHE_EVICTION_POLICY:-LRU}' in src, "the default must be LRU"
    # the mutant: lru default -> the pin parser rejects it
    mutant = src.replace('${LMCACHE_EVICTION_POLICY:-LRU}',
                         '${LMCACHE_EVICTION_POLICY:-lru}')
    assert mutant != src
    # the normalization should still rescue the lowercase default -- so the
    # REAL defense is the normalize step; drop it too and the mutant fails
    mutant2 = mutant.replace('    lru) printf \'LRU\' ;;\n', '')
    assert mutant2 != mutant
    out = subprocess.run(["bash", "-c",
                          'set -u\n. /dev/stdin\n_kvp_server_argv h p 1 1 /tmp/nonexist.json ""'],
                         input=mutant2, capture_output=True, text=True, timeout=30,
                         env=dict(os.environ, LMCACHE_EVICTION_POLICY=""))
    assert out.returncode != 0, (
        "with default lru and no normalization the argv build must fail")
# ---------------------------------------------------------------------------
# B2: mp_ping imports the PIN path first, falls back, code 3 when neither
# ---------------------------------------------------------------------------
def _make_lmcache_fake(base: pathlib.Path, path: str) -> pathlib.Path:
    """Create a fake lmcache package exposing RequestClientFactory at `path`
    (dotted). Returns the root to put on PYTHONPATH."""
    parts = path.split(".")
    root = base / ("_fake_" + path.replace(".", "_"))
    d = root
    for p in parts[:-1]:
        d = d / p
        (d).mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("")
    root.mkdir(exist_ok=True)
    # top-level lmcache package init
    top = root / "lmcache"
    top.mkdir(exist_ok=True)
    (top / "__init__.py").write_text("")
    for p in parts[1:-1]:
        top = top / p
        top.mkdir(exist_ok=True)
        (top / "__init__.py").write_text("")
    # factory.py with RequestClientFactory
    (top / "factory.py").write_text('''
class _Future:
    def __init__(self, v): self._v = v
    def result(self, timeout=None): return self._v


class _Client:
    def __init__(self, pong): self._pong = pong
    def ping(self, x):
        if self._pong == "RAISE":
            raise RuntimeError("no pong")
        return _Future(self._pong)
    def close(self): pass


class RequestClientFactory:
    @staticmethod
    def create(url, *, context=None, api_key=None):
        import os
        pong = os.environ.get("FAKE_PONG", "True")
        return _Client(pong)
''')
    return root


def _run_mp_ping(fake_root: pathlib.Path | None, pong: str = "True") -> int:
    code = (
        "import sys; sys.path.insert(0, r'" + str(IMPL) + "')\n"
        "import kvpersist.mp_ping as m\n"
        "sys.argv = ['mp_ping', '127.0.0.1', '5555', '2']\n"
        "raise SystemExit(m.main())\n"
    )
    env = dict(os.environ, FAKE_PONG=pong)
    if fake_root:
        env["PYTHONPATH"] = str(fake_root) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=30, env=env)
    return r.returncode


def test_b2_mp_ping_pin_path_pong(tmp_path):
    """> Fake on the PIN path (lmcache.v1.client.transport.factory): pong -> 0."""
    fake = _make_lmcache_fake(tmp_path, "lmcache.v1.client.transport.factory")
    assert _run_mp_ping(fake, "True") == 0, "pong via the pin path must be 0"


def test_b2_mp_ping_pin_path_no_pong(tmp_path):
    """> Fake on the PIN path, ping raises -> 2 (no pong)."""
    fake = _make_lmcache_fake(tmp_path, "lmcache.v1.client.transport.factory")
    assert _run_mp_ping(fake, "RAISE") == 2, "no pong must be 2"


def test_b2_mp_ping_legacy_path_still_works(tmp_path):
    """> Fake on the LEGACY path (lmcache.v1.multiprocess.transport.factory):
    fallback works -> 0."""
    fake = _make_lmcache_fake(tmp_path, "lmcache.v1.multiprocess.transport.factory")
    assert _run_mp_ping(fake, "True") == 0, "legacy path fallback must work"


def test_b2_mp_ping_neither_path_is_3(tmp_path):
    """> No RequestClientFactory anywhere -> exit 3 (skipped)."""
    assert _run_mp_ping(None) == 3, "no lmcache must be code 3"


# ---------------------------------------------------------------------------
# B3: the smoke loads the SHIPPED configs (chunk-size 256), not defaults
# ---------------------------------------------------------------------------
def _read_argv_via_stub(argv_file: pathlib.Path, script: str,
                        args: list[str], env: dict | None = None):
    """Run a REAL script with a python3 stub that records `kvpersist.launch`
    argv (NUL-separated) to argv_file and touches a `ready` file ONLY after
    the argv is fully written and the SIGTERM trap is installed (r23 D1:
    the mp_ping stub waits for `ready` -- no launch/SIGTERM race)."""
    bin_dir = argv_file.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    ready = argv_file.parent / "ready"
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    stub = bin_dir / "python3"
    stub.write_text(f'''#!/usr/bin/env bash
case "$*" in
  *kvpersist.launch*)
    : > "{argv_file}"
    # r23 D1/D2: record the argv STARTING at "server" (skip the interpreter's
    # own `-m kvpersist.launch` and any --help probe) -- the consumers parse
    # the recorded list as ("server" --flag val ...).
    seen=0
    for a in "$@"; do
      if [ "$a" = "server" ]; then seen=1; fi
      [ "$seen" = 1 ] || continue
      case "$a" in --help) continue ;; esac
      printf '%s\\0' "$a" >> "{argv_file}"
    done
    trap 'echo "MPCacheServer closed"; echo "persist_enabled=True, keeping data files on disk"; exit 143' TERM
    touch "{ready}"
    sleep 5 &
    wait $! 2>/dev/null
    echo "MPCacheServer closed"; echo "persist_enabled=True, keeping data files on disk"
    exit 0 ;;
  *kvpersist.mp_ping*)
    # r23 D1: readiness = the launch stub finished writing argv + trap
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [ -f "{ready}" ] && exit 0
      sleep 1
    done
    exit 2 ;;
  *check_codec_available*) exit 0 ;;
  *) exec "{real_py}" "$@" ;;
esac
''')
    stub.chmod(0o755)
    e = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
    if env:
        e.update(env)
    r = subprocess.run(["bash", str(script), *args], capture_output=True,
                       text=True, timeout=120, env=e, cwd=str(argv_file.parent))
    return r


def _argv_from_file(argv_file: pathlib.Path) -> list[str]:
    raw = argv_file.read_bytes()
    return [a.decode() for a in raw.split(b"\0") if a != b""]


def test_b3_b4_smoke_uses_shipped_configs_and_parses_like_pin(tmp_path):
    """> The smoke's argv: chunk-size 256 (from the shipped config), the
    --l2-adapter value is ONE element whose JSON equals the rendered file,
    and the whole argv parses with the PIN's parser with zero leftovers."""
    argv_file = tmp_path / "argv.bin"
    smoke = ROOT / "scripts" / "_smoke_cache_server.sh"
    # KVP_SMOKE_REPO points at the real repo (configs live there)
    r = _read_argv_via_stub(argv_file, str(smoke),
                            ["zstd", str(tmp_path / "cache"), "15555"],
                            env={"KVP_SMOKE_REPO": str(ROOT),
                                 "KVP_SMOKE_PORT": "15555"})
    assert argv_file.exists(), f"the smoke must start the server:\n{r.stdout}\n{r.stderr}"
    argv = _argv_from_file(argv_file)
    # pin-spec parse: exactly one --l2-adapter, zero unrecognized
    ns, leftovers = _parse_like_pin(argv[1:])  # drop the leading "server"
    assert leftovers == [], (
        f"the pin's parser must consume the argv entirely, leftovers: {leftovers}")
    assert ns.l2_adapter is not None and len(ns.l2_adapter) == 1, (
        f"--l2-adapter must arrive as ONE element: {ns.l2_adapter}")
    assert ns.chunk_size == 256, (
        f"the smoke must use the SHIPPED chunk-size (256), got {ns.chunk_size}")
    assert ns.eviction_policy == "LRU", (
        f"the smoke must use the shipped policy LRU, got {ns.eviction_policy}")
    # the adapter JSON value equals the rendered file (modulo whitespace)
    rendered = json.loads((tmp_path / "cache" / "l2-adapter.smoke.json").read_text())
    assert json.loads(ns.l2_adapter[0]) == rendered, (
        "the --l2-adapter value must equal the rendered adapter JSON")
    # element count: 17 base (server + 8 flag/value pairs + adapter) plus the
    # optional --http-port pair from the shipped config (LMCACHE_HTTP_PORT=8081)
    assert len(argv) == 19, f"expected 19 argv elements (17 + http-port), got {len(argv)}: {argv}"
    assert "--http-port" in argv and argv[argv.index("--http-port") + 1] == "8081", (
        "the shipped LMCACHE_HTTP_PORT must reach the argv")


def test_b3_smoke_chunk_size_not_1(tmp_path):
    """> chunk-size comes from the config (256), never the old hard-coded 1."""
    argv_file = tmp_path / "argv.bin"
    smoke = ROOT / "scripts" / "_smoke_cache_server.sh"
    _read_argv_via_stub(argv_file, str(smoke),
                        ["none", str(tmp_path / "cache"), "15555"],
                        env={"KVP_SMOKE_REPO": str(ROOT),
                             "KVP_SMOKE_PORT": "15555"})
    argv = _argv_from_file(argv_file)
    i = argv.index("--chunk-size")
    assert argv[i + 1] == "256", f"chunk-size must be 256, got {argv[i + 1]}"

# ---------------------------------------------------------------------------
# B5: clean-exit criterion (code 0 or 143 + both shutdown markers)
# ---------------------------------------------------------------------------
def _run_smoke_with_server_stub(tmp_path: pathlib.Path, *, exit_code: int,
                                closed: bool = True, persist: bool = True,
                                hang: bool = False):
    """Run the REAL _smoke_cache_server.sh with a python3 stub whose server
    behaves like the real one. r23 D5.1: `closed` (MPCacheServer closed) and
    `persist` (persist_enabled=True, keeping data files on disk) are separate;
    `hang` = the stub ignores SIGTERM (trap '' TERM). The `ready` file is
    touched only after the trap is installed and the markers are armed (r23
    D1: no SIGTERM race)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ready = tmp_path / "ready"
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    m_closed = 'echo "MPCacheServer closed"' if closed else 'true'
    m_persist = ('echo "persist_enabled=True, keeping data files on disk"'
                 if persist else 'true')
    if hang:
        trap_line = "trap '' TERM"
        tail = "sleep 360 & wait $! 2>/dev/null; exit 0"
    else:
        trap_line = (f"trap '{m_closed}; {m_persist}; exit {exit_code}' TERM")
        tail = f"{m_closed}; {m_persist}; exit {exit_code}"
    stub = bin_dir / "python3"
    stub.write_text(f'''#!/usr/bin/env bash
case "$*" in
  *kvpersist.launch*)
    {trap_line}
    touch "{ready}"
    sleep 360 &
    wait $! 2>/dev/null
    {tail} ;;
  *kvpersist.mp_ping*)
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [ -f "{ready}" ] && exit 0
      sleep 1
    done
    exit 2 ;;
  *check_codec_available*) exit 0 ;;
  *) exec "{real_py}" "$@" ;;
esac
''')
    stub.chmod(0o755)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_SMOKE_REPO=str(ROOT),
               KVP_SMOKE_PORT="15556",
               KVP_SMOKE_TERM_TIMEOUT="3")
    for var in ("LMCACHE_EVICTION_POLICY",):
        env.pop(var, None)
    r = subprocess.run(["bash", str(ROOT / "scripts" / "_smoke_cache_server.sh"),
                        "zstd", str(tmp_path / "cache"), "15556"],
                       capture_output=True, text=True, timeout=120, env=env,
                       cwd=str(tmp_path))
    return r


def test_b5_exit_143_with_markers_passes(tmp_path):
    """> The real server exits 143 after SIGTERM with both shutdown markers
    -> the smoke PASSES and prints EXIT=0 (143 is NORMAL for this server)."""
    r = _run_smoke_with_server_stub(tmp_path, exit_code=143, closed=True, persist=True)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"143 + both markers must pass:\n{out[-800:]}"
    assert "KVP_SMOKE_ZSTD_EXIT=0" in out, out[-800:]


def test_b5_exit_0_with_markers_passes(tmp_path):
    """> Exit code 0 with both markers -> passes."""
    r = _run_smoke_with_server_stub(tmp_path, exit_code=0, closed=True, persist=True)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"0 + both markers must pass:\n{out[-800:]}"
    assert "KVP_SMOKE_ZSTD_EXIT=0" in out, out[-800:]


def test_b5_exit_143_without_markers_refuses(tmp_path):
    """> Exit 143 but the shutdown markers are MISSING -> refusal (the
    markers prove the server closed cleanly and kept the L2 data)."""
    r = _run_smoke_with_server_stub(tmp_path, exit_code=143, closed=False, persist=False)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"143 without markers must be refused:\n{out[-800:]}"
    assert "MPCacheServer closed" in out, (
        f"the refusal must name the missing marker:\n{out[-800:]}")
    assert "KVP_SMOKE_ZSTD_EXIT=0" not in out, out[-800:]


def test_b5_exit_137_refuses(tmp_path):
    """> Exit 137 (killed by the timeout) -> refusal."""
    r = _run_smoke_with_server_stub(tmp_path, exit_code=137, closed=True, persist=True)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"137 must be refused:\n{out[-800:]}"
    assert "137" in out, out[-800:]


def test_b5_exit_1_refuses(tmp_path):
    """> Exit code 1 -> refusal."""
    r = _run_smoke_with_server_stub(tmp_path, exit_code=1, closed=True, persist=True)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"exit 1 must be refused:\n{out[-800:]}"


# ---------------------------------------------------------------------------
# B6: the smoke port is separate from the production cache port
# ---------------------------------------------------------------------------
def test_b6_smoke_port_not_the_production_port():
    """> The smoke's default port (15555) must NOT equal the production
    LMCACHE_PORT (5555) -- otherwise the fd-channel sockets collide."""
    prod = (ROOT / "configs" / "generic" / "lmcache-server.env").read_text()
    m = re.search(r"^LMCACHE_PORT=(\d+)", prod, re.M)
    assert m, "the production config must pin LMCACHE_PORT"
    prod_port = int(m.group(1))
    smoke = (ROOT / "scripts" / "_smoke_cache_server.sh").read_text()
    bm = re.search(r"KVP_SMOKE_PORT:-([0-9]+)", smoke)
    assert bm, "the smoke must have a KVP_SMOKE_PORT default"
    assert int(bm.group(1)) != prod_port, (
        f"the smoke port must differ from the production port {prod_port}")


def test_b6_live_fd_socket_refuses_with_port_number(tmp_path):
    """> A LIVE AF_UNIX SOCK_SEQPACKET listener on /dev/shm/lmcache_fd_<port>.sock
    (the pinned lmcache fd-channel type) -> the smoke refuses with the port
    number in the message."""
    import socket
    port = 15557
    sock_path = f"/dev/shm/lmcache_fd_{port}.sock"
    if pathlib.Path(sock_path).exists():
        pathlib.Path(sock_path).unlink()
    # r23 D3: the pinned lmcache fd-channel socket is AF_UNIX
    # SOCK_SEQPACKET (fd_channel.py:14,53,92) -- the probe must match
    # the REAL type, not SOCK_STREAM.
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    srv.bind(sock_path)
    srv.listen(1)
    try:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        real_py = subprocess.run(["which", "python3"], capture_output=True,
                                 text=True).stdout.strip()
        (bin_dir / "python3").write_text(f'''#!/usr/bin/env bash
exec "{real_py}" "$@"
''')
        (bin_dir / "python3").chmod(0o755)
        env = dict(os.environ,
                   PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                   KVP_SMOKE_REPO=str(ROOT))
        r = subprocess.run(["bash", str(ROOT / "scripts" / "_smoke_cache_server.sh"),
                            "zstd", str(tmp_path / "cache"), str(port)],
                           capture_output=True, text=True, timeout=60, env=env,
                           cwd=str(tmp_path))
        out = r.stdout + r.stderr
        assert r.returncode != 0, f"a live fd socket must be refused:\n{out[-800:]}"
        assert str(port) in out, (
            f"the refusal must name the port {port}:\n{out[-800:]}")
    finally:
        srv.close()
        pathlib.Path(sock_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# B4 robustness: a cache path with a space and a quote arrives as ONE element
# ---------------------------------------------------------------------------
def test_b4_cache_path_with_space_and_quote_is_one_element(tmp_path):
    """> A cache root with a space and a quote reaches the --l2-adapter JSON
    as a single element (the JSON value carries it, never split)."""
    weird = tmp_path / "cache with space and 'quote'"
    weird.mkdir()
    argv_file = tmp_path / "argv.bin"
    smoke = ROOT / "scripts" / "_smoke_cache_server.sh"
    r = _read_argv_via_stub(argv_file, str(smoke),
                            ["zstd", str(weird), "15558"],
                            env={"KVP_SMOKE_REPO": str(ROOT),
                                 "KVP_SMOKE_PORT": "15558"})
    assert argv_file.exists(), f"the smoke must start the server:\n{r.stdout}\n{r.stderr}"
    argv = _argv_from_file(argv_file)
    ns, leftovers = _parse_like_pin(argv[1:])
    assert leftovers == [], leftovers
    assert ns.l2_adapter and len(ns.l2_adapter) == 1, ns.l2_adapter
    parsed = json.loads(ns.l2_adapter[0])
    assert "cache with space and 'quote'" in json.dumps(parsed), (
        f"the weird path must survive into the adapter JSON: {parsed}")

# ---------------------------------------------------------------------------
# B2: smoke stops IMMEDIATELY when mp_ping exits 3 (not 60 s of no-pong)
# ---------------------------------------------------------------------------
def test_b2_smoke_stops_immediately_on_mp_ping_exit3(tmp_path):
    """> mp_ping exits 3 (lmcache not importable) -> the smoke stops at once
    with the right message, NOT 60s of "no pong"."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    (bin_dir / "python3").write_text(f'''#!/usr/bin/env bash
case "$*" in
  *kvpersist.launch*) sleep 360 & wait $! 2>/dev/null; exit 0 ;;
  *kvpersist.mp_ping*) exit 3 ;;
  *) exec "{real_py}" "$@" ;;
esac
''')
    (bin_dir / "python3").chmod(0o755)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_SMOKE_REPO=str(ROOT),
               KVP_SMOKE_PORT="15559")
    import time
    t0 = time.monotonic()
    r = subprocess.run(["bash", str(ROOT / "scripts" / "_smoke_cache_server.sh"),
                        "zstd", str(tmp_path / "cache"), "15559"],
                       capture_output=True, text=True, timeout=120, env=env,
                       cwd=str(tmp_path))
    elapsed = time.monotonic() - t0
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"mp_ping exit 3 must refuse:\n{out[-800:]}"
    assert "not importable" in out or "exit 3" in out, (
        f"the refusal must say lmcache is not importable:\n{out[-800:]}")
    assert elapsed < 15, (
        f"the smoke must stop immediately on exit 3, took {elapsed:.0f}s:\n{out[-800:]}")
    assert "no pong" not in out, (
        "the misleading 'no pong' message must NOT appear on exit 3")

# ---------------------------------------------------------------------------
# B4: the REAL consumers (start-cache.sh docker + host, smoke) all deliver a
# ONE-ELEMENT --l2-adapter -- and the argv parity smoke == docker start-cache
# ---------------------------------------------------------------------------
def _free_port() -> int:
    """r25 G4: a FREE port for every stub run (the fixed 15562 kept a
    running fake_server bound -- the next run failed with
    'Address already in use')."""
    import socket as _s
    s = _s.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _kill_stub_servers(tmp_path: pathlib.Path):
    """r25 G4: stop every fake_server this helper spawned (they live for
    30s when start-cache.sh refuses before SIGTERM -- that held the port)."""
    me = str(tmp_path)
    me_bytes = me.encode()
    for proc in pathlib.Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes()
        except OSError:
            continue
        if b"fake_server.py" in cmd and me_bytes in cmd:
            try:
                os.kill(int(proc.name), 9)
            except OSError:
                pass


def _start_cache_argv(tmp_path: pathlib.Path, mode: str, cache_root: pathlib.Path,
                      extra_env: dict | None = None,
                      require_launch: bool = True,
                      docker_state: str = "running",
                      run_dies: bool = False,
                      create_delay: float = 0.0,
                      docker_pong: bool = True,
                      pong_delay: float = 0.0,
                      ping_slow: bool = False,
                      port: int | None = None):
    """docker_state: running | exited | absent | created_late (G1: the
    container appears only create_delay seconds after `docker run`);
    run_dies: the docker-run client exits at once without creating it."""
    argv_file = tmp_path / f"argv_{mode}.bin"
    bin_dir = tmp_path / f"bin_{mode}"
    bin_dir.mkdir(exist_ok=True)
    # r25 G4: a free port per run + a finalizer that kills the stub servers
    # even when start-cache.sh refuses before SIGTERM (they sleep 30s).
    port = port if port is not None else _free_port()
    try:
        return _start_cache_argv_inner(
            tmp_path, mode, cache_root, argv_file, bin_dir, port,
            extra_env=extra_env, require_launch=require_launch,
            docker_state=docker_state, run_dies=run_dies,
            create_delay=create_delay,
            docker_pong=docker_pong, pong_delay=pong_delay,
            ping_slow=ping_slow)
    finally:
        _kill_stub_servers(tmp_path)


def _start_cache_argv_inner(tmp_path, mode, cache_root, argv_file, bin_dir,
                            port, *, extra_env, require_launch,
                            docker_state, run_dies, create_delay,
                            docker_pong, pong_delay, ping_slow):
    """Run the REAL start-cache.sh (docker/host) with stubs recording the
    `kvpersist.launch server` argv and a fake server that holds the request
    port and shuts down cleanly on SIGTERM. r23 D2.1: the helper owns its
    KVP_LIVE_DIR (image + image-identity.env written there -- NEVER the repo
    live/); r23 D1: the `ready` file is touched only after listen() + trap."""
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    (live / "image-identity.env").write_text(
        "KVP_LMC_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb\n")
    # r25 G4: the readiness marker lives in THIS helper's tmp_path -- callers
    # that run the helper twice give each run its own tmp dir, so a stale
    # marker of a former run can never prove readiness of the current one.
    ready = tmp_path / "ready"
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    # fake server: bind the request port, exit 143 + markers on SIGTERM
    (bin_dir / "fake_server.py").write_text('''
import signal, socket, sys, time
port = int(sys.argv[1])
ready = sys.argv[2]
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(5)

def _term(*a):
    print("MPCacheServer closed")
    print("persist_enabled=True, keeping data files on disk")
    sys.exit(143)

signal.signal(signal.SIGTERM, _term)
open(ready, "w").close()
time.sleep(30)
print("MPCacheServer closed")
print("persist_enabled=True, keeping data files on disk")
sys.exit(143)
''')
    record = f'''
    : > "{argv_file}"
    seen=0
    _port=5555
    for a in "$@"; do
      if [ "$a" = "server" ]; then seen=1; continue; fi
      [ "$seen" = 1 ] || continue
      printf '%s\\0' "$a" >> "{argv_file}"
      if [ "$prev" = "--port" ]; then _port="$a"; fi
      prev="$a"
    done
    exec "{real_py}" "{bin_dir}/fake_server.py" "$_port" "{ready}"'''
    (bin_dir / "python3").write_text(f'''#!/usr/bin/env bash
case "$*" in
  *kvpersist.launch*server*--help*) echo "--http-host --http-port --otlp-endpoint --enable-tracing" ;;
  *kvpersist.launch*server*)
    prev=""
    {record} ;;
  *kvpersist.mp_ping*)
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [ -f "{ready}" ] && exit 0
      sleep 1
    done
    exit 2 ;;
  *check_codec_available*) exit 0 ;;
  *) exec "{real_py}" "$@" ;;
esac
''')
    (bin_dir / "python3").chmod(0o755)
    # docker stub: answer the K1 resolver (labels + identity of a REAL build)
    import hashlib
    patch_dir = ROOT / "vendor" / "patches" / "lmcache"

    def _sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    labels = json.dumps({
        "org.kvp.lmc-base": ("vllm/vllm-openai:nightly-8e2beaa3bff595577a069d49d"
                             "96bde9074294ec2@sha256:167ccd8696bc09f1b63301185134"
                             "e04b883623f8adcbd8e3a0ec3140a193abcf"),
        "org.kvp.lmc-patch-4136": _sha(patch_dir / "4136-mp-retrieve-abort.patch"),
        "org.kvp.lmc-patch-5972": _sha(patch_dir / "5972-incomplete-kv-objects.patch"),
    })
    wv = "0.5.8rc1.post48+kvp.60dce759ea93"
    ident = json.dumps({
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr4136_patch_sha256": _sha(patch_dir / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(patch_dir / "5972-incomplete-kv-objects.patch"),
        "wheel": f"lmcache-{wv}-cp312-cp312-linux_x86_64.whl",
        "wheel_sha256": "a" * 64,
        "wheel_version": wv,
    })
    (bin_dir / "docker").write_text(f'''#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
_all="$*"
case "$cmd" in
  image)
    sub="${{1:-}}"; shift || true
    case "$sub" in
      inspect) echo '{labels}' ;;
    esac ;;
  run)
    # r24 F1/F2/E2.1: the image ENTRYPOINT is `vllm` (vllm/vllm-openai).
    #   * --entrypoint is REQUIRED for any non-vllm command: without it the
    #     args go to `vllm`, which rejects them and the container dies (the
    #     hardware defect F1).
    #   * EVERY docker run is logged (any args) so tests can assert "0 server
    #     launches" on a refusal (E2.1).
    echo "$*" >> "{bin_dir}/docker-run.log"
    case " $* " in
      *" --entrypoint "*) : ;;
      *)
        case "$_all" in
          serve|chat|complete|api|serve*) : ;;
          *)
            # r24 F1.3: this is exactly the hardware failure -- the vllm
            # ENTRYPOINT rejects the command and the container never runs.
            echo "vllm: error: unrecognized arguments: $_all" >&2
            touch "{bin_dir}/dead-marker"
            exit 2 ;;
        esac ;;
    esac
    case "$_all" in
      *kvpersist.launch*server*--help*) echo "--http-host --http-port --otlp-endpoint --enable-tracing" ;;
      *kvpersist.launch*server*)
        # r25 G1: `run-dies` = the docker-run client fails BEFORE creating
        # the container (e.g. "could not select device driver") -- exit 125.
        if [ -f "{bin_dir}/run-dies" ]; then
          echo "docker: Error response from daemon: could not select device driver \"nvidia\" with capabilities: [[gpu]]" >&2
          exit 125
        fi
        prev=""
        {record} ;;
      *--entrypoint*cat*) cat <<'IDENTEOF'
{ident}
IDENTEOF
        exit 0 ;;
      *importlib.metadata*) echo "{wv}"; exit 0 ;;
      *find_spec*) exit 0 ;;
      *check_codec_available*) exit 0 ;;
    esac
    exit 0 ;;
  inspect)
    # r25 G1: the readiness gate asks for {{.State.Status}} (absent when the
    # container was never created; exited/dead refuse at once; created/
    # running -> continue to the ping). Markers (files in bin_dir):
    #   dead-marker     -> exited   (created and stopped at once)
    #   run-dies        -> absent   (docker run failed before creating it)
    #   appear-after-N  -> absent until N seconds after start, then running
    for a in "$@"; do
      case "$a" in
        *State.Status*|*State.Running*)
          _st=running
          if [ -f "{bin_dir}/run-dies" ]; then
            _st=absent
          elif [ -f "{bin_dir}/dead-marker" ]; then
            _st=exited
          elif [ -f "{bin_dir}/appear-at" ]; then
            _at=$(cat "{bin_dir}/appear-at")
            if [ "$(date +%s)" -lt "$_at" ]; then _st=absent; fi
          fi
          case "$a" in
            *State.Running*)
              if [ "$_st" = running ]; then echo true; else echo false; fi ;;
            *)
              if [ "$_st" = absent ]; then exit 1; fi
              echo "$_st" ;;
          esac
          exit 0 ;;
      esac
    done
    echo "sha256:{'c' * 64}" ;;
  exec)
    # r24 F2: `docker exec <name> python3 -m kvpersist.mp_ping ...` -- the
    # readiness gate pings the server INSIDE the container. r25 G2: a slow
    # ping (`ping-slow` marker) simulates the real mp_ping (up to 5s per try).
    case "$*" in
      *mp_ping*)
        if [ -f "{bin_dir}/ping-slow" ]; then sleep 5; fi
        if [ -f "{bin_dir}/pong" ]; then exit 0; else exit 1; fi ;;
    esac
    cat <<'IDENTEOF'
{ident}
IDENTEOF
    ;;
  ps) echo "" ;;
  *) exit 0 ;;
esac
''')
    (bin_dir / "docker").chmod(0o755)
    # host mode requires a `lmcache` CLI on PATH (its pre-flight checks it)
    (bin_dir / "lmcache").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "lmcache").chmod(0o755)
    # r24 F2 / r25 G1: the docker readiness gate needs a created (not dead)
    # container + a server that answers mp_ping. Markers control the stub
    # state BEFORE the script runs: docker_state=running|exited|absent,
    # run_dies -> the docker-run client exits without creating the container,
    # create_delay -> the container appears only N seconds later,
    # docker_pong=False -> mp_ping never answers, pong_delay -> a slow server.
    for m in ("dead-marker", "run-dies", "appear-at"):
        (bin_dir / m).unlink(missing_ok=True)
    if docker_state == "exited":
        (bin_dir / "dead-marker").write_text("1\n")
    if run_dies:
        (bin_dir / "run-dies").write_text("1\n")
    if create_delay > 0:
        (bin_dir / "appear-at").write_text(
            str(int(time.time() + create_delay)) + "\n")
    if docker_pong:
        (bin_dir / "pong").write_text("1\n")
    else:
        (bin_dir / "pong").unlink(missing_ok=True)
    if ping_slow:
        (bin_dir / "ping-slow").write_text("1\n")
    if pong_delay > 0:
        import threading
        def _later():
            time.sleep(pong_delay)
            (bin_dir / "pong").write_text("1\n")
        threading.Thread(target=_later, daemon=True).start()
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_LAUNCH_MODE=mode,
               KVP_CACHE_ROOT=str(cache_root),
               KVP_CACHE_BUDGET_GB="1",
               KVP_LIVE_DIR=str(live),
               LIVE_DIR=str(live),
               KVP_SKIP_IPC_TEST="1",
               KVP_CACHE_READY_TIMEOUT="10",
               LMCACHE_L2_SERDE="none")
    for var in ("IMAGE", "DOCKER_IMAGE", "KVP_LMCACHE_BUILD",
                "KVP_RUNNING_LMCACHE_BUILD", "KVP_SKIP_LMC_IDENTITY",
                "KVP_SKIP_IMAGE_CHECK", "LMCACHE_EVICTION_POLICY"):
        env.pop(var, None)
    cache_root.mkdir(exist_ok=True)
    # host mode: the cache root must exist and the request port must be free
    env["LMCACHE_PORT"] = str(port)
    env["LMCACHE_HOST"] = "127.0.0.1"
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(["bash", str(ROOT / "scripts" / "start-cache.sh")],
                       capture_output=True, text=True, timeout=120, env=env,
                       cwd=str(tmp_path))
    if require_launch and not argv_file.exists():
        raise AssertionError(
            f"start-cache ({mode}) must launch the server:\n{r.stdout}\n{r.stderr}")
    return (_argv_from_file(argv_file) if argv_file.exists() else []), r


def test_b4_start_cache_docker_argv_parses_like_pin(tmp_path):
    """> The REAL start-cache.sh (docker mode) delivers a ONE-ELEMENT
    --l2-adapter and zero leftovers with the pin's parser."""
    cache_root = tmp_path / "cache"
    argv, _r = _start_cache_argv(tmp_path, "docker", cache_root)
    ns, leftovers = _parse_like_pin([a for a in argv if a != "server"])
    assert leftovers == [], f"docker start-cache leftovers: {leftovers}"
    assert ns.l2_adapter and len(ns.l2_adapter) == 1, ns.l2_adapter
    rendered = json.loads((cache_root / "l2-adapter.rendered.json").read_text())
    assert json.loads(ns.l2_adapter[0]) == rendered, (
        "the docker --l2-adapter value must equal the rendered adapter")


def test_b4_start_cache_host_argv_parses_like_pin(tmp_path):
    """> The REAL start-cache.sh (host mode) delivers a ONE-ELEMENT
    --l2-adapter and zero leftovers."""
    cache_root = tmp_path / "cache"
    argv, _r = _start_cache_argv(tmp_path, "host", cache_root)
    ns, leftovers = _parse_like_pin([a for a in argv if a != "server"])
    assert leftovers == [], f"host start-cache leftovers: {leftovers}"
    assert ns.l2_adapter and len(ns.l2_adapter) == 1, ns.l2_adapter


def test_b4_smoke_and_start_cache_argv_parity(tmp_path):
    """The smoke's argv and the docker start-cache argv are equal modulo
    host, port, L1 and the adapter path (the smoke is the production launch)."""
    smoke_file = tmp_path / "argv_smoke.bin"
    r = _read_argv_via_stub(smoke_file, str(ROOT / "scripts" / "_smoke_cache_server.sh"),
                            ["zstd", str(tmp_path / "smoke-cache"), "15561"],
                            env={"KVP_SMOKE_REPO": str(ROOT),
                                 "KVP_SMOKE_PORT": "15561"})
    assert smoke_file.exists(), f"smoke must launch:\n{r.stdout}\n{r.stderr}"
    smoke_argv = _argv_from_file(smoke_file)
    cache_root = tmp_path / "cache"
    docker_argv, _r = _start_cache_argv(tmp_path, "docker", cache_root)
    skip = {"--host", "--port", "--l1-size-gb", "--l2-adapter", "--http-host"}
    def pairs(argv):
        it = iter(a for a in argv if a != "server")
        out = []
        for flag in it:
            if flag in skip:
                next(it, None)
                continue
            out.append((flag, next(it, None)))
        return out
    s_pairs = pairs(smoke_argv)
    d_pairs = pairs(docker_argv)
    assert s_pairs == d_pairs, (
        f"argv parity failed (modulo host/port/L1/adapter/http-host):\n"
        f"  smoke : {s_pairs}\n  docker: {d_pairs}")


# ---------------------------------------------------------------------------
# B4 mutations -- each must turn a specific test RED
# ---------------------------------------------------------------------------
def test_b4_mutation_line_split_argv_is_red(tmp_path):
    """Mutation: the adapter JSON goes into argv as the FULL pretty-printed
    multi-line document AND the consumer reads it line-by-line (both halves
    of the r20 K6.3 hardware defect) -> the adapter explodes into many argv
    elements. The real code (compact one-line JSON + NUL + mapfile -d '')
    delivers exactly one --l2-adapter element (caught by the consumer tests)."""
    src = (ROOT / "scripts" / "env.sh").read_text()
    bs = chr(92)
    nul_lit = "printf '%s" + bs + "0'"      # env.sh: printf '%s\0'
    nl_lit = "printf '%s" + bs + "n'"
    assert nul_lit in src, "argv must be NUL-separated in env.sh"
    # the mutant, built by exact line surgery (no fragile regex):
    # 1) the adapter value becomes the full pretty-printed multi-line JSON
    # 2) argv is newline-separated
    # 3) the consumer reads line-by-line (mapfile -t instead of mapfile -d '')
    out_lines = []
    for ln in src.splitlines(keepends=True):
        if "_adapter_compact=" in ln and "python3 -c" in ln:
            out_lines.append('  _adapter_compact="$(cat "$_adapter")"\n')
            continue     # drop the whole compact-JSON assignment line
        if ln.strip().startswith("|| { echo") and "cannot compact" in ln:
            continue     # and its `|| { ... }` continuation
        out_lines.append(ln)
    mutant = "".join(out_lines)
    mutant = mutant.replace(nul_lit + " " + bs, nl_lit + " " + bs)
    mutant = mutant.replace("mapfile -d '' -t \"$_arr\"",
                            "mapfile -t \"$_arr\"")
    assert mutant != src, "mutation produced no change"
    # the mutant must be VALID bash (the line surgery keeps the syntax)
    chk = subprocess.run(["bash", "-n"], input=mutant,
                         capture_output=True, text=True, timeout=30)
    assert chk.returncode == 0, f"the mutant is not valid bash:\n{chk.stderr[:300]}"
    # a multi-line pretty adapter + line-by-line reading = many argv elements.
    # The pretty JSON spans 30+ lines, so the explosion is unambiguous
    # (the real compact code delivers exactly one --l2-adapter element).
    ad = tmp_path / "ad.json"
    pretty = "{\n" + "".join(
        f'  "k{i}": "v{i}",\n' for i in range(30)
    ) + '  "z": 1\n}'
    ad.write_text(pretty)
    script = (
        "set -u\n" + mutant + "\n"
        f"_kvp_server_argv_read ARR h p 1 1 '{ad}' \"\"\n"
        "printf '%s\\n' \"${ARR[@]}\""
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True,
                         text=True, timeout=30,
                         env=dict(os.environ, LMCACHE_EVICTION_POLICY=""))
    lines = [l for l in out.stdout.splitlines() if l.strip()]
    assert len(lines) > 25, (
        "the line-split mutant must explode the argv into many elements, "
        f"got {len(lines)}: {lines[:5]}")
