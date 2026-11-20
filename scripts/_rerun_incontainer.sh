#!/usr/bin/env bash
# _rerun_incontainer.sh -- the IN-CONTAINER phase of rerun-lmcache-gate.sh
# (r16 P3): install the ALREADY BUILT wheel, then run C8-C12 of the reference
# pipeline against the freshly patched source tree. No compile here -- the
# tests come from the source tree (staging), the wheel only provides the
# runtime.
#
# The gate LOGIC is shared with _incontainer_build.sh via the module
# kvpersist.gate_common (C8 check-imports, C12 junit-counts) -- never copied.
#
# Environment contract (passed via -e by rerun-lmcache-gate.sh):
#   KVP_LMC_SRC, KVP_SCRIPTS_DIR, KVP_IMPL_DIR, KVP_WORK_DIR, KVP_WHEELS_DIR
set -Eeuo pipefail

LMC_SRC="${KVP_LMC_SRC:-/usr/src/lmcache}"
KVP_WORK="${KVP_WORK_DIR:-/kvp-work}"
KVP_WHEELS="${KVP_WHEELS_DIR:-/kvp-wheels}"
KVP_SCRIPTS="${KVP_SCRIPTS_DIR:-/kvp-scripts}"
KVP_IMPL="${KVP_IMPL_DIR:-/kvp-impl}"

note() { echo "[rerun-incontainer] $*"; }
die()  { echo "REGRESSION FAILURE: $*" >&2; exit 1; }

# r18 G3.3: the container runs as root and used to leave the work dirs
# root-owned (undeletable without sudo on the host). Return ownership to the
# host user on EXIT -- via trap so it runs even when die() aborted a gate.
_kvp_chown_back() {
  _uid="${KVP_HOST_UID:-}"
  _gid="${KVP_HOST_GID:-}"
  [ -n "$_uid" ] && [ -n "$_gid" ] || return 0
  # LMC_SRC is mounted rw (the gate may touch it) and KVP_WORK holds the
  # reports/testroot -- both come back to the host user
  if chown -R "$_uid:$_gid" "$LMC_SRC" "$KVP_WORK" 2>/dev/null; then
    echo "[rerun-incontainer] ownership returned to $_uid:$_gid"
  else
    _rc=$?
    echo "[rerun-incontainer] WARNING: could not return ownership (rc=$_rc): the work dir may stay root-owned" >&2
  fi
}
trap _kvp_chown_back EXIT

# ---------------------------------------------------------------------------
# C7-lite: install the already built wheel
# ---------------------------------------------------------------------------
WHEEL_FILE="$(ls "$KVP_WHEELS"/*.whl 2>/dev/null | head -1 || true)"
[ -n "$WHEEL_FILE" ] || die "no *.whl in $KVP_WHEELS -- nothing to install"
note "installing prebuilt wheel: $WHEEL_FILE"
pip install --no-deps --force-reinstall "$WHEEL_FILE" \
  || die "cannot install the prebuilt wheel"

# ---------------------------------------------------------------------------
# C8: import-check from the installed distribution (shared gate_common)
# ---------------------------------------------------------------------------
PYTHONPATH="$KVP_SCRIPTS:$KVP_IMPL" KVP_LMC_SRC="$LMC_SRC" \
  python3 -P -m kvpersist.gate_common check-imports \
  || die "C8 import-check failed"

# ---------------------------------------------------------------------------
# C9-lite: minimal test deps (pytest only)
# ---------------------------------------------------------------------------
note "installing pytest (minimal test deps)"
pip install --no-cache-dir -q "pytest>=7.0,<9.1" \
  || die "test dependencies cannot be installed"

# ---------------------------------------------------------------------------
# C10: staging -- tests + pytest.ini into testroot, NO lmcache/ directory
# ---------------------------------------------------------------------------
TESTROOT="$KVP_WORK/testroot"
rm -rf "$TESTROOT"
mkdir -p "$TESTROOT"
cp -r "$LMC_SRC/tests" "$LMC_SRC/pytest.ini" "$TESTROOT/"
[ -f "$LMC_SRC/pyproject.toml" ] && cp "$LMC_SRC/pyproject.toml" "$TESTROOT/"
if [ -d "$TESTROOT/lmcache" ]; then
  die "staging must NOT contain an lmcache/ directory"
fi
note "test staging: $TESTROOT (no lmcache/ dir)"

# ---------------------------------------------------------------------------
# C11: run the five CPU gate files with the real plugin + -P
# ---------------------------------------------------------------------------
note "running CPU gate: 5 regression files (skipped==0)"
set +e
(cd "$TESTROOT" && PYTHONPATH="$KVP_SCRIPTS" KVP_LMC_SRC="$LMC_SRC" python3 -P -m pytest -p kvp_assert_installed -q -rs \
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

# ---------------------------------------------------------------------------
# C12: summary gate -- the SHARED junit counting (gate_common), same gate
# logic as _incontainer_build.sh -- never a copy
# ---------------------------------------------------------------------------
if [ "$rc" != "0" ]; then
  die "pytest exited rc=$rc -- aborting gate rerun"
fi
_counts="$(PYTHONPATH="$KVP_IMPL" python3 -m kvpersist.gate_common junit-counts "$KVP_WORK/pytest-report.xml")"
case "$_counts" in
  __ERROR__*)
    die "junit report missing/unparsable ($KVP_WORK/pytest-report.xml): ${_counts#__ERROR__}" ;;
esac
set -- $_counts
_tests="$1" _failures="$2" _errors="$3" _skipped="$4" _passed="$5"
if [ "$_failures" != "0" ] || [ "$_errors" != "0" ]; then
  die "gate reported failures (failures=$_failures, errors=$_errors)"
fi
if [ "$_skipped" != "0" ] || [ "$_passed" = "0" ]; then
  echo "REGRESSION FAILURE: gate did not run cleanly (passed=$_passed, skipped=$_skipped)" >&2
  echo "  The five CPU files must run 0 skipped, >0 passed. SKIPPED rows:" >&2
  grep '^SKIPPED \[' "$KVP_WORK/pytest-report.txt" >&2 || true
  exit 1
fi
echo "CPU gate PASSED ($_passed passed, $_failures failed, $_errors errors, $_skipped skipped)"
note "gate rerun complete (C8-C12)"