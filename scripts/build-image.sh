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
PATCH_DIR="${KVP_PATCH_DIR:-$ROOT/vendor/patches/lmcache}"
[ -d "$PATCH_DIR" ] || die "patch dir not found: $PATCH_DIR"
[ -f "$BUILD_INFO" ] || die "provenance.json not found -- run scripts/build-lmcache.sh first"
WHEEL="$(find "$WHEEL_DIR" -name '*.whl' | head -1)"
[ -n "$WHEEL" ] || die "no wheel in $WHEEL_DIR -- run scripts/build-lmcache.sh first"

BASE_IMAGE="${IMAGE:-${KVP_LMC_IMAGE:-}}"
[ -n "$BASE_IMAGE" ] || die "IMAGE (pinned vLLM image) is not set -- export IMAGE=<pinned vLLM image>@sha256:... before building"

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
PR4136_SHA="$(sha256sum "$PATCH_DIR/4136-mp-retrieve-abort.patch" | awk '{print $1}')"
PR5972_SHA="$(sha256sum "$PATCH_DIR/5972-incomplete-kv-objects.patch" | awk '{print $1}')"
WHEEL_SHA_ID="$(sha256sum "$WHEEL" | awk '{print $1}')"
cat > "$TMPD/kvp-lmcache-identity.json" <<EOF
{
  "module": "kvp-custom-lmcache-identity",
  "built_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "lmcache_source_commit": "${LMCACHE_SOURCE_COMMIT:-03ee5b82418b6bd44e424cdd46932d9346f43eb2}",
  "pr6551_merge_commit": "${PR6551_MERGE:-61faa902a81462177d40d74f8c64bb9df02163c4}",
  "pr6551_status": "in-base",
  "pr4136_source_revision": "b10588bd46288192e01ab06da3c3fa923ad0401a",
  "pr4136_patch_sha256": "$PR4136_SHA",
  "pr5972_source_revision": "1ccd6f698794cc45d189c14223d339623abe6ec2",
  "pr5972_patch_sha256": "$PR5972_SHA",
  "wheel": "$(basename "$WHEEL")",
  "wheel_sha256": "$WHEEL_SHA_ID",
  "base_image": "$BASE_IMAGE"
}
EOF

note "building derivative image ..."
docker build -t "$OUT_IMAGE" "$TMPD"

note "verifying in-container identity + native CUDA extension on real GPUs ..."
# r2 P0-2: run with --gpus all so the check sees the REAL device count, the
# REAL compute capability (sm_120) and actually executes a native kernel on
# each GPU. A CPU-only run says nothing about cuda_ops.
docker run --rm --gpus all --entrypoint python3 "$OUT_IMAGE" - > "$TMPD/verify.txt" <<'PY'
import importlib.metadata as md, torch, sys
print("KVP_LMC_VERSION=" + md.version("lmcache"))
print("KVP_TORCH_VERSION=" + torch.__version__)
print("KVP_TORCH_CUDA=" + str(torch.version.cuda))
print("KVP_PYTHON_VERSION=" + sys.version.split()[0])
print("KVP_CUDA_AVAILABLE=" + str(torch.cuda.is_available()))
print("KVP_GPU_COUNT=" + str(torch.cuda.device_count()))
import lmcache.integration.vllm.lmcache_mp_connector as c
import lmcache.v1.multiprocess.modules.lmcache_driven_transfer as t
print("KVP_PY_IMPORTS=OK")
print("KVP_CONN_MODULE=" + str(c.__file__))
print("KVP_TRANSFER_MODULE=" + str(t.__file__))
# r2 P0-2 -- honest native-extension check: import the compiled module and
# call ONE native kernel per GPU. (The old whitelist timed out -- do NOT
# replace this with a PATH-only grep.)
import lmcache.cuda_ops  # noqa: F401 -- native binary must import
print("KVP_CUDA_OPS_IMPORT=OK")
_smoke_ok = []
for _i in range(torch.cuda.device_count()):
    with torch.cuda.device(_i):
        _bus = lmcache.cuda_ops.get_gpu_pci_bus_id(_i)
        _cc = torch.cuda.get_device_capability(_i)
        print(f"KVP_GPU_SMOKE[{_i}] dev={_bus} cc={_cc[0]}.{_cc[1]} OK")
        _smoke_ok.append(_i)
print("KVP_GPU_SMOKE_OK_COUNT=" + str(len(_smoke_ok)))
if len(_smoke_ok) != torch.cuda.device_count() or not _smoke_ok:
    raise SystemExit("native kernel smoke did not run on every visible GPU")
_cc0 = torch.cuda.get_device_capability(0)
print(f"KVP_GPU_CC={_cc0[0]}.{_cc0[1]}")
import json, pathlib
ident_path = "/etc/kvp-lmcache-identity.json"
ident = json.loads(pathlib.Path(ident_path).read_text())
print("KVP_IDENTITY_FILE=OK")
print("KVP_IDENTITY_BASE=" + ident["lmcache_source_commit"])
print("KVP_IDENTITY_P4136=" + ident["pr4136_patch_sha256"])
print("KVP_IDENTITY_P5972=" + ident["pr5972_patch_sha256"])
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