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
# r18 G2: EXACTLY one wheel, and it must be the wheel provenance describes.
# `find | head -1` picked an arbitrary one of several and never checked the
# provenance -- an untested or foreign wheel could be baked into the image.
_wheel_list=("$WHEEL_DIR"/*.whl)
if [ ! -e "${_wheel_list[0]:-}" ]; then
  echo "BUILD REFUSED: no wheel in $WHEEL_DIR -- run scripts/build-lmcache.sh first" >&2
  exit 1
fi
if [ "${#_wheel_list[@]}" != "1" ]; then
  echo "BUILD REFUSED: expected exactly one wheel in $WHEEL_DIR, found ${#_wheel_list[@]}:" >&2
  for _w in "${_wheel_list[@]}"; do echo "  $_w" >&2; done
  echo "  refuse to pick one arbitrarily (clean the dir and run scripts/build-lmcache.sh)" >&2
  exit 1
fi
WHEEL="${_wheel_list[0]}"
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
# r20 K6: the codec requirements file is copied INTO the build context from
# the repo (SINGLE SOURCE of the pins -- requirements-image-codecs.txt at the
# repo root; the Dockerfile COPYs it and installs the codecs with
# --require-hashes). Exact `==` pins with --hash=sha256 (cp312 manylinux
# x86_64 wheels from PyPI, ranges in requirements-lmcache.txt:
# zstandard>=0.22,<1, lz4>=4.3,<5).
CODECS_REQ="$ROOT/requirements-image-codecs.txt"
[ -f "$CODECS_REQ" ] || die "codec requirements missing: $CODECS_REQ"
cp "$CODECS_REQ" "$TMPD/requirements-image-codecs.txt"
CODECS_SHA="$(sha256sum "$TMPD/requirements-image-codecs.txt" | awk '{print $1}')"
# the pinned versions come FROM THE FILE (never a second hardcoded copy)
CODECS_VERSIONS="$(grep -E '^[a-zA-Z0-9]' "$TMPD/requirements-image-codecs.txt" \
  | awk '{print $1}' | tr '\n' ' ' | sed 's/ $//')"

# r19 H1: the wheel keeps its REAL filename in the image. pip parses a local
# `.whl` name as {name}-{version}-{python}-{abi}-{platform}.whl -- the old
# rename to /tmp/kvp-lmcache.whl broke that and `docker build` died at step
# [4/4]: ERROR: Invalid wheel filename (wrong number of parts): 'kvp-lmcache'.
# The name is taken ONCE and used for COPY, pip install and rm (quoted --
# the local version carries '+').
WHEEL_BASENAME="$(basename "$WHEEL")"

# r3 P0-1 fix: compute these BEFORE the Dockerfile/identity heredocs expand
# $PR4136_SHA/$PR5972_SHA/$WHEEL_SHA_ID -- under `set -u` a heredoc referencing
# an unassigned var aborts the whole script with 'unbound variable' on first
# run. Must stay above every use of these three names.
PR4136_SHA="$(sha256sum "$PATCH_DIR/4136-mp-retrieve-abort.patch" | awk '{print $1}')"
PR5972_SHA="$(sha256sum "$PATCH_DIR/5972-incomplete-kv-objects.patch" | awk '{print $1}')"
WHEEL_SHA_ID="$(sha256sum "$WHEEL" | awk '{print $1}')"

# ---------------------------------------------------------------------------
# r18 G2: the wheel and the patches must match provenance.json BEFORE any
# image is baked. provenance.json is written at the end of a FULL build; if
# the wheel was swapped or a patch edited afterwards, the identity label
# would LIE about the image content.
# ---------------------------------------------------------------------------
prov_wheel_sha="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("wheel_sha256",""))' "$BUILD_INFO" 2>/dev/null || echo "")"
if [ "$prov_wheel_sha" != "$WHEEL_SHA_ID" ]; then
  echo "BUILD REFUSED: wheel does not match provenance.json (rebuild: bash scripts/build-lmcache.sh)" >&2
  echo "  wheel    : $WHEEL" >&2
  echo "  wheel sha: $WHEEL_SHA_ID" >&2
  echo "  provenance wheel_sha256: ${prov_wheel_sha:-<missing>}" >&2
  exit 1
fi
for _pair in "pr4136_patch_sha256:$PR4136_SHA:4136-mp-retrieve-abort.patch" \
             "pr5972_patch_sha256:$PR5972_SHA:5972-incomplete-kv-objects.patch"; do
  _key="${_pair%%:*}"
  _rest="${_pair#*:}"
  _cur="${_rest%%:*}"
  _file="${_rest#*:}"
  _prov="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$BUILD_INFO" "$_key" 2>/dev/null || echo "")"
  if [ "$_prov" != "$_cur" ]; then
    echo "BUILD REFUSED: patches changed since the wheel was built: full rebuild required" >&2
    echo "  $_file ($_key): current=$_cur provenance=${_prov:-<missing>}" >&2
    exit 1
  fi
done
# r18 G2: a wheel built with KVP_BUILD_SKIP_TESTS=1 must never become an
# image silently -- provenance records tests_skipped.
_prov_tests_skipped="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("tests_skipped","unknown"))' "$BUILD_INFO" 2>/dev/null || echo unknown)"
if [ "$_prov_tests_skipped" != "0" ]; then
  if [ "${KVP_ALLOW_UNTESTED_WHEEL:-0}" = "1" ]; then
    echo "WARNING(build-image): KVP_ALLOW_UNTESTED_WHEEL=1 -- proceeding with an UNTESTED wheel." >&2
    echo "  provenance tests_skipped=$_prov_tests_skipped (the build skipped its test gates)" >&2
    echo "  the image identity will be marked 'untested-wheel'." >&2
    UNTESTED_WHEEL_MARK="untested-wheel"
  else
    echo "BUILD REFUSED: the wheel was built WITHOUT its tests (provenance tests_skipped=$_prov_tests_skipped)." >&2
    echo "  rebuild with tests: bash scripts/build-lmcache.sh" >&2
    echo "  (or set KVP_ALLOW_UNTESTED_WHEEL=1 to bake an explicitly untested wheel)" >&2
    exit 1
  fi
else
  UNTESTED_WHEEL_MARK=""
fi

cat > "$TMPD/Dockerfile" <<EOF
# Custom derivative of the pinned vLLM runtime.
# Adds ONLY the custom LMCache wheel (built in-image by build-lmcache.sh) and
# the two lossless L2 codecs (zstandard, lz4 -- requirements-image-codecs.txt).
# Everything else -- vLLM, PyTorch, CUDA, Python -- is preserved bit-for-bit.
FROM $BASE_IMAGE
LABEL org.kvp.lmc-base="$BASE_IMAGE" \\
      org.kvp.lmc-patch-4136="$PR4136_SHA" \\
      org.kvp.lmc-patch-5972="$PR5972_SHA"
COPY $WHEEL_BASENAME /tmp/$WHEEL_BASENAME
COPY requirements-image-codecs.txt /tmp/requirements-image-codecs.txt
COPY kvp-lmcache-identity.json /etc/kvp-lmcache-identity.json
RUN python3 -m pip install --no-cache-dir --no-deps "/tmp/$WHEEL_BASENAME" \\
 && python3 -m pip install --no-cache-dir --no-deps --only-binary=:all: --require-hashes -r /tmp/requirements-image-codecs.txt \\
 && rm -f "/tmp/$WHEEL_BASENAME" /tmp/requirements-image-codecs.txt \\
 && python3 -c "import lmcache, lmcache.integration.vllm.lmcache_mp_connector; print('LMCache', lmcache.__version__ if hasattr(lmcache,'__version__') else 'installed')"
EOF

# r2 P1-2: bake the build identity INTO the image (local scheme = no local
# version, so wheel version alone cannot tell our build from upstream nightly).
# verify-engine-identity.sh consults this file for server AND every worker.
# r18 G2: the patch SHAs come from PROVENANCE.json (the source of truth for
# what the wheel was built with), not from the current patch files -- after
# the checks above they are equal, but one source of truth stays. An
# explicitly untested wheel (KVP_ALLOW_UNTESTED_WHEEL=1) is marked.
cat > "$TMPD/kvp-lmcache-identity.json" <<EOF
{
  "module": "kvp-custom-lmcache-identity",
  "built_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "lmcache_source_commit": "${LMCACHE_SOURCE_COMMIT:-07f1c6bb642f4772606a36eab730ae009ddce08f}",
  "pr6551_merge_commit": "${PR6551_MERGE:-61faa902a81462177d40d74f8c64bb9df02163c4}",
  "pr6551_status": "in-base",
  "pr4136_source_revision": "b10588bd46288192e01ab06da3c3fa923ad0401a",
  "pr4136_patch_sha256": "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("pr4136_patch_sha256"))' "$BUILD_INFO")",
  "pr5972_source_revision": "bd79d4c6acf6096061b5092517698239440cb93b",
  "pr5972_patch_sha256": "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("pr5972_patch_sha256"))' "$BUILD_INFO")",
  "wheel": "$(basename "$WHEEL")",
  "wheel_sha256": "$WHEEL_SHA_ID",
  "wheel_version": "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("wheel_version","unknown"))' "$BUILD_INFO" 2>/dev/null || echo unknown)",
  "tests_skipped": "$_prov_tests_skipped",
  "untested_wheel": "${UNTESTED_WHEEL_MARK:-}",
  "image_codecs_sha256": "$CODECS_SHA",
  "image_codecs_versions": "$CODECS_VERSIONS",
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
for _marker in KVP_CUDA_OPS_IMPORT=OK KVP_NATIVE_IMPORT=OK KVP_PY_IMPORTS=OK KVP_IDENTITY_FILE=OK KVP_ZSTD_IMPORT=OK KVP_LZ4_IMPORT=OK; do
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
# r20 K6.3: the SERVER SMOKE. The import markers above prove the modules load;
# this proves the real cache server STARTS in the image (codecs, nixl, otel,
# the rendered L2 adapter) with the SAME command source as the production
# launch (env.sh _kvp_server_argv + _kvp_render_l2_adapter, used by
# start-cache.sh). Two codecs: zstd (the default) and none (byte-exact raw).
# Wait for the RPC ping, then SIGTERM and demand a clean exit. Without this a
# broken codec/adapter only surfaced at the operator's first start-cache.
_note_smoke() { note "server smoke ($1): $2"; }
for _codec in zstd none; do
  _uc="$(printf '%s' "$_codec" | tr '[:lower:]' '[:upper:]')"
  _note_smoke "$_codec" "starting the cache server in the image ..."
  _smoke_out="$TMPD/smoke-${_codec}.txt"
  _smoke_rc=0
  docker run --rm --gpus all --ipc host \
    -v "$ROOT":/kvp-repo:ro -v "$ROOT/implementation":/kvp-impl:ro \
    --entrypoint bash "$OUT_IMAGE" /kvp-repo/scripts/_smoke_cache_server.sh \
      "$_codec" "/tmp/kvp-smoke-$_codec" "${KVP_SMOKE_PORT:-15555}" \
    > "$_smoke_out" 2>&1 || _smoke_rc=$?
  [ "$_smoke_rc" = 0 ] || die "server smoke FAILED for codec '$_codec' (rc=$_smoke_rc):
$(tail -n 30 "$_smoke_out" 2>/dev/null)"
  for _m in "KVP_SMOKE_${_uc}_READY=OK" "KVP_SMOKE_${_uc}_EXIT=0"; do
    grep -q "^${_m}$" "$_smoke_out" 2>/dev/null \
      || die "server smoke marker '${_m}' missing in the smoke output (codec '$_codec'):
$(tail -n 30 "$_smoke_out" 2>/dev/null)"
  done
  _note_smoke "$_codec" "READY + clean exit"
done

# r19 H2: image-identity.env must be a REAL env-file. The raw verify.txt
# carries log noise and rows like `KVP_GPU_SMOKE[0] dev=...` (invalid shell
# names) -- `set -e; . image-identity.env` would die on the first of them.
# Only clean `^KVP_[A-Z0-9_]+=` lines go into the .env (KEY=value, no quotes
# -- the marker values carry no spaces); the raw output is kept next to it
# as live/image-verify.txt for diagnostics.
# r23 D2 clean-clone: the identity file must land under its directory even
# when live/ does not exist yet (fresh clone -- live/ is gitignored). The
# mkdir below only covers $KVP_LIVE_DIR; IDENTITY may point elsewhere
# (KVP_IDENTITY_OUT) and must be creatable independently.
mkdir -p "$(dirname "$IDENTITY")"
{
  echo "KVP_CUSTOM_IMAGE=$OUT_IMAGE"
  echo "KVP_CUSTOM_IMAGE_ID=sha256:$IMAGE_ID"
  echo "KVP_LMC_BASE_IMAGE=$BASE_IMAGE"
  echo "KVP_IMAGE_CODECS_SHA256=$CODECS_SHA"
  echo "KVP_IMAGE_CODECS_VERSIONS=$CODECS_VERSIONS"
  grep -E '^KVP_[A-Z0-9_]+=' "$TMPD/verify.txt" || true
} > "$IDENTITY"

# r6-7 B-2: record the CUSTOM image id for the whole launch chain. This is
# written ONLY after the strict verify gate above passed -- a failed smoke
# must never create or overwrite live/image. start-cache.sh / start-all.sh /
# the recipe launchers read this file when IMAGE is empty; an explicit IMAGE
# still wins. KVP_LIVE_DIR keeps test runs out of the repo's live/.
KVP_LIVE_DIR="${KVP_LIVE_DIR:-$ROOT/live}"
mkdir -p "$KVP_LIVE_DIR"
# r19 H2: the raw verify output for diagnostics lives NEXT TO the .env
cp "$TMPD/verify.txt" "$KVP_LIVE_DIR/image-verify.txt"
printf 'sha256:%s\n' "$IMAGE_ID" > "$KVP_LIVE_DIR/image"
note "custom image id recorded: $KVP_LIVE_DIR/image (sha256:${IMAGE_ID:0:16}...)"
note "ready to launch: bash scripts/start-all.sh   # picks the image up from live/image"
note "custom image built: $OUT_IMAGE (id sha256:${IMAGE_ID:0:16}...)"
note "identity written:   $IDENTITY"
note "DIGEST use: pin the runtime by EXACT digest -- record ${IMAGE_ID} and pass the"
note " SAME custom image to both start-cache (MP server) and the model workers."
note "align identity gate: scripts/verify-engine-identity.sh will validate the"
note " workers run THIS exact LMCache build."