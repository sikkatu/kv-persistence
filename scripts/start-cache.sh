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
# R2 P0-1 (review): the DEFAULT is now `docker`, because only the custom derivative
# image carries the required downstream fixes (PR #4136 retrieve/abort race, PR #5972
# incomplete objects, see docs/lmcache-build.md). `host` still works, but it runs a
# host-vendored LMCache which MIGHT lack those fixes; when chosen deliberately, the
# operator is alerted below. Aligning the shared MP cache server with every worker
# (custom image) guarantees they all carry ONE identical LMCache build.
# r24 F3: ONE resolver (env.sh, already sourced above), default docker,
# refusal on anything else.
KVP_LAUNCH_MODE="$(_kvp_resolve_launch_mode)" || exit 1   # host | docker
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
    # r20 K1: ONE resolution of the runtime image, the same function the
    # launch chain uses (start-all.sh / the recipe launchers) -- a standalone
    # scripts/start-cache.sh behaves exactly like the chain. Legacy note: an
    # explicit DOCKER_IMAGE with an EMPTY IMAGE keeps the old behaviour
    # (DOCKER_IMAGE is used as-is).
    if [ -z "${IMAGE:-}" ] && [ -n "${DOCKER_IMAGE:-}" ]; then
      IMAGE="$DOCKER_IMAGE"
    fi
    _kvp_resolve_runtime_image "$ROOT" "${_kvp_image_from_env:-0}" \
      || die "runtime image resolution failed (see above).
Fix: comment out IMAGE= in tp4.env, or unset IMAGE in the environment, or run
  IMAGE=\"\$(cat ${KVP_LIVE_DIR:-$ROOT/live}/image)\" bash scripts/start-cache.sh"
    DOCKER_IMAGE="$IMAGE"
    [ -n "$DOCKER_IMAGE" ] || die "KVP_LAUNCH_MODE=docker requires IMAGE (pinned vLLM digest) or DOCKER_IMAGE"
    command -v docker >/dev/null || die "docker required for KVP_LAUNCH_MODE=docker"
    # r2 P0-1: the shared MP cache server MUST run the custom LMCache build.
    # A stock image would serve upstream LMCache (no #4136/#5972) while workers
    # run the fixed build -> protocol-level inconsistencies and corrupted cache.
    # r20 K1: _kvp_resolve_runtime_image above already ran the deep custom
    # check (and resolved the stock base pin to the custom image); this is the
    # final guard for the legacy direct path.
    _kvp_require_custom_image "$DOCKER_IMAGE" "$ROOT" \
      || die "IMAGE '$DOCKER_IMAGE' is NOT the custom LMCache derivative.
Where it came from: IMAGE=${IMAGE:-unset} (tp4.env or the environment), DOCKER_IMAGE=${DOCKER_IMAGE:-unset}.
Fix: comment out IMAGE= in tp4.env (or unset IMAGE), or point IMAGE at the custom
image recorded in ${KVP_LIVE_DIR:-$ROOT/live}/image:
  IMAGE=\"\$(cat ${KVP_LIVE_DIR:-$ROOT/live}/image)\" bash scripts/start-cache.sh
Build it first if needed: bash scripts/build-lmcache.sh && bash scripts/build-image.sh.
Details: bash scripts/verify-engine-identity.sh" ;;
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
# Round 45f: resolve the lmcache venv python even under sudo (the cache
# server runs as root to open CUDA IPC memory; sudo resets HOME/PATH).
KVP_PY="$(_kvp_python)"
echo "python: $KVP_PY"
LAUNCH=("$KVP_PY" -m kvpersist.launch)
if [ "$KVP_LAUNCH_MODE" = "docker" ]; then
  LMCACHE_HELP="$(docker run --rm -v "$ROOT/implementation:/kvp-impl:ro" \
    -e PYTHONPATH=/kvp-impl --entrypoint python3 "$DOCKER_IMAGE" \
    -m kvpersist.launch server --help 2>&1 || true)"
else
  LMCACHE_HELP="$("${LAUNCH[@]}" server --help 2>&1 || true)"
fi
case "$LMCACHE_HELP" in *--http-host*) : ;; *) unset LMCACHE_HTTP_HOST ;; esac
# Review fix (round 38): the HTTP admin frontend binds loopback ONLY
# (docs/security.md) -- LMCACHE_HTTP_HOST=0.0.0.0 was accepted silently.
case "${LMCACHE_HTTP_HOST:-}" in
  ""|127.0.0.1|localhost|::1) : ;;
  *) die "LMCACHE_HTTP_HOST must be loopback-only (127.0.0.1/localhost/::1), got '$LMCACHE_HTTP_HOST'" ;;
esac
case "$LMCACHE_HELP" in *--http-port*) : ;; *) unset LMCACHE_HTTP_PORT ;; esac
# OTLP push for the observability stack (scripts/start-observability.sh).
# Review fix (round 47b, user): metrics used to need a manual flag -- pointless.
# Resolution order:
#   1. LMCACHE_OTLP_ENDPOINT / KVP_OTLP_ENDPOINT (explicit endpoint),
#   2. USE_OBSERVABILITY=1 (assume the collector default),
#   3. AUTO-DETECT: if a collector is listening on 127.0.0.1:4320 right now,
#      metrics are enabled automatically -- the cache only starts pushing
#      when the stack is actually up.
# Explicit opt-out: KVP_NO_OBSERVABILITY=1 (never probe, never push).
case "${USE_OBSERVABILITY:-}" in
  1|yes|true) LMCACHE_OTLP_ENDPOINT="${LMCACHE_OTLP_ENDPOINT:-http://localhost:4320}" ;;
esac
LMCACHE_OTLP_ENDPOINT="${LMCACHE_OTLP_ENDPOINT:-${KVP_OTLP_ENDPOINT:-}}"
if [ -z "$LMCACHE_OTLP_ENDPOINT" ] && [ "${KVP_NO_OBSERVABILITY:-0}" != "1" ]; then
  if timeout 1 bash -c 'exec 3<>/dev/tcp/127.0.0.1/4320' 2>/dev/null; then
    LMCACHE_OTLP_ENDPOINT="http://localhost:4320"
    echo "observability: OTel collector detected at 127.0.0.1:4320 -- metrics enabled automatically (opt out: KVP_NO_OBSERVABILITY=1)"
  fi
fi
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
# NOTE: there is NO --l2-serde CLI flag in LMCache 0.5.6 -- serde is part of the
# --l2-adapter JSON ("serde" sub-dict), rendered below from __KVP_L2_SERDE_JSON__.
# The codec is therefore never dropped here; it is validated when the JSON is
# built and the server refuses to start if the requested codec is unavailable.
# The optional-flag expansions below use ${VAR:+...}, which is safe under
# `set -u` even when the variable is unset -- but the *log line* at the end
# referenced ${LMCACHE_HTTP_PORT} unconditionally. Use a safe fallback there.
BUDGET_GB="${KVP_CACHE_BUDGET_GB:?}"
ROOT_PATH="${KVP_CACHE_ROOT:?}"
mkdir -p "$ROOT_PATH"; chmod 700 "$ROOT_PATH"
# r4 P0-2: stamp the directory as a CONFIRMED cache root. The destructive
# reset script accepts an explicit cache root as wipeable only when this
# marker exists or the directory holds nothing but known LMCache/kvpersist
# entries. Creating the marker here (at cache initialization) is the
# operator-visible proof that this directory was set up as cache data by
# start-cache.sh, never a home/repo dir.
if [ ! -e "$ROOT_PATH/.kvp-cache-root-marker" ]; then
  printf 'kvp-cache-root\ncreated_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$ROOT_PATH/.kvp-cache-root-marker"
  echo "cache root marker created: $ROOT_PATH/.kvp-cache-root-marker"
fi

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
The codecs (zstandard, lz4) ship IN the image -- a user cannot add them after the
fact. Rebuild the image: bash scripts/build-image.sh (the codecs are part of it),
or fall back to LMCACHE_L2_SERDE=none."
  else
    PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}" \
      "$KVP_PY" -c "from kvpersist.compress_serde import check_codec_available; check_codec_available('$LMCACHE_L2_SERDE')" 2>&1 \
      || die "lossless codec '$LMCACHE_L2_SERDE' is not available in this python.
Install it (pip install ${LMCACHE_L2_SERDE/zstd/zstandard}) or fall back to LMCACHE_L2_SERDE=none."
  fi
  echo "codec check: $LMCACHE_L2_SERDE bindings present"
fi

LOG="$ROOT_PATH/lmcache-server.log"
PIDF="$ROOT_PATH/lmcache-server.pid"
# Review fix (round 37): refuse to start when a server is ALREADY running --
# the old behaviour overwrote this PID file and orphaned the live server
# (stop-cache.sh then reported "already exited" for the stale PID).
if [ -f "$PIDF" ]; then
  _old_pid="$(cat "$PIDF" 2>/dev/null | tr -d '[:space:]')"
  case "$_old_pid" in
    ''|*[!0-9]*) : ;;  # unparsable pid file -- fall through
    *)
      if kill -0 "$_old_pid" 2>/dev/null; then
        _old_cmd="$(tr '\0' ' ' < "/proc/$_old_pid/cmdline" 2>/dev/null || true)"
        case "$_old_cmd" in
          *lmcache*|*kvpersist*)
            die "lmcache server already running (pid $_old_pid) -- stop it first:
  bash scripts/stop-cache.sh
(or inspect: tr '\\0' ' ' < /proc/$_old_pid/cmdline)" ;;
        esac
      fi
      ;;
  esac
fi

# Render the L2 adapter spec from the single source of truth.
ADAPTER_JSON="$ROOT_PATH/l2-adapter.rendered.json"
# Review fix (round 38): ONE watermark source for the rendered JSON, the
# summary line and the server flag -- the sed used the bare env name while
# the flag used the resolved LMCACHE_* value (JSON 0.9 vs server 0.8).
EFFECTIVE_WM="${LMCACHE_EVICTION_TRIGGER_WATERMARK:-${KVP_EVICT_TRIGGER_WATERMARK:-0.9}}"
EFFECTIVE_RATIO="${LMCACHE_EVICTION_RATIO:-${KVP_EVICT_RATIO:-0.1}}"
# r20 K6.3: the render AND the server arg list live in env.sh so the
# image-build server smoke (build-image.sh) runs the exact same launch.
_kvp_render_l2_adapter "$ADAPTER_JSON" "$ROOT" "$ROOT_PATH" "$BUDGET_GB" \
  "$LMCACHE_L2_SERDE" "${KVP_MODEL_CONFIG:-mimo-v2.6-pro}" \
  || die "L2 adapter JSON is invalid after render — check configs/${KVP_MODEL_CONFIG}/l2-adapter.json.in"
echo "L2 adapter rendered: $ADAPTER_JSON"
echo "  tier   : nixl_store_dynamic / POSIX  persist=${KVP_PERSIST_ENABLED:-true}"
echo "  path   : $ROOT_PATH"
echo "  budget : ${BUDGET_GB} GB hard, soft-watermark $EFFECTIVE_WM"
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
  # Review fix (round 45): in docker mode the cache files are written by
# root (the container user). Scripts run as the operator user (e.g. 'de')
# may then hit PermissionError on purge/budget-report -- say so up front.
if [ "$KVP_LAUNCH_MODE" = "docker" ]; then
  echo "NOTE (docker mode): cache files under $ROOT_PATH are written by ROOT"
  echo "      (the container user). If purge/budget-report.sh fails with"
  echo "      Permission denied, run them with sudo -- or use KVP_LAUNCH_MODE=host"
  echo "      with the cache server as the same user as the engine."
fi
echo "existing cache data preserved: $EXISTING (automatic wipe on start is disabled by design)"
fi

# setsid detaches the service from this shell's terminal -- closing the SSH
# session or dropping the connection will NOT kill the cache service. nohup
# additionally shields it from HUP. Both matter because the service is meant
# to outlive the operator's shell.
# --l2-adapter takes the adapter spec as a JSON STRING (lmcache 0.5.6:
# lmcache/v1/distributed/l2_adapters/config.py add_l2_adapters_args does
# json.loads(raw) on the value). Passing the file PATH fails with
# "Invalid JSON for --l2-adapter #1", so the rendered file is passed inline;
# the file itself is still written for inspection and for the tests.
if [ "$KVP_LAUNCH_MODE" = "docker" ]; then
  # r20 K6.3: the server arg list is built by _kvp_server_argv (env.sh) --
  # the SAME source the image-build server smoke uses. docker binds 0.0.0.0
  # inside the container (the 127.0.0.1 -p mapping is the host-side gate).
  _kvp_server_argv_read _srv 0.0.0.0 "$LMCACHE_PORT" "$LMCACHE_L1_SIZE_GB" "$LMCACHE_CHUNK_SIZE" "$ADAPTER_JSON" "${LMCACHE_HTTP_HOST:+0.0.0.0}" \
    || die "server argv build failed -- LMCACHE_EVICTION_POLICY is not valid (allowed: LRU, ARC, IsolatedLRU, noop) -- refusing to start the server"
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
  # r24 F1: the vllm/vllm-openai image has ENTRYPOINT `vllm` -- WITHOUT an
  # explicit --entrypoint the args after the image name go to `vllm`, which
  # rejects them ("vllm: error: unrecognized arguments: -m kvpersist.launch ...")
  # and the container exits immediately. Every OTHER docker run in the repo
  # already sets --entrypoint; this one (the production server) did not.
  setsid nohup docker run --name "$CONTAINER_NAME" --rm \
    --gpus all --ipc host \
    -p "127.0.0.1:$LMCACHE_PORT:$LMCACHE_PORT" \
    ${LMCACHE_HTTP_PORT:+-p "127.0.0.1:$LMCACHE_HTTP_PORT:$LMCACHE_HTTP_PORT"} \
    -v "$ROOT_PATH:$ROOT_PATH" \
    -v "$ROOT/implementation:/kvp-impl:ro" \
    -e PYTHONPATH=/kvp-impl \
    -e "LMCACHE_LOG_LEVEL=${LMCACHE_LOG_LEVEL:-DEBUG}" \
    -e PYTHONFAULTHANDLER=1 \
    --entrypoint python3 "$DOCKER_IMAGE" -m kvpersist.launch "${_srv[@]}" \
    >>"$LOG" 2>&1 &
else
  _kvp_server_argv_read _srv "$LMCACHE_HOST" "$LMCACHE_PORT" "$LMCACHE_L1_SIZE_GB" "$LMCACHE_CHUNK_SIZE" "$ADAPTER_JSON" "${LMCACHE_HTTP_HOST:-}" \
    || die "server argv build failed -- LMCACHE_EVICTION_POLICY is not valid (allowed: LRU, ARC, IsolatedLRU, noop) -- refusing to start the server"
  # PYTHONFAULTHANDLER=1 (round 44): if a server thread hangs, SIGABRT dumps
  # every thread stack to the log -- a hang becomes identifiable by stack,
  # not by silence (the register_kv_caches timeout gave no clue).
  PYTHONFAULTHANDLER=1 setsid nohup "${LAUNCH[@]}" "${_srv[@]}" \
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
if [ "$KVP_LAUNCH_MODE" = "docker" ]; then
  # r24 F2: in docker mode `kill -0 $SERVER_PID` only checks the docker-run
  # CLIENT and `ss` / /dev/tcp only see the port the Docker proxy publishes --
  # both go green while the server INSIDE the container never started (the
  # hardware log: "READY" printed, then "no pong"). Readiness here = (1) the
  # CONTAINER is alive AND (2) the server answers an RPC ping inside it.
  # L1 64 GB + CUDA init are slow -> a generous, configurable limit.
  READY_TIMEOUT="${KVP_CACHE_READY_TIMEOUT:-180}"
  case "$READY_TIMEOUT" in
    ''|*[!0-9]*|0) die "KVP_CACHE_READY_TIMEOUT must be an integer >= 1, got '$READY_TIMEOUT'" ;;
  esac
  _waited=0
  while [ "$_waited" -lt "$READY_TIMEOUT" ]; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" != "true" ]; then
      echo "ERROR: the cache container '$CONTAINER_NAME' is not running (exited or removed) after ${_waited}s." >&2
      echo "Failed condition: the CONTAINER must stay alive. Last log lines:" >&2
      tail -c "+$((LOG_START_BYTES + 1))" "$LOG" >&2 || true
      exit 1
    fi
    NEW_BYTES=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    if [ "$NEW_BYTES" -gt "$LOG_START_BYTES" ] && tail -c "+$((LOG_START_BYTES + 1))" "$LOG" 2>/dev/null | grep -Eiq 'traceback|fatal error|cannot load|failed to (open|register|parse)'; then
      echo "ERROR: lmcache server reported a startup error (adapter load or port bind). Last log lines:" >&2
      tail -c "+$((LOG_START_BYTES + 1))" "$LOG" >&2 || true
      docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
      exit 1
    fi
    if docker exec "$CONTAINER_NAME" python3 -m kvpersist.mp_ping 127.0.0.1 "$LMCACHE_PORT" 5 >/dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 2
    _waited=$((_waited + 2))
  done
  if [ "$READY" != 1 ]; then
    echo "ERROR: the cache server did NOT answer an RPC ping within ${READY_TIMEOUT}s" >&2
    echo "Failed condition: the SERVER must answer mp_ping (the container is alive," >&2
    echo "but the server inside is not accepting requests)." >&2
    tail -c "+$((LOG_START_BYTES + 1))" "$LOG" >&2 || true
    exit 1
  fi
  echo "cache service READY: container alive, server answered the RPC ping (port $LMCACHE_PORT)"
else
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
fi
echo "NOTE: the HTTP frontend is intentionally loopback-only; do not proxy it without auth."
# Review fix (round 43): the [kvp] patch-status lines printed by
# kvpersist.launch go to the server log (stderr of the daemon), never to
# this terminal -- on success the operator could not see whether the
# cache_salt filename mapping and the active-write registry are actually
# installed. Surface the fresh portion of the log here.
_kvp_log_tail="$(tail -c "+$((LOG_START_BYTES + 1))" "$LOG" 2>/dev/null || true)"
if printf '%s' "$_kvp_log_tail" | grep -q '\[kvp\]'; then
  printf '%s\n' "$_kvp_log_tail" | grep '\[kvp\]' | sed 's/^/  /'
else
  echo "  [kvp] patch status not found in the server log yet -- if the"
  echo "        service behaves oddly, check: grep '\[kvp\]' $LOG"
fi
# r2 P0-2: a server running WITHOUT the compiled cuda_ops extension silently
# falls back to the slow Python copy path (the cudaMemcpy error 1 incident).
# A production start must not hide that. Warn loudly when the fallback line
# appears; never treat the server as healthy on that line.
if printf '%s' "$_kvp_log_tail" | grep -Eiq 'lmcache\.cuda_ops compiled extension not found|cuda_ops.*not found.*fallback'; then
  echo "" >&2
  echo "WARNING: the LMCache server is running WITHOUT the compiled cuda_ops" >&2
  echo "  extension -- all transfers use the slow Python fallback." >&2
  echo "  Rebuild the custom wheel with nvcc in PATH (scripts/build-lmcache.sh)" >&2
  echo "  and redeploy BOTH the MP server and the workers from that wheel." >&2
  echo "  (grep 'cuda_ops' $LOG for the exact line)" >&2
  CUDA_OPS_FALLBACK=1
fi