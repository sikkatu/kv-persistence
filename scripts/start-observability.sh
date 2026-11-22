#!/usr/bin/env bash
# start-observability.sh -- bring up the full LMCache monitoring stack
# (OTel Collector + Tempo + Prometheus + Grafana with the official LMCache
# dashboards) with ONE command, then print where to look.
#
# Usage: bash scripts/start-observability.sh [--down]
#   (no args)  -> docker compose up -d + summary
#   --down     -> docker compose down (data volumes are kept)
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OBS_DIR="$ROOT/observability"
[ -f "$OBS_DIR/docker-compose.yml" ] || die "observability/docker-compose.yml not found at $OBS_DIR"

command -v docker >/dev/null || die "docker is required for the observability stack"
docker compose version >/dev/null 2>&1 \
  || die "docker compose v2 is required (the 'docker-compose' v1 standalone is not supported here)"

if [ "${1:-}" = "--down" ]; then
  echo "-- stopping the observability stack (data volumes kept) --"
  ( cd "$OBS_DIR" && docker compose down )
  echo "stopped. Start it again with: bash scripts/start-observability.sh"
  exit 0
fi

echo "-- starting the observability stack (OTel Collector, Tempo, Prometheus, Grafana) --"
( cd "$OBS_DIR" && docker compose up -d )

echo
echo "Waiting for Grafana to answer on :3000 (up to 180s) ..."
START_TS=$(date +%s)
READY=0
LAST_LINE=""
for _ in $(seq 1 90); do
  if curl -fsS --max-time 3 -o /dev/null http://127.0.0.1:3000/api/health 2>/dev/null; then
    READY=1
    break
  fi
  ELAPSED=$(( $(date +%s) - START_TS ))
  # Review fix (round 40): print CHANGING progress only -- an identical line
  # every 2s read as a hang (user output rule: real progress, no repeats).
  # Every 10s show the stack's container states so the operator sees life.
  if [ $(( ELAPSED % 10 )) -lt 2 ]; then
    STATES="$(docker ps -a --filter name=obs_ --format '{{.Names}}={{.State}}' 2>/dev/null | paste -sd, -)"
    LINE="[$ELAPSED s] grafana starting (containers: ${STATES:-none})"
    if [ "$LINE" != "$LAST_LINE" ]; then
      printf '%s\n' "$LINE"
      LAST_LINE="$LINE"
    fi
  fi
  sleep 2
done
echo
if [ "$READY" = "1" ]; then
  echo "=== observability stack is UP (after $(( $(date +%s) - START_TS ))s) ==="
else
  echo "WARNING: Grafana did not answer within 180s -- check: docker logs obs_grafana" >&2
  docker ps -a --filter name=obs_ --format '{{.Names}} {{.State}} {{.Status}}' 2>/dev/null || true
fi
# r89d: Prometheus liveness is checked EXPLICITLY -- the scrape.d shape bug
# (r89d) left obs_prometheus crash-looping while Grafana alone answered fine,
# so the stack looked healthy and "no data" was the only symptom. Report the
# container state DIRECTLY when Prometheus is not ready.
if ! curl -fsS --max-time 3 -o /dev/null http://127.0.0.1:9091/-/ready 2>/dev/null; then
  echo "WARNING: Prometheus is NOT answering on :9091 -- dashboards will show 'no data'. Check: docker ps -a --filter name=obs_prometheus; docker logs --tail 15 obs_prometheus" >&2
  docker ps -a --filter name=obs_prometheus --format '{{.Names}} {{.State}} {{.Status}}' 2>/dev/null || true
  echo "  A founding cause before r89d: scrape.d files without a top-level 'scrape_configs:' key -- run: bash observability/extras/check.sh --static" >&2
fi
echo
echo "  Grafana     : http://localhost:3000   (anonymous Admin, no login)"
echo "                Dashboards -> 'LMCache' and 'LMCache Coordinator'"
echo "                (imported automatically from observability/grafana/provisioning/)"
echo "  Prometheus  : http://localhost:9091"
echo "  Tempo       : http://localhost:3200"
echo "  OTLP gRPC   : localhost:4320  (lmcache server pushes here via --otlp-endpoint)"
echo
echo "Point the lmcache server at the collector with:"
echo "  --otlp-endpoint http://localhost:4320   (add to scripts/start-cache.sh or /etc/kv-persistence.env)"
echo
echo "Key panels to watch (LMCache dashboard):"
echo "  * cache hit rate          -- hit / miss ratio per request"
echo "  * L1 / L2 usage           -- tier occupancy vs capacity"
echo "  * store / retrieve throughput  -- bytes per second written and read"
echo "  * evictions               -- LRU evictions per tier"
echo "  * restore (prefetch) time -- how fast a restarted server refills L1 from NVMe"

# --- observability EXTRA layer (part B round 87): never blocks the base ---
# Idempotent, opt-out with KVP_OBS_EXTRAS=0. Extra dashboard (kvp-inference),
# vLLM metrics relay, GPU exporters. Best-effort: failures here never change
# the exit status of THIS script (base stack is the deliverable).
# --- extras hook: BEGIN (tests extract and EXECUTE this block) ---
if [ "${KVP_OBS_EXTRAS:-1}" != "0" ]; then
  bash "$HERE/extras-up.sh" || echo "[extras] extras-up.sh returned non-zero -- base stack is unaffected (see above)" >&2
  # L4 (r89): the old `_obs_extras_started=1` was set in THIS child shell and
  # never reached start-all.sh (not exported, script not sourced) -- the
  # "already deployed" branch there was dead code and extras ran TWICE (extra
  # compose up + extra HUP). A marker FILE is the only channel that crosses
  # process boundaries: record it here, start-all.sh reads and clears it.
  # Only when extras ACTUALLY ran: with KVP_OBS_EXTRAS=0 there is nothing to
  # skip later, and a stale marker would mute the NEXT start-all's extras run
  # (e.g. right after switching KVP_OBS_GPU_PROF -- the change would silently
  # not apply).
  mkdir -p "$ROOT/live" 2>/dev/null || true
  printf '1\n' > "$ROOT/live/extras_started" 2>/dev/null || true
  # r90 B1: verify the SCRAPE path Prometheus will use, from INSIDE the
  # container (host curl proves nothing: round 90 host->relay = 200 / 0.03s
  # while container->relay timed out under ufw). A blocked leg here is a
  # HOST FIREWALL problem -- print the exact ufw command with the REAL
  # compose subnet. Never modify the firewall from here.
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx obs_prometheus; then
    _RELAY_PORT="${KVP_OBS_RELAY_PORT:-30008}"
    _GW="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo 172.17.0.1)"
    if ! docker exec obs_prometheus wget -q -T 4 -O /dev/null "http://$_GW:$_RELAY_PORT/metrics" >/dev/null 2>&1 \
       && ! docker exec obs_prometheus curl -fsS --max-time 4 -o /dev/null "http://$_GW:$_RELAY_PORT/metrics" >/dev/null 2>&1; then
      _SUBNET="$(docker network inspect observability_default --format '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || echo '172.16.0.0/12')"
      echo "WARNING: the HOST FIREWALL blocks compose-subnet traffic to the relay ($_GW:$_RELAY_PORT); Prometheus cannot scrape vllm:* dashboards will stay empty for the vllm row. Fix:" >&2
      echo "  sudo ufw allow from ${_SUBNET} to $_GW port $_RELAY_PORT proto tcp" >&2
      echo "  (wider alternative: sudo ufw allow from 172.16.0.0/12 to $_GW port $_RELAY_PORT proto tcp) -- the exact rule shape you already used on this host." >&2
    fi
  fi
fi
# --- extras hook: END ---
# r28 D1: the network the monitoring stack needs appears only HERE -- if the
# cache server was started before observability it cannot reach the collector
# (it is not in this network). One line, no automatic restart.
if [ "$(docker inspect -f '{{.State.Running}}' obs_otel_collector 2>/dev/null)" = "true" ]; then
  _srv_net="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' obs_otel_collector 2>/dev/null | head -n 1 | tr -d '[:space:]')"
  _cache_net="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "${KVP_CACHE_CONTAINER:-lmcache-kvp-server}" 2>/dev/null | head -n 1 | tr -d '[:space:]')"
  if [ -n "$_cache_net" ] && [ "$_cache_net" != "$_srv_net" ]; then
    echo "NOTE: the cache server was started before observability; restart it (bash scripts/stop-cache.sh && bash scripts/start-all.sh) so its metrics reach Grafana"
  fi
fi
