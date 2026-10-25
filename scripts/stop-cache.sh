#!/usr/bin/env bash
# stop-cache.sh — stop the lmcache server WITH the documented procedure.
# persist_enabled=true keeps the data files on disk; nothing is deleted here.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=/dev/null
. "$ROOT/configs/generic/cache.env"
[ -f "$ROOT/configs/generic/cache.env.local" ] && . "$ROOT/configs/generic/cache.env.local"

PIDF="$KVP_CACHE_ROOT/lmcache-server.pid"
if [ ! -f "$PIDF" ]; then
  echo "no pid file at $PIDF — nothing to stop (or server started differently)"
  exit 0
fi
PID="$(cat "$PIDF")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "lmcache server pid $PID already exited; cleaning pid file"
  rm -f "$PIDF"; exit 0
fi
echo "sending SIGTERM to lmcache server pid $PID (graceful shutdown, data files are KEPT)"
kill "$PID"
for _ in $(seq 1 30); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
if kill -0 "$PID" 2>/dev/null; then
  echo "pid $PID did not exit after 30s — sending SIGKILL (data files are still kept)" >&2
  kill -9 "$PID" || true
fi
rm -f "$PIDF"
echo "cache service stopped. persistent data preserved under $KVP_CACHE_ROOT"
echo "verify with: bash scripts/budget-report.sh   (usage must be unchanged)"