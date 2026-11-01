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
# Review round 13 (docker mode): `lmcache server --help` must run in the SAME
# environment as the server itself. In docker mode the host has no lmcache CLI,
# so probing on the host silently unsets all optional flags AND made the later
# `${LMCACHE_HTTP_PORT}` expansion fail with "unbound variable" (set -u). Probe
# inside the container in docker mode; on the host only in host mode.
# Review round 15: the server is started through the kv-persistence launcher
# (kvpersist.launch) which applies BOTH runtime patches: the cache_salt
# filename fix AND the lossless compression serde registration. The probe must
# run through the same launcher to match the actual server.
export PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}"
LAUNCH=(python3 -m kvpersist.launch)
if [ "$KVP_LAUNCH_MODE" = "docker" ]; then
  LMCACHE_HELP="$(docker run --rm -v "$ROOT/implementation:/kvp-impl:ro" \
    -e PYTHONPATH=/kvp-impl --entrypoint python3 "$DOCKER_IMAGE" \
    -m kvpersist.launch server --help 2>&1 || true)"
else
  LMCACHE_HELP="$("${LAUNCH[@]}" server --help 2>&1 || true)"
fi
case "$LMCACHE_HELP" in *--http-host*) : ;; *) unset LMCACHE_HTTP_HOST ;; esac
case "$LMCACHE_HELP" in *--http-port*) : ;; *) unset LMCACHE_HTTP_PORT ;; esac
# OTLP push for the observability stack (scripts/start-observability.sh).
# Off by default: the cache must start and work WITHOUT the monitoring stack.
# Enabled by KVP_OTLP_ENDPOINT (e.g. http://localhost:4320) or
# USE_OBSERVABILITY=1 which assumes the collector on its default port.
case "${USE_OBSERVABILITY:-}" in
  1|yes|true) LMCACHE_OTLP_ENDPOINT="${LMCACHE_OTLP_ENDPOINT:-http://localhost:4320}" ;;
esac
LMCACHE_OTLP_ENDPOINT="${LMCACHE_OTLP_ENDPOINT:-${KVP_OTLP_ENDPOINT:-}}"
if [ -n "$LMCACHE_OTLP_ENDPOINT" ] && [ "$KVP_LAUNCH_MODE" = "docker" ]; then
  # Review fix: inside the sidecar container `localhost` is the CONTAINER
  # itself, so the default endpoint would silently point nowhere. The
  # collector runs on the HOST (observability/docker-compose.yml binds
  # 127.0.0.1), so the container must use the docker bridge gateway.
  case "$LMCACHE_OTLP_ENDPOINT" in
    *localhost*|*127.0.0.1*)
      LMCACHE_OTLP_ENDPOINT="${LMCACHE_OTLP_ENDPOINT/localhost/host.docker.internal}"
      LMCACHE_OTLP_ENDPOINT="${LMCACHE_OTLP_ENDPOINT/127.0.0.1/host.docker.internal}"
      echo "docker mode: rewriting OTLP endpoint to $LMCACHE_OTLP_ENDPOINT (localhost inside the container would be the container itself)"
      ;;
  esac
fi
if [ -n "$LMCACHE_OTLP_ENDPOINT" ]; then
  case "$LMCACHE_HELP" in
    *--otlp-endpoint*) : ;;
    *) echo "WARNING: this lmcache build has no --otlp-endpoint flag -- metrics will NOT reach the collector" >&2
       LMCACHE_OTLP_ENDPOINT="" ;;
  esac
fi
# NOTE: there is NO --l2-serde CLI flag in LMCache 0.5.3 -- serde is part of the
# --l2-adapter JSON ("serde" sub-dict), rendered below from __KVP_L2_SERDE_JSON__.
# The codec is therefore never dropped here; it is validated when the JSON is
# built and the server refuses to start if the requested codec is unavailable.
# The optional-flag expansions below use ${VAR:+...}, which is safe under
# `set -u` even when the variable is unset -- but the *log line* at the end
# referenced ${LMCACHE_HTTP_PORT} unconditionally. Use a safe fallback there.
BUDGET_GB="${KVP_CACHE_BUDGET_GB:?}"
ROOT_PATH="${KVP_CACHE_ROOT:?}"
mkdir -p "$ROOT_PATH"; chmod 700 "$ROOT_PATH"

# ---- Lossless L2 codec (task: lossless ONLY, never lossy) -----------------
# USE_LOSSLESS_SERDE is folded into the exported LMCACHE_L2_SERDE by
# scripts/env.sh (_kvp_resolve_l2_serde), so start-cache.sh, the identity gate
# and the Python loader all agree on one value. Accepted values:
#   none|raw = byte-exact raw chunks (no "serde" key in the adapter JSON).
#   zstd|lz4 = lossless compression (the kvp-zstd / kvp-lz4 serde types
#              registered by kvpersist.compress_serde).
# Anything lossy (fp8, turboquant, lbq, cachegen, kivi) is REFUSED loudly:
# this project is lossless by contract and the decoded KV must be bit-exact.
LMCACHE_L2_SERDE="$(printf '%s' "${LMCACHE_L2_SERDE:-none}" | tr '[:upper:]' '[:lower:]')"
case "$LMCACHE_L2_SERDE" in
  none|raw) SERDE_JSON="" ;;
  zstd|lz4) SERDE_JSON="\"serde\": {\"type\": \"kvp-$LMCACHE_L2_SERDE\", \"level\": \"${KVP_L2_COMPRESS_LEVEL:-3}\", \"align_bytes\": \"4096\"}," ;;
  fp8|turboquant|lbq|cachegen|kivi|asym*)
    die "LMCACHE_L2_SERDE=$LMCACHE_L2_SERDE is LOSSY and is not supported by kv-persistence.
The L2 tier is lossless-only: decoded KV must be bit-identical to the engine
cache. Use none (byte-exact raw), zstd or lz4." ;;
  *) die "LMCACHE_L2_SERDE=$LMCACHE_L2_SERDE not understood (use none / zstd / lz4)" ;;
esac
# Fail-fast: when a codec is selected, verify its Python bindings BEFORE the
# server starts. Otherwise the server reaches READY and every store silently
# fails later inside the serde thread pool (log-only). In docker mode the
# probe runs INSIDE the container -- the host has no lmcache/python stack.
if [ "$LMCACHE_L2_SERDE" = "zstd" ] || [ "$LMCACHE_L2_SERDE" = "lz4" ]; then
  if [ "$KVP_LAUNCH_MODE" = "docker" ]; then
    CODEC_PROBE="$(docker run --rm -v "$ROOT/implementation:/kvp-impl:ro" \
      -e PYTHONPATH=/kvp-impl --entrypoint python3 "$DOCKER_IMAGE" \
      -c "from kvpersist.compress_serde import check_codec_available; check_codec_available('$LMCACHE_L2_SERDE')" 2>&1)" \
      || die "lossless codec '$LMCACHE_L2_SERDE' is not available inside the docker image.
$CODEC_PROBE
Either install the codec into the image, or fall back to LMCACHE_L2_SERDE=none."
  else
    PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -c "from kvpersist.compress_serde import check_codec_available; check_codec_available('$LMCACHE_L2_SERDE')" 2>&1 \
      || die "lossless codec '$LMCACHE_L2_SERDE' is not available in this python.
Install it (pip install ${LMCACHE_L2_SERDE/zstd/zstandard}) or fall back to LMCACHE_L2_SERDE=none."
  fi
  echo "codec check: $LMCACHE_L2_SERDE bindings present"
fi

# Render the L2 adapter spec from the single source of truth.
ADAPTER_JSON="$ROOT_PATH/l2-adapter.rendered.json"
sed -e "s|__KVP_CACHE_ROOT__|$ROOT_PATH|" \
    -e "s|__KVP_CACHE_BUDGET_GB__|$BUDGET_GB|" \
    -e "s|__KVP_EVICT_TRIGGER_WATERMARK__|${KVP_EVICT_TRIGGER_WATERMARK:-0.9}|" \
    -e "s|__KVP_EVICT_RATIO__|${KVP_EVICT_RATIO:-0.1}|" \
    -e "s|__KVP_L2_SERDE_JSON__|$SERDE_JSON|" \
    "$ROOT/configs/${KVP_MODEL_CONFIG}/l2-adapter.json.in" \
  | python3 -c 'import json,re,sys; s=sys.stdin.read(); s=re.sub(r"^\s*//.*$","",s,flags=re.M); json.loads(s); sys.stdout.write(s)' \
  > "$ADAPTER_JSON" || die "L2 adapter JSON is invalid after render — check configs/${KVP_MODEL_CONFIG}/l2-adapter.json.in"
echo "L2 adapter rendered: $ADAPTER_JSON"
echo "  tier   : nixl_store_dynamic / POSIX  persist=${KVP_PERSIST_ENABLED:-true}"
echo "  path   : $ROOT_PATH"
echo "  budget : ${BUDGET_GB} GB hard, soft-watermark ${KVP_EVICT_TRIGGER_WATERMARK:-0.9}"
echo "  codec  : ${LMCACHE_L2_SERDE} $([ -n "$SERDE_JSON" ] && echo '(lossless serde active)' || echo '(lossless raw, byte-exact)')"
echo "  otlp   : ${LMCACHE_OTLP_ENDPOINT:-<disabled>} (metrics/traces push)"

if [ "${KVP_PERSIST_ENABLED:-true}" != "true" ]; then
  die "KVP_PERSIST_ENABLED=${KVP_PERSIST_ENABLED:-} is not supported: it contradicts the persistence
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
  # Review round 13 (docker mode fixes):
  #  * The server must bind 0.0.0.0 INSIDE the container: binding 127.0.0.1
  #    there means only the loopback inside the container, and the published
  #    port mapping (-p) cannot reach it. The HOST-side exposure is controlled
  #    by the 127.0.0.1 prefix of -p (review 2C), so this is safe.
  #  * --gpus all and --ipc host are required by the upstream LMCache docs for
  #    CUDA IPC and shared-memory zero-copy transfers (the mp connector uses
  #    CUDA IPC / POSIX SHM between the engine and the cache server).
  #  * The HTTP frontend also binds 0.0.0.0 inside the container; the -p
  #    mapping keeps it loopback-only on the host.
  setsid nohup docker run --name "$CONTAINER_NAME" --rm \
    --gpus all --ipc host \
    -p "127.0.0.1:$LMCACHE_PORT:$LMCACHE_PORT" \
    ${LMCACHE_HTTP_PORT:+-p "127.0.0.1:$LMCACHE_HTTP_PORT:$LMCACHE_HTTP_PORT"} \
    -v "$ROOT_PATH:$ROOT_PATH" \
    -v "$ROOT/implementation:/kvp-impl:ro" \
    -e PYTHONPATH=/kvp-impl \
    "$DOCKER_IMAGE" python3 -m kvpersist.launch server \
    --host 0.0.0.0 --port "$LMCACHE_PORT" \
    --l1-size-gb "$LMCACHE_L1_SIZE_GB" \
    --eviction-policy "$LMCACHE_EVICTION_POLICY" \
    --eviction-trigger-watermark "$LMCACHE_EVICTION_TRIGGER_WATERMARK" \
    --eviction-ratio "$LMCACHE_EVICTION_RATIO" \
    --chunk-size "$LMCACHE_CHUNK_SIZE" \
    --l2-adapter "$ADAPTER_JSON_ARGS" \
    ${LMCACHE_HTTP_HOST:+--http-host 0.0.0.0} \
    ${LMCACHE_HTTP_PORT:+--http-port "$LMCACHE_HTTP_PORT"} \
    ${LMCACHE_OTLP_ENDPOINT:+--otlp-endpoint "$LMCACHE_OTLP_ENDPOINT"} \
    ${LMCACHE_OTLP_ENDPOINT:+--enable-tracing} \
    >>"$LOG" 2>&1 &
else
  setsid nohup "${LAUNCH[@]}" server \
    --host "$LMCACHE_HOST" --port "$LMCACHE_PORT" \
    --l1-size-gb "$LMCACHE_L1_SIZE_GB" \
    --eviction-policy "$LMCACHE_EVICTION_POLICY" \
    --eviction-trigger-watermark "$LMCACHE_EVICTION_TRIGGER_WATERMARK" \
    --eviction-ratio "$LMCACHE_EVICTION_RATIO" \
    --chunk-size "$LMCACHE_CHUNK_SIZE" \
    --l2-adapter "$ADAPTER_JSON_ARGS" \
    ${LMCACHE_HTTP_HOST:+--http-host "$LMCACHE_HTTP_HOST"} \
    ${LMCACHE_HTTP_PORT:+--http-port "$LMCACHE_HTTP_PORT"} \
    ${LMCACHE_OTLP_ENDPOINT:+--otlp-endpoint "$LMCACHE_OTLP_ENDPOINT"} \
    ${LMCACHE_OTLP_ENDPOINT:+--enable-tracing} \
    >>"$LOG" 2>&1 &
fi
echo $! > "$PIDF"
SERVER_PID="$(cat "$PIDF")"
echo "lmcache server started: pid $SERVER_PID, log $LOG"
echo "request port : $LMCACHE_HOST:$LMCACHE_PORT"
echo "HTTP frontend: 127.0.0.1:${LMCACHE_HTTP_PORT:-<disabled>} (management/metrics)"

# -- readiness: a written pid is NOT proof the service works ---------------
echo "-- waiting for cache-service readiness (adapter load + request port)"
# Log grows append-only (>>): only inspect the bytes appended BY THIS START, so
# an old error from a previous run can never fail a healthy new server.
LOG_START_BYTES=$(wc -c < "$LOG" 2>/dev/null || echo 0)
READY=0
for i in $(seq 1 30); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: lmcache server died within ${i}s of startup. Last log lines:" >&2
    # Review round 13: `tail -n +BYTES` treats BYTES as a LINE number, not a
    # byte offset -- with a large existing log the requested start line is
    # beyond EOF and the diagnostic is EMPTY. Use `tail -c +N` (byte offset).
    tail -c "+$((LOG_START_BYTES + 1))" "$LOG" >&2 || true
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