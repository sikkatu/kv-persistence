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
# Ownership decision (round 83, corrected after the behavioural stand):
#  - the compose label of ANY running obs_* container decides OURS vs FOREIGN;
#    paths are compared with `pwd -P` on BOTH sides, so a symlinked repo path
#    or a symlinked label path still resolves to the same real directory
#    (the first round-83 fix used logical `pwd`, and a symlink to the repo
#    made our OWN stack look foreign -- scenario F);
#  - a label that PROVES "foreign" ALWAYS overrides a stale live/obs_started
#    marker (scenario G: a stale marker must never stop a foreign stack);
#  - the marker is trusted only when there is NO label to decide (old
#    containers without the compose labels);
#  - KVP_STOP_OBS=1 forces a stop whenever ANY obs_* container is up -- not
#    only when Grafana happens to be alive (scenario E: a lone Prometheus
#    used to print "nothing to stop").
_obs_any=0
for _svc in obs_grafana obs_prometheus obs_tempo obs_otel_collector; do
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$_svc"; then
    _obs_any=1
  fi
done
_obs_owner=none   # ours | foreign | none
if [ "$_obs_any" = "1" ]; then
  _obs_wd=""
  for _svc in obs_grafana obs_prometheus obs_tempo obs_otel_collector; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$_svc"; then
      _obs_wd="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$_svc" 2>/dev/null || true)"
      [ -n "$_obs_wd" ] && break
    fi
  done
  if [ -n "$_obs_wd" ]; then
    [ -e "$_obs_wd" ] && _obs_wd="$(cd "$_obs_wd" 2>/dev/null && pwd -P || echo "$_obs_wd")"
    _obs_dir="$(cd "$HERE/../observability" 2>/dev/null && pwd -P || true)"
    if [ -n "$_obs_dir" ] && [ "$_obs_wd" = "$_obs_dir" ]; then
      _obs_owner=ours
    else
      _obs_owner=foreign
    fi
  fi
fi
_obs_ours=0
if [ "$_obs_any" = "0" ]; then
  echo "observability is not running -- nothing to stop."
elif [ "${KVP_STOP_OBS:-0}" = "1" ]; then
  _obs_ours=1
  echo "(KVP_STOP_OBS=1: forcing the stop even for a foreign stack)"
elif [ "$_obs_owner" = "ours" ]; then
  _obs_ours=1
  echo "(ownership proven by the compose label)"
elif [ "$_obs_owner" = "foreign" ]; then
  echo "observability is running but was NOT started by this repo -- leaving it alone."
  echo "(force: KVP_STOP_OBS=1 bash scripts/stop-all.sh)"
elif [ -f "$HERE/../live/obs_started" ]; then
  _obs_ours=1
  echo "(no compose label found -- trusting live/obs_started)"
else
  echo "observability is running but ownership cannot be proven (no compose label, no marker) -- leaving it alone."
  echo "(force: KVP_STOP_OBS=1 bash scripts/stop-all.sh)"
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
    echo "observability containers stopped (removed)."
  fi
fi
echo "== done. cache data kept =="
