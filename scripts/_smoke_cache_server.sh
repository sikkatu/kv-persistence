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
# Usage: bash _smoke_cache_server.sh <zstd|none> <cache-root> [port]
#
# r22 B3: the SHIPPED configs (configs/generic/*.env) are loaded with the
# SAME parser as start-cache.sh (_kvp_seed) but WITHOUT *.local -- the smoke
# result must not depend on files on the user's machine. chunk-size, policy,
# watermark and ratio come from there; L1 stays 1 GB and the adapter is
# rendered from the SAME template with a 1 GB budget.
#
# r22 B5: clean-exit criteria -- the process exits on its own within 30 s of
# SIGTERM with exit code 0 OR 143 (128+SIGTERM is NORMAL for this server: the
# HTTP server handles SIGTERM, shuts down cleanly and re-raises the signal),
# and BOTH shutdown markers appear in the log: `MPCacheServer closed` and
# `persist_enabled=True, keeping data files on disk` (the latter proves L2
# data is kept on stop). Any other code (137 = killed by the timeout, 1, ...)
# or 143 without both markers -> refusal.
#
# r22 B6: the smoke uses its OWN port (default 15555, override
# KVP_SMOKE_PORT) so it never fights the production cache for the fd-channel
# socket /dev/shm/lmcache_fd_<port>.sock (--ipc host shares /dev/shm).
#
# Markers (one per line, the build gate matches them STRICTLY):
#   KVP_SMOKE_<CODEC>_READY=OK    -- server answered an RPC ping
#   KVP_SMOKE_<CODEC>_EXIT=0      -- clean SIGTERM shutdown (0 or 143 + markers)
set -Eeuo pipefail
CODEC="${1:?codec (zstd|none) required}"
CACHE_ROOT="${2:?cache root required}"
PORT="${3:-${KVP_SMOKE_PORT:-15555}}"
# r23 D5: the SIGTERM wait is configurable (tests set 2-3s; production 30s)
# r24 E4.1: validate it BEFORE anything starts -- a non-numeric (or empty)
# value made `seq` fail later, the wait loop never ran and the smoke lied
# ("did NOT exit within Xs"). `${VAR-default}` treats "" as set, so an empty
# override must be an ERROR, not silently fall back to 30.
TERM_TIMEOUT="${KVP_SMOKE_TERM_TIMEOUT-30}"
case "$TERM_TIMEOUT" in
  ''|*[!0-9]*|0)
    echo "SMOKE FAIL($CODEC): KVP_SMOKE_TERM_TIMEOUT must be an integer >= 1, got '${TERM_TIMEOUT:-<empty>}'" >&2
    exit 1 ;;
esac
# markers are uppercase: KVP_SMOKE_ZSTD_READY=OK / KVP_SMOKE_NONE_EXIT=0
UCODEC="$(printf '%s' "$CODEC" | tr '[:lower:]' '[:upper:]')"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The repo is mounted read-only at /kvp-repo (build-image.sh), the implementation
# at /kvp-impl. env.sh is the ONE source of the launch command.
REPO="${KVP_SMOKE_REPO:-/kvp-repo}"
. "$HERE/env.sh"

# ---- r22 B3: load the SHIPPED configs exactly like start-cache.sh (no *.local)
for _cfg in "$REPO/configs/generic/cache.env" \
            "$REPO/configs/generic/lmcache-server.env"; do
  [ -f "$_cfg" ] && _kvp_seed "$_cfg" $'\n'
done
# r22 B3: chunk-size / policy / watermark / ratio come from the configs; L1
# stays 1 GB (the smoke is not a load test) and the adapter budget is 1 GB.
L1_GB=1
CHUNK_SIZE="${LMCACHE_CHUNK_SIZE:-256}"

# ---- r22 B6: refuse when the fd-channel socket is held by a live server
FD_SOCK="/dev/shm/lmcache_fd_${PORT}.sock"
if [ -S "$FD_SOCK" ]; then
  # a live listener on the socket = a server already owns this port's fd channel
  if python3 - "$FD_SOCK" <<'PYCHK' 2>/dev/null; then
import socket, sys
# r23 D3: the LMCache fd-channel socket is AF_UNIX SOCK_SEQPACKET (pinned
# lmcache/v1/platform/fd_channel.py:14,53,92). A SOCK_STREAM connect to a
# SEQPACKET listener fails with EPROTOTYPE -- NOT proof of staleness.
# Fail-safe: refuse unless staleness is PROVEN (FileNotFoundError /
# ConnectionRefusedError).
s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
try:
    s.connect(sys.argv[1])
    s.close()
    sys.exit(0)   # a live server owns the socket -> refuse
except (FileNotFoundError, ConnectionRefusedError):
    sys.exit(1)   # no socket / stale socket file -> proceed
except OSError:
    sys.exit(0)   # any other error -> NOT proven stale -> refuse
PYCHK
    echo "SMOKE FAIL($CODEC): the fd-channel socket $FD_SOCK is held by a live" >&2
    echo "  server (port $PORT). The production cache and the smoke must not share" >&2
    echo "  the port. Set KVP_SMOKE_PORT to a free port (default 15555)." >&2
    exit 1
  fi
fi

mkdir -p "$CACHE_ROOT"
ADAPTER="$CACHE_ROOT/l2-adapter.smoke.json"
_kvp_render_l2_adapter "$ADAPTER" "$REPO" "$CACHE_ROOT" 1 "$CODEC" \
  || { echo "SMOKE FAIL($CODEC): adapter render failed" >&2; exit 1; }
_kvp_server_argv_read SRV 127.0.0.1 "$PORT" "$L1_GB" "$CHUNK_SIZE" "$ADAPTER" "" \
  || { echo "SMOKE FAIL($CODEC): server argv build failed" >&2
       echo "  (check LMCACHE_EVICTION_POLICY -- allowed: LRU, ARC, IsolatedLRU, noop)" >&2; exit 1; }
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
PING_RC=1
for _ in $(seq 1 60); do
  if ! kill -0 "$SRV_PID" 2>/dev/null; then
    echo "SMOKE FAIL($CODEC): the server died within startup window. Log tail:" >&2
    tail -n 25 "$LOG" >&2 || true
    exit 1
  fi
  # r22 B2: mp_ping exit 3 = lmcache not importable in the image -- stop
  # waiting immediately (not 60 s of "no pong" nonsense).
  PING_RC=0
  PYTHONPATH="/kvp-impl${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m kvpersist.mp_ping 127.0.0.1 "$PORT" 2 >/dev/null 2>&1 || PING_RC=$?
  if [ "$PING_RC" = 0 ]; then
    READY=1
    break
  fi
  if [ "$PING_RC" = 3 ]; then
    echo "SMOKE FAIL($CODEC): lmcache is not importable inside the image" >&2
    echo "  (mp_ping exit 3) -- the server cannot be reached via the RPC protocol." >&2
    tail -n 25 "$LOG" >&2 || true
    exit 1
  fi
  sleep 1
done
if [ "$READY" != 1 ]; then
  echo "SMOKE FAIL($CODEC): no pong from 127.0.0.1:$PORT within 60s. Log tail:" >&2
  tail -n 25 "$LOG" >&2 || true
  exit 1
fi
echo "KVP_SMOKE_${UCODEC}_READY=OK"

# ---- r22 B5: clean shutdown criteria ---------------------------------------
# SIGTERM; the process must exit on its own within 30 s with code 0 or 143
# (128+SIGTERM is NORMAL for this server), and BOTH shutdown markers must be
# in the log: `MPCacheServer closed` (the server closed) and
# `persist_enabled=True, keeping data files on disk` (L2 data is kept).
kill -TERM "$SRV_PID" 2>/dev/null || true
EXIT_RC=0
# r23 D5: the SIGTERM wait is configurable (tests set 2-3s; production 30s).
# TERM_TIMEOUT was validated at script start (r24 E4.1).
for _ in $(seq 1 "$TERM_TIMEOUT"); do
  kill -0 "$SRV_PID" 2>/dev/null || break
  sleep 1
done
if kill -0 "$SRV_PID" 2>/dev/null; then
  echo "SMOKE FAIL($CODEC): the server did NOT exit within ${TERM_TIMEOUT}s after SIGTERM" >&2
  kill -KILL "$SRV_PID" 2>/dev/null || true
  EXIT_RC=137
else
  wait "$SRV_PID" 2>/dev/null || EXIT_RC=$?
fi
trap - EXIT
# r22 B5: the exit-code criterion (0 or 143) AND both shutdown markers.
case "$EXIT_RC" in
  0|143) : ;;
  *)
    echo "SMOKE FAIL($CODEC): unclean shutdown, exit code $EXIT_RC" >&2
    echo "  (expected 0 or 143 = 128+SIGTERM, the normal shutdown code for this server)" >&2
    tail -n 25 "$LOG" >&2 || true
    exit 1 ;;
esac
if ! grep -q "MPCacheServer closed" "$LOG" 2>/dev/null; then
  echo "SMOKE FAIL($CODEC): shutdown marker 'MPCacheServer closed' missing in the log" >&2
  tail -n 25 "$LOG" >&2 || true
  exit 1
fi
if ! grep -q "persist_enabled=True, keeping data files on disk" "$LOG" 2>/dev/null; then
  echo "SMOKE FAIL($CODEC): shutdown marker 'persist_enabled=True, keeping data files on disk'" >&2
  echo "  missing in the log (L2 data must survive the stop)" >&2
  tail -n 25 "$LOG" >&2 || true
  exit 1
fi
echo "KVP_SMOKE_${UCODEC}_EXIT=0"