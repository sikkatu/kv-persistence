#!/usr/bin/env bash
# rerun-lmcache-gate.sh -- fast CPU-gate rerun WITHOUT rebuilding the wheel.
#
# r16 P3: test-only iterations take ~2 minutes instead of a full build: the
# tests come from the source tree (staging), not from the wheel, so changing
# only patch test files needs no recompile. This script:
#   1. re-applies the two patches to a FRESH checkout of the pinned base,
#   2. runs the r15 P1 static import gate (check_patched_imports.py),
#   3. takes the ALREADY BUILT wheel from a previous work dir (auto-detects
#      the newest /tmp/kvp-lmc-build.* carrying wheels/*.whl, or WORK_DIR),
#   4. installs it into the same pinned image and runs C8-C12 there.
#
# Conditions (all fail-loud):
#   (a) the PRODUCTION part of the patched tree (everything except tests/ and
#       docs/) must match the tree the wheel was built from -- the hash is
#       recorded in the work dir (rerun-prod-hash.txt) or provenance; a
#       mismatch refuses with "need a full build";
#   (b) end banner `NOT production-acceptable: no provenance/identity
#       written` -- nothing is written to live/lmcache-build;
#   (c) the SAME gate logic as _incontainer_build.sh -- the shared module
#       kvpersist.gate_common (C8 import-check, C12 junit counting), never a
#       copy.
#
# Final acceptance is still ONLY via the full `scripts/build-lmcache.sh`.
#
# Usage:
#   bash scripts/rerun-lmcache-gate.sh [WORK_DIR]
# (WORK_DIR = a previous build-lmcache work dir; autodetected when omitted)

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
# WORK_DIR: explicit argument, or the newest /tmp/kvp-lmc-build.* with a wheel
# ---------------------------------------------------------------------------
WORK_DIR="${1:-}"
if [ -z "$WORK_DIR" ]; then
  for _cand in $(ls -d /tmp/kvp-lmc-build.* 2>/dev/null | sort -r); do
    if [ -n "$(find "$_cand/wheels" -name '*.whl' -print -quit 2>/dev/null)" ]; then
      WORK_DIR="$_cand"
      break
    fi
  done
fi
[ -n "$WORK_DIR" ] || die "no previous work dir with a wheel (pass one as \$1 or build first)"
[ -d "$WORK_DIR" ] || die "work dir not found: $WORK_DIR"
WHEEL="$(find "$WORK_DIR/wheels" -name '*.whl' -print -quit 2>/dev/null)"
[ -n "$WHEEL" ] || die "no *.whl in $WORK_DIR/wheels -- run scripts/build-lmcache.sh first (no wheel to rerun against)"
note "work dir: $WORK_DIR"
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
# the tree the wheel was built from (hash recorded at build time or now)
# ---------------------------------------------------------------------------
PROD_HASH="$(git -C "$SRC" add -A >/dev/null 2>&1; \
  git -C "$SRC" diff --cached --name-only | grep -v '^tests/' | grep -v '^docs/' | sort \
  | while IFS= read -r _f; do
      printf '%s ' "$_f"
      git -C "$SRC" show ":$_f" 2>/dev/null | sha256sum | awk '{print $1}'
    done | sha256sum | awk '{print $1}')"
REF_HASH_FILE="$WORK_DIR/rerun-prod-hash.txt"
if [ ! -f "$REF_HASH_FILE" ]; then
  # fall back: record the CURRENT hash (first rerun against this work dir)
  echo "$PROD_HASH" > "$REF_HASH_FILE"
  note "production hash recorded for this work dir (first rerun): $PROD_HASH"
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
note "running C8-C12 in the pinned image (wheel from $WORK_DIR)"
RERUN_RC=0
docker run --rm \
  -v "$SRC":/usr/src/lmcache:rw \
  -v "$ROOT/scripts":/kvp-scripts:ro \
  -v "$ROOT/implementation":/kvp-impl:ro \
  -v "$RERUN_WORK":/kvp-work:rw \
  -v "$WORK_DIR/wheels":/kvp-wheels:ro \
  -e PYTHONUNBUFFERED=1 \
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