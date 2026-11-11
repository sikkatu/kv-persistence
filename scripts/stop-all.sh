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
# Round 83 (user report): the marker alone was NOT reliable -- when the stack
# was already up (earlier run, different script, or the marker write failed),
# stop-all skipped it AND start-all then skipped it too ("already running"),
# so the stack was never stoppable. Ownership is now PROVEN by the compose
# label on obs_grafana (working_dir == this repo's observability/ dir); the
# marker is only a fallback. KVP_STOP_OBS=1 forces, even a foreign stack.
_obs_ours=0
_obs_running=0
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx obs_grafana; then
  _obs_running=1
  _obs_wd="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' obs_grafana 2>/dev/null || true)"
  if [ -n "$_obs_wd" ] && [ -d "$_obs_wd" ]; then _obs_wd="$(cd "$_obs_wd" && pwd)"; fi
  [ "$_obs_wd" = "$HERE/../observability" ] && _obs_ours=1
fi
[ "$_obs_ours" = "0" ] && [ -f "$HERE/../live/obs_started" ] && _obs_ours=1
if [ "${KVP_STOP_OBS:-0}" = "1" ] && [ "$_obs_running" = "1" ]; then
  _obs_ours=1
  echo "(KVP_STOP_OBS=1: forcing the stop even for a foreign stack)"
fi
if [ "$_obs_ours" = "1" ]; then
  if [ -d "$HERE/../observability" ]; then
    (cd "$HERE/../observability" && docker compose down >/dev/null 2>&1) \
      || echo "(observability was not running)"
    rm -f "$HERE/../live/obs_started"
    echo "observability stopped (volumes grafana_data/prometheus_data/tempo_data kept)"
  else
    echo "(observability directory not found -- stopping containers by name)"
    for _svc in obs_grafana obs_prometheus obs_tempo obs_otel_collector; do
      docker rm -f "$_svc" >/dev/null 2>&1 || true
    done
    rm -f "$HERE/../live/obs_started"
  fi
elif [ "$_obs_running" = "1" ]; then
  echo "observability is running but was NOT started by this repo -- leaving it alone."
  echo "(force: KVP_STOP_OBS=1 bash scripts/stop-all.sh)"
else
  echo "observability is not running -- nothing to stop."
fi
echo "== done. cache data kept =="