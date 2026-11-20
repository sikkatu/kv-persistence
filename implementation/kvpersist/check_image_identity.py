# SPDX-License-Identifier: Apache-2.0
"""Refuse to run a vLLM/model image that does NOT carry the custom LMCache build.

r2 P0-1: after the obsolete downstream race-guard was removed from recipes
(799d634), the ONLY remaining retrieve/abort fix lives inside the custom
LMCache build baked into the derivative image (see vendor/patches/lmcache/
and docs/lmcache-build.md). Launching a stock vllm-openai image would silently
run upstream LMCache WITHOUT the #4136/#5972 fixes: engine crashes on every
retrieve/abort race and incomplete KV objects get persisted.

`scripts/build-image.sh` labels the derived image:
    org.kvp.lmc-base          -- the pinned vLLM base image
    org.kvp.lmc-patch-4136    -- sha256 of 4136-mp-retrieve-abort.patch
    org.kvp.lmc-patch-5972    -- sha256 of 5972-incomplete-kv-objects.patch

This module reads the LABELS OUT OF A LOCAL IMAGE via `docker image inspect`
(no container launch) and compares them against the patch files in the repo.
Any mismatch / missing label is a MISMATCH -- the caller (run-tp4-*.sh,
start-cache.sh) must refuse to launch.
"""

from __future__ import annotations

# Standard
import argparse
import json
import pathlib
import re
import subprocess
import sys

LABEL_BASE = "org.kvp.lmc-base"
LABEL_4136 = "org.kvp.lmc-patch-4136"
LABEL_5972 = "org.kvp.lmc-patch-5972"
REQUIRED_LABELS = (LABEL_BASE, LABEL_4136, LABEL_5972)

ID_RE = re.compile(r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}$")


def spec_digest(spec: str) -> str | None:
    """The digest part of an image spec (r20 K1): `repo:tag@sha256:<64hex>`
    and bare `sha256:<64hex>` both yield `sha256:<64hex>`. None when the spec
    carries no digest. The SAME image under another tag / repo name has the
    same digest -- base-pin comparison must follow the digest, not the full
    string (a retagged base must not be a false refusal)."""
    m = DIGEST_RE.search((spec or "").strip())
    return m.group(0) if m else None

# r3 P1-3: deep verification runs normal sub-processes inside the image read
# only (no GPU, no model load) -- reading the baked identity file and probing
# the compiled cuda_ops extension. LABELs alone are forgeable with one line.
IDENT_JSON_PATH = "/etc/kvp-lmcache-identity.json"
CUDA_OPS_PROBE = (
    "import importlib.util as u, sys; "
    "spec = u.find_spec('lmcache.cuda_ops'); "
    "sys.exit(0 if spec else 1)"
)


def is_pinned_image_spec(image: str) -> tuple[bool, str]:
    """Validate the IMAGE pin format.

    Two accepted forms (r2 P0-1.2 -- a locally built image has no RepoDigest):
      1. <repo>:<tag>@sha256:<64 hex>   -- remote-registry digest pin
      2. sha256:<64 hex>                -- bare docker image ID pin
    """
    if not image:
        return False, "IMAGE is empty"
    if not ID_RE.match(image):
        return False, (
            "IMAGE must be pinned by digest ('repo:tag@sha256:<64 hex>') or by "
            f"image ID ('sha256:<64 hex>') -- a locally built custom image has no "
            f"RepoDigest, use its ID from `docker inspect --format '{{{{.Id}}}}' "
            f"\"<image>\"`. Got: {image}"
        )
    return True, ""


def read_image_labels(image: str) -> dict | None:
    """Return image LABELS dict via `docker image inspect`, or None on failure."""
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        labels = json.loads(out.stdout.strip() or "null")
        return labels if isinstance(labels, dict) else None
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None


def read_identity_json(image: str) -> dict | None:
    """Dump /etc/kvp-lmcache-identity.json from inside the image (read-only,
    no GPU, no model). Returns parsed dict, or None if unreadable/invalid."""
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "cat", image, IDENT_JSON_PATH],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return None
        obj = json.loads(out.stdout.strip() or "null")
        return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None


def probe_lmcache_version(image: str) -> str | None:
    """Return `importlib.metadata.version('lmcache')` from inside the image
    (r4 P0-3: verify the REAL installation against wheel_version in the baked
    identity -- a LABEL and even a JSON file can claim anything)."""
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "python3", image, "-c",
             "import importlib.metadata as m; print(m.version('lmcache'))"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def probe_cuda_ops(image: str) -> bool | None:
    """Probe find_spec('lmcache.cuda_ops') inside the image. True/False on a
    completed run, None when the probe itself could not run."""
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "python3", image, "-c",
             CUDA_OPS_PROBE],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.returncode == 0


def verify_custom_image(image: str, patch_dir: pathlib.Path,
                        expect_base: str | None = None,
                        deep: bool = False) -> tuple[bool, list[str], str]:
    """Check <image> carries the custom LMCache identity labels matching the repo.
    Args:
        image: the IMAGE spec that is about to be launched.
        patch_dir: repo directory holding the two downstream patch files.
        expect_base: optional base image string the label must match.
        deep: run the additional no-GPU checks inside the image (r3 P1-3) --
            read the baked /etc/kvp-lmcache-identity.json and probe
            find_spec('lmcache.cuda_ops'). A LABEL alone is forgeable.
    Returns:
        (ok, messages, wheel_version). ok==False means the image must NOT be
        launched. wheel_version is the build identity read from the image's
        baked identity JSON (r21 L1) -- empty when it could not be read; the
        caller (the resolver) exports it as KVP_LMCACHE_BUILD so the cache
        namespace follows the image that ACTUALLY runs.
    """
    msgs: list[str] = []
    ok, fmtmsg = is_pinned_image_spec(image)
    if not ok:
        return False, [fmtmsg], ""

    labels = read_image_labels(image)
    if labels is None:
        return False, [
            f"docker cannot inspect image '{image}' -- is it built and available "
            "locally? Build it with scripts/build-lmcache.sh + scripts/build-image.sh "
            "or `docker pull` first."
        ], ""

    for key in REQUIRED_LABELS:
        if not labels.get(key):
            return False, [
                f"image '{image}' has NO label {key} -- it is NOT the custom LMCache "
                "derive. Launching it reverts to upstream LMCache without the #4136 "
                "retrieve/abort fix and the #5972 incomplete-object fix. Build the "
                "custom derivative with scripts/build-image.sh."
            ], ""

    from .lmc_identity import sha256_file  # local import to avoid cycles

    for key, fname in (("pr4136_patch_sha256", "4136-mp-retrieve-abort.patch"),
                       ("pr5972_patch_sha256", "5972-incomplete-kv-objects.patch")):
        label_key = LABEL_4136 if "4136" in fname else LABEL_5972
        expected = labels[label_key]
        fpath = patch_dir / fname
        if not fpath.is_file():
            msgs.append(f"repo patch file not found: {fpath}")
            ok = False
            continue
        actual = sha256_file(fpath)
        if actual != expected:
            msgs.append(
                f"{fname}: image label sha {expected} != repo patch sha {actual}"
            )
            ok = False

    if expect_base and labels.get(LABEL_BASE):
        # r20 K1: compare the base pin BY DIGEST -- the same base image under
        # another tag or repo name is the same base (no false refusal). Full
        # string equality stays for specs without a digest (old behaviour for
        # other callers preserved).
        _lbl_base = labels[LABEL_BASE]
        _lbl_digest = spec_digest(_lbl_base)
        _exp_digest = spec_digest(expect_base)
        if _lbl_digest and _exp_digest:
            base_match = _lbl_digest == _exp_digest
        else:
            base_match = _lbl_base == expect_base
        if not base_match:
            msgs.append(
                f"image base label {_lbl_base} != expected base {expect_base} "
                f"(base digest {_lbl_digest or '<none>'} != expected digest "
                f"{_exp_digest or '<none>'})"
            )
            ok = False

    if not ok:
        return ok, msgs, ""

    # ---- r3 P1-3 deep: prove the wheel is really baked in, not just labeled.
    if not deep:
        return True, msgs, ""

    ident = read_identity_json(image)
    if ident is None:
        return False, [
            f"image '{image}': cannot read {IDENT_JSON_PATH} from inside -- the "
            "identity file is missing or unreadable. The LABELs alone do NOT prove "
            "our wheel is baked in (any derivative can forge a LABEL). Build with "
            "scripts/build-image.sh."
        ], ""
    for key in ("pr4136_patch_sha256", "pr5972_patch_sha256", "wheel_sha256"):
        if not ident.get(key):
            return False, [f"{IDENT_JSON_PATH} is missing key {key}"], ""
    for key, fname in (("pr4136_patch_sha256", "4136-mp-retrieve-abort.patch"),
                       ("pr5972_patch_sha256", "5972-incomplete-kv-objects.patch")):
        fpath = patch_dir / fname
        if fpath.is_file():
            expected = sha256_file(fpath)
            if ident[key] != expected:
                return False, [
                    f"{IDENT_JSON_PATH}: {key}={ident[key]} != repo patch sha {expected}"
                ], ""

    res = probe_cuda_ops(image)
    if res is None:
        return False, [
            f"image '{image}': probe of 'lmcache.cuda_ops' failed to run -- cannot "
            "confirm the compiled extension is present"
        ], ""
    if not res:
        return False, [
            f"image '{image}': importlib.util.find_spec('lmcache.cuda_ops') is empty -- "
            "the wheel lacks the compiled CUDA extension (silent pure-Python fallback). "
            "Rebuild with scripts/build-lmcache.sh (nvcc + CUDA dev packages required)."
        ], ""

    # r4 P0-3: the identity JSON can CLAIM any wheel_version. Cross-check the
    # REAL installed metadata (importlib.metadata.version('lmcache')) against
    # the identity's wheel_version -- a custom build always carries the
    # `+kvp.<hash>` local part (build-lmcache.sh SETUPTOOLS_SCM_PRETEND_VERSION
    # pin), so a pure upstream nightly fails both this and the +kvp check.
    wheel_version = ident.get("wheel_version") or ""
    if "+kvp." not in wheel_version:
        return False, [
            f"{IDENT_JSON_PATH}: wheel_version '{wheel_version}' lacks the '+kvp.' "
            "local part -- this identity was NOT produced by scripts/build-lmcache.sh"
        ], ""
    actual_version = probe_lmcache_version(image)
    if actual_version is None:
        return False, [
            f"image '{image}': cannot read importlib.metadata.version('lmcache') -- "
            "the wheel is not installed (or metadata is missing); refusing"
        ], ""
    if actual_version != wheel_version:
        return False, [
            f"image '{image}': installed lmcache version '{actual_version}' != "
            f"identity wheel_version '{wheel_version}' -- the baked identity "
            "describes a different build than the one actually installed"
        ], ""

    return True, msgs, wheel_version


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", nargs="?", default=None,
                    help="IMAGE spec about to launch (omit with --print-wheel-version)")
    ap.add_argument("patch_dir", nargs="?", default=None,
                    help="repo dir with the downstream patches (omit with --print-wheel-version)")
    ap.add_argument("--expected-base", default=None)
    ap.add_argument("--deep", action="store_true",
                    help="r3 P1-3: additionally verify the baked identity json "
                         "and find_spec('lmcache.cuda_ops') inside the image")
    ap.add_argument("--print-wheel-version", metavar="IDENT_JSON", default=None,
                    help="r20 K7: print the wheel_version recorded in the "
                         "given /etc/kvp-lmcache-identity.json and exit "
                         "(single source for the lmcache_build identity field)")
    args = ap.parse_args(argv)
    # r20 K7: single-source reader of the build identity.
    if args.print_wheel_version is not None:
        try:
            with open(args.print_wheel_version, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            ver = (data.get("wheel_version") or "").strip()
        except (OSError, ValueError) as exc:
            print(f"ERROR: cannot read identity JSON "
                  f"{args.print_wheel_version}: {exc}", file=sys.stderr)
            return 4
        if not ver:
            print(f"ERROR: wheel_version missing from "
                  f"{args.print_wheel_version}", file=sys.stderr)
            return 4
        print(ver)
        return 0
    if args.image is None or args.patch_dir is None:
        ap.error("image and patch_dir are required unless --print-wheel-version")
    ok, msgs, wheel_version = verify_custom_image(args.image, pathlib.Path(args.patch_dir),
                                   args.expected_base, deep=args.deep)
    if ok:
        print(f"custom LMCache image verified: {args.image}")
        # r21 L1: the BUILD identity of the image that will actually run --
        # the resolver (env.sh) captures this line and exports it as
        # KVP_LMCACHE_BUILD so the cache namespace follows the RUNNING image,
        # not the last build's record in live/image-identity.env.
        if wheel_version:
            print(f"KVP_WHEEL_VERSION={wheel_version}")
        return 0
    print(f"MISMATCH: refused image '{args.image}':", file=sys.stderr)
    for m in msgs:
        print(f"  - {m}", file=sys.stderr)
    return 4


if __name__ == "__main__":
    sys.exit(main())