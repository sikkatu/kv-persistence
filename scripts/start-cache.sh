#!/usr/bin/env bash
# start-cache.sh — start the standalone LMCache server with the persistent NVMe
# L2 tier (nixl_store_dynamic, POSIX). The server is a separate process from
# vLLM: restarting vLLM never touches this data.
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/env.sh"
_kvp_load_env "$ROOT"

# Launch mode (review 3A): host venv OR Docker sidecar in the same pinned image.
# Docker mode ELIMINATES the Python-version / ABI mismatch problem entirely --
# the lmcache server runs inside the same vLLM image (same Python, same PyTorch).
KVP_LAUNCH_MODE="${KVP_LAUNCH_MODE:-host}"   # host | docker
# Loopback-binding is a SECURITY contract (review 2A): never allow 0.0.0.0 / :: here.
case "$LMCACHE_HOST" in
  127.0.0.1|localhost|::1) : ;;
  *) die "LMCACHE_HOST must be loopback-only (127.0.0.1/localhost/::1), got '$LMCACHE_HOST'" ;;
esac
CONTAINER_NAME="${KVP_CACHE_CONTAINER:-lmcache-kvp-server}"   # docker-mode container name
case "$KVP_LAUNCH_MODE" in
  host)
    command -v lmcache >/dev/null || die "lmcache CLI missing — see docs/deployment.md (or set KVP_LAUNCH_MODE=docker)" ;;
  docker)
    DOCKER_IMAGE="${IMAGE:-${DOCKER_IMAGE:-}}"
    [ -n "$DOCKER_IMAGE" ] || die "KVP_LAUNCH_MODE=docker requires IMAGE (pinned vLLM digest) or DOCKER_IMAGE"
    command -v docker >/dev/null || die "docker required for KVP_LAUNCH_MODE=docker" ;;
  *) die "KVP_LAUNCH_MODE must be 'host' or 'docker', got '$KVP_LAUNCH_MODE'" ;;
esac
# Optional HTTP / serde flags: keep only those THIS build's --help understands,
# so a build without them still starts rather than crash on an unknown flag.
LMCACHE_HELP="$(lmcache server --help 2>&1 || true)"
case "$LMCACHE_HELP" in *--http-host*) : ;; *) unset LMCACHE_HTTP_HOST ;; esac
case "$LMCACHE_HELP" in *--http-port*) : ;; *) unset LMCACHE_HTTP_PORT ;; esac
case "$LMCACHE_HELP" in *--l2-serde*|*--serde*) : ;; *) unset LMCACHE_L2_SERDE ;; esac
BUDGET_GB="${KVP_CACHE_BUDGET_GB:?}"
ROOT_PATH="${KVP_CACHE_ROOT:?}"
mkdir -p "$ROOT_PATH"; chmod 700 "$ROOT_PATH"

# Render the L2 adapter spec from the single source of truth.
ADAPTER_JSON="$ROOT_PATH/l2-adapter.rendered.json"
sed -e "s|__KVP_CACHE_ROOT__|$ROOT_PATH|" \
    -e "s|__KVP_CACHE_BUDGET_GB__|$BUDGET_GB|" \
    -e "s|__KVP_EVICT_TRIGGER_WATERMARK__|${KVP_EVICT_TRIGGER_WATERMARK:-0.9}|" \
    -e "s|__KVP_EVICT_RATIO__|${KVP_EVICT_RATIO:-0.1}|" \
    "$ROOT/configs/${KVP_MODEL_CONFIG}/l2-adapter.json.in" \
  | python3 -c 'import json,re,sys; s=sys.stdin.read(); s=re.sub(r"^\s*//.*$","",s,flags=re.M); json.loads(s); sys.stdout.write(s)' \
  > "$ADAPTER_JSON" || die "L2 adapter JSON is invalid after render — check configs/${KVP_MODEL_CONFIG}/l2-adapter.json.in"
echo "L2 adapter rendered: $ADAPTER_JSON"
echo "  tier   : nixl_store_dynamic / POSIX  persist=${KVP_PERSIST_ENABLED:-true}"
echo "  path   : $ROOT_PATH"
echo "  budget : ${BUDGET_GB} GB hard, soft-watermark ${KVP_EVICT_TRIGGER_WATERMARK:-0.9}"

if [ "${KVP_PERSIST_ENABLED:-true}" != "true" ]; then
  die "KVP_PERSIST_ENABLED=false is not supported: it contradicts the persistence
requirement (data would be deleted on shutdown) and the L2 template is
persist_enabled=true. To discard cache data, purge explicitly:
  bash scripts/budget-report.sh purge --all --yes"
fi

# Never wipe on startup: only report what is already there.
if [ -d "$ROOT_PATH" ]; then
  EXISTING="$(du -sh "$ROOT_PATH" 2>/dev/null | cut -f1)"
  echo "existing cache data preserved: $EXISTING (automatic wipe on start is disabled by design)"
fi

LOG="$ROOT_PATH/lmcache-server.log"
PIDF="$ROOT_PATH/lmcache-server.pid"
# setsid detaches the service from this shell's terminal -- closing the SSH
# session or dropping the connection will NOT kill the cache service. nohup
# additionally shields it from HUP. Both matter because the service is meant
# to outlive the operator's shell.
# --l2-adapter takes the adapter spec as a JSON STRING (lmcache 0.5.3:
# lmcache/v1/distributed/l2_adapters/config.py add_l2_adapters_args does
# json.loads(raw) on the value). Passing the file PATH fails with
# "Invalid JSON for --l2-adapter #1", so the rendered file is passed inline;
# the file itself is still written for inspection and for the tests.
ADAPTER_JSON_ARGS="$(cat "$ADAPTER_JSON")"
if [ "$KVP_LAUNCH_MODE" = "docker" ]; then
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  # Review 2C: explicit loopback-bound port mapping instead of --network host,
  # so even if lmcache misbinds inside the container the host exposure stays local.
  setsid nohup docker run --name "$CONTAINER_NAME" --rm \
    -p "127.0.0.1:$LMCACHE_PORT:$LMCACHE_PORT" \
    ${LMCACHE_HTTP_PORT:+-p "127.0.0.1:$LMCACHE_HTTP_PORT:$LMCACHE_HTTP_PORT"} \
    -v "$ROOT_PATH:$ROOT_PATH" \
    "$DOCKER_IMAGE" lmcache server \
    --host "$LMCACHE_HOST" --port "$LMCACHE_PORT" \
    --l1-size-gb "$LMCACHE_L1_SIZE_GB" \
    --eviction-policy "$LMCACHE_EVICTION_POLICY" \
    --eviction-trigger-watermark "$LMCACHE_EVICTION_TRIGGER_WATERMARK" \
    --eviction-ratio "$LMCACHE_EVICTION_RATIO" \
    --chunk-size "$LMCACHE_CHUNK_SIZE" \
    --l2-adapter "$ADAPTER_JSON_ARGS" \
    ${LMCACHE_HTTP_HOST:+--http-host "$LMCACHE_HTTP_HOST"} \
    ${LMCACHE_HTTP_PORT:+--http-port "$LMCACHE_HTTP_PORT"} \
    ${LMCACHE_L2_SERDE:+--l2-serde "$LMCACHE_L2_SERDE"} \
    >>"$LOG" 2>&1 &
else
  setsid nohup lmcache server \
    --host "$LMCACHE_HOST" --port "$LMCACHE_PORT" \
    --l1-size-gb "$LMCACHE_L1_SIZE_GB" \
    --eviction-policy "$LMCACHE_EVICTION_POLICY" \
    --eviction-trigger-watermark "$LMCACHE_EVICTION_TRIGGER_WATERMARK" \
    --eviction-ratio "$LMCACHE_EVICTION_RATIO" \
    --chunk-size "$LMCACHE_CHUNK_SIZE" \
    --l2-adapter "$ADAPTER_JSON_ARGS" \
    ${LMCACHE_HTTP_HOST:+--http-host "$LMCACHE_HTTP_HOST"} \
    ${LMCACHE_HTTP_PORT:+--http-port "$LMCACHE_HTTP_PORT"} \
    ${LMCACHE_L2_SERDE:+--l2-serde "$LMCACHE_L2_SERDE"} \
    >>"$LOG" 2>&1 &
fi
echo $! > "$PIDF"
SERVER_PID="$(cat "$PIDF")"
echo "lmcache server started: pid $SERVER_PID, log $LOG"
echo "request port : $LMCACHE_HOST:$LMCACHE_PORT"
echo "HTTP frontend: 127.0.0.1:${LMCACHE_HTTP_PORT} (management/metrics)"

# -- readiness: a written pid is NOT proof the service works ---------------
echo "-- waiting for cache-service readiness (adapter load + request port)"
# Log grows append-only (>>): only inspect the bytes appended BY THIS START, so
# an old error from a previous run can never fail a healthy new server.
LOG_START_BYTES=$(wc -c < "$LOG" 2>/dev/null || echo 0)
READY=0
for i in $(seq 1 30); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: lmcache server died within ${i}s of startup. Last log lines:" >&2
    tail -n +"$((LOG_START_BYTES > 0 ? LOG_START_BYTES : 1))" "$LOG" >&2 || true
    exit 1
  fi
  NEW_BYTES=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  if [ "$NEW_BYTES" -gt "$LOG_START_BYTES" ] && tail -c "+$((LOG_START_BYTES + 1))" "$LOG" 2>/dev/null | grep -Eiq 'traceback|fatal error|cannot load|failed to (open|register|parse)'; then
    echo "ERROR: lmcache server reported a startup error (adapter load or port bind). Last log lines:" >&2
    tail -c "+$((LOG_START_BYTES + 1))" "$LOG" >&2 || true
    kill "$SERVER_PID" 2>/dev/null || true
    exit 1
  fi
  # request port (ZMQ, TCP transport) must be bound for vLLM to connect
  if command -v ss >/dev/null; then
    ss -ltn 2>/dev/null | grep -q ":$LMCACHE_PORT " && READY=1
  elif command -v bash >/dev/null; then
    timeout 1 bash -c "exec 3<>/dev/tcp/$LMCACHE_HOST/$LMCACHE_PORT" 2>/dev/null && READY=1
  fi
  [ "$READY" = 1 ] && break
  sleep 1
done
[ "$READY" = 1 ] || { echo "ERROR: request port $LMCACHE_HOST:$LMCACHE_PORT not listening after 30s -- the service is up but not accepting connections." >&2; tail -n 20 "$LOG" >&2 || true; exit 1; }
echo "cache service READY: process alive, no adapter errors, port $LMCACHE_PORT listening"
echo "NOTE: the HTTP frontend is intentionally loopback-only; do not proxy it without auth."