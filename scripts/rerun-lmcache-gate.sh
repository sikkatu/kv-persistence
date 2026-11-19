#!/usr/bin/env bash
# rerun-lmcache-gate.sh -- fast CPU-gate rerun WITHOUT rebuilding the wheel.
#
# r16 P3: test-only iterations take ~2 minutes instead of a full build: the
# tests come from the source tree (staging), not from the wheel, so changing
# only patch test files needs no recompile. This script:
#   1. re-applies the two patches to a FRESH checkout of the pinned base,
#   2. runs the r15 P1 static import gate (check_patched_imports.py),
#   3. takes the ALREADY BUILT wheel from the build output dir (OUT_DIR,
#      default $ROOT/live/lmcache-build -- the same place build-lmcache.sh
#      writes it to, mounted at /kvp-wheels during the build),
#   4. installs it into the same pinned image and runs C8-C12 there.
#
# Conditions (all fail-loud):
#   (a) the PRODUCTION part of the patched tree (everything except tests/ and
#       docs/) must match the tree the wheel was built from -- the hash is
#       recorded by build-lmcache.sh in $OUT_DIR/rerun-prod-hash.txt BEFORE
#       the build. No hash file -> "Need a full build" (or --trust-wheel,
#       which records the CURRENT hash with a loud warning);
#   (b) end banner `NOT production-acceptable: no provenance/identity
#       written` -- nothing is written to live/lmcache-build;
#   (c) the SAME gate logic as _incontainer_build.sh -- the shared module
#       kvpersist.gate_common (C8 import-check, C12 junit counting, prod-hash
#       algorithm), never a copy.
#
# Final acceptance is still ONLY via the full `scripts/build-lmcache.sh`.
#
# Usage:
#   bash scripts/rerun-lmcache-gate.sh [--trust-wheel] [OUT_DIR]
# (OUT_DIR = the build output dir, default $ROOT/live/lmcache-build or
#  $KVP_OUT_DIR; --trust-wheel records the CURRENT production hash when the
#  build predates the hash file -- loudly assumed, never silently)

set -Eeuo pipefail

die() { echo "ERROR(rerun-lmcache-gate): $*" >&2; exit 1; }
note() { echo "[rerun-gate] $*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PATCH_DIR="${KVP_PATCH_DIR:-$ROOT/vendor/patches/lmcache}"
[ -d "$PATCH_DIR" ] || die "patch dir not found: $PATCH_DIR"
GATE_PY="$ROOT/implementation/kvpersist/check_patched_imports.py"
GATE_COMMON="$ROOT/implementation/kvpersist/gate_common.py"
[ -f "$GATE_PY" ] || die "import gate not found: $GATE_PY"
[ -f "$GATE_COMMON" ] || die "shared gate module not found: $GATE_COMMON"

# r6-7 B-1: the same single pin file as build-lmcache.sh
KVP_PIN_FILE="${KVP_PIN_FILE:-$ROOT/vendor/pinned-base-image.env}"
[ -f "$KVP_PIN_FILE" ] || die "pin file not found: $KVP_PIN_FILE"
KVP_BASE_IMAGE_DEFAULT=""
# shellcheck disable=SC1090
. "$KVP_PIN_FILE" || die "cannot read pin file: $KVP_PIN_FILE"
BASE_IMAGE="${KVP_BASE_IMAGE:-${KVP_BASE_IMAGE_DEFAULT:-}}"
[ -n "$BASE_IMAGE" ] || die "no base image resolved -- set KVP_BASE_IMAGE or KVP_BASE_IMAGE_DEFAULT"
case "$BASE_IMAGE" in
  *@sha256:*) : ;;
  *) die "base image must be pinned by digest -- got '$BASE_IMAGE'" ;;
esac

LMCACHE_SOURCE_COMMIT="${LMCACHE_SOURCE_COMMIT:-07f1c6bb642f4772606a36eab730ae009ddce08f}"
LMCACHE_REPO_URL="${LMCACHE_REPO_URL:-https://github.com/LMCache/LMCache.git}"
PATCH_4136="$PATCH_DIR/4136-mp-retrieve-abort.patch"
PATCH_5972="$PATCH_DIR/5972-incomplete-kv-objects.patch"
[ -f "$PATCH_4136" ] || die "missing patch: $PATCH_4136"
[ -f "$PATCH_5972" ] || die "missing patch: $PATCH_5972"

# ---------------------------------------------------------------------------
# OUT_DIR: the build output dir (where build-lmcache.sh writes the wheel and
# the production hash). Optional argument [OUT_DIR]; --trust-wheel records
# the CURRENT production hash when the build predates the hash file.
# ---------------------------------------------------------------------------
TRUST_WHEEL=0
OUT_DIR="${KVP_OUT_DIR:-$ROOT/live/lmcache-build}"
for _arg in "$@"; do
  case "$_arg" in
    --trust-wheel) TRUST_WHEEL=1 ;;
    -*) die "unknown option: $_arg (usage: bash scripts/rerun-lmcache-gate.sh [--trust-wheel] [OUT_DIR])" ;;
    *) OUT_DIR="$_arg" ;;
  esac
done
WHEELS="$OUT_DIR/wheels"
[ -d "$WHEELS" ] || die "wheel dir not found: $WHEELS -- run scripts/build-lmcache.sh first (or pass the right OUT_DIR)"
# r17 B3: EXACTLY one wheel, refused otherwise (C5's rm -f *.whl leaves the
# wheel of the LAST build here, even a failed one -- no arbitrary pick)
_wheel_list=("$WHEELS"/*.whl)
if [ ! -e "${_wheel_list[0]:-}" ]; then
  die "no *.whl in $WHEELS -- run scripts/build-lmcache.sh first (no wheel to rerun against)"
fi
if [ "${#_wheel_list[@]}" != "1" ]; then
  die "expected exactly one wheel in $WHEELS, found ${#_wheel_list[@]} -- refuse to pick one arbitrarily (clean the dir or rebuild)"
fi
WHEEL="${_wheel_list[0]}"
note "out dir:  $OUT_DIR"
note "wheel:    $WHEEL"

# ---------------------------------------------------------------------------
# Fresh checkout of the pinned base + the two patches (same order as the
# build): this is the tree the gate sees; the tests run from THIS tree.
# ---------------------------------------------------------------------------
RERUN_DIR="${KVP_RERUN_DIR:-$(mktemp -d /tmp/kvp-lmc-rerun.XXXXXX)}"
SRC="$RERUN_DIR/LMCache"
note "fresh checkout: $SRC (pinned $LMCACHE_SOURCE_COMMIT)"
git clone --quiet --filter=blob:none "$LMCACHE_REPO_URL" "$SRC" \
  || die "clone failed"
git -C "$SRC" fetch --quiet origin "$LMCACHE_SOURCE_COMMIT" 2>/dev/null \
  || git -C "$SRC" fetch --quiet origin
git -C "$SRC" checkout --quiet --detach "$LMCACHE_SOURCE_COMMIT" \
  || die "cannot check out pinned commit $LMCACHE_SOURCE_COMMIT"
ACTUAL_SHA="$(git -C "$SRC" rev-parse HEAD)"
[ "$ACTUAL_SHA" = "$LMCACHE_SOURCE_COMMIT" ] \
  || die "source revision mismatch: expected $LMCACHE_SOURCE_COMMIT got $ACTUAL_SHA"

apply_patch() {
  local file="$1" label="$2"
  if git -C "$SRC" apply --reverse --check "$file" >/dev/null 2>&1; then
    die "patch $label is ALREADY applied unexpectedly"
  fi
  git -C "$SRC" apply --check "$file" \
    || die "patch $label does NOT apply cleanly to $LMCACHE_SOURCE_COMMIT"
  git -C "$SRC" apply "$file"
  note "applied $label ($(grep -c '^diff --git' "$file") files)"
}
apply_patch "$PATCH_4136" "4136-mp-retrieve-abort"
apply_patch "$PATCH_5972" "5972-incomplete-kv-objects"

# ---------------------------------------------------------------------------
# (a) production-part check: everything except tests/ and docs/ must match
# the tree the wheel was built from (recorded by build-lmcache.sh BEFORE the
# build, in $OUT_DIR/rerun-prod-hash.txt)
# ---------------------------------------------------------------------------
PROD_HASH="$(PYTHONPATH="$ROOT/implementation" python3 -m kvpersist.gate_common prod-hash "$SRC")" \
  || die "cannot compute the production-part hash of the patched tree"
REF_HASH_FILE="$OUT_DIR/rerun-prod-hash.txt"
if [ ! -f "$REF_HASH_FILE" ]; then
  if [ "$TRUST_WHEEL" = "1" ]; then
    # r17 B3: the ONLY place that records a hash outside the build. Loud:
    # the operator asserts the wheel was built from THIS production tree.
    echo "$PROD_HASH" > "$REF_HASH_FILE"
    echo "WARNING(rerun-lmcache-gate): --trust-wheel -- ASSUMING the wheel was built from this production tree." >&2
    echo "  No $REF_HASH_FILE existed (the build predates the hash recording)." >&2
    echo "  The CURRENT production hash is recorded: $PROD_HASH" >&2
    echo "  If the production code changed since that build, this rerun tests the WRONG wheel." >&2
    note "production hash recorded via --trust-wheel ($PROD_HASH)"
  else
    echo "ERROR(rerun-lmcache-gate): no production hash recorded: $REF_HASH_FILE" >&2
    echo "  The wheel cannot be tied to a production tree -- the gate would run" >&2
    echo "  against a wheel of unknown provenance." >&2
    echo "  Need a full build: bash scripts/build-lmcache.sh" >&2
    echo "  (or, if this wheel really was built from the current production tree:" >&2
    echo "   bash scripts/rerun-lmcache-gate.sh --trust-wheel)" >&2
    exit 1
  fi
else
  REF_HASH="$(cat "$REF_HASH_FILE")"
  if [ "$REF_HASH" != "$PROD_HASH" ]; then
    echo "ERROR(rerun-lmcache-gate): the PRODUCTION part of the patched tree" >&2
    echo "  differs from the tree the wheel was built from" >&2
    echo "  (built: $REF_HASH, now: $PROD_HASH)" >&2
    echo "  Need a full build: bash scripts/build-lmcache.sh" >&2
    exit 1
  fi
  note "production part matches the wheel's tree ($PROD_HASH)"
fi

# ---------------------------------------------------------------------------
# r15 P1: static import gate on the patched tree (same scope as the build)
# ---------------------------------------------------------------------------
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
        missing.append(rel)
        continue
    seen.add(rel)
    if not rel.endswith(".py"):
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
if ! python3 "$GATE_PY" "$SRC" "${GATE_FILES[@]}"; then
  echo "BUILD REFUSED (stale patch/test import): the patched tree carries unresolvable imports" >&2
  echo "  (fix the patch and re-export it -- do NOT weaken this gate)" >&2
  exit 1
fi
note "patched-tree import gate OK (${#GATE_FILES[@]} files)"

# ---------------------------------------------------------------------------
# C8-C12 in the pinned image, with the ALREADY BUILT wheel
# ---------------------------------------------------------------------------
RERUN_WORK="$RERUN_DIR/work"
mkdir -p "$RERUN_WORK"
note "running C8-C12 in the pinned image (wheel from $WHEELS)"
RERUN_RC=0
docker run --rm \
  -v "$SRC":/usr/src/lmcache:rw \
  -v "$ROOT/scripts":/kvp-scripts:ro \
  -v "$ROOT/implementation":/kvp-impl:ro \
  -v "$RERUN_WORK":/kvp-work:rw \
  -v "$WHEELS":/kvp-wheels:ro \
  -e PYTHONUNBUFFERED=1 \
  -e KVP_HOST_UID="$(id -u)" \
  -e KVP_HOST_GID="$(id -g)" \
  -e KVP_LMC_SRC=/usr/src/lmcache \
  -e KVP_SCRIPTS_DIR=/kvp-scripts \
  -e KVP_IMPL_DIR=/kvp-impl \
  -e KVP_WORK_DIR=/kvp-work \
  -e KVP_WHEELS_DIR=/kvp-wheels \
  -e KVP_RERUN_ONLY=1 \
  --entrypoint bash "$BASE_IMAGE" /kvp-scripts/_rerun_incontainer.sh \
  || RERUN_RC=$?

if [ "$RERUN_RC" != "0" ]; then
  echo "ERROR(rerun-lmcache-gate): the gate rerun failed (docker run rc=$RERUN_RC)" >&2
  [ -s "$RERUN_WORK/pytest-report.txt" ] && tail -n 20 "$RERUN_WORK/pytest-report.txt" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# (b) the honest banner -- never production-acceptable
# ---------------------------------------------------------------------------
echo
echo "============================================================"
echo " NOT production-acceptable: no provenance/identity written"
echo " (fast gate rerun -- final acceptance is the full"
echo "  scripts/build-lmcache.sh run)"
echo "============================================================"
note "done"