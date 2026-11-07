#!/usr/bin/env bash
# stop-all.sh -- stop the whole stack (pair of start-all.sh, round 56).
# Stops the engine, then the cache service. The observability stack is left
# running (harmless, ~100 MB); stop it with:
#   cd observability && docker compose down
# Persistent cache data is NEVER touched.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "== stopping the vLLM engine =="
bash "$HERE/stop-engine.sh" || echo "(engine was not running)"
echo "== stopping the cache service =="
bash "$HERE/stop-cache.sh" || echo "(cache service was not running)"
echo "== done. observability still runs (docker ps | grep obs_); cache data kept =="