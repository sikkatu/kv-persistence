#!/usr/bin/env bash
# budget-report.sh — print current persistent-cache usage against the budget.
# Uses the kvpersist admin CLI; pure reporting, never modifies anything.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/env.sh"
_kvp_load_env "$ROOT"

export KVP_CACHE_ROOT="${KVP_CACHE_ROOT:?}"
export KVP_CACHE_BUDGET_GB="${KVP_CACHE_BUDGET_GB:?}"
export PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m kvpersist.cli "$@"