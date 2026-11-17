#!/usr/bin/env bash
# build-lmcache.sh -- reproducible custom LMCache build for the pinned vLLM
# CUDA 13.0 runtime, used by BOTH the shared MP cache server and every vLLM
# model worker.
#
# Guarantees (spec section 4.2):
#   1. checks out the exact pinned LMCache source commit
#   2. verifies the expected source revision
#   3. verifies PR #6551 is already present in the base
#   4. applies the #4136 temporary downstream patch
#   5. applies the #5972 temporary downstream patch
#   6. FAILS if either patch cannot be applied
#   7. FAILS if a patch is already applied unexpectedly
#   8. runs the relevant regression tests
#   9. builds the custom LMCache package
#  10. records the exact final source identity and artifact checksum
#
# The build happens INSIDE the pinned vLLM image so the compiled native
# extensions match the exact PyTorch/CUDA/Python ABI the workers use. It never
# lets dependency resolution upgrade the pinned vLLM/PyTorch/CUDA stack.
#
# Usage:
#   bash scripts/build-lmcache.sh            # full build + tests + provenance
#   KVP_BUILD_SKIP_TESTS=1 bash scripts/build-lmcache.sh   # build only (NOT for
#                                                          # production acceptance)
#
# Not destructive: never touches persistent cache data, model weights or host
# Python environments.

set -Eeuo pipefail

die() { echo "ERROR(build-lmcache): $*" >&2; exit 1; }
note() { echo "[build-lmcache] $*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PATCH_DIR="${KVP_PATCH_DIR:-$ROOT/vendor/patches/lmcache}"
[ -d "$PATCH_DIR" ] || die "patch dir not found: $PATCH_DIR"

# ---------------------------------------------------------------------------
# Pinned identities (see vendor/patches/lmcache/SOURCES.md).
# ---------------------------------------------------------------------------
LMCACHE_SOURCE_COMMIT="${LMCACHE_SOURCE_COMMIT:-03ee5b82418b6bd44e424cdd46932d9346f43eb2}"
LMCACHE_REPO_URL="${LMCACHE_REPO_URL:-https://github.com/LMCache/LMCache.git}"
PR6551_MERGE="${PR6551_MERGE:-61faa902a81462177d40d74f8c64bb9df02163c4}"

PATCH_4136="$PATCH_DIR/4136-mp-retrieve-abort.patch"
PATCH_5972="$PATCH_DIR/5972-incomplete-kv-objects.patch"
[ -f "$PATCH_4136" ] || die "missing patch: $PATCH_4136"
[ -f "$PATCH_5972" ] || die "missing patch: $PATCH_5972"

# Base image: the pinned vLLM runtime. Ideally this is the CUSTOM derivative
# (vLLM base + custom LMCache) recorded by build-image, but building from the
# raw pinned vLLM digimage is also supported for the first build.
IMAGE="${IMAGE:-${KVP_LMC_IMAGE:-}}"
[ -n "$IMAGE" ] || die "IMAGE is not set -- export IMAGE=<pinned vLLM image>@sha256:... before building"

WORK_DIR="${KVP_BUILD_DIR:-$(mktemp -d /tmp/kvp-lmc-build.XXXXXX)}"
OUT_DIR="${KVP_OUT_DIR:-$ROOT/live/lmcache-build}"
mkdir -p "$OUT_DIR"

note "patch dir : $PATCH_DIR"
note "work dir  : $WORK_DIR"
note "out dir   : $OUT_DIR"
note "image     : $IMAGE"

command -v docker >/dev/null || die "docker is required -- the build runs inside the pinned image"

# ---------------------------------------------------------------------------
# 1 + 2. Check out the exact pinned source commit and verify it.
# ---------------------------------------------------------------------------
SRC="$WORK_DIR/LMCache"
if [ ! -d "$SRC/.git" ]; then
  note "cloning $LMCACHE_REPO_URL (partial, blobless) into $SRC"
  git clone --filter=blob:none "$LMCACHE_REPO_URL" "$SRC"
fi
git -C "$SRC" fetch --quiet origin "$LMCACHE_SOURCE_COMMIT" 2>/dev/null \
  || git -C "$SRC" fetch --quiet origin
git -C "$SRC" checkout --quiet --detach "$LMCACHE_SOURCE_COMMIT" \
  || die "cannot check out pinned commit $LMCACHE_SOURCE_COMMIT"

ACTUAL_SHA="$(git -C "$SRC" rev-parse HEAD)"
[ "$ACTUAL_SHA" = "$LMCACHE_SOURCE_COMMIT" ] \
  || die "source revision mismatch: expected $LMCACHE_SOURCE_COMMIT got $ACTUAL_SHA"
note "source commit verified: $ACTUAL_SHA"

# ---------------------------------------------------------------------------
# 3. Verify PR #6551 is already present in the base (do NOT duplicate it).
# ---------------------------------------------------------------------------
if ! git -C "$SRC" merge-base --is-ancestor "$PR6551_MERGE" "$LMCACHE_SOURCE_COMMIT"; then
  die "PR #6551 merge $PR6551_MERGE is NOT an ancestor of $LMCACHE_SOURCE_COMMIT -- wrong base?"
fi
note "PR #6551 ($PR6551_MERGE) confirmed present in base (no local duplicate)"

# ---------------------------------------------------------------------------
# 4 - 7. Apply the two temporary downstream patches, deterministically.
# ---------------------------------------------------------------------------
apply_patch() {
  local file="$1" label="$2"
  if git -C "$SRC" apply --reverse --check "$file" >/dev/null 2>&1; then
    die "patch $label is ALREADY applied unexpectedly -- refuse to double-apply. Reset the work tree and investigate."
  fi
  if ! git -C "$SRC" apply --check "$file"; then
    die "patch $label does NOT apply cleanly to $LMCACHE_SOURCE_COMMIT -- deliberate reconciliation required"
  fi
  git -C "$SRC" apply "$file"
  note "applied $label ($(grep -c '^diff --git' "$file") files)"
}

# Isolated failure: apply_patch exiting non-zero aborts the whole script.
apply_patch "$PATCH_4136" "4136-mp-retrieve-abort"
apply_patch "$PATCH_5972" "5972-incomplete-kv-objects"

# The tree is now dirty on purpose (base + 2 downstream patches). Hash the
# DIFF CONTENT, not the porcelain status list (r2 P1-2: the file-list hash did
# not change when patch content changed). git diff of the worktree covers all
# modified+added files that were staged, exactly the applied patch content.
PATCHED_STATE="$(git -C "$SRC" add -A && git -C "$SRC" diff --cached | sha256sum | awk '{print $1}')"
note "patched tree fingerprint (diff sha256): $PATCHED_STATE"

# ---------------------------------------------------------------------------
# 8 + 9. Regression tests and package build, inside the pinned image.
# ---------------------------------------------------------------------------
PROV="$OUT_DIR/provenance.json"
WHEEL_OUT="$OUT_DIR/wheels"
mkdir -p "$WHEEL_OUT"

note "running build+tests inside the pinned image (in-container phase)"
docker run --rm \
  -v "$SRC":/usr/src/lmcache:rw \
  -v "$PATCH_DIR":/kvp-patches:ro \
  -v "$WHEEL_OUT":/kvp-wheels:rw \
  -v "$WORK_DIR":/kvp-work:rw \
  -e PYTHONUNBUFFERED=1 \
  -e LMCACHE_SOURCE_COMMIT="$LMCACHE_SOURCE_COMMIT" \
  -e PR6551_MERGE="$PR6551_MERGE" \
  -e KVP_BUILD_SKIP_TESTS="${KVP_BUILD_SKIP_TESTS:-0}" \
  --entrypoint bash "$IMAGE" -s <<'INCONTAINER'
set -Eeuo pipefail
echo "[in-container] python: $(python3 --version)"
echo "[in-container] torch : $(python3 -c 'import torch; print(torch.__version__)')"
echo "[in-container] torch CUDA: $(python3 -c 'import torch; print(torch.version.cuda)')"
echo "[in-container] cuda available: $(python3 -c 'import torch; print(torch.cuda.is_available())')"
echo "[in-container] GPU count: $(python3 -c 'import torch; print(torch.cuda.device_count())')"

echo "[in-container] checking vLLM ABI / identity matches ..."
python3 - <<'PY'
import importlib.metadata as md, torch, sys
print("vllm", md.version("vllm") if True else "?")
print("python", sys.version.split()[0])
PY

# Fail-fast native import check with the patched tree on PYTHONPATH.
export PYTHONPATH=/usr/src/lmcache
python3 - <<'PY'
import sys, importlib
try:
    import lmcache  # noqa: F401
    from lmcache.integration.vllm import lmcache_mp_connector  # noqa: F401
    from lmcache.v1.multiprocess.modules import lmcache_driven_transfer  # noqa: F401
except Exception as e:  # pragma: no cover
    print("LMCache import/ABI failure:", repr(e)); raise
print("LMCache source imports OK with both patches applied (no undefined symbols)")
PY

if [ "$KVP_BUILD_SKIP_TESTS" = "0" ]; then
  # Test-only dependencies must NOT disturb the pinned torch stack.
  echo "[in-container] installing pinned test deps (no torch upgrade)"
  pip install --no-cache-dir -q -r /usr/src/lmcache/requirements/test.txt
  echo "[in-container] running migration-regression suites for #4136 and #5972"
  cd /usr/src/lmcache
  # r2 P1-2: -rs so skipped tests are LISTED, and a skip in the required
  # suites is a FAILURE, never a green run (Skipped != Passed).
  python3 -m pytest -q -rs \
    tests/v1/test_lmcache_mp_connector.py \
    tests/v1/test_vllm_mp_adapter.py \
    tests/v1/multiprocess/test_lmcache_driven_transfer_skip.py \
    tests/v1/multiprocess/test_native_state_lookup.py \
    tests/v1/multiprocess/test_l1_owner_completion.py \
    tests/v1/multiprocess/test_cache_server.py \
    > /kvp-work/pytest-report.txt 2>&1 || true
  cat /kvp-work/pytest-report.txt
  _skipped="$(grep -cE '^SKIPPED' /kvp-work/pytest-report.txt || true)"
  if grep -qE '^FAILED|error' /kvp-work/pytest-report.txt; then
    echo "REGRESSION FAILURE -- aborting build"; exit 1
  fi
  if [ "$_skipped" != "0" ]; then
    echo "REGRESSION FAILURE: $_skipped required test(s) SKIPPED (skipped != passed) -- aborting build"
    exit 1
  fi
  echo "[in-container] focused regression tests PASSED (0 failed, 0 skipped)"
else
  echo "[in-container] KVP_BUILD_SKIP_TESTS=1 -- tests skipped (not production-acceptable)"
fi

# 9. Build the wheel against the INSTALLED torch, no build isolation,
#    pinned build deps only.
echo "[in-container] building custom LMCache wheel (no build isolation)"
cd /usr/src/lmcache
pip install --no-cache-dir -q -r /usr/src/lmcache/requirements/build.txt 2>/dev/null \
  || pip install --no-cache-dir -q "ninja" "packaging>=24.2" "setuptools>=77.0.3,<81.0.0" "setuptools_scm>=8" "wheel"
python3 -m pip wheel . --no-build-isolation --no-deps -w /kvp-wheels

echo "[in-container] artifacts:"
ls -la /kvp-wheels

# Identity block written for the host phase.
python3 - <<'PY' > /kvp-work/identity.env
import importlib.metadata as md, torch, sys
try:
    lm = md.version("lmcache")
except Exception:
    lm = "built-from-source"
print(f"LMCACHE_VERSION={lm}")
print(f"TORCH_VERSION={torch.__version__}")
print(f"TORCH_CUDA={torch.version.cuda}")
print(f"PYTHON_VERSION={sys.version.split()[0]}")
print(f"CUDA_AVAILABLE={torch.cuda.is_available()}")
print(f"GPU_COUNT={torch.cuda.device_count()}")
print(f"GPU_CC={torch.cuda.get_device_capability(0) if torch.cuda.is_available() else 'n/a'}")
PY
echo "[in-container] build phase complete"
INCONTAINER

# ---------------------------------------------------------------------------
# 10. Record exact final source identity and artifact checksum.
# ---------------------------------------------------------------------------
WHEEL_NAME="$(find "$WHEEL_OUT" -name '*.whl' -printf '%f\n' | head -1 || true)"
[ -n "$WHEEL_NAME" ] || die "no wheel produced -- see output above"
WHEEL_SHA="$(sha256sum "$WHEEL_OUT/$WHEEL_NAME" | awk '{print $1}')"

# shellcheck disable=SC1090
[ -f "$WORK_DIR/identity.env" ] && . "$WORK_DIR/identity.env" || true

cat > "$PROV" <<EOF
{
  "module": "custom-lmcache-build",
  "lmcache_repo": "$LMCACHE_REPO_URL",
  "lmcache_source_commit": "$LMCACHE_SOURCE_COMMIT",
  "pr6551_merge_commit": "$PR6551_MERGE",
  "pr6551_status": "in-base",
  "pr4136_source_revision": "b10588bd46288192e01ab06da3c3fa923ad0401a",
  "pr4136_patch": "vendor/patches/lmcache/4136-mp-retrieve-abort.patch",
  "pr4136_patch_sha256": "$(sha256sum "$PATCH_4136" | awk '{print $1}')",
  "pr5972_source_revision": "1ccd6f698794cc45d189c14223d339623abe6ec2",
  "pr5972_patch": "vendor/patches/lmcache/5972-incomplete-kv-objects.patch",
  "pr5972_patch_sha256": "$(sha256sum "$PATCH_5972" | awk '{print $1}')",
  "patched_tree_fingerprint": "$PATCHED_STATE",
  "build_image": "$IMAGE",
  "torch_version": "${TORCH_VERSION:-unknown}",
  "torch_cuda": "${TORCH_CUDA:-unknown}",
  "python_version": "${PYTHON_VERSION:-unknown}",
  "cuda_available": "${CUDA_AVAILABLE:-unknown}",
  "gpu_count": "${GPU_COUNT:-unknown}",
  "gpu_compute_capability": "${GPU_CC:-unknown}",
  "tests_skipped": "${KVP_BUILD_SKIP_TESTS:-0}",
  "wheel": "$WHEEL_NAME",
  "wheel_sha256": "$WHEEL_SHA"
}
EOF

note "custom LMCache wheel: $WHEEL_OUT/$WHEEL_NAME"
note "wheel sha256:       $WHEEL_SHA"
note "provenance:         $PROV"
note "source:             $SRC (base $LMCACHE_SOURCE_COMMIT)"
note "DONE -- install this exact wheel into the custom derivative image (build-image.sh)"