# SPDX-License-Identifier: Apache-2.0
"""r22 M2: the `IMAGE=sha256:<id>` and `IMAGE == live/image` resolver paths
must be protected by tests.

Facts (review, HEAD 7a9e615): replacing `_kvp_require_custom_image "$IMAGE" ...`
with `true` in either branch left the whole suite green -- no test covered
these paths. This is exactly where `IMAGE=sha256:<stock-id>` could launch a
stock image with the KV chain (forbidden).

Contract (r22 M2, behavioural on the REAL _kvp_resolve_runtime_image):
- IMAGE=sha256:<stock id> (labels WITHOUT org.kvp.*): rc 1, "NOT the custom",
  no build exported.
- IMAGE=sha256:<other custom D> with live/image=B: D's build is exported
  (explicit IMAGE wins), the namespace follows D.
- IMAGE == live/image and the image fails the deep check: rc 1.
- Mutations: the deep check dropped in either branch -> the scenarios above
  break (the detectors run the same scenarios on a mutant env.sh).
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMPL = ROOT / "implementation"
sys.path.insert(0, str(IMPL))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

import test_r21_l1_build_binds_to_running_image as r21  # noqa: E402

BUILD_A = r21.BUILD_A      # the stale live/ history
BUILD_B = r21.BUILD_B      # the image in live/image
BUILD_D = "0.5.8rc1.post48+kvp.dddddddddddd"   # a different custom build


def _labels_for(stock: bool) -> dict:
    if stock:   # a stock image: NO org.kvp.* labels at all
        return {"org.some.other": "x"}
    return r21._default_labels(
        ROOT / "vendor" / "patches" / "lmcache")


def _run_resolver_with(repo: pathlib.Path, bin_dir: pathlib.Path,
                       image: str, envsh: pathlib.Path | None = None,
                       expect_base_ok: bool = True):
    """Run the REAL resolver with IMAGE=<image>; the docker stub answers the
    deep check per `expect_base_ok` (labels real / stock)."""
    envsh = envsh or (repo / "scripts" / "env.sh")
    script = f"""
set -u
. "{envsh}"
KVP_LIVE_DIR="{repo}/live"
IMAGE="{image}"
_kvp_resolve_runtime_image "{repo}" 0
rc=$?
echo "RC=$rc"
echo "RESOLVED_IMAGE=${{IMAGE:-<unset>}}"
echo "RESOLVED_RUNNING=${{KVP_RUNNING_LMCACHE_BUILD:-<unset>}}"
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


def _make_repo_stock(tmp_path: pathlib.Path, image_build: str):
    """The r21 mini repo, but the docker stub serves a STOCK image (no
    org.kvp.* labels) when asked for the explicit sha256 override."""
    repo, bin_dir, ident_file = r21._make_repo(tmp_path, image_build)
    # rebuild the docker stub: identity/labels of the CUSTOM build, but the
    # image-inspect probe answers stock labels (no org.kvp.*)
    labels_stock = json.dumps({"org.other": "stock"})
    labels_custom = json.dumps(r21._default_labels(
        ROOT / "vendor" / "patches" / "lmcache"))
    (bin_dir / "docker").write_text(f"""#!/usr/bin/env bash
cmd="${{1:-}}"; shift || true
_all="$*"
case "$cmd" in
  image)
    sub="${{1:-}}"; shift || true
    case "$sub" in
      inspect)
        case "$_all" in
          *sha256:{"e" * 64}*) echo '{labels_stock}' ;;  # the STOCK override
          *) echo '{labels_custom}' ;;
        esac ;;
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
          *importlib.metadata*) echo "{image_build}"; exit 0 ;;
        esac
        exit 0 ;;
    esac ;;
  exec) cat "{ident_file}" ;;
  inspect) echo "sha256:{'c' * 64}" ;;
  *) exit 0 ;;
esac
""")
    (bin_dir / "docker").chmod(0o755)
    return repo, bin_dir, ident_file


STOCK_SHA = "sha256:" + "e" * 64     # the stock image id (no org.kvp.* labels)


def test_m2_explicit_sha256_stock_refused(tmp_path):
    """> IMAGE=sha256:<stock id> -> rc 1, 'NOT the custom', no build exported."""
    repo, bin_dir, ident_file = _make_repo_stock(tmp_path, BUILD_B)
    r = _run_resolver_with(repo, bin_dir, STOCK_SHA)
    out = r.stdout + r.stderr
    assert "RC=1" in r.stdout, f"a stock sha256 override must be refused:\n{out[-1000:]}"
    assert "NOT the custom" in out, out[-800:]
    assert "RESOLVED_RUNNING=<unset>" in r.stdout, (
        f"no build may be exported for a stock image:\n{out[-800:]}")


def test_m2_explicit_sha256_custom_wins(tmp_path):
    """> IMAGE=sha256:<custom D> with live/image=B: D's build is exported
    (the explicit IMAGE wins) and the namespace follows D, not B/A."""
    repo, bin_dir, ident_file = r21._make_repo(tmp_path, BUILD_D)
    r = _run_resolver_with(repo, bin_dir, "sha256:" + "d" * 64)
    out = r.stdout + r.stderr
    assert "RC=0" in r.stdout, f"an explicit custom override must resolve:\n{out[-1000:]}"
    assert f"RESOLVED_RUNNING={BUILD_D}" in r.stdout, (
        f"the explicit custom image's build must be exported (D):\n{out[-1000:]}")
    ns_D = r21._namespace_for(repo, bin_dir, BUILD_D)
    ns_B = r21._namespace_for(repo, bin_dir, BUILD_B)
    ns_A = r21._namespace_for(repo, bin_dir, BUILD_A)
    assert ns_D != ns_B and ns_D != ns_A, (
        "the namespace must follow the explicit image build D")


def test_m2_live_image_fails_deep_check_refused(tmp_path):
    """> IMAGE == live/image and the deep check fails -> rc 1."""
    import hashlib
    repo, bin_dir, ident_file = r21._make_repo(tmp_path, BUILD_B)
    # make the deep check FAIL: the identity JSON is unreadable (empty file)
    ident_file.write_text("")
    r = _run_resolver_with(repo, bin_dir, "sha256:" + "c" * 64)
    out = r.stdout + r.stderr
    assert "RC=1" in r.stdout, (
        f"IMAGE == live/image with a failing deep check must be refused:\n{out[-1000:]}")
    assert "RESOLVED_RUNNING=<unset>" in r.stdout, out[-800:]


# ---------------------------------------------------------------------------
# Mutations (r22 M2): the deep check dropped in each branch -> the scenarios
# above break. Detectors run the SAME scenario on a mutant env.sh.
# ---------------------------------------------------------------------------
def _mutant_envsh_without_deep_check(repo: pathlib.Path, branch: str) -> pathlib.Path:
    """Copy env.sh with `_kvp_require_custom_image ...` replaced by `true` in
    the given branch ('sha256' or 'live')."""
    src = (repo / "scripts" / "env.sh").read_text()
    if branch == "sha256":
        marker = ('      _kvp_require_custom_image "$IMAGE" "$root" \\\n'
                  '        || { echo "ERROR: IMAGE \'$IMAGE\' is NOT the custom')
    elif branch == "live":
        marker = ('    _kvp_require_custom_image "$IMAGE" "$root" \\\n'
                  '      || { echo "ERROR: IMAGE \'$IMAGE\' is NOT the custom LMCache derivative." >&2\n'
                  '           echo "  Fix: rebuild with bash scripts/build-lmcache.sh && bash scripts/build-image.sh" >&2')
    else:
        raise ValueError(branch)
    assert marker in src, f"branch marker not found: {branch}"
    mutant = src.replace(marker, marker.replace("_kvp_require_custom_image", "true"), 1)
    assert mutant != src
    p = repo / "scripts" / f"env-mutant-{branch}.sh"
    p.write_text(mutant)
    return p


def test_m2_mutation_deep_check_dropped_in_sha256_branch(tmp_path):
    """> Mutation (detector): the deep check dropped in the sha256:* branch ->
    the stock-override scenario must break (a stock image would be accepted).
    Proves test_m2_explicit_sha256_stock_refused is meaningful."""
    repo, bin_dir, ident_file = _make_repo_stock(tmp_path, BUILD_B)
    mutant = _mutant_envsh_without_deep_check(repo, "sha256")
    r = _run_resolver_with(repo, bin_dir, STOCK_SHA, envsh=mutant)
    # the mutant accepts the stock image (rc 0) -- the defect; the real test
    # (RC=1 + NOT the custom) would be RED against this mutant
    assert "RC=0" in r.stdout, (
        f"the mutant must accept the stock image (that is the defect):\n{r.stdout}\n{r.stderr}")


def test_m2_mutation_deep_check_dropped_in_live_branch(tmp_path):
    """> Mutation (detector): the deep check dropped in the `== live/image`
    branch -> the failing-deep-check scenario must break (rc 0).
    Proves test_m2_live_image_fails_deep_check_refused is meaningful."""
    repo, bin_dir, ident_file = r21._make_repo(tmp_path, BUILD_B)
    ident_file.write_text("")   # the deep check would fail
    mutant = _mutant_envsh_without_deep_check(repo, "live")
    r = _run_resolver_with(repo, bin_dir, "sha256:" + "c" * 64, envsh=mutant)
    assert "RC=0" in r.stdout, (
        f"the mutant must accept the broken live/image (the defect):\n"
        f"{r.stdout}\n{r.stderr}")