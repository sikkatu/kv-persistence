#!/usr/bin/env bash
# stop-all.sh -- stop the WHOLE stack (pair of start-all.sh, round 56).
# Round 80 review: start-all.sh brings up observability as part of the stack,
# so the pair must stop it too (the old comment said "left running" -- that
# contradicted the one-command lifecycle). Persistent cache data is NEVER
# touched.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "== stopping the vLLM engine =="
bash "$HERE/stop-engine.sh" || echo "(engine was not running)"
echo "== stopping the cache service =="
bash "$HERE/stop-cache.sh" || echo "(cache service was not running)"
echo "== stopping the observability stack =="
if [ -d "$HERE/../observability" ]; then
  (cd "$HERE/../observability" && docker compose down >/dev/null 2>&1) \
    || echo "(observability was not running)"
else
  echo "(observability directory not found -- skipping)"
fi
echo "== done. cache data kept =="