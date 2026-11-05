#!/usr/bin/env bash
# stop-cache.sh — stop the lmcache server WITH the documented procedure.
# persist_enabled=true keeps the data files on disk; nothing is deleted here.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/env.sh"
_kvp_load_env "$ROOT"

PIDF="$KVP_CACHE_ROOT/lmcache-server.pid"
if [ ! -f "$PIDF" ]; then
  echo "no pid file at $PIDF — nothing to stop (or server started differently)"
  exit 0
fi
PID="$(cat "$PIDF" | tr -d '[:space:]')"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "lmcache server pid $PID already exited; cleaning pid file"
  rm -f "$PIDF"; exit 0
fi
# Review fix (round 37): verify the process is actually a cache server before
# killing -- a stale PID file must never kill an unrelated process that reused
# the PID. Accept only processes whose cmdline mentions lmcache/kvpersist.
_pid_cmd="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
case "$_pid_cmd" in
  *lmcache*|*kvpersist*) : ;;
  *)
    echo "WARNING: pid $PID is NOT an lmcache/kvpersist process (cmdline: ${_pid_cmd:-<unreadable>})" >&2
    echo "         stale pid file -- cleaning it, NOT killing the process" >&2
    rm -f "$PIDF"; exit 0 ;;
esac
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