# SPDX-License-Identifier: Apache-2.0
"""Verify the custom LMCache build identity of a running component.

The LMCache wheel version alone (``0.5.7.dev428``) cannot tell our patched
build (pinned base + downstream PR #4136/#5972) from the upstream nightly:
``pyproject.toml`` uses ``local_scheme = no-local-version``. Therefore
``scripts/build-image.sh`` bakes an identity file into the derivative image as
``/etc/kvp-lmcache-identity.json`` (base commit, patch SHA-256s, wheel
SHA-256). This module compares a runtime-reported identity document against
the patch files in the repository and refuses deployment on any drift.

Used by ``scripts/verify-engine-identity.sh`` (MISMATCH -> exit 4) so that the
MP cache server and every vLLM worker provably run the SAME custom build.
"""

from __future__ import annotations

# Standard
import argparse
import hashlib
import json
import pathlib
import sys

REQUIRED_KEYS = (
    "lmcache_source_commit",
    "pr4136_patch_sha256",
    "pr5972_patch_sha256",
    "wheel_sha256",
)

PATCH_FILES = {
    "pr4136_patch_sha256": "4136-mp-retrieve-abort.patch",
    "pr5972_patch_sha256": "5972-incomplete-kv-objects.patch",
}


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def compare_wheel_version(
    ident: dict, runtime_version: str | None
) -> tuple[bool, str]:
    """Compare the built wheel's recorded version against a runtime-reported
    one (r3 P0-2).

    The upstream nightly's own version (0.5.7.dev428) is identical to what a
    bare source checkout reports -- only our ``+kvp.<12 hex>`` local part
    (see build-lmcache.sh ``KVP_WHEEL_VERSION``) disambiguates our build from
    the upstream nightly. A bare upstream version therefore must NOT satisfy
    this check: run the derived image's ``md.version('lmcache')`` and compare
    it against the ``wheel_version`` recorded in the baked identity file.

    Args:
        ident: the ``/etc/kvp-lmcache-identity.json`` document (or provenance).
        runtime_version: the version reported by the installed package.

    Returns:
        ``(ok, message)``.
    """
    expected = (ident or {}).get("wheel_version", "")
    if not expected:
        return False, "wheel_version missing from the identity document"
    if not runtime_version:
        return False, "runtime wheel_version could not be read"
    if runtime_version != expected:
        return False, (
            f"wheel_version mismatch: runtime={runtime_version!r} != "
            f"expected={expected!r}"
        )
    if "+kvp." not in expected:
        return False, (
            f"wheel_version {expected!r} lacks the '+kvp.<hash>' local part -- "
            "indistinguishable from the upstream nightly"
        )
    return True, ""


def verify_identity(
    ident: dict,
    patch_dir: pathlib.Path,
    expected_base: str | None = None,
) -> tuple[bool, list[str]]:
    """Compare a runtime identity document with the repo's patch files.

    Args:
        ident: parsed ``/etc/kvp-lmcache-identity.json`` from the runtime.
        patch_dir: repo directory holding the two downstream patch files.
        expected_base: recorded build base commit (from provenance.json);
            when given, the document must carry the same base.

    Returns:
        ``(ok, messages)`` -- messages explain every failed comparison.

    Notes:
        Missing keys are a mismatch, never a silent pass: an identity without
        patch hashes proves nothing about which fixes the build carries.
    """
    msgs: list[str] = []
    ok = True

    for key in REQUIRED_KEYS:
        if not ident.get(key):
            ok = False
            msgs.append(f"identity file is missing key {key!r}")

    for key, fname in PATCH_FILES.items():
        expected = ident.get(key)
        if not expected:
            continue  # already reported above
        fpath = patch_dir / fname
        if not fpath.is_file():
            ok = False
            msgs.append(f"repo patch file not found: {fpath}")
            continue
        actual = sha256_file(fpath)
        if actual != expected:
            ok = False
            msgs.append(
                f"{fname}: image patch sha {expected} != repo patch sha {actual}"
            )

    base = ident.get("lmcache_source_commit", "")
    if expected_base and base and base != expected_base:
        ok = False
        msgs.append(f"image LMCache base {base} != recorded build base {expected_base}")

    return ok, msgs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "ident_json",
        help="path to /etc/kvp-lmcache-identity.json, or '-' to read stdin",
    )
    ap.add_argument("patch_dir", help="repo directory with the downstream patches")
    ap.add_argument("--expected-base", default=None, help="recorded build base commit")
    args = ap.parse_args(argv)

    raw = (
        sys.stdin.read()
        if args.ident_json == "-"
        else pathlib.Path(args.ident_json).read_text()
    )
    try:
        ident = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"MISMATCH: identity file is not valid JSON: {e}", file=sys.stderr)
        return 4
    if not isinstance(ident, dict):
        print("MISMATCH: identity file must be a JSON object", file=sys.stderr)
        return 4

    ok, msgs = verify_identity(ident, pathlib.Path(args.patch_dir), args.expected_base)
    if ok:
        print(
            "custom LMCache build identity OK "
            f"(base={ident.get('lmcache_source_commit')}, "
            f"#4136={ident.get('pr4136_patch_sha256')}, "
            f"#5972={ident.get('pr5972_patch_sha256')})"
        )
        return 0
    print("MISMATCH: the runtime does not carry the expected custom LMCache build:", file=sys.stderr)
    for m in msgs:
        print(f"  - {m}", file=sys.stderr)
    return 4


if __name__ == "__main__":
    sys.exit(main())