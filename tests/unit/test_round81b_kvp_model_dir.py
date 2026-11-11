"""Round 81b regression (review): KVP_MODEL_DIR must WORK, not just be read.

The recipe launcher reads MODEL_DIR only. A bare operator-set KVP_MODEL_DIR
was silently ignored (the resolver guard checked both names together, but
only MODEL_DIR was ever exported). This test runs the REAL start-all.sh
delegated path with ONLY KVP_MODEL_DIR set and asserts the recipe receives
MODEL_DIR -- and that the obs ownership marker is written even when live/ is
absent (fresh clone: live/ is gitignored).
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
import _targets as _T
import os
import stat
import subprocess
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
RECIPE_DIR = _T.recipe_launcher().parent.parent if _T.recipe_launcher() else None  # the recipe CHECKOUT dir, not the launcher file

STUB = '''#!/usr/bin/env bash
echo "$0 $*" >> "$STUB_LOG"
case "$(basename "$0")" in
  docker)
    case "$1" in
      info) exit 0 ;;
      container) exit 1 ;;
      inspect) echo "running exit=0 oom=false"; exit 0 ;;
      logs) printf '%s' "${STUB_DOCKER_LOGS:-}" ;;
      ps) exit 0 ;;
      run)
        case "$*" in
          *--rm*) echo "/usr/local/lib/python3.12/dist-packages"; exit 0 ;;
        esac
        echo "STUB_DOCKER_RUN" >> "$STUB_LOG"
        exit 0 ;;
    esac
    exit 0 ;;
  nvidia-smi) [ "$1" = "-L" ] && for i in 1 2 3 4; do echo "GPU $i: STUB"; done; exit 0 ;;
  curl)
    case "$*" in
      *"/v1/models"*) echo '{"data":[{"id":"mimo-pro"}]}' ;;
      *"/v1/chat/completions"*) echo '{"choices":[{"message":{"content":"TP4_OK"},"finish_reason":"stop"}]}' ;;
      *) echo '{}' ;;
    esac
    exit 0 ;;
  sha256sum) exec /usr/bin/sha256sum "$@" ;;
  *) exit 0 ;;
esac
'''
PYTHON_STUB = '''#!/usr/bin/env bash
case "$*" in
  *kvpersist.cli*model-name*) echo "mimo-pro/testhash" ;;
esac
exec /usr/bin/python3 "$@"
'''
GOOD_MARKERS = (
    "HOOK STATUS boot_hook=INACTIVE\n"
    "hotsplit dynamic: step hook installed on GPUModelRunnerImpl (update every 2000 steps, <=2 swaps/layer, margin 1.25)\n"
    "hotsplit live counter: 69 layers -> /live/counts.json\n"
    "HOOK STATUS numa_bind=ACTIVE\n"
)


def _fixture(tmp_path):
    import atexit
    import socket
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "stub.log"
    for name in ("docker", "nvidia-smi", "curl", "sha256sum"):
        p = bin_dir / name
        p.write_text(STUB)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    py = bin_dir / "python3"
    py.write_text(PYTHON_STUB)
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    (home / ".config" / "kvp").mkdir(parents=True)
    key_file = home / ".config" / "kvp" / "api_key"
    key_file.write_text("sharedkey123\n")
    key_file.chmod(0o600)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "lmcache-server.pid").write_text(str(os.getpid()))
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    atexit.register(srv.close)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["STUB_LOG"] = str(log)
    env["STUB_DOCKER_LOGS"] = GOOD_MARKERS
    env["HOME"] = str(home)
    env["KVP_API_KEY_FILE"] = str(key_file)
    env["KVP_CACHE_ROOT"] = str(cache_root)
    env["KVP_SKIP_GPU_CHECK"] = "1"
    env["KVP_SKIP_IPC_TEST"] = "1"
    env["KVP_SKIP_HOTSPLIT_CHECK"] = "1"
    env["WAIT_TRIES"] = "1"
    env["WAIT_SLEEP"] = "0"
    env["LMCACHE_HOST"] = "127.0.0.1"
    env["LMCACHE_PORT"] = str(srv.getsockname()[1])
    env["KV_PERSIST_HOST"] = "127.0.0.1"
    env["KV_PERSIST_PORT"] = str(srv.getsockname()[1])
    # A FAKE kv-persistence checkout (stub compat file + identity gate +
    # kvpersist.cli), the same device as test_engine_delegation: the wiring
    # (KVP_MODEL_DIR -> MODEL_DIR -> recipe) is under test here, not the
    # operator's identity pins (machine-specific setup territory).
    kvp = tmp_path / "kv-persistence"
    (kvp / "implementation" / "kvpersist").mkdir(parents=True)
    (kvp / "implementation" / "kvpersist" / "cli.py").write_text("# stub\n")
    (kvp / "KVP_COMPAT_VERSION").write_text("1\n")
    (kvp / "scripts").mkdir(exist_ok=True)
    gate = kvp / "scripts" / "verify-engine-identity.sh"
    gate.write_text(
        '#!/usr/bin/env bash\n'
        'echo "$0 $*" >> "$STUB_LOG"\n'
        'exit "${GATE_RC:-0}"\n')
    gate.chmod(gate.stat().st_mode | stat.S_IEXEC)
    env["KVP_REPO"] = str(kvp)
    env["KVP_REPO_ROOT"] = str(kvp)
    return env, log


def test_kvp_model_dir_alone_reaches_the_recipe(tmp_path):
    """Only KVP_MODEL_DIR set (no MODEL_DIR): the delegated path must export
    MODEL_DIR=$KVP_MODEL_DIR so the recipe launcher actually finds the model.
    This is the round-81b review defect: the guard skipped the resolver but
    never exported MODEL_DIR."""
    env, log = _fixture(tmp_path)
    model = tmp_path / "kvp-model"
    model.mkdir()
    (model / "config.json").write_text('{"torch_dtype":"bfloat16"}')
    # scrub every model name so ONLY KVP_MODEL_DIR is set
    for k in ("MODEL_DIR", "KVP_MODEL_DIR"):
        env.pop(k, None)
    env["KVP_MODEL_DIR"] = str(model)
    if log.exists():
        log.unlink()
    r = subprocess.run(
        ["bash", str(KV / "scripts" / "start-all.sh")],
        env={**env, "RECIPE_DIR": str(RECIPE_DIR), "KVP_ENGINE": "recipe",
             "IMAGE": "x@sha256:" + "a" * 64, "ALLOW_RUN": "1",
             "KVP_PERSIST_ENABLED": "false"},
        capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert "KVP_ENGINE=recipe" in out, out[-800:]
    assert "MODEL_DIR taken from KVP_MODEL_DIR" in out, (
        f"a bare KVP_MODEL_DIR must be exported as MODEL_DIR:\n{out[-800:]}")
    # the recipe must receive the model (it mounts MODEL_DIR -- check the run)
    assert log.exists(), out[-800:]
    run_lines = [l for l in log.read_text().splitlines()
                 if "docker run " in l and "--rm" not in l]
    assert run_lines, out[-800:]
    assert str(model) in run_lines[-1], (
        f"the model dir must reach the docker run: {run_lines[-1][:300]}")


def test_model_dir_wins_over_kvp_model_dir(tmp_path):
    """An explicit MODEL_DIR must win over KVP_MODEL_DIR (the documented
    priority)."""
    env, log = _fixture(tmp_path)
    m1 = tmp_path / "model-dir-wins"
    m1.mkdir()
    (m1 / "config.json").write_text('{"torch_dtype":"bfloat16"}')
    m2 = tmp_path / "kvp-model-loses"
    m2.mkdir()
    (m2 / "config.json").write_text('{"torch_dtype":"bfloat16"}')
    if log.exists():
        log.unlink()
    r = subprocess.run(
        ["bash", str(KV / "scripts" / "start-all.sh")],
        env={**env, "RECIPE_DIR": str(RECIPE_DIR), "KVP_ENGINE": "recipe",
             "IMAGE": "x@sha256:" + "a" * 64, "ALLOW_RUN": "1",
             "MODEL_DIR": str(m1), "KVP_MODEL_DIR": str(m2),
             "KVP_PERSIST_ENABLED": "false"},
        capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert "KVP_ENGINE=recipe" in out, out[-800:]
    run_lines = [l for l in log.read_text().splitlines()
                 if "docker run " in l and "--rm" not in l]
    assert run_lines, out[-800:]
    assert str(m1) in run_lines[-1], (
        f"explicit MODEL_DIR must win over KVP_MODEL_DIR: {run_lines[-1][:300]}")
    assert str(m2) not in run_lines[-1], (
        f"KVP_MODEL_DIR must NOT override MODEL_DIR: {run_lines[-1][:300]}")


def test_obs_marker_written_on_fresh_clone(tmp_path):
    """live/ is gitignored -- absent on a fresh clone. The ownership marker
    (live/obs_started) must still be written (mkdir before the write), or
    stop-all.sh would leave a stack it started running forever."""
    env, log = _fixture(tmp_path)
    # simulate a fresh clone: no live/ dir in the repo
    repo_live = KV / "live"
    marker = repo_live / "obs_started"
    saved = marker.read_text() if marker.exists() else None
    if repo_live.exists():
        # keep the dir but remove the marker (the mkdir case is covered by
        # start-all creating it when missing)
        marker.unlink(missing_ok=True)
    try:
        r = subprocess.run(
            ["bash", str(KV / "scripts" / "start-all.sh")],
            env={**env, "RECIPE_DIR": str(RECIPE_DIR), "KVP_ENGINE": "recipe",
                 "IMAGE": "x@sha256:" + "a" * 64, "ALLOW_RUN": "1",
                 "MODEL_DIR": str(tmp_path / "no-model-needed"),
                 "KVP_PERSIST_ENABLED": "false"},
            capture_output=True, text=True, timeout=180,
            cwd=str(tmp_path))   # a foreign cwd: live/ must still be the repo's
        out = r.stdout + r.stderr
        assert "KVP_ENGINE=recipe" in out, out[-800:]
        assert marker.exists(), (
            "live/obs_started must be written even on a fresh clone (mkdir "
            "before the write) -- otherwise stop-all.sh never stops what it "
            "started")
    finally:
        if saved is not None:
            marker.write_text(saved)
        else:
            marker.unlink(missing_ok=True)