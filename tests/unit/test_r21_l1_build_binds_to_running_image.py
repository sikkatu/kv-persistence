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
import re
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
    for var in ("KVP_LMCACHE_IDENTITY", "KVP_MODEL_CONFIG", "KVP_LMC_VERSION",
                "KVP_RUNNING_LMCACHE_BUILD"):
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
# (1) r21 L1 / r22 M4: the namespace follows the RUNNING image. Computed in
# the SAME shell after the resolver (exactly like the launcher), expected
# values from separate processes (one per build).
# ---------------------------------------------------------------------------
def _run_resolver_and_ns(repo: pathlib.Path, bin_dir: pathlib.Path,
                         envsh: pathlib.Path | None = None):
    """Run the REAL resolver, then compute the namespace in the SAME shell
    (exactly like the launcher: the resolver exports, then the launcher's
    python step reads the namespace)."""
    envsh = envsh or (repo / "scripts" / "env.sh")
    ns_py = repo / "_ns.py"
    ns_py.write_text(NS_CODE.format(impl=str(IMPL)))
    script = f"""
set -u
. "{envsh}"
KVP_LIVE_DIR="{repo}/live"
IMAGE=""
_kvp_resolve_runtime_image "{repo}" 0 || exit $?
echo "RESOLVED_BUILD=${{KVP_LMCACHE_BUILD:-<unset>}}"
echo "RESOLVED_RUNNING=${{KVP_RUNNING_LMCACHE_BUILD:-<unset>}}"
KVP_REPO_ROOT="{repo}" python3 "{ns_py}"
"""
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
               KVP_LIVE_DIR=str(repo / "live"))
    for var in ("IMAGE", "KVP_LMCACHE_BUILD", "KVP_RUNNING_LMCACHE_BUILD",
                "KVP_LMCACHE_IDENTITY", "KVP_LMC_VERSION", "KVP_VERIFY_SHALLOW",
                "KVP_SKIP_LMC_IDENTITY", "KVP_SKIP_IMAGE_CHECK"):
        env.pop(var, None)
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=60, env=env, cwd=str(repo))


def test_namespace_follows_running_image_not_history(tmp_path):
    """> live/image-identity.env says A, the launchable image carries B.
    The resolver exports B (KVP_RUNNING_LMCACHE_BUILD) and the namespace
    computed in the SAME shell equals the B-computed namespace (from a
    separate process) and differs from A's."""
    repo, bin_dir, ident_file = _make_repo(tmp_path, BUILD_B)
    r = _run_resolver_and_ns(repo, bin_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"RESOLVED_RUNNING={BUILD_B}" in r.stdout, (
        f"the resolver must export the RUNNING image's build:\n{r.stdout}\n{r.stderr}")
    ns_shell = r.stdout.strip().splitlines()[-1].strip()
    ns_A = _namespace_for(repo, bin_dir, BUILD_A)
    ns_B = _namespace_for(repo, bin_dir, BUILD_B)
    assert ns_shell == ns_B, (
        f"the namespace in the launcher shell must equal the B-computed one:\n"
        f"  shell {ns_shell}\n  B     {ns_B}")
    assert ns_shell != ns_A, "the live/ history (A) must NOT affect the namespace"


# ---------------------------------------------------------------------------
# (2) r21 L1: the gate cross-check without a pin
# ---------------------------------------------------------------------------
def test_gate_without_pin_crosschecks_build(tmp_path):
    """> No pin; live/ history says A; the container runs B. When the namespace
    was hashed from A and the engine runs B, the gate must refuse (exit 4)
    naming lmcache_build."""
    repo, bin_dir, ident_file = _make_repo(tmp_path, BUILD_B)
    (tmp_path / "cache").mkdir()
    gate_env = dict(os.environ,
                    PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                    KVP_REPO_ROOT=str(repo),
                    KVP_LIVE_DIR=str(repo / "live"),
                    KVP_CACHE_ROOT=str(tmp_path / "cache"))
    for var in ("KVP_LMCACHE_BUILD", "KVP_RUNNING_LMCACHE_BUILD",
                "KVP_SKIP_LMC_IDENTITY", "KVP_SKIP_IMAGE_CHECK",
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
# (3) r22 M4: DETECTORS of the real code (the r21 "mutation" tests asserted
# the behaviour of a hand-made mutant -- green at any state of the real code;
# the mutants are killed by the tests above instead)
# ---------------------------------------------------------------------------
def test_export_of_running_build_present():
    """> detector: the resolver exports KVP_RUNNING_LMCACHE_BUILD (the r21
    defect was the resolver overwriting the operator pin name)."""
    src = (ROOT / "scripts" / "env.sh").read_text()
    assert "KVP_RUNNING_LMCACHE_BUILD" in src, (
        "the resolver must export KVP_RUNNING_LMCACHE_BUILD")
    assert re.search(r"KVP_RUNNING_LMCACHE_BUILD=\"\$_wv\"", src), (
        "the export must carry the wheel_version captured from the deep check")
    # the pin name must NOT be written by the resolver (it is the operator's)
    resolver_start = src.index("_kvp_require_custom_image()")
    resolver = src[resolver_start:src.index("\n}\n", resolver_start)]
    assert "export KVP_LMCACHE_BUILD" not in resolver, (
        "the resolver must not export the operator pin name KVP_LMCACHE_BUILD")


def test_gate_expected_build_priority_present():
    """> detector: the gate's expected build is pin > KVP_RUNNING >
    read_lmcache_build (the r21 defect: only a pin was checked)."""
    src = (ROOT / "scripts" / "verify-engine-identity.sh").read_text()
    assert "KVP_RUNNING_LMCACHE_BUILD" in src, (
        "the gate must fall back to KVP_RUNNING_LMCACHE_BUILD")
    assert "read_lmcache_build" in src, (
        "the gate must fall back to read_lmcache_build()")


def test_read_prefers_running_export_present():
    """> detector: read_lmcache_build prefers KVP_RUNNING_LMCACHE_BUILD over
    the baked JSON and the live/ record."""
    src = (IMPL / "kvpersist" / "identity.py").read_text()
    assert "KVP_RUNNING_LMCACHE_BUILD" in src, (
        "read_lmcache_build must read KVP_RUNNING_LMCACHE_BUILD first")
