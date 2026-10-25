#!/usr/bin/env bash
# start-cache.sh — start the standalone LMCache server with the persistent NVMe
# L2 tier (nixl_store_dynamic, POSIX). The server is a separate process from
# vLLM: restarting vLLM never touches this data.
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=/dev/null
. "$ROOT/configs/generic/cache.env"
[ -f "$ROOT/configs/generic/cache.env.local" ] && . "$ROOT/configs/generic/cache.env.local"
# shellcheck source=/dev/null
. "$ROOT/configs/generic/lmcache-server.env"

command -v lmcache >/dev/null || die "lmcache CLI missing — see docs/deployment.md"
BUDGET_GB="${KVP_CACHE_BUDGET_GB:?}"
ROOT_PATH="${KVP_CACHE_ROOT:?}"
mkdir -p "$ROOT_PATH"; chmod 700 "$ROOT_PATH"

# Render the L2 adapter spec from the single source of truth.
ADAPTER_JSON="$ROOT_PATH/l2-adapter.rendered.json"
sed -e "s|__KVP_CACHE_ROOT__|$ROOT_PATH|" \
    -e "s|__KVP_CACHE_BUDGET_GB__|$BUDGET_GB|" \
    -e "s|__KVP_EVICT_TRIGGER_WATERMARK__|${KVP_EVICT_TRIGGER_WATERMARK:-0.9}|" \
    -e "s|__KVP_EVICT_RATIO__|${KVP_EVICT_RATIO:-0.1}|" \
    "$ROOT/configs/mimo-v2.6-pro/l2-adapter.json.in" \
  | python3 -c 'import json,re,sys; s=sys.stdin.read(); s=re.sub(r"^\s*//.*$","",s,flags=re.M); json.loads(s); sys.stdout.write(s)' \
  > "$ADAPTER_JSON" || die "L2 adapter JSON is invalid after render — check configs/mimo-v2.6-pro/l2-adapter.json.in"
echo "L2 adapter rendered: $ADAPTER_JSON"
echo "  tier   : nixl_store_dynamic / POSIX  persist=${KVP_PERSIST_ENABLED:-true}"
echo "  path   : $ROOT_PATH"
echo "  budget : ${BUDGET_GB} GB hard, soft-watermark ${KVP_EVICT_TRIGGER_WATERMARK:-0.9}"

if [ "${KVP_PERSIST_ENABLED:-true}" != "true" ]; then
  echo "WARNING: KVP_PERSIST_ENABLED=${KVP_PERSIST_ENABLED} — data files are DELETED on shutdown." >&2
  echo "         This breaks the principal requirement (persistence across restarts)." >&2
fi

# Never wipe on startup: only report what is already there.
if [ -d "$ROOT_PATH" ]; then
  EXISTING="$(du -sh "$ROOT_PATH" 2>/dev/null | cut -f1)"
  echo "existing cache data preserved: $EXISTING (automatic wipe on start is disabled by design)"
fi

LOG="$ROOT_PATH/lmcache-server.log"
PIDF="$ROOT_PATH/lmcache-server.pid"
lmcache server \
  --host "$LMCACHE_HOST" --port "$LMCACHE_PORT" \
  --l1-size-gb "$LMCACHE_L1_SIZE_GB" \
  --eviction-policy "$LMCACHE_EVICTION_POLICY" \
  --eviction-trigger-watermark "$LMCACHE_EVICTION_TRIGGER_WATERMARK" \
  --eviction-ratio "$LMCACHE_EVICTION_RATIO" \
  --chunk-size "$LMCACHE_CHUNK_SIZE" \
  --l2-adapter "$ADAPTER_JSON" \
  >>"$LOG" 2>&1 &
echo $! > "$PIDF"
echo "lmcache server started: pid $(cat "$PIDF"), log $LOG"
echo "request port : $LMCACHE_HOST:$LMCACHE_PORT"
echo "HTTP frontend: 127.0.0.1:${LMCACHE_HTTP_PORT} (management/metrics)"
echo "NOTE: the HTTP frontend is intentionally loopback-only; do not proxy it without auth."