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
                        deep: bool = False) -> tuple[bool, list[str]]:
    """Check <image> carries the custom LMCache identity labels matching the repo.

    Args:
        image: the IMAGE spec that is about to be launched.
        patch_dir: repo directory holding the two downstream patch files.
        expect_base: optional base image string the label must match.
        deep: run the additional no-GPU checks inside the image (r3 P1-3) --
            read the baked /etc/kvp-lmcache-identity.json and probe
            find_spec('lmcache.cuda_ops'). A LABEL alone is forgeable.

    Returns:
        (ok, messages). ok==False means the image must NOT be launched.
    """
    msgs: list[str] = []
    ok, fmtmsg = is_pinned_image_spec(image)
    if not ok:
        return False, [fmtmsg]

    labels = read_image_labels(image)
    if labels is None:
        return False, [
            f"docker cannot inspect image '{image}' -- is it built and available "
            "locally? Build it with scripts/build-lmcache.sh + scripts/build-image.sh "
            "or `docker pull` first."
        ]

    for key in REQUIRED_LABELS:
        if not labels.get(key):
            return False, [
                f"image '{image}' has NO label {key} -- it is NOT the custom LMCache "
                "derive. Launching it reverts to upstream LMCache without the #4136 "
                "retrieve/abort fix and the #5972 incomplete-object fix. Build the "
                "custom derivative with scripts/build-image.sh."
            ]

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

    if expect_base and labels.get(LABEL_BASE) and labels[LABEL_BASE] != expect_base:
        msgs.append(
            f"image base label {labels[LABEL_BASE]} != expected base {expect_base}"
        )
        ok = False

    if not ok:
        return ok, msgs

    # ---- r3 P1-3 deep: prove the wheel is really baked in, not just labeled.
    if not deep:
        return True, msgs

    ident = read_identity_json(image)
    if ident is None:
        return False, [
            f"image '{image}': cannot read {IDENT_JSON_PATH} from inside -- the "
            "identity file is missing or unreadable. The LABELs alone do NOT prove "
            "our wheel is baked in (any derivative can forge a LABEL). Build with "
            "scripts/build-image.sh."
        ]
    for key in ("pr4136_patch_sha256", "pr5972_patch_sha256", "wheel_sha256"):
        if not ident.get(key):
            return False, [f"{IDENT_JSON_PATH} is missing key {key}"]
    for key, fname in (("pr4136_patch_sha256", "4136-mp-retrieve-abort.patch"),
                       ("pr5972_patch_sha256", "5972-incomplete-kv-objects.patch")):
        fpath = patch_dir / fname
        if fpath.is_file():
            expected = sha256_file(fpath)
            if ident[key] != expected:
                return False, [
                    f"{IDENT_JSON_PATH}: {key}={ident[key]} != repo patch sha {expected}"
                ]

    res = probe_cuda_ops(image)
    if res is None:
        return False, [
            f"image '{image}': probe of 'lmcache.cuda_ops' failed to run -- cannot "
            "confirm the compiled extension is present"
        ]
    if not res:
        return False, [
            f"image '{image}': importlib.util.find_spec('lmcache.cuda_ops') is empty -- "
            "the wheel lacks the compiled CUDA extension (silent pure-Python fallback). "
            "Rebuild with scripts/build-lmcache.sh (nvcc + CUDA dev packages required)."
        ]

    return True, msgs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", help="IMAGE spec about to launch")
    ap.add_argument("patch_dir", help="repo dir with the downstream patches")
    ap.add_argument("--expected-base", default=None)
    ap.add_argument("--deep", action="store_true",
                    help="r3 P1-3: additionally verify the baked identity json "
                         "and find_spec('lmcache.cuda_ops') inside the image")
    args = ap.parse_args(argv)

    ok, msgs = verify_custom_image(args.image, pathlib.Path(args.patch_dir),
                                   args.expected_base, deep=args.deep)
    if ok:
        print(f"custom LMCache image verified: {args.image}")
        return 0
    print(f"MISMATCH: refused image '{args.image}':", file=sys.stderr)
    for m in msgs:
        print(f"  - {m}", file=sys.stderr)
    return 4


if __name__ == "__main__":
    sys.exit(main())