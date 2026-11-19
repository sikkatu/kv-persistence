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
LMCACHE_SOURCE_COMMIT="${LMCACHE_SOURCE_COMMIT:-07f1c6bb642f4772606a36eab730ae009ddce08f}"
LMCACHE_REPO_URL="${LMCACHE_REPO_URL:-https://github.com/LMCache/LMCache.git}"
PR6551_MERGE="${PR6551_MERGE:-61faa902a81462177d40d74f8c64bb9df02163c4}"
# r15 P0': #6608 (KVCacheGroup in the MP connector) must also be an ancestor
# of the pinned base -- same gate as #6551 below.
PR6608_MERGE="${PR6608_MERGE:-bcafb031401d404a5dabae4853f5e23bf9e5685c}"

PATCH_4136="$PATCH_DIR/4136-mp-retrieve-abort.patch"
PATCH_5972="$PATCH_DIR/5972-incomplete-kv-objects.patch"
[ -f "$PATCH_4136" ] || die "missing patch: $PATCH_4136"
[ -f "$PATCH_5972" ] || die "missing patch: $PATCH_5972"

# Base image: the pinned vLLM runtime. Ideally this is the CUSTOM derivative
# (vLLM base + custom LMCache) recorded by build-image, but building from the
# raw pinned vLLM digimage is also supported for the first build.
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
IMAGE="$BASE_IMAGE"  # legacy name used by the docker run below

WORK_DIR="${KVP_BUILD_DIR:-$(mktemp -d /tmp/kvp-lmc-build.XXXXXX)}"
OUT_DIR="${KVP_OUT_DIR:-$ROOT/live/lmcache-build}"
mkdir -p "$OUT_DIR"
# r17 B3: never leave a stale production hash from a previous build around
# -- the file is rewritten below, BEFORE the docker run
rm -f "$OUT_DIR/rerun-prod-hash.txt"
# r18 G1: provenance.json exists ONLY for a build that passed every gate.
# The wheel lands in $OUT_DIR/wheels at C5 (long before gates C8-C14), so a
# build that fails at any gate would otherwise leave a NEW untested wheel
# next to the OLD provenance.json -- and build-image.sh would bake an image
# from that mismatched pair. Remove the stale provenance up front; it is
# rewritten at the very end of a fully successful build.
rm -f "$OUT_DIR/provenance.json"

note "patch dir : $PATCH_DIR"
note "work dir  : $WORK_DIR"
note "out dir   : $OUT_DIR"
note "image     : $IMAGE"

command -v docker >/dev/null || die "docker is required -- the build runs inside the pinned image.
What to do: install docker (https://docs.docker.com/engine/install/) and re-run."
# r6-7 B-4 pre-flight checks -- every refusal has ONE clear 'what to do' line.
if ! docker info >/dev/null 2>&1; then
  die "docker daemon is not reachable.
What to do: start the docker service ('sudo systemctl start docker') or add your user to the docker group."
fi
# the base image must be pull-able or already present; a pull can be huge.
if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  echo "[build-lmcache] base image not present locally -- 'docker pull' will fetch it (tens of GB)." >&2
  echo "[build-lmcache] free space for /var/lib/docker must cover it; abort and free space if unsure." >&2
fi
# disk space: wheel output lives under $OUT_DIR, the build work under $WORK_DIR
_avail_kb="$(df -Pk "$(dirname "$OUT_DIR")" 2>/dev/null | awk 'NR==2{print $4}')"
if [ -n "${_avail_kb:-}" ] && [ "$_avail_kb" -lt 4194304 ] 2>/dev/null; then
  echo "WARNING(build-lmcache): less than 4 GB free for $OUT_DIR -- the build may run out of space." >&2
fi

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
# r15 P0': #6608 (KVCacheGroup in the MP connector) is required the same way.
if ! git -C "$SRC" merge-base --is-ancestor "$PR6608_MERGE" "$LMCACHE_SOURCE_COMMIT"; then
  die "PR #6608 merge $PR6608_MERGE is NOT an ancestor of $LMCACHE_SOURCE_COMMIT -- wrong base?"
fi
note "PR #6608 ($PR6608_MERGE) confirmed present in base"

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

# r17 B3: record the PRODUCTION-part hash of the patched tree (everything
# except tests/ and docs/) BEFORE the docker run -- scripts/rerun-lmcache-
# gate.sh compares against this file to know whether the wheel still matches
# the production tree. One shared algorithm (gate_common prod-hash), never a
# bash copy.
PROD_HASH_FILE="$OUT_DIR/rerun-prod-hash.txt"
PROD_HASH="$(PYTHONPATH="$ROOT/implementation" python3 -m kvpersist.gate_common prod-hash "$SRC")" \
  || die "cannot compute the production-part hash of the patched tree"
[ -n "$PROD_HASH" ] || die "empty production-part hash -- refusing to record"
echo "$PROD_HASH" > "$PROD_HASH_FILE"
note "production hash recorded: $PROD_HASH_FILE ($PROD_HASH)"

# r15 P1: static import gate for the patched tree -- catches stale patch/test
# imports (like the IPCCacheServerKey move to lmcache.v1.common) in seconds,
# BEFORE the 20-minute docker compile. Scope: the files touched by the patches
# (git diff vs HEAD -- the tree is staged above) + the five CPU-gate test files
# + their transitive tests/... imports (same ast walk). No torch/vllm needed.
GATE_SCOPE="$(python3 - "$SRC" <<'PY'
import ast, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
patched = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.split()
gate = ["tests/v1/test_lmcache_mp_connector.py", "tests/v1/test_vllm_mp_adapter.py",
        "tests/v1/multiprocess/test_lmcache_driven_transfer_skip.py",
        "tests/v1/multiprocess/test_native_state_lookup.py",
        "tests/v1/multiprocess/test_l1_owner_completion.py"]
seen, queue, missing = set(), list(patched) + gate, []
while queue:
    rel = queue.pop()
    if rel in seen:
        continue
    p = root / rel
    if not p.is_file():
        # a gate file absent from the tree is a sign this is not the pinned
        # source -- the EMPTY-scope refusal below catches it
        missing.append(rel)
        continue
    seen.add(rel)
    if not rel.endswith(".py"):
        # docs/markdown/rst touched by a patch carry no imports to resolve
        continue
    try:
        tree = ast.parse(p.read_text())
    except SyntaxError:
        continue
    for n in ast.walk(tree):
        if (isinstance(n, ast.ImportFrom) and n.level == 0 and n.module
                and n.module.split(".")[0] == "tests"):
            parts = n.module.split(".")
            mod_file = "/".join(parts) + ".py"
            pkg_init = "/".join(parts) + "/__init__.py"
            for cand in (mod_file, pkg_init):
                if (root / cand).is_file() and cand not in seen:
                    queue.append(cand)
print("\n".join(sorted(f for f in seen if f.endswith(".py"))))
PY
)"
[ -n "$GATE_SCOPE" ] || {
  echo "BUILD REFUSED (stale patch/test import): gate scope is EMPTY -- refusing to skip the import gate" >&2
  exit 1
}
GATE_FILES=()
while IFS= read -r _f; do
  [ -n "$_f" ] && GATE_FILES+=("$_f")
done <<< "$GATE_SCOPE"
if ! python3 "$ROOT/implementation/kvpersist/check_patched_imports.py" "$SRC" "${GATE_FILES[@]}"; then
  echo "BUILD REFUSED (stale patch/test import): the patched tree carries unresolvable imports" >&2
  echo "  A patch taken from a PR head is NOT automatically compatible with the pinned base" >&2
  echo "  ('applies cleanly' does NOT mean 'semantically current') -- port the patch to the pin" >&2
  echo "  (fix the import/stale name in the patch and re-export it), do NOT weaken this gate." >&2
  exit 1
fi
note "patched-tree import gate OK (${#GATE_FILES[@]} files)"

# r3 P0-2: inside the container the build runs as root while /usr/src/lmcache
# is mounted from the host (different uid) -- git refuses with "dubious
# ownership" and setuptools_scm's `pip wheel` aborts. The container is
# disposable, so an unconditional safe.directory entry is safe here.
# Also pin the version explicitly: pyproject has no setuptools_scm
# fallback_version, so without this the wheel came out as 0.0.0 / 0.1.dev...
# or the build died "unable to detect version". The +kvp.<12 hex> local part
# makes this wheel distinguishable from the upstream build.
# r15 P0': the base label follows the PINNED BASE, not a nightly tag. The old
# label (the nightly name of the old pin) was BELOW the real base of the new
# pin; 0.5.8rc1.post48 matches `git rev-list --count
# v0.5.8rc1..<pin>` (48 for 07f1c6bb6) -- PEP 440 valid and not below the real
# base. The +kvp local part keeps the release-egg_info hook (setup.py) from
# treating this as a release.
# Written to SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE first (its literal value
# must be visible here for the r3 P0-2 wiring test), then reused as-is.
export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=0.5.8rc1.post48+kvp.${PATCHED_STATE:0:12}
KVP_WHEEL_VERSION=$SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE
note "wheel version pin: SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE=$KVP_WHEEL_VERSION"
# ---------------------------------------------------------------------------
# 8 + 9. Regression tests and package build, inside the pinned image.
# ---------------------------------------------------------------------------
PROV="$OUT_DIR/provenance.json"
WHEEL_OUT="$OUT_DIR/wheels"
mkdir -p "$WHEEL_OUT"
note "running build+tests inside the pinned image (in-container phase)"
# r9 P1-1: the GPU regression step needs REAL GPUs. KVP_BUILD_GPU_TESTS=1 adds
# --gpus all (array args); without the nvidia runtime the build stops HERE,
# before the container starts. A GPU run that would only skip must never be
# recorded as done.
KVP_DOCKER_ARGS=(--rm)
if [ "${KVP_BUILD_GPU_TESTS:-0}" = "1" ]; then
  if ! docker info 2>/dev/null | grep -qi "Runtimes:.*nvidia\|nvidia"; then
    echo "ERROR(build-lmcache): KVP_BUILD_GPU_TESTS=1 needs the nvidia container runtime." >&2
    echo "  What to do: install/configure the NVIDIA Container Toolkit" >&2
    echo "  (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)," >&2
    echo "  verify with: docker info | grep -i nvidia, then re-run." >&2
    exit 1
  fi
  KVP_DOCKER_ARGS+=(--gpus all)
  note "GPU regression requested -- docker run with --gpus all"
fi
# r6-7 A-1: the in-container phase is delivered as a FILE mounted read-only at
# /kvp-scripts (scripts/_incontainer_build.sh), NOT over stdin. `docker run`
# without `-i` leaves stdin EMPTY, so the old `bash -s <<'INCONTAINER'` ran
# NOTHING and exited 0 -- the entire phase silently vanished (operator-machine
# bug). A mounted file always arrives, and the phase is shellcheck-able alone.
# NOTE: no heredoc here on purpose -- never hand build work over stdin.
DOCKER_RC=0
docker run "${KVP_DOCKER_ARGS[@]}" \
  -v "$SRC":/usr/src/lmcache:rw \
  -v "$PATCH_DIR":/kvp-patches:ro \
  -v "$HERE":/kvp-scripts:ro \
  -v "$ROOT/implementation":/kvp-impl:ro \
  -v "$WHEEL_OUT":/kvp-wheels:rw \
  -v "$WORK_DIR":/kvp-work:rw \
  -e PYTHONUNBUFFERED=1 \
  -e LMCACHE_SOURCE_COMMIT="$LMCACHE_SOURCE_COMMIT" \
  -e PR6551_MERGE="$PR6551_MERGE" \
  -e KVP_BUILD_SKIP_TESTS="${KVP_BUILD_SKIP_TESTS:-0}" \
  -e SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE="$KVP_WHEEL_VERSION" \
  -e KVP_HOST_UID="$(id -u)" \
  -e KVP_HOST_GID="$(id -g)" \
  -e KVP_BUILD_GPU_TESTS="${KVP_BUILD_GPU_TESTS:-0}" \
  -e KVP_TORCH_CUDA_ARCH_LIST="${KVP_TORCH_CUDA_ARCH_LIST:-12.0}" \
  --entrypoint bash "$IMAGE" /kvp-scripts/_incontainer_build.sh \
  || DOCKER_RC=$?

# r6-7 A-4: a failed or silent phase must produce REAL diagnostics, not
# "see output above" into empty output.
if [ "$DOCKER_RC" != "0" ]; then
  echo "ERROR(build-lmcache): in-container phase failed (docker run rc=$DOCKER_RC)." >&2
  echo "  work dir: $WORK_DIR" >&2
  if [ -s "$WORK_DIR/pytest-report.txt" ]; then
    echo "  last lines of $WORK_DIR/pytest-report.txt:" >&2
    tail -n 20 "$WORK_DIR/pytest-report.txt" >&2
  else
    echo "  (no pytest-report.txt in the work dir -- the phase likely died before tests)" >&2
  fi
  exit 1
fi
if [ ! -d "$WHEEL_OUT" ] || [ -z "$(find "$WHEEL_OUT" -name '*.whl' -print -quit 2>/dev/null)" ]; then
  echo "ERROR(build-lmcache): the in-container phase produced no wheel." >&2
  echo "  docker run rc=0 but no *.whl appeared in $WHEEL_OUT -- the build" >&2
  echo "  script inside the container did not reach 'pip wheel'." >&2
  echo "  work dir: $WORK_DIR" >&2
  if [ -s "$WORK_DIR/pytest-report.txt" ]; then
    echo "  last lines of $WORK_DIR/pytest-report.txt:" >&2
    tail -n 20 "$WORK_DIR/pytest-report.txt" >&2
  fi
  exit 1
fi

# ---------------------------------------------------------------------------
# 10. Record exact final source identity and artifact checksum.
# ---------------------------------------------------------------------------
WHEEL_NAME="$(find "$WHEEL_OUT" -name '*.whl' -printf '%f\n' | head -1 || true)"
[ -n "$WHEEL_NAME" ] || die "no wheel produced (see the phase diagnostics above; work dir: $WORK_DIR)"

# r2 P0-2: the wheel MUST contain the compiled cuda_ops extension. A wheel
# without it was built without nvcc and silently degrades to the slow Python
# copy path at runtime (cudaMemcpy error 1 incident).
PYTHONPATH="$ROOT/implementation" python3 -m kvpersist.check_wheel_native "$WHEEL_OUT/$WHEEL_NAME" \
  || die "wheel $WHEEL_NAME lacks lmcache/cuda_ops*.so -- refusing to record a pure-Python wheel"

WHEEL_SHA="$(sha256sum "$WHEEL_OUT/$WHEEL_NAME" | awk '{print $1}')"
NVCC_VERSION_REC="$(cat "$WORK_DIR/nvcc-version.txt" 2>/dev/null || echo unknown)"

# shellcheck disable=SC1090
# r11: identity.env is sourced without `|| true` that hides breakage.
# The container writes single-quoted values (C15) so a non-empty JSON
# (NATIVE_EXTENSIONS_JSON='{"a": "b"}') is a valid shell assignment.
if [ -f "$WORK_DIR/identity.env" ]; then
  . "$WORK_DIR/identity.env"
else
  echo "WARNING(build-lmcache): identity.env not found -- provenance will use defaults" >&2
fi

# r12 P0: default values assigned separately before the heredoc -- the old
# brace-default form appended a stray '}' to non-empty values (bash closes the
# parameter expansion at the first closing brace).
TORCH_CUDA_ARCH_LIST_USED_VALUE="${TORCH_CUDA_ARCH_LIST_USED:-}"
CUDA_OPS_ARCHS_VALUE="${CUDA_OPS_ARCHS:-}"
NATIVE_EXTENSIONS_JSON_VALUE="${NATIVE_EXTENSIONS_JSON:-}"
GPU_REGRESSION_RUN_VALUE="${GPU_REGRESSION_RUN:-}"
[ -n "$TORCH_CUDA_ARCH_LIST_USED_VALUE" ] || TORCH_CUDA_ARCH_LIST_USED_VALUE="12.0"
[ -n "$CUDA_OPS_ARCHS_VALUE" ] || CUDA_OPS_ARCHS_VALUE="unknown"
[ -n "$NATIVE_EXTENSIONS_JSON_VALUE" ] || NATIVE_EXTENSIONS_JSON_VALUE="{}"
[ -n "$GPU_REGRESSION_RUN_VALUE" ] || GPU_REGRESSION_RUN_VALUE="false"
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
  "pr5972_source_revision": "bd79d4c6acf6096061b5092517698239440cb93b",
  "pr5972_patch": "vendor/patches/lmcache/5972-incomplete-kv-objects.patch",
  "pr5972_patch_sha256": "$(sha256sum "$PATCH_5972" | awk '{print $1}')",
  "patched_tree_fingerprint": "$PATCHED_STATE",
  "build_image": "$IMAGE",
  "nvcc_version": "$NVCC_VERSION_REC",
  "build_with_cuda": "1",
  "torch_cuda_arch_list": "$TORCH_CUDA_ARCH_LIST_USED_VALUE",
  "cuda_ops_archs": "$CUDA_OPS_ARCHS_VALUE",
  "native_extensions": $NATIVE_EXTENSIONS_JSON_VALUE,
  "cuda_ops_in_wheel": true,
  "wheel_version": "${KVP_WHEEL_VERSION:-unknown}",
  "torch_version": "${TORCH_VERSION:-unknown}",
  "torch_cuda": "${TORCH_CUDA:-unknown}",
  "python_version": "${PYTHON_VERSION:-unknown}",
  "cuda_available": "${CUDA_AVAILABLE:-unknown}",
  "gpu_count": "${GPU_COUNT:-unknown}",
  "gpu_compute_capability": "${GPU_CC:-unknown}",
  "tests_skipped": "${KVP_BUILD_SKIP_TESTS:-0}",
  "gpu_regression_run": $GPU_REGRESSION_RUN_VALUE,
  "wheel": "$WHEEL_NAME",
  "wheel_sha256": "$WHEEL_SHA"
}
EOF

note "custom LMCache wheel: $WHEEL_OUT/$WHEEL_NAME"
note "wheel sha256:       $WHEEL_SHA"
note "provenance:         $PROV"
note "source:             $SRC (base $LMCACHE_SOURCE_COMMIT)"
note "DONE -- install this exact wheel into the custom derivative image (build-image.sh)"