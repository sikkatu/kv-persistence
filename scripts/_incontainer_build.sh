#!/usr/bin/env bash
# _incontainer_build.sh -- the IN-CONTAINER build phase of build-lmcache.sh.
# Reference pipeline (r11): steps C0-C16 in exact order. Each gate is covered
# by a REAL-program test (see tests/unit/test_r11_*.py). No stdin heredoc
# delivery -- the file is mounted at /kvp-scripts (r6-7 A-1).
#
# Environment contract (passed via -e by build-lmcache.sh):
#   LMCACHE_SOURCE_COMMIT, PR6551_MERGE, KVP_BUILD_SKIP_TESTS,
#   KVP_BUILD_GPU_TESTS, KVP_HOST_UID/GID, KVP_TORCH_CUDA_ARCH_LIST,
#   SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE, PYTHONUNBUFFERED=1
# Mounted: /usr/src/lmcache (rw), /kvp-patches (ro), /kvp-scripts (ro),
# /kvp-impl (ro), /kvp-wheels (rw), /kvp-work (rw).
set -Eeuo pipefail

# ---- C0: prologue ---------------------------------------------------------
LMC_SRC="${KVP_LMC_SRC:-/usr/src/lmcache}"
KVP_WORK="${KVP_WORK_DIR:-/kvp-work}"
KVP_WHEELS="${KVP_WHEELS_DIR:-/kvp-wheels}"
KVP_SCRIPTS="${KVP_SCRIPTS_DIR:-/kvp-scripts}"
KVP_IMPL="${KVP_IMPL_DIR:-/kvp-impl}"

_kvp_chown_back() {
  _uid="${KVP_HOST_UID:-}"
  _gid="${KVP_HOST_GID:-}"
  [ -n "$_uid" ] && [ -n "$_gid" ] || return 0
  chown -R "$_uid:$_gid" "$LMC_SRC" "$KVP_WHEELS" "$KVP_WORK" 2>/dev/null || true
  echo "[in-container] ownership returned to $_uid:$_gid"
}
trap _kvp_chown_back EXIT

pip freeze > "$KVP_WORK/image-freeze.before.txt" 2>/dev/null || true
echo "[in-container] baseline freeze recorded: $KVP_WORK/image-freeze.before.txt"

# ---- C1: apt + git + CUDA dev packages -------------------------------------
CUDA_TORCH="$(python3 -c 'import torch; print(torch.version.cuda)')"
DEV_SUFFIX="$(bash "$KVP_SCRIPTS/_cuda_dev_suffix.sh" "$CUDA_TORCH")"
[ -n "$DEV_SUFFIX" ] || { echo "BUILD REFUSED: cannot derive dev suffix from torch.version.cuda='$CUDA_TORCH'" >&2; exit 1; }
echo "[in-container] CUDA dev suffix (torch.version.cuda=$CUDA_TORCH): $DEV_SUFFIX"
export CUDA_HOME=/usr/local/cuda

apt_out="$(mktemp)"
if ! apt-get update -y >"$apt_out" 2>&1; then
  echo "BUILD REFUSED: 'apt-get update' failed in this image." >&2
  tail -n 20 "$apt_out" >&2; exit 1
fi
for pkg in git cuda-cudart-dev-$DEV_SUFFIX libcusparse-dev-$DEV_SUFFIX \
           libcusolver-dev-$DEV_SUFFIX libcublas-dev-$DEV_SUFFIX; do
  if ! apt-get install -y --no-install-recommends "$pkg" >"$apt_out" 2>&1; then
    echo "BUILD REFUSED: package '$pkg' is unavailable in this image." >&2
    tail -n 20 "$apt_out" >&2; exit 1
  fi
  echo "[in-container] installed $pkg"
done

# ---- C2: git safe.directory (strictly after C1) ----------------------------
git config --global --add safe.directory "$LMC_SRC"
echo "[in-container] git: $(git --version 2>/dev/null || echo unknown)"

# ---- C3: toolchain ----------------------------------------------------------
for tool in nvcc g++ ninja; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "BUILD REFUSED: $tool not found -- cannot compile the CUDA extensions." >&2
    exit 1
  fi
done
# r12 P1: every pipeline in a substitution is marked -- either `|| true`
# (empty result acceptable) or an explicit BUILD REFUSED below. A pipeline
# that dies under `pipefail` must never end the script silently.
NVCC_VERSION="$(nvcc --version | tail -1 || true)"
[ -n "$NVCC_VERSION" ] || { echo "BUILD REFUSED: 'nvcc --version' produced no output -- toolchain broken" >&2; exit 1; }
echo "[in-container] nvcc: $NVCC_VERSION"
export BUILD_WITH_CUDA=1
if [ -n "${TORCH_CUDA_ARCH_LIST:-}" ]; then
  echo "[in-container] NOTE: image TORCH_CUDA_ARCH_LIST='$TORCH_CUDA_ARCH_LIST' overridden to '${KVP_TORCH_CUDA_ARCH_LIST:-12.0}'"
fi
export TORCH_CUDA_ARCH_LIST="${KVP_TORCH_CUDA_ARCH_LIST:-12.0}"
echo "[in-container] BUILD_WITH_CUDA=1 TORCH_CUDA_ARCH_LIST=$TORCH_CUDA_ARCH_LIST"
echo "$NVCC_VERSION" > "$KVP_WORK/nvcc-version.txt"

# ---- C4: build dependencies (constraints, no silent fallback) --------------
# constraints = engine pins from the baseline freeze (torch*, vllm, numpy,
# triton) so pip cannot touch the engine stack.
grep -iE '^(torch|vllm|numpy|triton)' "$KVP_WORK/image-freeze.before.txt" \
  > "$KVP_WORK/build-constraints.txt" 2>/dev/null || true
if ! pip install --no-cache-dir -q -c "$KVP_WORK/build-constraints.txt" \
     -r "$LMC_SRC/requirements/build.txt"; then
  echo "BUILD REFUSED: build dependencies cannot be installed (see pip output above)." >&2
  exit 1
fi
echo "[in-container] build dependencies installed (constraints: $KVP_WORK/build-constraints.txt)"

# ---- C5: build the wheel (exactly once) ------------------------------------
rm -f "$KVP_WHEELS"/*.whl 2>/dev/null || true
cd "$LMC_SRC"
python3 -m pip wheel . --no-build-isolation --no-deps -w "$KVP_WHEELS"
# r12 P1: `ls | wc` and `ls | head` are fail-soft pipelines (empty result is
# acceptable -- the check below refuses loudly); marked `|| true` so a failing
# `ls` cannot end the script silently under pipefail.
_wheel_count="$(ls "$KVP_WHEELS"/*.whl 2>/dev/null | wc -l || true)"
if [ "$_wheel_count" != "1" ]; then
  echo "BUILD REFUSED: expected exactly one wheel, found $_wheel_count in $KVP_WHEELS" >&2
  exit 1
fi
WHEEL_FILE="$(ls "$KVP_WHEELS"/*.whl | head -1 || true)"
echo "[in-container] wheel built: $WHEEL_FILE"

# ---- C6a: wheel content vs git ls-files ------------------------------------
python3 - "$KVP_WHEELS" "$LMC_SRC" <<'PY' || exit 1
import subprocess, sys, zipfile, pathlib
wheels, src = sys.argv[1], sys.argv[2]
whl = sorted(pathlib.Path(wheels).glob("*.whl"))
names = set(zipfile.ZipFile(whl[0]).namelist())
tracked = subprocess.run(["git", "-C", src, "ls-files", "lmcache"],
                         capture_output=True, text=True, check=True).stdout.split()
missing = [f for f in tracked
           if not f.endswith(".py") and not f.endswith(".so") and f not in names]
if missing:
    print("BUILD REFUSED: wheel is missing non-py package data tracked by git:", file=sys.stderr)
    for f in missing:
        print("  missing:", f, file=sys.stderr)
    raise SystemExit(1)
print(f"[in-container] wheel file-list check OK ({len(tracked)} tracked, none missing)")
PY

# ---- C6b: native extensions (both required) --------------------------------
NATIVE_OUT="$(PYTHONPATH="$KVP_IMPL" python3 -m kvpersist.check_wheel_native "$WHEEL_FILE")" \
  || { echo "BUILD REFUSED: the wheel lacks required native extensions (see above)." >&2; exit 1; }
echo "$NATIVE_OUT"
# r12 P0: the old brace-default parameter expansion (literal `:`, `-`, `{`,
# then a closing brace) closed at the first `}` -- the second brace became a
# literal and a non-empty JSON got a stray `}` appended (garbage in
# identity.env / provenance.json). Default is assigned on its own line.
# r12 P1: fail-soft pipeline (empty result is acceptable -- the default
# below covers it), marked `|| true`.
NATIVE_EXTENSIONS_JSON="$(printf '%s\n' "$NATIVE_OUT" | sed -n 's/^native_extensions=//p' | tail -1 || true)"
[ -n "$NATIVE_EXTENSIONS_JSON" ] || NATIVE_EXTENSIONS_JSON='{}'
export NATIVE_EXTENSIONS_JSON

# ---- C6c: cuobjdump arch check (sm_120 required) ---------------------------
# r12 P1: the .so is located and extracted with python3 zipfile -- `unzip` is
# NOT guaranteed in the image (a missing unzip used to kill the script with
# rc=127 and NO message). Every pipeline is fail-soft (`|| true`) and every
# empty result gets an EXPLICIT BUILD REFUSED with diagnostics.
CUDA_OPS_ARCHS="unknown"
if command -v cuobjdump >/dev/null 2>&1; then
  _so="$(python3 -c 'import sys, zipfile
for n in zipfile.ZipFile(sys.argv[1]).namelist():
    if n.startswith("lmcache/cuda_ops") and n.endswith(".so"):
        print(n); break' "$WHEEL_FILE" || true)"
  if [ -n "$_so" ]; then
    mkdir -p /tmp/kvp-arch && cd /tmp/kvp-arch
    python3 -c 'import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extract(sys.argv[2], "/tmp/kvp-arch")' "$WHEEL_FILE" "$_so"
    CUDA_OPS_ARCHS="$(cuobjdump --list-elf "/tmp/kvp-arch/$_so" | grep -o 'sm_[0-9]*' | sort -u | tr '\n' ' ' | sed 's/ $//' || true)"
    cd - >/dev/null
  fi
  echo "[in-container] cuda_ops archs (cuobjdump): $CUDA_OPS_ARCHS"
  if [ -z "$CUDA_OPS_ARCHS" ] || [ "$CUDA_OPS_ARCHS" = "unknown" ]; then
    echo "BUILD REFUSED: cuobjdump produced no architectures (wheel member: '${_so:-none}')" >&2
    echo "  raw 'cuobjdump --list-elf' output:" >&2
    cuobjdump --list-elf "/tmp/kvp-arch/${_so:-missing.so}" >&2 || true
    exit 1
  fi
  case " $CUDA_OPS_ARCHS " in
    *" sm_120 "*) : ;;
    *) echo "BUILD REFUSED: cuda_ops*.so does NOT contain sm_120 (found: '$CUDA_OPS_ARCHS')" >&2
       exit 1 ;;
  esac
else
  echo "[in-container] WARNING: cuobjdump not found -- arch check skipped (see program audit)" >&2
fi
export CUDA_OPS_ARCHS

# ---- C7: install the built wheel (pip show before/after) --------------------
echo "[in-container] pip show lmcache BEFORE install:"
pip show lmcache 2>/dev/null | head -3 || echo "(not installed)"
pip install --no-deps --force-reinstall "$WHEEL_FILE" || {
  echo "BUILD REFUSED: cannot install the built wheel." >&2; exit 1; }
echo "[in-container] pip show lmcache AFTER install:"
pip show lmcache 2>/dev/null | head -3

# ---- C8: import-check from site-packages (python3 -P) -----------------------
python3 -P - <<'PY' || exit 1
import sys
import lmcache
import lmcache.cuda_ops
import lmcache.lmcache_native
from lmcache.integration.vllm import lmcache_mp_connector
from lmcache.v1.multiprocess.modules import lmcache_driven_transfer
for mod in (lmcache, sys.modules["lmcache.cuda_ops"], sys.modules["lmcache.lmcache_native"]):
    f = getattr(mod, "__file__", "")
    if "site-packages" not in f:
        print(f"BUILD REFUSED: {mod.__name__} imported from {f}, not site-packages",
              file=sys.stderr)
        raise SystemExit(1)
print("lmcache.__file__ =", lmcache.__file__)
print("lmcache.cuda_ops.__file__ =", sys.modules["lmcache.cuda_ops"].__file__)
print("lmcache.lmcache_native.__file__ =", sys.modules["lmcache.lmcache_native"].__file__)
print("[in-container] import-check OK (all native modules from site-packages)")
PY

# ---- C9-C13: tests (CPU gate + optional GPU step) ---------------------------
if [ "$KVP_BUILD_SKIP_TESTS" = "0" ]; then
  # C9: minimal test deps -- pytest only, constrained to the engine pins.
  echo "[in-container] installing minimal test deps (pytest only, constrained)"
  if ! pip install --no-cache-dir -q -c "$KVP_WORK/build-constraints.txt" "pytest>=7.0,<9.1"; then
    echo "BUILD REFUSED: test dependencies cannot be installed (see pip output above)." >&2
    exit 1
  fi

  # C10: staging -- tests + pytest.ini into testroot, NO lmcache/ directory
  # (the source package must NOT shadow the installed wheel).
  TESTROOT="$KVP_WORK/testroot"
  rm -rf "$TESTROOT"
  mkdir -p "$TESTROOT"
  cp -r "$LMC_SRC/tests" "$LMC_SRC/pytest.ini" "$TESTROOT/"
  [ -f "$LMC_SRC/pyproject.toml" ] && cp "$LMC_SRC/pyproject.toml" "$TESTROOT/"
  if [ -d "$TESTROOT/lmcache" ]; then
    echo "BUILD REFUSED: staging must NOT contain an lmcache/ directory" >&2; exit 1
  fi
  echo "[in-container] test staging: $TESTROOT (no lmcache/ dir)"

  # C11: run the five CPU gate files with the real plugin + -P.
  echo "[in-container] running CPU gate: 5 regression files (skipped==0)"
  set +e
  (cd "$TESTROOT" && PYTHONPATH="$KVP_SCRIPTS" python3 -P -m pytest -p kvp_assert_installed -q -rs \
    --junitxml="$KVP_WORK/pytest-report.xml" \
    tests/v1/test_lmcache_mp_connector.py \
    tests/v1/test_vllm_mp_adapter.py \
    tests/v1/multiprocess/test_lmcache_driven_transfer_skip.py \
    tests/v1/multiprocess/test_native_state_lookup.py \
    tests/v1/multiprocess/test_l1_owner_completion.py \
    > "$KVP_WORK/pytest-report.txt" 2>&1)
  rc=$?
  set -e
  cat "$KVP_WORK/pytest-report.txt"

  # C12: summary gate -- rc==0, summary found, skipped==0, passed>0.
  if [ "$rc" != "0" ]; then
    echo "REGRESSION FAILURE: pytest exited rc=$rc -- aborting build" >&2
    exit 1
  fi
  _summary="$(grep -E '^[0-9]+ (passed|failed|error)' "$KVP_WORK/pytest-report.txt" | tail -1 || true)"
  if [ -z "$_summary" ]; then
    echo "REGRESSION FAILURE: pytest summary not found -- suite died before completion" >&2
    exit 1
  fi
  _skipped="$(python3 -c 'import re,sys; m=re.search(r"(\d+) skipped", sys.argv[1]); print(m.group(1) if m else 0)' "$_summary")"
  _passed="$(python3 -c 'import re,sys; m=re.search(r"(\d+) passed", sys.argv[1]); print(m.group(1) if m else 0)' "$_summary")"
  if [ "$_skipped" != "0" ] || [ "$_passed" = "0" ]; then
    echo "REGRESSION FAILURE: gate did not run cleanly (passed=$_passed, skipped=$_skipped)" >&2
    echo "  The five CPU files must run 0 skipped, >0 passed. SKIPPED rows:" >&2
    grep '^SKIPPED \[' "$KVP_WORK/pytest-report.txt" >&2 || true
    exit 1
  fi
  echo "pytest rc=$rc -- CPU gate PASSED ($_summary)"

  # C13: GPU step (r9 behaviour: torch.cuda check, skip!=0 refuse, etc.)
  GPU_REGRESSION_RUN=false
  export GPU_REGRESSION_RUN=false
  if [ "${KVP_BUILD_GPU_TESTS:-0}" = "1" ]; then
    echo "[in-container] GPU regression step: test_cache_server.py (needs --gpus all)"
    if ! python3 -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)'; then
      echo "REGRESSION FAILURE: KVP_BUILD_GPU_TESTS=1 but torch.cuda.is_available() is False" >&2
      exit 1
    fi
    set +e
    (cd "$TESTROOT" && PYTHONPATH="$KVP_SCRIPTS" python3 -P -m pytest -p kvp_assert_installed -q -rs \
      --junitxml="$KVP_WORK/pytest-gpu-report.xml" \
      tests/v1/multiprocess/test_cache_server.py \
      > "$KVP_WORK/pytest-gpu-report.txt" 2>&1)
    _gpu_rc=$?
    set -e
    cat "$KVP_WORK/pytest-gpu-report.txt"
    if [ "$_gpu_rc" != "0" ]; then
      echo "REGRESSION FAILURE: GPU regression pytest exited rc=$_gpu_rc" >&2
      exit 1
    fi
    _gpu_summary="$(grep -E '^[0-9]+ (passed|failed|error|skipped)' "$KVP_WORK/pytest-gpu-report.txt" | tail -1 || true)"
    _gpu_skipped="$(python3 -c 'import re,sys; m=re.search(r"(\d+) skipped", sys.argv[1]); print(m.group(1) if m else 0)' "${_gpu_summary:-}")"
    _gpu_passed="$(python3 -c 'import re,sys; m=re.search(r"(\d+) passed", sys.argv[1]); print(m.group(1) if m else 0)' "${_gpu_summary:-}")"
    if [ "$_gpu_skipped" != "0" ] || [ "$_gpu_passed" = "0" ]; then
      echo "REGRESSION FAILURE: GPU regression did NOT run (passed=$_gpu_passed, skipped=$_gpu_skipped)" >&2
      grep '^SKIPPED \[' "$KVP_WORK/pytest-gpu-report.txt" >&2 || true
      exit 1
    fi
    GPU_REGRESSION_RUN=true
    export GPU_REGRESSION_RUN=true
    echo "[in-container] GPU regression PASSED ($_gpu_passed passed, 0 skipped)"
  else
    export GPU_REGRESSION_RUN=false
    echo "[in-container] GPU regression step SKIPPED (set KVP_BUILD_GPU_TESTS=1)"
  fi
else
  export GPU_REGRESSION_RUN=false
  echo "[in-container] KVP_BUILD_SKIP_TESTS=1 -- tests skipped (not production-acceptable)"
fi

# ---- C14: engine identity (pip freeze diff) ---------------------------------
# r12 P2: runs AFTER C9-C13 (the pytest install must be covered by the diff)
# and also when KVP_BUILD_SKIP_TESTS=1 -- the engine stack must not change
# during ANY step of the build.
pip freeze > "$KVP_WORK/image-freeze.after.txt" 2>/dev/null || true
python3 - "$KVP_WORK/image-freeze.before.txt" "$KVP_WORK/image-freeze.after.txt" <<'PY' || exit 1
import re, sys
def engine_pins(path):
    out = {}
    for ln in open(path):
        m = re.match(r"^(torch|vllm|numpy|triton)([A-Za-z0-9_.-]*)==(\S+)", ln.strip())
        if m:
            out[m.group(1) + m.group(2)] = m.group(3)
    return out
before = engine_pins(sys.argv[1])
after = engine_pins(sys.argv[2])
changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
if changed:
    print("BUILD REFUSED: engine packages changed during the build:", file=sys.stderr)
    for k in sorted(changed):
        print(f"  {k}: {before.get(k, '(absent)')} -> {after.get(k, '(absent)')}", file=sys.stderr)
    raise SystemExit(1)
print(f"[in-container] engine identity OK ({len(before)} engine pins unchanged)")
PY

# ---- C15: identity.env (exported vars, single-quoted JSON) -------------------
python3 - <<'PY' > "$KVP_WORK/identity.env"
import importlib.metadata as md, os, torch, sys
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
print("GPU_CC=" + ("%d.%d" % torch.cuda.get_device_capability(0) if torch.cuda.is_available() else "n/a"))
print('TORCH_CUDA_ARCH_LIST_USED=' + repr(os.environ.get("TORCH_CUDA_ARCH_LIST", "")))
print('CUDA_OPS_ARCHS=' + repr(os.environ.get("CUDA_OPS_ARCHS", "unknown")))
print('NATIVE_EXTENSIONS_JSON=' + repr(os.environ.get("NATIVE_EXTENSIONS_JSON", "{}")))
print('GPU_REGRESSION_RUN=' + repr(os.environ.get("GPU_REGRESSION_RUN", "false")))
PY
echo "[in-container] identity.env written"
echo "[in-container] build phase complete"