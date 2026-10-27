#!/usr/bin/env bash
# budget-report.sh -- print current persistent-cache usage against the budget.
# Uses the kvpersist admin CLI; pure reporting, never modifies anything.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/env.sh"
_kvp_load_env "$ROOT"
export KVP_CACHE_ROOT="${KVP_CACHE_ROOT:?}"
export KVP_CACHE_BUDGET_GB="${KVP_CACHE_BUDGET_GB:?}"
export PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}"
# Physical free-space check (review 2B): report but do not replace the logical
# budget; the admin CLI is the single source for used_bytes/evictions.
if command -v stat >/dev/null; then
  AVAIL_KB=$(stat -f -c %a "$KVP_CACHE_ROOT" 2>/dev/null || echo -1)
  BLOCK_KB=$(stat -f -c %S "$KVP_CACHE_ROOT" 2>/dev/null || echo 0)
  if [ "$AVAIL_KB" != "-1" ]; then
    AVAIL_GB=$((AVAIL_KB * BLOCK_KB / 1024 / 1024 / 1024))
    echo "physical_free_gb: $AVAIL_GB"
    if [ "$AVAIL_GB" -lt 5 ]; then
      echo "WARNING: physical free space on cache root is below 5 GB -- emergency eviction\nshould run (see docs/storage-management.md 'Physical free-space emergency eviction')." >&2
    fi
  fi
fi
exec python3 -m kvpersist.cli "$@"
