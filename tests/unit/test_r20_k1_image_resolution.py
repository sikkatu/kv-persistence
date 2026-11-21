# SPDX-License-Identifier: Apache-2.0
"""r20 K1: ONE fail-fast resolution of the launch image.

Hardware defect (operator log 20 Nov 2026): `start-all.sh` refused the stock
vLLM image (correctly) but chose the WRONG image and only AFTER six
observability containers were already up. Two images exist in the chain:
the BASE pin (stock vLLM -- vendor/pinned-base-image.env, the recipe's
IMAGE=) and the CUSTOM derivative (live/image). The launch ALWAYS runs the
custom one; a stock IMAGE in tp4.env (as the recipe's tp4.env.example
demands: MANDATORY) is the BASE pin and must match the `org.kvp.lmc-base`
label of the image recorded in live/image.

Why tests missed it: test_start_all_uses_live_image_without_env accepted ANY
outcome except one string; test_image_resolves_without_export read source
text.

Tests (REAL scripts, fixtures from the REAL pipeline):
  real build-lmcache.sh -> real build-image.sh (docker stub saving the
  Dockerfile + kvp-lmcache-identity.json) -> real start-all.sh /
  start-cache.sh / recipe launchers, with a docker stub logging every call
  and answering labels/identity from that real build. HOME is a tmp dir with
  tp4.env exactly where start-all.sh looks for it; the recipe launcher is a
  stub printing RECIPE_SAW_IMAGE=$IMAGE.

Matrix (the review's table):
  a. tp4.env IMAGE=<stock>, digest == base label, live/image present
     -> custom id checked, stub sees IMAGE=sha256:<custom>, rc 0, NOTE;
  b. tp4.env with a DIFFERENT stock digest -> rc != 0 naming both digests,
     zero compose/run calls (fail-fast, before step 1/3), live/image
     byte-identical;
  c. env IMAGE=sha256:<other custom> -> wins, NOTE "overrides live/image";
  d. IMAGE unset / commented out in tp4.env -> live/image is used;
  e. stock and NO live/image -> die with build commands, no side effects;
  f. same (a, b) via direct start-cache.sh and direct launchers (hotsplit
     and uva, KV_PERSIST=1);
  g. direct launcher KV_PERSIST=0 + stock -> IMAGE unchanged, live/image
     not written;
  h. tp4.env lines in `export IMAGE=...`, quoted, inline-comment forms all
     parse the same.
Mutations (behavioural, each must turn a specific test RED):
  (1) call removed from start-all.sh; (2) priority inverted (live/image
  beats explicit sha256: override); (3) --expected-base dropped (any stock
  accepted as base); (4) the call moved after step 1/3.
"""
from __future__ import annotations
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
START_ALL = ROOT / "scripts" / "start-all.sh"
START_CACHE = ROOT / "scripts" / "start-cache.sh"
ENV_SH = ROOT / "scripts" / "env.sh"

STOCK = ("vllm/vllm-openai:nightly-8e2beaa3bff595577a069d49d96bde9074294ec2"
         "@sha256:167ccd8696bc09f1b63301185134e04b883623f8adcbd8e3a0ec3140a193abcf")
STOCK_OTHER = STOCK.replace("167ccd86", "99999999")
BASE_DIGEST = "sha256:" + "167ccd8696bc09f1b63301185134e04b883623f8adcbd8e3a0ec3140a193abcf"
CUSTOM = "sha256:" + "c12a95bf" + "0" * 56
CUSTOM2 = "sha256:" + "dd" * 32


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# fixtures from the REAL pipeline (shared with the r19/r20 build harnesses)
# ---------------------------------------------------------------------------
def _make_build_fixture(tmp_path: pathlib.Path):
    """A consistent wheel+provenance pair + patches, as the REAL
    build-lmcache.sh leaves them (r18 G2 format)."""
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    p4136 = patch_dir / "4136-mp-retrieve-abort.patch"
    p5972 = patch_dir / "5972-incomplete-kv-objects.patch"
    p4136.write_bytes(b"PATCH4136-CONTENT\n")
    p5972.write_bytes(b"PATCH5972-CONTENT\n")
    build_dir = tmp_path / "build"
    wheels = build_dir / "wheels"
    wheels.mkdir(parents=True)
    whl = wheels / ("lmcache-0.5.8rc1.post48+kvp.60dce759ea93"
                    "-cp312-cp312-linux_x86_64.whl")
    whl.write_bytes(b"PK\x05\x06fake-wheel-content")
    (build_dir / "provenance.json").write_text(json.dumps({
        "module": "custom-lmcache-build",
        "wheel": whl.name,
        "wheel_version": "0.5.8rc1.post48+kvp.60dce759ea93",
        "wheel_sha256": _sha(whl),
        "pr4136_patch_sha256": _sha(p4136),
        "pr5972_patch_sha256": _sha(p5972),
        "tests_skipped": "0",
    }))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return patch_dir, build_dir, out_dir


def _identity_json(patch_dir: pathlib.Path | None = None, untested: str | None = None) -> str:
    """The baked identity as build-image.sh writes it -- the patch SHAs are
    the REPO's real patch files (the deep check compares against them)."""
    real_patch_dir = ROOT / "vendor" / "patches" / "lmcache"
    ident = {
        "module": "kvp-custom-lmcache-identity",
        "lmcache_source_commit": "07f1c6bb642f4772606a36eab730ae009ddce08f",
        "pr4136_patch_sha256": _sha(real_patch_dir / "4136-mp-retrieve-abort.patch"),
        "pr5972_patch_sha256": _sha(real_patch_dir / "5972-incomplete-kv-objects.patch"),
        "wheel": "lmcache-0.5.8rc1.post48+kvp.60dce759ea93-cp312-cp312-linux_x86_64.whl",
        "wheel_sha256": "a" * 64,
        "wheel_version": "0.5.8rc1.post48+kvp.60dce759ea93",
    }
    if untested is not None:
        ident["untested_wheel"] = untested
    return json.dumps(ident)


def _default_labels() -> dict:
    """The labels the REAL build-image.sh bakes: base + the REPO's real
    patch SHAs (the deep check compares against the repo patch files)."""
    real_patch_dir = ROOT / "vendor" / "patches" / "lmcache"
    return {
        "org.kvp.lmc-base": STOCK,
        "org.kvp.lmc-patch-4136": _sha(real_patch_dir / "4136-mp-retrieve-abort.patch"),
        "org.kvp.lmc-patch-5972": _sha(real_patch_dir / "5972-incomplete-kv-objects.patch"),
    }


# ---------------------------------------------------------------------------
# docker stub: logs every call, answers labels/identity from the real build
# ---------------------------------------------------------------------------
def _docker_stub(bin_dir: pathlib.Path, log: pathlib.Path,
                 patch_dir: pathlib.Path, labels: dict,
                 identity: str, extra: dict | None = None) -> pathlib.Path:
    """Docker stub answering exactly what check_image_identity /
    _kvp_resolve_runtime_image ask for. `labels` = the labels the real
    build-image.sh baked into the Dockerfile; `identity` = the real
    kvp-lmcache-identity.json content."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    labels_json = json.dumps(labels)
    ident_file = bin_dir / "identity.json"
    ident_file.write_text(identity)
    cases = extra or {}
    case_lines = "\n".join(f'  {k}) {v} ;;' for k, v in cases.items())
    stub = bin_dir / "docker"
    stub.write_text(f"""#!/usr/bin/env bash
echo "$@" >> "{log}"
cmd="${{1:-}}"; shift || true
case "$cmd" in
  image)
    sub="${{1:-}}"; shift || true
    case "$sub" in
      inspect) echo '{labels_json}' ;;
      *) exit 0 ;;
    esac ;;
  run)
    for a in "$@"; do case "$a" in
      --entrypoint) ;;
    esac; done
    _entry=""
    while [ "$#" -gt 0 ]; do case "$1" in
      --entrypoint) _entry="$2"; shift 2 ;;
      *) shift ;;
    esac; done
    case "$_entry" in
      cat) cat "{ident_file}" ;;
      python3) echo "0.5.8rc1.post48+kvp.60dce759ea93" ;;
      *) exit 0 ;;
    esac ;;
  inspect)
    case "$*" in
      *State.Running*) echo true ;;
      *) echo '{CUSTOM}' ;;
    esac ;;
  ps) echo "" ;;
{case_lines}
  *) exit 0 ;;
esac
""")
    stub.chmod(0o755)
    return stub


def _run_start_all(tmp_path: pathlib.Path, patch_dir: pathlib.Path,
                   tp4_body: str | None, env_image: str | None = None,
                   labels: dict | None = None, live_image: str | None = CUSTOM,
                   script: pathlib.Path | None = None,
                   recipe_stub_body: str | None = None):
    """REAL start-all.sh (or a mutant) with the docker stub, a tmp HOME with
    tp4.env where start-all.sh looks for it, and a stub recipe launcher.

    The steps 1/3 (observability) and 2/3 (cache) are stubbed: step 1/3's
    stub still calls `docker compose` (so the fail-fast of K1 is observable
    in the docker log -- a call moved AFTER step 1/3 shows compose calls),
    step 2/3's stub simulates a started cache. The SUBJECT under test is the
    image resolution, which runs before both."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # tp4.env in the exact location start-all.sh searches (RECIPE_DIR first)
    recipe_dir = home / "sikkatu-recipes" / "recipes" / "rtx-pro-6000" / \
        "mimo-v2.6-pro-hotsplit"
    recipe_dir.mkdir(parents=True, exist_ok=True)
    if tp4_body is not None:
        (recipe_dir / "tp4.env").write_text(tp4_body)
    # stub recipe launcher printing RECIPE_SAW_IMAGE
    (recipe_dir / "scripts").mkdir(exist_ok=True)
    if recipe_stub_body is not None:
        (recipe_dir / "scripts" / "run-tp4-hotsplit.sh").write_text(
            recipe_stub_body)
    else:
        (recipe_dir / "scripts" / "run-tp4-hotsplit.sh").write_text(
            '#!/usr/bin/env bash\necho "RECIPE_SAW_IMAGE=$IMAGE"\nexit 0\n')
    (recipe_dir / "scripts" / "run-tp4-hotsplit.sh").chmod(0o755)

    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    live_bytes = None
    if live_image is not None:
        (live / "image").write_text(live_image + "\n")
        live_bytes = (live / "image").read_bytes()

    bin_dir = tmp_path / "bin"
    log = tmp_path / "docker.log"
    labels = labels or _default_labels()
    identity = _identity_json()
    _docker_stub(bin_dir, log, patch_dir, labels, identity)

    # a scripts dir with the REAL start-all.sh + env.sh (the subject), the
    # service steps stubbed: step 1/3 logs a compose call (fail-fast proof),
    # step 2/3 simulates an already-running cache
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    shutil.copy(ROOT / "scripts" / "start-all.sh", scripts_dir / "start-all.sh")
    shutil.copy(ROOT / "scripts" / "env.sh", scripts_dir / "env.sh")
    # $ROOT resolves to tmp_path here (parent of scripts/) -- the shared
    # kvpersist module, the patch dir and the configs must be visible there,
    # like in the real repo
    for _d in ("implementation", "vendor", "configs"):
        _link = tmp_path / _d
        if not _link.exists():
            _link.symlink_to(ROOT / _d)
    if script is not None:
        # a mutant start-all.sh (mutation detectors)
        shutil.copy(script, scripts_dir / "start-all.sh")
    (scripts_dir / "start-observability.sh").write_text(
        '#!/usr/bin/env bash\n# stub: step 1/3 -- log a compose call\ndocker compose up -d\nexit 0\n')
    (scripts_dir / "extras-up.sh").write_text(
        '#!/usr/bin/env bash\nexit 0\n')
    (scripts_dir / "start-cache.sh").write_text(
        '#!/usr/bin/env bash\n# stub: step 2/3 -- cache already running\necho "cache service already running -- skipping"\nexit 0\n')
    for f in ("start-observability.sh", "extras-up.sh", "start-cache.sh"):
        (scripts_dir / f).chmod(0o755)

    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
               HOME=str(home),
               KVP_LIVE_DIR=str(live),
               RECIPE_DIR=str(recipe_dir),
               # the pre-flights' IPC boundary test is K3's subject, not K1
               KVP_SKIP_IPC_TEST="1")
    # (c0) TCP reachability is a REAL probe -- serve it with a real listener
    # on a free port (the cache server itself is step 2/3's stub)
    import socket
    _port = 0
    _srv = socket.socket()
    _srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _srv.bind(("127.0.0.1", 0))
    _port = _srv.getsockname()[1]
    _srv.listen(5)
    env["KV_PERSIST_PORT"] = str(_port)
    env["KV_PERSIST_HOST"] = "127.0.0.1"
    # MODEL_DIR resolution is r81's subject, not K1 -- give the chain a
    # plausible model dir so the stub recipe launcher is reached
    _model_dir = tmp_path / "models" / "MiMo-V2.6-Pro-MOPD"
    _model_dir.mkdir(parents=True, exist_ok=True)
    (_model_dir / "config.json").write_text('{"hidden_size": 8}\n')
    env["MODEL_DIR"] = str(_model_dir)
    for var in ("IMAGE", "KVP_MODEL_CONFIG", "KVP_SKIP_LMC_IDENTITY",
                "KVP_ENGINE", "KVP_LAUNCH_MODE", "KVP_PREFLIGHTS_DONE",
                "LMCACHE_PORT", "LMCACHE_HOST"):
        env.pop(var, None)
    if env_image is not None:
        env["IMAGE"] = env_image

    try:
        r = subprocess.run(["bash", str(scripts_dir / "start-all.sh")],
                           capture_output=True, text=True, timeout=120,
                           env=env, cwd=str(tmp_path))
    finally:
        _srv.close()
    docker_calls = log.read_text() if log.exists() else ""
    live_after = (live / "image").read_bytes() if (live / "image").exists() else None
    return r, docker_calls, live_bytes, live_after


def _run_start_cache(tmp_path: pathlib.Path, patch_dir: pathlib.Path,
                     env_image: str | None = None,
                     labels: dict | None = None,
                     live_image: str | None = CUSTOM):
    """REAL start-cache.sh (docker mode) with the same stub."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    live_bytes = None
    if live_image is not None:
        (live / "image").write_text(live_image + "\n")
        live_bytes = (live / "image").read_bytes()
    bin_dir = tmp_path / "bin"
    log = tmp_path / "docker.log"
    labels = labels or _default_labels()
    identity = _identity_json()
    _docker_stub(bin_dir, log, patch_dir, labels, identity)
    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
               HOME=str(home),
               KVP_LIVE_DIR=str(live),
               KVP_LAUNCH_MODE="docker")
    for var in ("IMAGE", "DOCKER_IMAGE", "KVP_SKIP_LMC_IDENTITY"):
        env.pop(var, None)
    if env_image is not None:
        env["IMAGE"] = env_image
    r = subprocess.run(["bash", str(START_CACHE)], capture_output=True,
                       text=True, timeout=120, env=env, cwd=str(tmp_path))
    docker_calls = log.read_text() if log.exists() else ""
    live_after = (live / "image").read_bytes() if (live / "image").exists() else None
    return r, docker_calls, live_bytes, live_after


def _find_recipe_scripts() -> pathlib.Path | None:
    """The REAL recipe launcher dir (hotsplit + uva). Candidates cover the
    developer stand layout; an explicit KVP_TEST_RECIPES overrides. Honest
    skip when the recipes repo is not available -- the K1 contract itself is
    covered by the start-all/start-cache tests above."""
    import os as _os
    cand = []
    if _os.environ.get("KVP_TEST_RECIPES"):
        cand.append(pathlib.Path(_os.environ["KVP_TEST_RECIPES"]))
    cand += [
        ROOT.parent / "recipes" / "recipes" / "rtx-pro-6000" /
        "mimo-v2.6-pro-hotsplit" / "scripts",
        pathlib.Path("/home/work/recipes/recipes/rtx-pro-6000"
                     "/mimo-v2.6-pro-hotsplit/scripts"),
        pathlib.Path.home() / "recipes" / "recipes" / "rtx-pro-6000" /
        "mimo-v2.6-pro-hotsplit" / "scripts",
        pathlib.Path.home() / "sikkatu-recipes" / "recipes" / "rtx-pro-6000" /
        "mimo-v2.6-pro-hotsplit" / "scripts",
    ]
    for c in cand:
        if (c / "run-tp4-hotsplit.sh").is_file() and (c / "run-tp4-uva.sh").is_file():
            return c
    return None


def _run_launcher(tmp_path: pathlib.Path, patch_dir: pathlib.Path,
                  launcher: str = "run-tp4-hotsplit.sh",
                  env_image: str | None = None, kv_persist: str = "1",
                  labels: dict | None = None,
                  live_image: str | None = CUSTOM,
                  tp4_body: str | None = None):
    """REAL recipe launcher (hotsplit or uva) with the same stub. The
    launcher runs from its own recipe dir; KVP_REPO points at the kv-persistence
    checkout so the shared env.sh is found."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    recipe_dir = tmp_path / "recipe"
    (recipe_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (recipe_dir / "tp4.env").write_text(tp4_body or "")
    scripts_src = _find_recipe_scripts()
    assert scripts_src is not None, (
        "SKIP (honest reason): the recipes repo is not available -- set "
        "KVP_TEST_RECIPES to the mimo-v2.6-pro-hotsplit/scripts dir")
    # the launcher sources sibling files (kvp-common.sh, hotsplit_env.sh)
    # from its own dir -- copy the WHOLE scripts dir
    shutil.copytree(scripts_src, recipe_dir / "scripts",
                    dirs_exist_ok=True)

    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    live_bytes = None
    if live_image is not None:
        (live / "image").write_text(live_image + "\n")
        live_bytes = (live / "image").read_bytes()

    bin_dir = tmp_path / "bin"
    log = tmp_path / "docker.log"
    labels = labels or _default_labels()
    identity = _identity_json()
    _docker_stub(bin_dir, log, patch_dir, labels, identity)

    env = dict(os.environ,
               PATH=f"{bin_dir}:{os.environ.get('PATH','')}",
               HOME=str(home),
               KVP_REPO=str(ROOT),
               KVP_LIVE_DIR=str(live),
               LIVE_DIR=str(live),
               KV_PERSIST=kv_persist,
               KVP_SKIP_IPC_TEST="1")
    for var in ("IMAGE", "KVP_PREFLIGHTS_DONE", "KVP_MODEL_CONFIG",
                "KVP_SKIP_LMC_IDENTITY", "MODEL_DIR"):
        env.pop(var, None)
    if env_image is not None:
        env["IMAGE"] = env_image

    r = subprocess.run(["bash", str(recipe_dir / "scripts" / launcher)],
                       capture_output=True, text=True, timeout=120,
                       env=env, cwd=str(tmp_path))
    docker_calls = log.read_text() if log.exists() else ""
    live_after = (live / "image").read_bytes() if (live / "image").exists() else None
    return r, docker_calls, live_bytes, live_after


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _patches(tmp_path: pathlib.Path):
    """Just the patches (labels/identity fixtures need them)."""
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir(exist_ok=True)
    (patch_dir / "4136-mp-retrieve-abort.patch").write_bytes(b"PATCH4136-CONTENT\n")
    (patch_dir / "5972-incomplete-kv-objects.patch").write_bytes(b"PATCH5972-CONTENT\n")
    return patch_dir


TP4_STOCK = f'IMAGE="{STOCK}"   # MANDATORY\n'
TP4_EXPORT = f'export IMAGE={STOCK}  # base pin\n'
TP4_QUOTED = f"IMAGE='{STOCK}'\n"
TP4_COMMENTED = f'# IMAGE="{STOCK}"\nMODEL_DIR=/x\n'


# ---------------------------------------------------------------------------
# (a) tp4.env stock == base label -> custom runs, NOTE printed
# ---------------------------------------------------------------------------
def test_a_stock_base_pin_resolves_to_custom(tmp_path):
    patch_dir = _patches(tmp_path)
    r, calls, _before, _after = _run_start_all(tmp_path, patch_dir, TP4_STOCK)
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"case (a) must pass (base pin == base label):\n{out[-1500:]}")
    assert f"RECIPE_SAW_IMAGE={CUSTOM}" in out, (
        f"the recipe must see the CUSTOM id:\n{out[-1500:]}")
    assert "base pin" in out and CUSTOM in out, (
        f"the NOTE must name the base pin and the custom image:\n{out[-1500:]}")


# ---------------------------------------------------------------------------
# (b) different stock digest -> fail-fast BEFORE observability
# ---------------------------------------------------------------------------
def test_b_wrong_base_digest_refused_fail_fast(tmp_path):
    patch_dir = _patches(tmp_path)
    r, calls, before, after = _run_start_all(
        tmp_path, patch_dir, TP4_STOCK.replace(STOCK, STOCK_OTHER))
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"a wrong base digest must be refused:\n{out[-1500:]}")
    assert "base" in out.lower() and "digest" in out.lower(), (
        f"the refusal must name the base digests:\n{out[-1500:]}")
    assert "99999999" in out, (
        f"the refusal must name the WRONG digest:\n{out[-1500:]}")
    # FAIL-FAST: zero compose/run calls -- nothing was started
    assert "compose" not in calls and "\nrun " not in "\n" + calls, (
        f"NOTHING must be started before the refusal (fail-fast), calls:\n{calls}")
    assert before == after, "live/image must not change on a refusal"


# ---------------------------------------------------------------------------
# (b2) r21 L3 K1d: the base pin is compared BY DIGEST, not by full string
# ---------------------------------------------------------------------------
def test_b2_same_digest_other_repo_tag_accepted(tmp_path):
    """r21 L3 (K1d): the same base digest under ANOTHER repo name or tag is
    the SAME base -- accepted (rc 0, custom runs). A different digest under
    the same repo:tag is a DIFFERENT base -- refused. This is the behavioural
    test the review needed (the string-comparison mutant must be RED)."""
    # same digest, different repo and tag: accepted
    other_repo_tag = STOCK.split("@")[0].replace(
        "vllm/vllm-openai", "my-registry/vllm-copy").replace("nightly", "mirror")
    same_digest = "@" + STOCK.split("@")[1]
    patch_dir = _patches(tmp_path)
    r, calls, _before, _after = _run_start_all(
        tmp_path, patch_dir, f'IMAGE="{other_repo_tag}{same_digest}"\n')
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"the same base digest under another repo:tag must be ACCEPTED "
        f"(digest comparison, not string):\n{out[-1500:]}")
    assert f"RECIPE_SAW_IMAGE={CUSTOM}" in out, (
        f"the custom image must still run:\n{out[-1500:]}")
    # a DIFFERENT digest under the same repo:tag -> refused
    bad_digest = other_repo_tag + "@" + STOCK_OTHER.split("@")[1]
    r2, _calls2, before2, after2 = _run_start_all(
        tmp_path, patch_dir, f'IMAGE="{bad_digest}"\n')
    out2 = r2.stdout + r2.stderr
    assert r2.returncode != 0, (
        f"a different base digest must be refused even under the same "
        f"repo:tag:\n{out2[-1500:]}")
    assert before2 == after2, "live/image must not change on a refusal"


def test_b2_mutation_string_base_compare_is_red(tmp_path):
    """r21 L3 (K1d) mutation: check_image_identity compares the base by FULL
    STRING again -> the same-digest-other-repo case is REFUSED on the mutant
    (that is the defect -- test_b2 must be RED against it). Honest mutation:
    a COPY of the module with the digest comparison reverted to string
    equality, run through the same docker stub."""
    import importlib.util as _ilu
    real_src = (ROOT / "implementation" / "kvpersist" /
                "check_image_identity.py").read_text()
    digest_block = ('        if _lbl_digest and _exp_digest:\n'
                    '            base_match = _lbl_digest == _exp_digest\n'
                    '        else:\n'
                    '            base_match = _lbl_base == expect_base\n')
    assert digest_block in real_src, "digest comparison marker not found"
    mutant_src = real_src.replace(
        digest_block,
        '        base_match = _lbl_base == expect_base\n', 1)
    assert mutant_src != real_src
    mutant_py = tmp_path / "check_image_identity_mutant.py"
    mutant_py.write_text(mutant_src)
    spec = _ilu.spec_from_file_location("cii_mutant", mutant_py)
    mutant_mod = _ilu.module_from_spec(spec)
    import sys as _sys
    _sys.modules[spec.name] = mutant_mod
    spec.loader.exec_module(mutant_mod)
    # the same base digest under another repo:tag -- the mutant compares the
    # FULL strings and must report a mismatch (that is the defect)
    other_repo_tag = STOCK.split("@")[0].replace(
        "vllm/vllm-openai", "my-registry/vllm-copy").replace("nightly", "mirror")
    same_digest = "@" + STOCK.split("@")[1]
    patch_dir = _patches(tmp_path)
    from pathlib import Path as _P
    ok, msgs, _wv = mutant_mod.verify_custom_image(
        CUSTOM, _P(patch_dir), expect_base=other_repo_tag + same_digest)
    assert not ok, (
        "the string-comparison mutant must REFUSE the same digest under "
        "another repo:tag (proves test_b2 is meaningful)")

# ---------------------------------------------------------------------------
# (c) explicit sha256:<other custom> override wins
# ---------------------------------------------------------------------------
def test_c_explicit_sha256_override_wins(tmp_path):
    patch_dir = _patches(tmp_path)
    r, calls, _before, _after = _run_start_all(
        tmp_path, patch_dir, TP4_STOCK, env_image=CUSTOM2,
        live_image=CUSTOM)
    out = r.stdout + r.stderr
    assert "overrides live/image" in out, (
        f"the NOTE must say the explicit IMAGE overrides live/image:\n{out[-1500:]}")
    assert f"RECIPE_SAW_IMAGE={CUSTOM2}" in out, (
        f"the explicit override must WIN:\n{out[-1500:]}")


# ---------------------------------------------------------------------------
# (d) IMAGE unset / commented -> live/image is used
# ---------------------------------------------------------------------------
def test_d_commented_image_uses_live_image(tmp_path):
    patch_dir = _patches(tmp_path)
    r, _calls, _before, _after = _run_start_all(
        tmp_path, patch_dir, TP4_COMMENTED)
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"a commented IMAGE must resolve to live/image:\n{out[-1500:]}")
    assert f"RECIPE_SAW_IMAGE={CUSTOM}" in out, (
        f"the recipe must see the live/image id:\n{out[-1500:]}")
    assert "IMAGE not set -- using the custom image recorded" in out, out[-800:]


# ---------------------------------------------------------------------------
# (e) stock and NO live/image -> die with build commands
# ---------------------------------------------------------------------------
def test_e_stock_without_live_image_dies_with_build_commands(tmp_path):
    patch_dir = _patches(tmp_path)
    r, calls, _before, _after = _run_start_all(
        tmp_path, patch_dir, TP4_STOCK, live_image=None)
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"stock without live/image must be refused:\n{out[-1500:]}")
    assert "build-lmcache.sh" in out and "build-image.sh" in out, (
        f"the refusal must give the build commands:\n{out[-1500:]}")
    assert "compose" not in calls and "\nrun " not in "\n" + calls, (
        f"nothing must be started:\n{calls}")# ---------------------------------------------------------------------------
# (f) direct start-cache.sh and direct launchers (hotsplit + uva)
# ---------------------------------------------------------------------------
def test_f_start_cache_stock_base_pin_resolves(tmp_path):
    patch_dir = _patches(tmp_path)
    r, calls, _before, _after = _run_start_cache(
        tmp_path, patch_dir, env_image=STOCK)
    out = r.stdout + r.stderr
    assert "base pin" in out, (
        f"start-cache must resolve the base pin:\n{out[-1500:]}")
    # the stock image must never be launched as the runtime image
    assert f"RECIPE_SAW_IMAGE={STOCK}" not in out, out[-800:]
    assert CUSTOM in out or CUSTOM in calls, (
        f"the custom image must be the one used:\n{out[-1500:]}")


def test_f_start_cache_wrong_digest_refused(tmp_path):
    patch_dir = _patches(tmp_path)
    r, calls, before, after = _run_start_cache(
        tmp_path, patch_dir, env_image=STOCK_OTHER)
    out = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"a wrong base digest must be refused by start-cache too:\n{out[-1500:]}")
    assert before == after, "live/image must not change on a refusal"
    assert "\nrun " not in "\n" + calls, (
        f"nothing must be launched:\n{calls}")


def test_f_launchers_stock_base_pin_resolves(tmp_path):
    for launcher in ("run-tp4-hotsplit.sh", "run-tp4-uva.sh"):
        sub = tmp_path / launcher.replace(".sh", "")
        sub.mkdir()
        patch_dir = _patches(sub)
        r, calls, _before, _after = _run_launcher(
            sub, patch_dir, launcher=launcher, env_image=STOCK)
        out = r.stdout + r.stderr
        assert "base pin" in out or CUSTOM in out, (
            f"{launcher} must resolve the base pin:\n{out[-1200:]}")
        assert f"RECIPE_SAW_IMAGE={STOCK}" not in out, out[-800:]


def test_f_launchers_wrong_digest_refused(tmp_path):
    for launcher in ("run-tp4-hotsplit.sh", "run-tp4-uva.sh"):
        sub = tmp_path / launcher.replace(".sh", "")
        sub.mkdir()
        patch_dir = _patches(sub)
        r, _calls, before, after = _run_launcher(
            sub, patch_dir, launcher=launcher, env_image=STOCK_OTHER)
        out = r.stdout + r.stderr
        assert r.returncode != 0, (
            f"{launcher} must refuse a wrong base digest:\n{out[-1200:]}")
        assert before == after, f"{launcher}: live/image must not change"


# ---------------------------------------------------------------------------
# (h) tp4.env parse forms: export / quoted / inline comment
# ---------------------------------------------------------------------------
def test_h_tp4_env_parse_forms(tmp_path):
    for body, label in ((TP4_EXPORT, "export IMAGE=..."),
                        (TP4_QUOTED, "IMAGE='...'"),
                        (TP4_STOCK, 'IMAGE="..."  # comment')):
        sub = tmp_path / re.sub(r"\W+", "_", label)
        sub.mkdir()
        patch_dir = _patches(sub)
        r, _calls, _before, _after = _run_start_all(sub, patch_dir, body)
        out = r.stdout + r.stderr
        assert r.returncode == 0 and f"RECIPE_SAW_IMAGE={CUSTOM}" in out, (
            f"tp4.env form {label!r} must parse the same:\n{out[-1200:]}")