#!/usr/bin/env bash
# build-image.sh -- build the CUSTOM DERIVATIVE IMAGE that becomes the single
# shared runtime for the LMCache MP server AND every vLLM model worker.
#
# Guaranteed (spec section 5):
#   * base = the exact pinned vLLM image digest (never a floating tag)
#   * preserves vLLM version, model patches, CUDA 13.0.2, PyTorch, Python
#   * installs the custom LMCache build compiled against THAT environment
#   * the result is pinned by its own resulting image digest
#   * verifies LMCache native extensions load and CUDA enumerates all 4 GPUs
#
# Prerequisite: run scripts/build-lmcache.sh first (it produces the wheel in
# live/lmcache-build/wheels and records provenance.json).
#
# Usage:
#   bash scripts/build-lmcache.sh
#   bash scripts/build-image.sh
#
# r6-7 B-3: the BASE vLLM image digest is NOT hardcoded here -- it lives in
# ONE place, vendor/pinned-base-image.env (KVP_BASE_IMAGE_DEFAULT), and is
# resolved by both build scripts (override: KVP_BASE_IMAGE).
#
# Result: image "kvp-lmcache:<tag>" + live/image-identity.env (digest pin).

set -Eeuo pipefail
die() { echo "ERROR(build-image): $*" >&2; exit 1; }
note() { echo "[build-image] $*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# r3: KVP_BUILD_DIR/KVP_IDENTITY_OUT let tests stage artifacts in a tmp dir
# (no writing into the repo's live/ from unit tests); production defaults keep
# the documented live/lmcache-build locations.
BUILD_DIR="${KVP_BUILD_DIR:-$ROOT/live/lmcache-build}"
BUILD_INFO="$BUILD_DIR/provenance.json"
WHEEL_DIR="$BUILD_DIR/wheels"
PATCH_DIR="${KVP_PATCH_DIR:-$ROOT/vendor/patches/lmcache}"
[ -d "$PATCH_DIR" ] || die "patch dir not found: $PATCH_DIR"
[ -f "$BUILD_INFO" ] || die "provenance.json not found ($BUILD_INFO) -- run scripts/build-lmcache.sh first"
WHEEL="$(find "$WHEEL_DIR" -name '*.whl' | head -1)"
[ -n "$WHEEL" ] || die "no wheel in $WHEEL_DIR -- run scripts/build-lmcache.sh first"
# r6-7 B-1: resolve the BASE vLLM image from ONE pin file. Order:
#   1. KVP_BASE_IMAGE (explicit override from the environment),
#   2. KVP_BASE_IMAGE_DEFAULT in the pin file.
# IMAGE / KVP_LMC_IMAGE are NOT read here: in the launch chain IMAGE means
# the CUSTOM derivative image (live/image), a different thing -- an old
# `export IMAGE=<custom>` must never become the build base.
KVP_PIN_FILE="${KVP_PIN_FILE:-$ROOT/vendor/pinned-base-image.env}"
[ -f "$KVP_PIN_FILE" ] || die "pin file not found: $KVP_PIN_FILE"
# shellcheck disable=SC1090
KVP_BASE_IMAGE_DEFAULT=""
. "$KVP_PIN_FILE" || die "cannot read pin file: $KVP_PIN_FILE"
BASE_IMAGE="${KVP_BASE_IMAGE:-${KVP_BASE_IMAGE_DEFAULT:-}}"
if [ -n "${IMAGE:-}" ] && [ "$IMAGE" != "$BASE_IMAGE" ]; then
  echo "WARNING(build): IMAGE is ignored by the build scripts -- use KVP_BASE_IMAGE." >&2
  echo "  IMAGE (in the launch chain) is the CUSTOM runtime image (live/image);" >&2
  echo "  the build BASE image is $BASE_IMAGE (from $KVP_PIN_FILE)." >&2
fi
[ -n "$BASE_IMAGE" ] || die "no base image resolved -- set KVP_BASE_IMAGE or add KVP_BASE_IMAGE_DEFAULT to $KVP_PIN_FILE"
case "$BASE_IMAGE" in
  *@sha256:*) : ;;
  *) die "base image must be pinned by digest (repo:tag@sha256:<64 hex>) -- got '$BASE_IMAGE' (a floating tag is refused); fix $KVP_PIN_FILE or KVP_BASE_IMAGE" ;;
esac
_hex="${BASE_IMAGE##*@sha256:}"
[ "${#_hex}" = "64" ] && [ -z "${_hex//[0-9a-f]/}" ] \
  || die "digest in the base image must be 64 hex -- got '${_hex}' in '$BASE_IMAGE' ($KVP_PIN_FILE)"
OUT_IMAGE="${KVP_OUT_IMAGE:-kvp-lmcache:latest}"
IDENTITY="${KVP_IDENTITY_OUT:-$ROOT/live/image-identity.env}"

note "base image : $BASE_IMAGE"
note "wheel      : $WHEEL"
note "out image  : $OUT_IMAGE"

command -v docker >/dev/null || die "docker is required"

TMPD="$(mktemp -d /tmp/kvp-image.XXXXXX)"
trap 'rm -rf "$TMPD"' EXIT

cp "$WHEEL" "$TMPD/"

# r3 P0-1 fix: compute these BEFORE the Dockerfile/identity heredocs expand
# $PR4136_SHA/$PR5972_SHA/$WHEEL_SHA_ID -- under `set -u` a heredoc referencing
# an unassigned var aborts the whole script with 'unbound variable' on first
# run. Must stay above every use of these three names.
PR4136_SHA="$(sha256sum "$PATCH_DIR/4136-mp-retrieve-abort.patch" | awk '{print $1}')"
PR5972_SHA="$(sha256sum "$PATCH_DIR/5972-incomplete-kv-objects.patch" | awk '{print $1}')"
WHEEL_SHA_ID="$(sha256sum "$WHEEL" | awk '{print $1}')"

cat > "$TMPD/Dockerfile" <<EOF
# Custom derivative of the pinned vLLM runtime.
# Adds ONLY the custom LMCache wheel (built in-image by build-lmcache.sh).
# Everything else -- vLLM, PyTorch, CUDA, Python -- is preserved bit-for-bit.
FROM $BASE_IMAGE
LABEL org.kvp.lmc-base="$BASE_IMAGE" \
      org.kvp.lmc-patch-4136="$PR4136_SHA" \
      org.kvp.lmc-patch-5972="$PR5972_SHA"
COPY $(basename "$WHEEL") /tmp/kvp-lmcache.whl
COPY kvp-lmcache-identity.json /etc/kvp-lmcache-identity.json
RUN python3 -m pip install --no-cache-dir --no-deps /tmp/kvp-lmcache.whl \\
 && rm /tmp/kvp-lmcache.whl \\
 && python3 -c "import lmcache, lmcache.integration.vllm.lmcache_mp_connector; print('LMCache', lmcache.__version__ if hasattr(lmcache,'__version__') else 'installed')"
EOF

# r2 P1-2: bake the build identity INTO the image (local scheme = no local
# version, so wheel version alone cannot tell our build from upstream nightly).
# verify-engine-identity.sh consults this file for server AND every worker.
cat > "$TMPD/kvp-lmcache-identity.json" <<EOF
{
  "module": "kvp-custom-lmcache-identity",
  "built_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "lmcache_source_commit": "${LMCACHE_SOURCE_COMMIT:-07f1c6bb642f4772606a36eab730ae009ddce08f}",
  "pr6551_merge_commit": "${PR6551_MERGE:-61faa902a81462177d40d74f8c64bb9df02163c4}",
  "pr6551_status": "in-base",
  "pr4136_source_revision": "b10588bd46288192e01ab06da3c3fa923ad0401a",
  "pr4136_patch_sha256": "$PR4136_SHA",
  "pr5972_source_revision": "bd79d4c6acf6096061b5092517698239440cb93b",
  "pr5972_patch_sha256": "$PR5972_SHA",
  "wheel": "$(basename "$WHEEL")",
  "wheel_sha256": "$WHEEL_SHA_ID",
  "wheel_version": "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("wheel_version","unknown"))' "$BUILD_INFO" 2>/dev/null || echo unknown)",
  "base_image": "$BASE_IMAGE"
}
EOF

note "building derivative image ..."
docker build -t "$OUT_IMAGE" "$TMPD"

note "verifying in-container identity + native CUDA extension on real GPUs ..."
# r2 P0-2: run with --gpus all so the check sees the REAL device count, the
# REAL compute capability (sm_120) and actually executes a native kernel on
# each GPU. A CPU-only run says nothing about cuda_ops.
# r6-7 A-2: verify via a mounted FILE (scripts/_verify_image.py), NOT over
# stdin. `docker run` without `-i` leaves stdin EMPTY -- the old heredoc
# `python3 - <<'PY'` silently produced an empty verify.txt and the script
# recorded FAKE success. A mounted file always runs; and after the run the
# markers below are checked STRICTLY (empty/partial verify == refusal).
VERIFY_RC=0
docker run --rm --gpus all -v "$HERE":/kvp-scripts:ro \
  --entrypoint python3 "$OUT_IMAGE" /kvp-scripts/_verify_image.py \
  > "$TMPD/verify.txt" 2>&1 || VERIFY_RC=$?

# r6-7 A-2 gate: every success marker must be present BEFORE anything is
# recorded. An empty or partial verify.txt means the verification never ran.
for _marker in KVP_CUDA_OPS_IMPORT=OK KVP_NATIVE_IMPORT=OK KVP_PY_IMPORTS=OK KVP_IDENTITY_FILE=OK; do
  grep -q "^${_marker}$" "$TMPD/verify.txt" 2>/dev/null \
    || die "verify FAILED (docker run rc=$VERIFY_RC) -- marker '${_marker}' missing in verify.txt:
$(tail -n 20 "$TMPD/verify.txt" 2>/dev/null)"
done
_smoke_count="$(sed -n 's/^KVP_GPU_SMOKE_OK_COUNT=//p' "$TMPD/verify.txt" | tail -1)"
_gpu_count="$(sed -n 's/^KVP_GPU_COUNT=//p' "$TMPD/verify.txt" | tail -1)"
[ -n "$_smoke_count" ] && [ -n "$_gpu_count" ] \
  && [ "$_smoke_count" = "$_gpu_count" ] \
  && [ "$_smoke_count" -gt 0 ] 2>/dev/null \
  || die "verify FAILED -- KVP_GPU_SMOKE_OK_COUNT ('${_smoke_count:-none}') must equal
KVP_GPU_COUNT ('${_gpu_count:-none}') and be > 0 (rc=$VERIFY_RC):
$(tail -n 20 "$TMPD/verify.txt" 2>/dev/null)"

IMAGE_ID="$(docker inspect --format='{{.Id}}' "$OUT_IMAGE" | sed 's/sha256://')"

{
  echo "KVP_CUSTOM_IMAGE=$OUT_IMAGE"
  echo "KVP_CUSTOM_IMAGE_ID=sha256:$IMAGE_ID"
  echo "KVP_LMC_BASE_IMAGE=$BASE_IMAGE"
  cat "$TMPD/verify.txt"
} > "$IDENTITY"

# r6-7 B-2: record the CUSTOM image id for the whole launch chain. This is
# written ONLY after the strict verify gate above passed -- a failed smoke
# must never create or overwrite live/image. start-cache.sh / start-all.sh /
# the recipe launchers read this file when IMAGE is empty; an explicit IMAGE
# still wins. KVP_LIVE_DIR keeps test runs out of the repo's live/.
KVP_LIVE_DIR="${KVP_LIVE_DIR:-$ROOT/live}"
mkdir -p "$KVP_LIVE_DIR"
printf 'sha256:%s\n' "$IMAGE_ID" > "$KVP_LIVE_DIR/image"
note "custom image id recorded: $KVP_LIVE_DIR/image (sha256:${IMAGE_ID:0:16}...)"
note "ready to launch: bash scripts/start-all.sh   # picks the image up from live/image"
note "custom image built: $OUT_IMAGE (id sha256:${IMAGE_ID:0:16}...)"
note "identity written:   $IDENTITY"
note "DIGEST use: pin the runtime by EXACT digest -- record ${IMAGE_ID} and pass the"
note " SAME custom image to both start-cache (MP server) and the model workers."
note "align identity gate: scripts/verify-engine-identity.sh will validate the"
note " workers run THIS exact LMCache build."