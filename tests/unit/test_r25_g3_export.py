# SPDX-License-Identifier: Apache-2.0
"""r25 G3 behavioural test: start-all.sh must EXPORT the resolved
KVP_LAUNCH_MODE so the recipe launcher (default host) sees it.

Behavioural acceptance (not grep-based):
  * a stub recipe launcher prints ${KVP_LAUNCH_MODE-UNSET} -- expect docker
    by default and host when explicitly set;
  * a stubbed _kvp_pre_flights (tmp copy of env.sh) records its 9th argument.
Mutations caught (r24 survivors): F3-3/F3-5 (default back to host) and
F3-6 (drop the export in start-all.sh) -- all RED here.
"""
from __future__ import annotations
import os
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _run_start_all(tmp_path: pathlib.Path, explicit_mode: str | None):
    """Run the REAL start-all.sh against stubs in a tmp tree."""
    scripts = tmp_path / "scripts"
    shutil.copytree(ROOT / "scripts", scripts)
    env_sh = scripts / "env.sh"
    text = env_sh.read_text()
    marker = "_kvp_pre_flights(){"
    i = text.index(marker)
    j = text.index("\n}\n", i) + 3
    recorder = (
        '_kvp_pre_flights(){\n'
        f'  printf \'%s\' "${{9:-}}" > "{tmp_path}/pre_flights_arg9"\n'
        '  return 0\n'
        '}\n'
    )
    env_sh.write_text(text[:i] + recorder + text[j:])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    stub = "#!/usr/bin/env bash\nexit 0\n"
    for name in ("docker", "nvidia-smi", "curl", "lmcache", "sha256sum"):
        (bin_dir / name).write_text(stub)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *kvpersist.cli*model-name*) echo "mimo-pro/testhash" ;;\n'
        "  *kvpersist.mp_ping*) exit 3 ;;\n"
        f'  *) exec "{real_py}" "$@" ;;\n'
        "esac\n")
    (bin_dir / "python3").chmod(0o755)
    recipe = tmp_path / "recipe"
    (recipe / "scripts").mkdir(parents=True)
    launcher = recipe / "scripts" / "run-tp4-hotsplit.sh"
    launcher.write_text(
        '#!/usr/bin/env bash\n'
        'echo "LAUNCH_MODE=[${KVP_LAUNCH_MODE-UNSET}]"\n'
        "exit 0\n")
    launcher.chmod(0o755)
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    (live / "image-identity.env").write_text(
        "KVP_LMC_VERSION=0.5.8rc1.post48+kvp.bbbbbbbbbbbb\n")
    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    (model / "config.json").write_text('{"hidden_size": 8}\n')
    home = tmp_path / "home"
    (home / ".config" / "kvp").mkdir(parents=True)
    (home / ".config" / "kvp" / "api_key").write_text("sharedkey123\n")
    cache_root = tmp_path / "cache"
    cache_root.mkdir(exist_ok=True)
    # a RUNNING cache-server pid stub makes start-all's step 2/3 skip
    # (same device as the engine-delegation tests) -- this test is about
    # the launch-mode EXPORT reaching step 3/3, not about the cache start
    (cache_root / "lmcache-server.pid").write_text(str(os.getpid()))
    # observability stubs: step 1/3 must not block on a missing compose file
    (tmp_path / "observability").mkdir(exist_ok=True)
    (tmp_path / "observability" / "docker-compose.yml").write_text("services: {}\n")
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_ENGINE="recipe",
               RECIPE_DIR=str(recipe),
               IMAGE="x@sha256:" + "a" * 64,
               ALLOW_RUN="1",
               MODEL_DIR=str(model),
               HOME=str(home),
               KVP_API_KEY_FILE=str(home / ".config" / "kvp" / "api_key"),
               KVP_LIVE_DIR=str(live),
               LIVE_DIR=str(live),
               KVP_CACHE_ROOT=str(cache_root),
               KVP_SKIP_IPC_TEST="1",
               KVP_SKIP_GPU_CHECK="1",
               KVP_SKIP_HOTSPLIT_CHECK="1",
               KVP_PERSIST_ENABLED="false",
               WAIT_TRIES="1", WAIT_SLEEP="0")
    if explicit_mode is None:
        env.pop("KVP_LAUNCH_MODE", None)
    else:
        env["KVP_LAUNCH_MODE"] = explicit_mode
    r = subprocess.run(["bash", str(scripts / "start-all.sh")],
                       capture_output=True, text=True, timeout=120, env=env,
                       cwd=str(tmp_path))
    out = r.stdout + r.stderr
    arg9 = ""
    f = tmp_path / "pre_flights_arg9"
    if f.exists():
        arg9 = f.read_text().strip()
    return r, out, arg9


def test_g3_default_mode_reaches_the_recipe_launcher(tmp_path):
    """> G3: without KVP_LAUNCH_MODE the launcher must see `docker` (not
    UNSET -- the resolver's export is lost in the assignment subshell)."""
    r, out, arg9 = _run_start_all(tmp_path, None)
    assert "LAUNCH_MODE=[docker]" in out, (
        f"the recipe launcher must see docker by default:\n{out[-800:]}")
    assert arg9 == "docker", (
        f"_kvp_pre_flights must receive docker as its 9th arg, got {arg9!r}")


def test_g3_explicit_host_reaches_the_recipe_launcher(tmp_path):
    """> G3: with KVP_LAUNCH_MODE=host the launcher and the pre-flights see
    `host`."""
    r, out, arg9 = _run_start_all(tmp_path, "host")
    assert "LAUNCH_MODE=[host]" in out, (
        f"the recipe launcher must see host:\n{out[-800:]}")
    assert arg9 == "host", (
        f"_kvp_pre_flights must receive host as its 9th arg, got {arg9!r}")


def test_g3_export_is_present_in_start_all():
    """> G3 pin: the assignment runs the resolver in a subshell -- the explicit
    export after it is REQUIRED (mutation F3-6 removes it)."""
    t = (ROOT / "scripts" / "start-all.sh").read_text()
    at = t.index('KVP_LAUNCH_MODE="$(_kvp_resolve_launch_mode)"')
    assert "export KVP_LAUNCH_MODE" in t[at:at + 300], (
        "start-all.sh must export KVP_LAUNCH_MODE right after the assignment")


def test_g3_pre_flights_default_mode_is_docker(tmp_path):
    """> G3/F3-5 (behavioural): _kvp_pre_flights called WITHOUT the 9th
    argument must run the docker branch (it inspects the cache container);
    mutation F3-5 (its default back to host) leaves the docker log empty."""
    import socket
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log}"\n'
        "exit 0\n")
    (bin_dir / "docker").chmod(0o755)
    real_py = subprocess.run(["which", "python3"], capture_output=True,
                             text=True).stdout.strip()
    (bin_dir / "python3").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *kvpersist.mp_ping*) exit 0 ;;\n"
        f'  *) exec "{real_py}" "$@" ;;\n'
        "esac\n")
    (bin_dir / "python3").chmod(0o755)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    try:
        script = (
            f' set -u\n. "{ROOT}/scripts/env.sh"\n'
            f'_kvp_pre_flights "/nonexistent-root" 127.0.0.1 {port} "/nonexistent-croot" '
            '"x@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" '
            '"" "" 1\n'
            'echo PF_RC=$?\n'
        )
        env = dict(os.environ,
                   PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                   KVP_SKIP_IPC_TEST="1")
        r = subprocess.run(["bash", "-c", script], capture_output=True,
                           text=True, timeout=60, env=env)
        docker_calls = log.read_text() if log.exists() else ""
        assert "lmcache-kvp-server" in docker_calls, (
            f"the docker branch must inspect the cache container when the "
            f"9th argument is omitted (the default is docker):\n"
            f"docker log={docker_calls!r}\n{r.stdout}{r.stderr}")
    finally:
        srv.close()
