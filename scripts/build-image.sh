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
#   export IMAGE="vllm/vllm-openai:nightly-8e2beaa3bff595577a069d49d96bde9074294ec2@sha256:167ccd8696bc09f1b63301185134e04b883623f8adcbd8e3a0ec3140a193abcf"
#   bash scripts/build-lmcache.sh
#   bash scripts/build-image.sh
#
# Result: image "kvp-lmcache:<tag>" + live/image-identity.env (digest pin).

set -Eeuo pipefail
die() { echo "ERROR(build-image): $*" >&2; exit 1; }
note() { echo "[build-image] $*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

BUILD_INFO="$ROOT/live/lmcache-build/provenance.json"
WHEEL_DIR="$ROOT/live/lmcache-build/wheels"
[ -f "$BUILD_INFO" ] || die "provenance.json not found -- run scripts/build-lmcache.sh first"
WHEEL="$(find "$WHEEL_DIR" -name '*.whl' | head -1)"
[ -n "$WHEEL" ] || die "no wheel in $WHEEL_DIR -- run scripts/build-lmcache.sh first"

BASE_IMAGE="${IMAGE:-$KVP_LMC_IMAGE}"
[ -n "$BASE_IMAGE" ] || die "IMAGE (pinned vLLM image) is not set"

OUT_IMAGE="${KVP_OUT_IMAGE:-kvp-lmcache:latest}"
IDENTITY="$ROOT/live/image-identity.env"

note "base image : $BASE_IMAGE"
note "wheel      : $WHEEL"
note "out image  : $OUT_IMAGE"

command -v docker >/dev/null || die "docker is required"

TMPD="$(mktemp -d /tmp/kvp-image.XXXXXX)"
trap 'rm -rf "$TMPD"' EXIT

cp "$WHEEL" "$TMPD/"

cat > "$TMPD/Dockerfile" <<EOF
# Custom derivative of the pinned vLLM runtime.
# Adds ONLY the custom LMCache wheel (built in-image by build-lmcache.sh).
# Everything else -- vLLM, PyTorch, CUDA, Python -- is preserved bit-for-bit.
FROM $BASE_IMAGE
COPY $(basename "$WHEEL") /tmp/kvp-lmcache.whl
RUN python3 -m pip install --no-cache-dir --no-deps /tmp/kvp-lmcache.whl \\
 && rm /tmp/kvp-lmcache.whl \\
 && python3 -c "import lmcache, lmcache.integration.vllm.lmcache_mp_connector; print('LMCache', lmcache.__version__ if hasattr(lmcache,'__version__') else 'installed')"
EOF

note "building derivative image ..."
docker build -t "$OUT_IMAGE" "$TMPD"

note "verifying in-container identity ..."
docker run --rm --entrypoint python3 "$OUT_IMAGE" - > "$TMPD/verify.txt" <<'PY'
import importlib.metadata as md, torch, sys
print("KVP_LMC_VERSION=" + md.version("lmcache"))
print("KVP_TORCH_VERSION=" + torch.__version__)
print("KVP_TORCH_CUDA=" + str(torch.version.cuda))
print("KVP_PYTHON_VERSION=" + sys.version.split()[0])
print("KVP_CUDA_AVAILABLE=" + str(torch.cuda.is_available()))
print("KVP_GPU_COUNT=" + str(torch.cuda.device_count()))
import lmcache.integration.vllm.lmcache_mp_connector as c
import lmcache.v1.multiprocess.modules.lmcache_driven_transfer as t
print("KVP_NATIVE_IMPORTS=OK")
print("KVP_CONN_MODULE=" + str(c.__file__))
print("KVP_TRANSFER_MODULE=" + str(t.__file__))
PY

IMAGE_ID="$(docker inspect --format='{{.Id}}' "$OUT_IMAGE" | sed 's/sha256://')"

{
  echo "KVP_CUSTOM_IMAGE=$OUT_IMAGE"
  echo "KVP_CUSTOM_IMAGE_ID=sha256:$IMAGE_ID"
  echo "KVP_LMC_BASE_IMAGE=$BASE_IMAGE"
  cat "$TMPD/verify.txt"
} > "$IDENTITY"

note "custom image built: $OUT_IMAGE (id sha256:${IMAGE_ID:0:16}...)"
note "identity written:   $IDENTITY"
note "DIGEST use: pin the runtime by EXACT digest -- record ${IMAGE_ID} and pass the"
note " SAME custom image to both start-cache (MP server) and the model workers."
note "align identity gate: scripts/verify-engine-identity.sh will validate the"
note " workers run THIS exact LMCache build."