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
# Round 81 (review): stop the stack ONLY if this repo's start-all.sh launched
# it (live/obs_started) -- a foreign docker compose stack on the same host
# (other projects share these dashboards) must not be torn down. Force with
# KVP_STOP_OBS=1.
if [ "${KVP_STOP_OBS:-0}" = "1" ] || [ -f "$HERE/../live/obs_started" ]; then
  if [ -d "$HERE/../observability" ]; then
    (cd "$HERE/../observability" && docker compose down >/dev/null 2>&1) \
      || echo "(observability was not running)"
    rm -f "$HERE/../live/obs_started"
  else
    echo "(observability directory not found -- skipping)"
  fi
else
  echo "observability was NOT started by this repo's start-all.sh -- leaving it alone."
  echo "(force: KVP_STOP_OBS=1 bash scripts/stop-all.sh)"
fi
echo "== done. cache data kept =="