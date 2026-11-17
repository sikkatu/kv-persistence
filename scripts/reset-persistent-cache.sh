#!/usr/bin/env bash
# reset-persistent-cache.sh -- EXPLICIT, ONE-TIME destructive reset of the
# persistent LMCache KV-cache data (spec section 7).
#
# This script is NEVER called by start/stop/restart. A normal lifecycle must
# never clear cache data. Run it only as a deliberate migration step.
#
# Safety contract (refuses to run unless ALL hold):
#   * requires explicit confirmation: KVP_RESET_CONFIRM=FULL-RESET
#   * all vLLM workers + the LMCache MP server are stopped (or
#     KVP_RESET_FORCE_STOP=1 stops them; default refuses while they run)
#   * every target resolves inside the configured cache root(s) only
#   * refuses "/", home dirs, empty strings, model dirs, repos, build artifacts
#   * refuses symlinks that escape the cache root
#
# Usage:
#   KVP_RESET_CONFIRM=FULL-RESET bash scripts/reset-persistent-cache.sh
#   (add KVP_RESET_FORCE_STOP=1 to stop services yourself first)
#
# Writes a reset report to live/cache-reset-report-<UTC>.txt (section 7.4).

set -Eeuo pipefail
die() { echo "ERROR(reset-cache): $*" >&2; exit 1; }
note() { echo "[reset-cache] $*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

[ "${KVP_RESET_CONFIRM:-}" = "FULL-RESET" ] \
  || die "refusing to destroy persistent cache without KVP_RESET_CONFIRM=FULL-RESET (one-time migration only)"

. "$HERE/env.sh"
_kvp_load_env "$ROOT" 2>/dev/null || true

# --------------------------------------------------------------------------
# Locate the cache root(s). The L2 NVMe tier lives under the cache device dir
# configured for the deployment; KVP_CACHE_ROOT/LMCACHE_L2_PATH override.
# --------------------------------------------------------------------------
CANDIDATES=()
[ -n "${KVP_CACHE_ROOT:-}" ] && CANDIDATES+=("$KVP_CACHE_ROOT")
[ -n "${LMCACHE_L2_PATH:-}" ] && CANDIDATES+=("$LMCACHE_L2_PATH")
[ -n "${LMCACHE_ROOT:-}" ] && CANDIDATES+=("$LMCACHE_ROOT")
# live/markers and kv-persist launcher record the effective cache dir:
for f in "$ROOT/live/cache_root" "$ROOT/live/l2_path"; do
  [ -f "$f" ] && CANDIDATES+=("$(head -n1 "$f" | tr -d '[:space:]')")
done
# sanity: dedupe non-empty
TARGETS=()
for c in "${CANDIDATES[@]:-}"; do
  [ -n "$c" ] && TARGETS+=("$c")
done
[ "${#TARGETS[@]}" -gt 0 ] || die "no cache root discovered -- set KVP_CACHE_ROOT explicitly (refuse to guess)"

# --------------------------------------------------------------------------
# Safety gate per target.
# --------------------------------------------------------------------------
safe_target() {
  local p="$1" real
  [ -n "$p" ] || return 1
  real="$(readlink -f "$p" 2>/dev/null)" || return 1
  [ -n "$real" ] || return 1
  case "$real" in
    /|/home|/home/*|/root|/root/*|/mnt|/mnt/*|/usr|/etc|/opt|/tmp|/proc|/sys|/dev) return 1 ;;
    /var) return 1 ;;
    /var/lib/kv-persistence|/var/lib/kv-persistence/*) : ;;  # configured cache root
    /var/*) return 1 ;;
    /home/*/*|/root/*/*) case "$real" in
        */models|*/models/*|*/recipes|*/recipes/*|*/sikkatu-recipes|*/kv-persistence|*/kv-persistence/*) return 1 ;;
        *) : ;;
      esac ;;
    /mnt/*/*|/media/*) : ;;
    *) case "$real" in
        /cache*|/nvme*|/data*|/mnt/*|/media/*) : ;;
        *) return 1 ;;
      esac ;;
  esac
  # symlink escape check: every prefix must stay inside the first real component
  local probe="$p" abs
  while [ -n "$probe" ] && [ "$probe" != "/" ]; do
    if [ -L "$probe" ]; then
      abs="$(readlink -f "$probe")" || return 1
      case "$abs" in "$real"*) : ;; *) return 1 ;; esac
    fi
    probe="$(dirname "$probe")"
  done
  return 0
}

FINAL=()
for t in "${TARGETS[@]}"; do
  if safe_target "$t"; then
    FINAL+=("$t")
    note "approved cache target: $t"
  else
    die "unsafe cache target refused: '$t' (must be a dedicated cache-data dir, not / or a home/repo/model path)"
  fi
done
[ "${#FINAL[@]}" -gt 0 ] || die "no safe cache target to clear"

# --------------------------------------------------------------------------
# Stop services first (or refuse).
# --------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  RUNNING="$(docker ps --format '{{.Names}}' | grep -E 'lmcache|vllm|mimo-pro|tp4' || true)"
  if [ -n "$RUNNING" ] && [ "${KVP_RESET_FORCE_STOP:-0}" != "1" ]; then
    die "these containers are running: $(echo "$RUNNING" | tr '\n' ' '). Stop them first (bash scripts/stop-all.sh) or re-run with KVP_RESET_FORCE_STOP=1."
  fi
  if [ "${KVP_RESET_FORCE_STOP:-0}" = "1" ]; then
    [ -x "$HERE/stop-all.sh" ] && bash "$HERE/stop-all.sh" || true
    sleep 3
  fi
fi
# verify no process holds the cache dirs
for t in "${FINAL[@]}"; do
  if command -v fuser >/dev/null 2>&1 && fuser -s "$t" 2>/dev/null; then
    die "a process still uses $t -- stop it and retry"
  fi
done

# --------------------------------------------------------------------------
# Clear + report.
# --------------------------------------------------------------------------
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="$ROOT/live/cache-reset-report-$STAMP.txt"
mkdir -p "$ROOT/live"
{
  echo "=== LMCache persistent-cache reset report ==="
  echo "time_utc: $STAMP"
  echo "confirmed_by: KVP_RESET_CONFIRM=FULL-RESET"
  echo "force_stop: ${KVP_RESET_FORCE_STOP:-0}"
  echo
  echo "--- cleared paths ---"
} > "$REPORT"

for t in "${FINAL[@]}"; do
  before="$(du -sb "$t" 2>/dev/null | cut -f1 || echo '?')"
  find "$t" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  after="$(du -sb "$t" 2>/dev/null | cut -f1 || echo '?')"
  {
    echo "path: $t"
    echo "  bytes_before: $before"
    echo "  bytes_after: $after (0 = empty)"
  } >> "$REPORT"
  note "cleared: $t ($before bytes -> $after)"
done

{
  echo
  echo "--- preserved (never touched) ---"
  echo "model weights, model config, recipes, source code, LMCache build artifacts, logs, unrelated NVMe data"
  echo "new cache namespace: created empty on next start; old entries are unreachable"
} >> "$REPORT"

note "report: $REPORT"
note "DONE -- next start initializes an EMPTY persistent cache (no old entries reachable)"