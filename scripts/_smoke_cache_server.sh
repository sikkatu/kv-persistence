#!/usr/bin/env bash
# _smoke_cache_server.sh -- r20 K6.3, the image-build SERVER SMOKE.
#
# Runs INSIDE the freshly built image (mounted from the repo) and proves the
# real cache server can start there: the same command source as the
# production launch (scripts/env.sh _kvp_server_argv + _kvp_render_l2_adapter
# -- NEVER a second hand-written arg list), a temporary cache root, then
# kvpersist.mp_ping, SIGTERM and a clean exit. This catches what the import
# markers cannot: codecs, nixl, otel and the rendered L2 adapter.
#
# Usage: bash _smoke_cache_server.sh <zstd|none> <cache-root> <port>
#
# Markers (one per line, the build gate matches them STRICTLY):
#   KVP_SMOKE_<CODEC>_READY=OK    -- server answered an RPC ping
#   KVP_SMOKE_<CODEC>_EXIT=0      -- clean SIGTERM shutdown
set -Eeuo pipefail
CODEC="${1:?codec (zstd|none) required}"
CACHE_ROOT="${2:?cache root required}"
PORT="${3:?port required}"
# markers are uppercase: KVP_SMOKE_ZSTD_READY=OK / KVP_SMOKE_NONE_EXIT=0
UCODEC="$(printf '%s' "$CODEC" | tr '[:lower:]' '[:upper:]')"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The repo is mounted read-only at /kvp-repo (build-image.sh), the implementation
# at /kvp-impl. env.sh is the ONE source of the launch command.
REPO="${KVP_SMOKE_REPO:-/kvp-repo}"
. "$HERE/env.sh"
mkdir -p "$CACHE_ROOT"
ADAPTER="$CACHE_ROOT/l2-adapter.smoke.json"
_kvp_render_l2_adapter "$ADAPTER" "$REPO" "$CACHE_ROOT" 1 "$CODEC" \
  || { echo "SMOKE FAIL($CODEC): adapter render failed" >&2; exit 1; }
mapfile -t SRV < <(_kvp_server_argv 127.0.0.1 "$PORT" 1 1 "$ADAPTER" "")
LOG="$CACHE_ROOT/smoke-server.log"
PYTHONPATH="/kvp-impl${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m kvpersist.launch "${SRV[@]}" > "$LOG" 2>&1 &
SRV_PID=$!
_cleanup() {
  kill "$SRV_PID" 2>/dev/null || true
  wait "$SRV_PID" 2>/dev/null || true
}
trap _cleanup EXIT
READY=0
for _ in $(seq 1 60); do
  if ! kill -0 "$SRV_PID" 2>/dev/null; then
    echo "SMOKE FAIL($CODEC): the server died within startup window. Log tail:" >&2
    tail -n 25 "$LOG" >&2 || true
    exit 1
  fi
  if PYTHONPATH="/kvp-impl${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m kvpersist.mp_ping 127.0.0.1 "$PORT" 2 >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" != 1 ]; then
  echo "SMOKE FAIL($CODEC): no pong from 127.0.0.1:$PORT within 60s. Log tail:" >&2
  tail -n 25 "$LOG" >&2 || true
  exit 1
fi
echo "KVP_SMOKE_${UCODEC}_READY=OK"
# -- clean shutdown: SIGTERM, then the process must exit on its own
kill -TERM "$SRV_PID" 2>/dev/null || true
EXIT_RC=0
for _ in $(seq 1 30); do
  kill -0 "$SRV_PID" 2>/dev/null || break
  sleep 1
done
if kill -0 "$SRV_PID" 2>/dev/null; then
  echo "SMOKE FAIL($CODEC): the server did NOT exit within 30s after SIGTERM" >&2
  kill -KILL "$SRV_PID" 2>/dev/null || true
  EXIT_RC=1
else
  wait "$SRV_PID" 2>/dev/null || EXIT_RC=$?
fi
trap - EXIT
if [ "$EXIT_RC" != 0 ]; then
  echo "SMOKE FAIL($CODEC): unclean shutdown, exit code $EXIT_RC. Log tail:" >&2
  tail -n 25 "$LOG" >&2 || true
  exit 1
fi
echo "KVP_SMOKE_${UCODEC}_EXIT=0"