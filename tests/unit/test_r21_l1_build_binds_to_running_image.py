# SPDX-License-Identifier: Apache-2.0
"""r21 L1: the cache namespace must follow the image that ACTUALLY runs.

Facts (verified on HEAD fdac79d):
- `check_image_identity.py --print-wheel-version` was never called from any
  script -- dead code.
- `kvpersist.cli model-name` derived `lmcache_build` from
  `live/image-identity.env` (KVP_LMC_VERSION) -- the LAST build's record,
  NOT the image being launched.
- `verify-engine-identity.sh` cross-checked `lmcache_build` only when an
  operator pin `KVP_LMCACHE_BUILD` was set (empty by default) -- an engine
  on a different image gave NO mismatch at all.

Contract (r21 L1):
- `_kvp_require_custom_image` captures the deep check's `KVP_WHEEL_VERSION=`
  output and exports `KVP_LMCACHE_BUILD`; every successful resolve path does
  this (except KVP_SKIP_IMAGE_CHECK=1).
- `read_lmcache_build` priority: env (the running image) > baked identity
  JSON > the live/ build record.
- `verify-engine-identity.sh` cross-checks ALWAYS (pin or effective value)
  against the container's /etc/kvp-lmcache-identity.json.

Scenario A != B: live/image-identity.env says build A, the launchable image
carries build B.
"""
from __future__ import annotations
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
sys.path.insert(0, str(IMPL))

BUILD_A = "0.5.8rc1.post48+kvp.aaaaaaaaaaaa"
BUILD_B = "0.5.8rc1.post48+kvp.bbbbbbbbbbbb"
GATE = ROOT / "scripts" / "verify-engine-identity.sh"


def _sha(p: pathlib.Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _identity_json(patch_dir: pathlib.Path, wheel_version: str) -> str:
    return json.dumps({
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr4136_patch_sha256": _sha(patch_dir / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(patch_dir / "5972-incomplete-kv-objects.patch"),
        "wheel": f"lmcache-{wheel_version}-cp312-cp312-linux_x86_64.whl",
        "wheel_sha256": "a" * 64,
        "wheel_version": wheel_version,
    })


def _default_labels(patch_dir: pathlib.Path) -> dict:
    return {
        "org.kvp.lmc-base": ("vllm/vllm-openai:nightly-8e2beaa3bff595577a069d49d"
                             "96bde9074294ec2@sha256:167ccd8696bc09f1b63301185134"
                             "e04b883623f8adcbd8e3a0ec3140a193abcf"),
        "org.kvp.lmc-patch-4136": _sha(patch_dir / "4136-mp-retrieve-abort.patch"),
        "org.kvp.lmc-patch-5972": _sha(patch_dir / "5972-incomplete-kv-objects.patch"),
    }


def _make_docker_stub(bin_dir: pathlib.Path, ident_file: pathlib.Path,
                      labels: dict, wheel_version: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    labels_json = json.dumps(labels)
    stub = bin_dir / "docker"
    stub.write_text(f"""#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
_all="$*"
case "$cmd" in
  image)
    sub="${{1:-}}"; shift || true
    case "$sub" in
      inspect) echo '{labels_json}' ;;
    esac ;;
  run)
    _entry=""
    while [ "$#" -gt 0 ]; do case "$1" in
      --entrypoint) _entry="$2"; shift 2 ;;
      *) shift ;;
    esac; done
    case "$_entry" in
      cat) cat "{ident_file}" ;;
      python3)
        case "$_all" in
          *importlib.metadata*) echo "{wheel_version}"; exit 0 ;;
        esac
        exit 0 ;;
    esac ;;
  exec) cat "{ident_file}" ;;
  inspect) echo "sha256:{'c' * 64}" ;;
  *) exit 0 ;;
esac
""")
    stub.chmod(0o755)


def _make_repo(tmp_path: pathlib.Path, wheel_version: str):
    """A mini repo: REAL env.sh + start-all.sh + verify-engine-identity.sh
    (the subjects), implementation/ + vendor/ symlinked, configs/ COPIED and
    pinned (the real repo is never touched -- r21 L5), live/image-identity.env
    carrying BUILD_A (the stale build history), live/image -> custom."""
    import re as _re
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for s in ("env.sh", "start-all.sh", "verify-engine-identity.sh"):
        shutil.copy(ROOT / "scripts" / s, repo / "scripts" / s)
    for d in ("implementation", "vendor"):
        (repo / d).symlink_to(ROOT / d)
    # configs as a COPY with the required pins filled (L5: never write into
    # the real repo)
    shutil.copytree(ROOT / "configs", repo / "configs")
    ident_env = repo / "configs" / "mimo-v2.6-pro" / "identity.env"
    text = ident_env.read_text()
    for name, value in (("KVP_MODEL_REVISION", "0" * 40),
                        ("KVP_TOKENIZER_REVISION", "0" * 40),
                        ("KVP_CHAT_TEMPLATE_DIGEST", "0" * 64)):
        text = _re.sub(r"^(" + name + r")=.*$", r"\1=" + value, text, flags=_re.M)
    ident_env.write_text(text)
    live = repo / "live"
    live.mkdir()
    (live / "image").write_text("sha256:" + "c" * 64 + "\n")
    # the BUILD HISTORY says A -- the stale record the namespace must ignore
    (live / "image-identity.env").write_text("KVP_LMC_VERSION=" + BUILD_A + "\n")
    ident_file = tmp_path / "identity.json"
    ident_file.write_text(_identity_json(ROOT / "vendor" / "patches" / "lmcache",
                                         wheel_version))
    bin_dir = tmp_path / "bin"
    _make_docker_stub(bin_dir, ident_file,
                      _default_labels(ROOT / "vendor" / "patches" / "lmcache"),
                      wheel_version)
    return repo, bin_dir, ident_file


def _run_resolver(repo: pathlib.Path, bin_dir: pathlib.Path,
                  envsh: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    envsh = envsh or (repo / "scripts" / "env.sh")
    script = f"""
set -u
. "{envsh}"
KVP_LIVE_DIR="{repo}/live"
IMAGE=""
_kvp_resolve_runtime_image "{repo}" 0 || exit $?
echo "RESOLVED_IMAGE=$IMAGE"
echo "RESOLVED_BUILD=${{KVP_LMCACHE_BUILD:-<unset>}}"
"""
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_LIVE_DIR=str(repo / "live"))
    for var in ("IMAGE", "KVP_LMCACHE_BUILD", "KVP_LMCACHE_IDENTITY",
                "KVP_LMC_VERSION", "KVP_VERIFY_SHALLOW", "KVP_SKIP_LMC_IDENTITY",
                "KVP_SKIP_IMAGE_CHECK"):
        env.pop(var, None)
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=60, env=env, cwd=str(repo))


NS_CODE = (
    "import sys; sys.path.insert(0, {impl!r}); "
    "from kvpersist.identity import identity_from_env_files; "
    "import os; g = os.path.join; root = os.environ['KVP_REPO_ROOT']; "
    "gen = g(root, 'configs/generic'); mc = g(root, 'configs/mimo-v2.6-pro'); "
    "paths = [g(gen, 'cache.env'), g(gen, 'lmcache-server.env'), "
    "g(mc, 'identity.env'), g(gen, 'cache.env.local'), "
    "g(gen, 'lmcache-server.env.local'), g(mc, 'identity.env.local')]; "
    "i = identity_from_env_files(*paths); print(i.namespace())"
)


def _namespace_for(repo: pathlib.Path, bin_dir: pathlib.Path,
                   build: str | None, pop_build: bool = False) -> str:
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_REPO_ROOT=str(repo),
               KVP_LIVE_DIR=str(repo / "live"))
    for var in ("KVP_LMCACHE_IDENTITY", "KVP_MODEL_CONFIG", "KVP_LMC_VERSION"):
        env.pop(var, None)
    if pop_build:
        env.pop("KVP_LMCACHE_BUILD", None)
    elif build is not None:
        env["KVP_LMCACHE_BUILD"] = build
    out = subprocess.run([sys.executable, "-c", NS_CODE.format(impl=str(IMPL))],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr[-500:]
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# (1) namespace follows the RUNNING image's build, not live/ history
# ---------------------------------------------------------------------------
def test_namespace_follows_running_image_not_history(tmp_path):
    """> live/image-identity.env says A, the launchable image carries B.
    The resolver exports B and the namespace follows B, not A."""
    repo, bin_dir, ident_file = _make_repo(tmp_path, BUILD_B)
    r = _run_resolver(repo, bin_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"RESOLVED_BUILD={BUILD_B}" in r.stdout, (
        f"the resolver must export KVP_LMCACHE_BUILD from the RUNNING image "
        f"(B), not the live/ history (A):\n{r.stdout}\n{r.stderr}")
    ns_b = _namespace_for(repo, bin_dir, BUILD_B)
    ns_a = _namespace_for(repo, bin_dir, BUILD_A)
    assert ns_b != ns_a, "builds A and B must give different namespaces"


# ---------------------------------------------------------------------------
# (2) gate without a pin still cross-checks
# ---------------------------------------------------------------------------
def test_gate_without_pin_crosschecks_build(tmp_path):
    """> No pin; live/ history says A; the container runs B (its identity JSON
    carries B; the expected value falls back to read_lmcache_build()).
    When the namespace was hashed from A and the engine runs B, the gate
    must refuse (exit 4) naming lmcache_build."""
    repo, bin_dir, ident_file = _make_repo(tmp_path, BUILD_B)
    (tmp_path / "cache").mkdir()
    gate_env = dict(os.environ,
                    PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                    KVP_REPO_ROOT=str(repo),
                    KVP_LIVE_DIR=str(repo / "live"),
                    KVP_CACHE_ROOT=str(tmp_path / "cache"))
    for var in ("KVP_LMCACHE_BUILD", "KVP_SKIP_LMC_IDENTITY", "KVP_SKIP_IMAGE_CHECK",
                "MODEL_DIR", "KVP_MODEL_CONFIG"):
        gate_env.pop(var, None)
    r = subprocess.run(["bash", str(repo / "scripts" / "verify-engine-identity.sh"),
                        "test-container"],
                       capture_output=True, text=True, timeout=60,
                       env=gate_env, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode == 4, (
        f"engine on image B while the namespace was hashed from live/ record A "
        f"must be a MISMATCH (exit 4):\n{out[-1500:]}")
    assert "lmcache_build" in out, (
        f"the refusal must name the lmcache_build field:\n{out[-800:]}")


# ---------------------------------------------------------------------------
# (3) mutations -- each must turn a specific test RED
# ---------------------------------------------------------------------------
def test_mutation_resolver_stops_exporting_build(tmp_path):
    """> Mutation (r21 L1): the resolver does not export KVP_LMCACHE_BUILD ->
    scenario (1) goes RED (the export assertion must catch it). This is an
    honest mutation: the mutant env.sh is sourced and the SAME resolver call
    is run -- only the export line is dropped."""
    repo, bin_dir, ident_file = _make_repo(tmp_path, BUILD_B)
    # honest mutation: drop the export in _kvp_require_custom_image
    envsh_src = (repo / "scripts" / "env.sh").read_text()
    marker = "      KVP_LMCACHE_BUILD=\"$_wv\"\n      export KVP_LMCACHE_BUILD\n"
    assert marker in envsh_src, "mutation marker not found in env.sh"
    mutated = envsh_src.replace(marker, "      :\n")
    mutant_envsh = repo / "scripts" / "env-mutant.sh"
    mutant_envsh.write_text(mutated)
    r = _run_resolver(repo, bin_dir, envsh=mutant_envsh)
    assert "RESOLVED_BUILD=<unset>" in r.stdout, (
        f"the mutant must NOT export KVP_LMCACHE_BUILD:\n{r.stdout}\n{r.stderr}")


def test_mutation_gate_only_checks_with_pin(tmp_path):
    """> Mutation (r21 L1): the gate again checks lmcache_build ONLY when a
    pin is set -> scenario (2) goes GREEN on the mutant (no refusal)."""
    repo, bin_dir, ident_file = _make_repo(tmp_path, BUILD_B)
    gate_src = (repo / "scripts" / "verify-engine-identity.sh").read_text()
    # restore the r20 behaviour: only check when vals pin is set
    mutant = gate_src.replace(
        '    if not _build_expect:\n'
        '        # no pin: the namespace\'s effective value (the same source it hashes)\n'
        '        _build_expect = (os.environ.get("KVP_LMCACHE_BUILD", "") or "").strip()\n'
        '    if not _build_expect:\n'
        '        try:\n'
        '            from kvpersist.identity import read_lmcache_build as _rlb\n'
        '            _build_expect = (_rlb() or "").strip()\n'
        '        except Exception:\n'
        '            _build_expect = ""\n',
        '')
    assert mutant != gate_src, "mutation marker not found in the gate"
    gate_mutant = repo / "scripts" / "gate-mutant.sh"
    gate_mutant.write_text(mutant)
    gate_mutant.chmod(0o755)
    (tmp_path / "cache").mkdir()
    gate_env = dict(os.environ,
                    PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                    KVP_REPO_ROOT=str(repo),
                    KVP_LIVE_DIR=str(repo / "live"),
                    KVP_CACHE_ROOT=str(tmp_path / "cache"))
    for var in ("KVP_LMCACHE_BUILD", "KVP_SKIP_LMC_IDENTITY", "KVP_SKIP_IMAGE_CHECK",
                "MODEL_DIR", "KVP_MODEL_CONFIG"):
        gate_env.pop(var, None)
    r = subprocess.run(["bash", str(gate_mutant), "test-container"],
                       capture_output=True, text=True, timeout=60,
                       env=gate_env, cwd=str(tmp_path))
    out = r.stdout + r.stderr
    assert r.returncode != 4 or "lmcache_build" not in out, (
        f"the mutant must NOT catch the build mismatch (that is the defect):\n{out[-1000:]}")


def test_mutation_read_lmcache_build_prefers_live(tmp_path):
    """> Mutation (r21 L1): read_lmcache_build prefers the live/ record over
    the env -> scenario (1) would go RED (the namespace would follow A).
    Honest mutation: a COPY of the identity module with the live/ record read
    FIRST is imported and run through the SAME call -- the mutant returns A
    where the real module returns B."""
    import importlib.util as _ilu
    repo, bin_dir, ident_file = _make_repo(tmp_path, BUILD_B)
    # an honest mutant: the live/ fallback moved BEFORE the env check
    real_src = (IMPL / "kvpersist" / "identity.py").read_text()
    env_block = ('    # 1. env (the resolver ties this to the RUNNING image)\n'
                 '    ver = (_os.environ.get("KVP_LMCACHE_BUILD") or "").strip()\n'
                 '    if ver:\n'
                 '        return ver\n')
    assert env_block in real_src, "mutation marker not found in identity.py"
    marker_tail = '    return ""\n\n\ndef _read_env_config'
    assert marker_tail in real_src, "mutation tail marker not found"
    mutant_src = real_src.replace(env_block, '', 1).replace(
        marker_tail,
        '    # mutant: the env check comes LAST (r20 behaviour)\n'
        '    ver = (_os.environ.get("KVP_LMCACHE_BUILD") or "").strip()\n'
        '    if ver:\n'
        '        return ver\n    return ""\n\n\ndef _read_env_config', 1)
    assert mutant_src != real_src
    mutant_py = tmp_path / "identity_mutant.py"
    mutant_py.write_text(mutant_src)
    spec = _ilu.spec_from_file_location("identity_mutant", mutant_py)
    mutant_mod = _ilu.module_from_spec(spec)
    import sys as _sys
    _sys.modules[spec.name] = mutant_mod  # dataclasses needs the module registered
    spec.loader.exec_module(mutant_mod)
    # the env says B (the resolver's export); the live/ record says A
    env = dict(os.environ,
               KVP_LMCACHE_BUILD=BUILD_B,
               KVP_LIVE_DIR=str(repo / "live"))
    for var in ("KVP_LMCACHE_IDENTITY", "KVP_IMAGE_IDENTITY"):
        env.pop(var, None)
    old = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        got_mutant = mutant_mod.read_lmcache_build()
    finally:
        os.environ.clear()
        os.environ.update(old)
    # the mutant follows the live/ record (A) -- that is the defect; the real
    # module's assertion (got == B) would be RED against this mutant
    assert got_mutant == BUILD_A, (
        f"the mutant must prefer the live/ record (A) over the env (B) -- "
        f"got {got_mutant!r} (proves the real test's assertion is meaningful)")
