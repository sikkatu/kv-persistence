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
if [ "$READY" = 1 ]; then
  echo "=== observability stack is UP (after $(( $(date +%s) - START_TS ))s) ==="
else
  echo "WARNING: Grafana did not answer within 180s -- check: docker logs obs_grafana" >&2
  docker ps -a --filter name=obs_ --format '{{.Names}} {{.State}} {{.Status}}' 2>/dev/null || true
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