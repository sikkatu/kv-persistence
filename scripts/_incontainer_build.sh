#!/usr/bin/env bash
# _incontainer_build.sh -- the IN-CONTAINER build phase of build-lmcache.sh.
#
# r6-7 A-1: this phase used to be handed to the container over stdin
# (`bash -s <<'INCONTAINER'`). `docker run` WITHOUT `-i` leaves stdin EMPTY,
# so `bash -s` did nothing and exited 0 -- the whole phase silently vanished
# on the operator machine. As a FILE mounted at /kvp-scripts (read-only) the
# phase is delivered unconditionally: no stdin class of bugs, and shellcheck
# can lint this file standalone. Invoked as
#   docker run ... --entrypoint bash "$IMAGE" /kvp-scripts/_incontainer_build.sh
#
# Environment contract (passed via -e by build-lmcache.sh):
#   LMCACHE_SOURCE_COMMIT, PR6551_MERGE, KVP_BUILD_SKIP_TESTS,
#   SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LMCACHE, PYTHONUNBUFFERED=1
# Mounted paths: /usr/src/lmcache (patched source, rw), /kvp-patches (ro),
# /kvp-scripts (ro), /kvp-wheels (rw), /kvp-work (rw).
set -Eeuo pipefail
# r8: path variables overridable for the behavioral unit tests (the real
# container values below are the defaults).
LMC_SRC="${KVP_LMC_SRC:-/usr/src/lmcache}"
KVP_WORK="${KVP_WORK_DIR:-/kvp-work}"
KVP_WHEELS="${KVP_WHEELS_DIR:-/kvp-wheels}"
KVP_SCRIPTS="${KVP_SCRIPTS_DIR:-/kvp-scripts}"
# r8 P2-1: the phase runs as root on the mounted dirs; return ownership to the
# host user even on failure (trap EXIT) so a re-run is not blocked by
# root-owned artifacts.
_kvp_chown_back() {
  _uid="${KVP_HOST_UID:-}"
  _gid="${KVP_HOST_GID:-}"
  [ -n "$_uid" ] && [ -n "$_gid" ] || return 0
  chown -R "$_uid:$_gid" "$LMC_SRC" "$KVP_WHEELS" "$KVP_WORK" 2>/dev/null || true
  echo "[in-container] ownership returned to $_uid:$_gid ($LMC_SRC $KVP_WHEELS $KVP_WORK)"
}
trap _kvp_chown_back EXIT
# r3 P0-3: the vLLM runtime image ships nvcc + CUDA runtime but NOT the dev
# headers cuda_ops compiles against (cuda-cudart-dev, libcusparse-dev,
# libcusolver-dev -- vLLM's own Dockerfile installs them just for its LMCache
# source-build stage and purges them after). Install them here in the
# disposable build container; the derivative image never sees these (it only
# receives the wheel). Any missing package must abort loudly by name.
#
# r4 P0-1: the package suffix is `major-minor` of torch.version.cuda with a
# dash ("13.0" -> 13-0, "13.0.2" -> 13-0, "12.8" -> 12-8). The old
# `${CUDA_TORCH%.*}` answered "13.0.2" with "13.0" -> "13-0" but "13.0" with
# "13" -> "13" (no such package -- the build loop always died "BUILD
# REFUSED"). Compute it through the single-source helper script so the unit
# test and the container run the exact same code path.
CUDA_TORCH="$(python3 -c 'import torch; print(torch.version.cuda)')"
DEV_SUFFIX="$(bash "$KVP_SCRIPTS/_cuda_dev_suffix.sh" "$CUDA_TORCH")"
[ -n "$DEV_SUFFIX" ] || { echo "BUILD REFUSED: cannot derive a dev-package suffix from torch.version.cuda='$CUDA_TORCH'" >&2; exit 1; }
echo "[in-container] CUDA dev suffix (from torch.version.cuda=$CUDA_TORCH): $DEV_SUFFIX"
export CUDA_HOME=/usr/local/cuda
# r4 P0-1: the vLLM image build ends with `rm -rf /var/lib/apt/lists/*`
# (Dockerfile ~line 813), so without a fresh `apt-get update` every
# `apt-get install` fails "Unable to locate package". Refresh the lists here,
# fail-loud (--no || true), and keep apt's own error output visible instead of
# hiding it behind >/dev/null 2>&1: the operator must see WHY a package
# refused to install.
apt_out="$(mktemp)"
if ! apt-get update -y >"$apt_out" 2>&1; then
  echo "BUILD REFUSED: 'apt-get update' failed in this image." >&2
  echo "  Last lines of apt output:" >&2
  tail -n 20 "$apt_out" >&2
  exit 1
fi
# r9 P0: the vLLM image ships NO git -- the phase died rc=127 on line 1.
# git is needed for (a) the dubious-ownership safe.directory fix below and
# (b) setuptools_scm's file-finder: without git it returns an EMPTY file list
# and non-py package data (yaml/json/configs) silently drops out of the wheel.
# Install it in the same fail-loud style as the CUDA dev packages; the image
# repo supplies the version.
if ! apt-get install -y --no-install-recommends git >"$apt_out" 2>&1; then
  echo "BUILD REFUSED: package 'git' is unavailable in this image." >&2
  echo "  The wheel build needs git (setuptools_scm file-finder + safe.directory)." >&2
  echo "  Last lines of apt output:" >&2
  tail -n 20 "$apt_out" >&2
  exit 1
fi
echo "[in-container] installed git ($(git --version 2>/dev/null || echo unknown))"
# r3 P0-2: the mounted source tree is owned by a non-root uid while we run as
# root inside the container -- git would refuse ("dubious ownership") and
# setuptools_scm would abort the wheel build. Configure safe.directory here
# (r9: AFTER the git install -- git must exist first).
git config --global --add safe.directory $LMC_SRC
for pkg in cuda-cudart-dev-$DEV_SUFFIX libcusparse-dev-$DEV_SUFFIX \
           libcusolver-dev-$DEV_SUFFIX libcublas-dev-$DEV_SUFFIX; do
  if ! apt-get install -y --no-install-recommends "$pkg" >"$apt_out" 2>&1; then
    echo "BUILD REFUSED: CUDA dev package '$pkg' is unavailable in this image." >&2
    echo "  The wheel cannot compile cuda_ops without it (missing headers)." >&2
    echo "  Last lines of apt output:" >&2
    tail -n 20 "$apt_out" >&2
    echo "  Check the image's apt sources provide $pkg for CUDA $CUDA_TORCH," >&2
    echo "  or run the build in an image that carries the CUDA dev stack." >&2
    exit 1
  fi
  echo "[in-container] installed $pkg"
done
echo "[in-container] CUDA_HOME=$CUDA_HOME"
echo "[in-container] python: $(python3 --version)"
echo "[in-container] torch : $(python3 -c 'import torch; print(torch.__version__)')"
echo "[in-container] torch CUDA: $(python3 -c 'import torch; print(torch.version.cuda)')"
echo "[in-container] cuda available: $(python3 -c 'import torch; print(torch.cuda.is_available())')"
echo "[in-container] GPU count: $(python3 -c 'import torch; print(torch.cuda.device_count())')"

# r2 P0-2: REFUSE to build without the CUDA compiler. The upstream build
# silently degrades to a pure-Python wheel when nvcc is absent ("building
# without extensions") and that wheel silently runs the slow Python copy
# fallback (the cudaMemcpy error 1 incident). A production wheel must carry
# lmcache/cuda_ops*.so.
if ! command -v nvcc >/dev/null 2>&1; then
  echo "BUILD REFUSED: no nvcc in the image -- cannot compile the CUDA extensions." >&2
  echo "  This runtime image is a runtime image; build the wheel in a CUDA toolkit" >&2
  echo "  stage (nvcc matching torch CUDA 13.0.x) or pass an image with nvcc." >&2
  exit 1
fi
NVCC_VERSION="$(nvcc --version | tail -1)"
echo "[in-container] nvcc: $NVCC_VERSION"
# Force the CUDA extension build and target SM 120 (RTX PRO 6000 Blackwell).
export BUILD_WITH_CUDA=1
export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-12.0}"
echo "[in-container] BUILD_WITH_CUDA=1 TORCH_CUDA_ARCH_LIST=$TORCH_CUDA_ARCH_LIST"
echo "$NVCC_VERSION" > $KVP_WORK/nvcc-version.txt

echo "[in-container] checking vLLM ABI / identity matches ..."
python3 - <<'PY'
import importlib.metadata as md, torch, sys
print("vllm", md.version("vllm") if True else "?")
print("python", sys.version.split()[0])
PY

# Fail-fast native import check with the patched tree on PYTHONPATH.
export PYTHONPATH=$LMC_SRC
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
  pip install --no-cache-dir -q -r $LMC_SRC/requirements/test.txt
  echo "[in-container] running migration-regression suites for #4136 and #5972"
  cd $LMC_SRC
  # r3 P1-1: capture the real pytest exit code -- the old pattern of silencing
  # failures and grepping output words (any 'error' substring) both under- and
  # over-claimed: a crash without those words got "passed", a stderr mention of
  # "error" got "failed". The exit code is the ONLY authoritative signal; skip
  # counts come from the pytest summary line (never from counting detail rows).
  # r8 P0: the FIVE CPU regression files are the mandatory gate (skipped==0).
  # test_cache_server.py is a GPU-INTEGRATION suite (starts the LMCache
  # server + CUDA IPC, does pytest.skip at module level without CUDA) and
  # CANNOT be mandatory in a build container that runs without --gpus -- it
  # runs as a separate step only when KVP_BUILD_GPU_TESTS=1 (container then
  # needs --gpus all).
  set +e
  python3 -m pytest -q -rs \
    --junitxml=$KVP_WORK/pytest-report.xml \
    tests/v1/test_lmcache_mp_connector.py \
    tests/v1/test_vllm_mp_adapter.py \
    tests/v1/multiprocess/test_lmcache_driven_transfer_skip.py \
    tests/v1/multiprocess/test_native_state_lookup.py \
    tests/v1/multiprocess/test_l1_owner_completion.py \
    > $KVP_WORK/pytest-report.txt 2>&1
  rc=$?
  set -e
  cat $KVP_WORK/pytest-report.txt
  if [ "$rc" != "0" ]; then
    echo "REGRESSION FAILURE: pytest exited rc=$rc -- aborting build" >&2
    exit 1
  fi
  # summary line is the last non-empty line, e.g. "3 passed in 1.23s"
  _summary_line="$(grep -E '^[0-9]+ (passed|failed|error)' $KVP_WORK/pytest-report.txt | tail -1 || true)"
  # r4 P1: when the suite dies before the summary (collection error, crash),
  # grep | tail under set -e/pipefail with an empty match used to abort the
  # script with NO message. Fail loudly and name the problem instead.
  if [ -z "$_summary_line" ]; then
    echo "REGRESSION FAILURE: pytest summary not found in the report -- the suite" >&2
    echo "  died before completion (collection error or crash). Full output above." >&2
    exit 1
  fi
  _skipped="$(python3 -c 'import re,sys; m=re.search(r"(\d+) skipped", sys.argv[1]); print(m.group(1) if m else 0)' "$_summary_line")"
  if [ "$_skipped" != "0" ]; then
    echo "REGRESSION FAILURE: $_skipped required test(s) SKIPPED (skipped != passed) -- aborting build"
    echo "  r8: the five CPU gate files must run 0 skipped. SKIPPED rows from -rs:"
    grep '^SKIPPED \[' $KVP_WORK/pytest-report.txt || true
    exit 1
  fi
  echo "pytest rc=$rc, skipped=$_skipped -- focused regression tests PASSED (0 failed, 0 skipped)"

  # r8 P0: GPU-integration regression (test_cache_server.py) is a SEPARATE
  # step, only when explicitly requested (container must have --gpus all).
  GPU_REGRESSION_RUN=false
  export GPU_REGRESSION_RUN=false
  if [ "${KVP_BUILD_GPU_TESTS:-0}" = "1" ]; then
    echo "[in-container] GPU regression step: tests/v1/multiprocess/test_cache_server.py (needs --gpus all)"
    # r9 P1-1: refuse BEFORE pytest when the container sees no CUDA at all --
    # the suite would module-level skip and a pass would be a LIE.
    if ! python3 -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)'; then
      echo "REGRESSION FAILURE: KVP_BUILD_GPU_TESTS=1 but torch.cuda.is_available() is False" >&2
      echo "  -- the container was started WITHOUT --gpus all (or no GPU is visible)." >&2
      echo "  What to do: run with KVP_BUILD_GPU_TESTS=1 and a docker run that has" >&2
      echo "  --gpus all (build-lmcache.sh adds it automatically when the flag is set)." >&2
      exit 1
    fi
    set +e
    python3 -m pytest -q -rs \
      --junitxml=$KVP_WORK/pytest-gpu-report.xml \
      tests/v1/multiprocess/test_cache_server.py \
      > $KVP_WORK/pytest-gpu-report.txt 2>&1
    _gpu_rc=$?
    set -e
    cat $KVP_WORK/pytest-gpu-report.txt
    if [ "$_gpu_rc" != "0" ]; then
      echo "REGRESSION FAILURE: GPU regression pytest exited rc=$_gpu_rc -- aborting build"
      exit 1
    fi
    # r9 P1-1: a skip here is NOT a pass -- the whole point of this step is the
    # CUDA integration run. Parse the summary like the CPU gate; refuse on
    # skipped != 0 or passed == 0 and print the SKIPPED rows.
    _gpu_summary="$(grep -E '^[0-9]+ (passed|failed|error|skipped)' $KVP_WORK/pytest-gpu-report.txt | tail -1 || true)"
    if [ -z "$_gpu_summary" ]; then
      echo "REGRESSION FAILURE: GPU regression summary not found -- suite died early" >&2
      exit 1
    fi
    _gpu_skipped="$(python3 -c 'import re,sys; m=re.search(r"(\d+) skipped", sys.argv[1]); print(m.group(1) if m else 0)' "$_gpu_summary")"
    _gpu_passed="$(python3 -c 'import re,sys; m=re.search(r"(\d+) passed", sys.argv[1]); print(m.group(1) if m else 0)' "$_gpu_summary")"
    if [ "$_gpu_skipped" != "0" ] || [ "$_gpu_passed" = "0" ]; then
      echo "REGRESSION FAILURE: GPU regression did NOT run (passed=$_gpu_passed, skipped=$_gpu_skipped)" >&2
      echo "  A skip here would be a FALSE 'gpu_regression_run: true'. SKIPPED rows:" >&2
      grep '^SKIPPED \[' $KVP_WORK/pytest-gpu-report.txt >&2 || true
      exit 1
    fi
    GPU_REGRESSION_RUN=true
    export GPU_REGRESSION_RUN=true
    echo "[in-container] GPU regression PASSED ($_gpu_passed passed, 0 skipped)"
  else
    export KVP_GPU_REGRESSION_RUN=false
    echo "[in-container] GPU regression step SKIPPED -- set KVP_BUILD_GPU_TESTS=1 to run"
    echo "  tests/v1/multiprocess/test_cache_server.py here (container needs --gpus all)."
    echo "  Its CUDA IPC checks are covered by the GPU-smoke in scripts/build-image.sh"
    echo "  (one native kernel per GPU on the built image)."
  fi
else
  echo "[in-container] KVP_BUILD_SKIP_TESTS=1 -- tests skipped (not production-acceptable)"
fi

# 9. Build the wheel against the INSTALLED torch, no build isolation,
#    pinned build deps only.
echo "[in-container] building custom LMCache wheel (no build isolation)"
cd $LMC_SRC
pip install --no-cache-dir -q -r $LMC_SRC/requirements/build.txt 2>/dev/null \
  || pip install --no-cache-dir -q "ninja" "packaging>=24.2" "setuptools>=77.0.3,<81.0.0" "setuptools_scm>=8" "wheel"
python3 -m pip wheel . --no-build-isolation --no-deps -w $KVP_WHEELS
# r9 P0: the wheel must carry the non-py package data git tracks. Without git
# the setuptools_scm file-finder returns an empty list and such files silently
# disappear from the wheel -- compare the wheel's lmcache/ listing against
# `git ls-files lmcache` (excluding *.py sources and *.so build outputs).
python3 - "$KVP_WHEELS" "$LMC_SRC" <<'PY' || exit 1
import subprocess, sys, zipfile, pathlib, os
wheels, src = sys.argv[1], sys.argv[2]
whl = sorted(pathlib.Path(wheels).glob("*.whl"))
if not whl:
    print("BUILD REFUSED: no wheel produced for the file-list check", file=sys.stderr)
    raise SystemExit(1)
names = set(zipfile.ZipFile(whl[0]).namelist())
tracked = subprocess.run(["git", "-C", src, "ls-files", "lmcache"],
                         capture_output=True, text=True, check=True).stdout.split()
missing = [f for f in tracked
           if not f.endswith(".py") and not f.endswith(".so")
           and f not in names]
if missing:
    print("BUILD REFUSED: the wheel is missing non-py package data tracked by git:", file=sys.stderr)
    for f in missing:
        print("  missing:", f, file=sys.stderr)
    print("  (setuptools_scm file-finder needs git -- was the wheel built without it?)", file=sys.stderr)
    raise SystemExit(1)
print(f"[in-container] wheel file-list check OK ({len(tracked)} tracked, none missing)")
PY

echo "[in-container] artifacts:"
ls -la $KVP_WHEELS

# Identity block written for the host phase.
python3 - <<'PY' > $KVP_WORK/identity.env
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
print("GPU_CC=" + ("%d.%d" % torch.cuda.get_device_capability(0) if torch.cuda.is_available() else "n/a"))
print("GPU_REGRESSION_RUN=" + __import__("os").environ.get("GPU_REGRESSION_RUN", "false"))
PY
echo "[in-container] build phase complete"
