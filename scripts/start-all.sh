#!/usr/bin/env bash
# start-all.sh -- ONE command for the whole stack (user request, round 56).
#
# Starts, in order:
#   1. observability (Prometheus + Grafana + OTel Collector) -- optional but
#      recommended; the cache auto-attaches metrics to it (round 47b);
#   2. the persistent cache service (lossless zstd by default);
#   3. the vLLM engine with the KV connector + the recipe's dynamic hotsplit
#      (HOTSPLIT_DYNAMIC=1 by default) -- runs in the FOREGROUND with the
#      standard progress output.
#
# Everything resolves itself (round 48/49/55/56): IMAGE from the recipe,
# MODEL_DIR from live/model_dir, the lmcache venv on PATH, KVP_CACHE_DEVICE
# auto-detected, ONE shared API key in $HOME/.config/kvp/api_key.
# No exports and no flags needed. Stop everything with: bash scripts/stop-all.sh
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# ONE configuration layer (review round 58): load cache.env / lmcache-server.env /
# identity.env (env values win over files) BEFORE any decision, so the PID probe
# below uses the EFFECTIVE KVP_CACHE_ROOT, not the built-in default.
. "$HERE/env.sh"
# tp4.env seeds FIRST (its values beat the config files), then the config layer.
for _tp4 in "${RECIPE_DIR:-}/tp4.env" "$HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/tp4.env" "$HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/tp4.env"; do
  [ -f "$_tp4" ] && { _kvp_seed_env_file "$_tp4"; break; }
done
_kvp_load_env "$ROOT"

# ONE codec decision (review round 58): resolve the effective L2 codec ONCE
# here and pass the SAME value to both children. start-cache.sh and
# run-vllm-with-persist.sh then never re-decide, so an lz4 (or raw) pin can
# never split the cache server and the engine into two codecs.
# Round 63 (review): seed the recipe's tp4.env BEFORE resolving the codec --
# a USE_LOSSLESS_SERDE=0 (or LMCACHE_L2_SERDE=lz4) set in tp4.env must reach the
# single codec decision, otherwise it is silently ignored (the engine launcher
# seeds later, after the value is already exported).
_kvp_resolve_l2_serde
EFFECTIVE_SERDE="${LMCACHE_L2_SERDE:-zstd}"
export LMCACHE_L2_SERDE="$EFFECTIVE_SERDE"
echo "codec: $EFFECTIVE_SERDE (resolved once, used by cache and engine)"

echo "== 1/3 observability (Grafana on :3000) =="
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q obs_grafana; then
  echo "observability already running -- skipping"
else
  bash "$HERE/start-observability.sh" || echo "WARNING: observability failed to start -- continuing without metrics (the cache works fine without it)"
fi

echo "== 2/3 persistent cache service =="
# Probe the EFFECTIVE cache root (from cache.env[.local]), not the default.
if [ -f "$KVP_CACHE_ROOT/lmcache-server.pid" ] && \
   kill -0 "$(cat "$KVP_CACHE_ROOT/lmcache-server.pid" 2>/dev/null)" 2>/dev/null; then
  echo "cache service already running at $KVP_CACHE_ROOT -- skipping"
else
  bash "$HERE/start-cache.sh"
fi

echo "== 3/3 vLLM engine (model load takes ~13-16 min; Ctrl-C stops the launcher, use bash scripts/stop-engine.sh to stop the engine) =="
# Round 70 (delegation, review step 2): the engine launch has TWO paths.
#   KVP_ENGINE=kv     (DEFAULT) -- the kv-persistence launcher
#                     (run-vllm-with-persist.sh), unchanged behaviour;
#   KVP_ENGINE=recipe           -- the hardware-verified recipe launcher
#                     (run-tp4-hotsplit.sh with KV_PERSIST=1). OFF by default
#                     until the hardware run confirms it; the old code stays.
KVP_ENGINE="${KVP_ENGINE:-kv}"
case "$KVP_ENGINE" in
  kv)
    KVP_SAME_UID="${KVP_SAME_UID:-1}" bash "$HERE/run-vllm-with-persist.sh"
    ;;
  recipe)
    # Resolve the recipe location the same way the kv launcher does
    # (explicit RECIPE_DIR > the documented clone layouts).
    if [ -z "${RECIPE_DIR:-}" ]; then
      for _rd in "$HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit"                  "$HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit"; do
        [ -d "$_rd" ] && RECIPE_DIR="$_rd" && break
      done
    fi
    [ -n "${RECIPE_DIR:-}" ] && [ -f "$RECIPE_DIR/scripts/run-tp4-hotsplit.sh" ]       || { echo "ERROR: recipe not found (set RECIPE_DIR to the mimo-v2.6-pro-hotsplit checkout)" >&2; exit 1; }
    # ---- ONE container-name source (checklist): the recipe names its
    # container mimo-pro-tp4-hotsplit, the kv launcher mimo-pro-tp4-kvp.
    # Record the EFFECTIVE name in live/engine_container so stop-engine.sh,
    # stop-all.sh and kvp down/status/logs stop exactly what is started here.
    KVP_ENGINE_CONTAINER="${KVP_ENGINE_CONTAINER:-mimo-pro-tp4-hotsplit}"
    # Round 71 (review probe A): the SAME hot-bank default as the kv path --
    # an empty HOTSPLIT_HOT_GIB with full UVA offload plans an EMPTY hot set
    # ("the split is a no-op"); the split must not be silently disabled here.
    export HOTSPLIT_HOT_GIB="$(_kvp_hotsplit_hot_gib_default)"
    [ -n "$HOTSPLIT_HOT_GIB" ] || { echo "ERROR: HOTSPLIT_HOT_GIB resolved empty -- refusing the delegated launch" >&2; exit 1; }
    mkdir -p "$ROOT/live"
    printf '%s\n' "$KVP_ENGINE_CONTAINER" > "$ROOT/live/engine_container"

    # ---- ONE key source (checklist + review probe C): the shared ensure step
    # (env.sh) generates the key for a fresh machine exactly like the kv path
    # does -- loopback-only, saved to ~/.config/kvp/api_key, mirrored to
    # $ROOT/live. The key travels in the environment, never in argv (ps).
    if [ -z "${API_KEY:-}" ]; then
      API_KEY="$(_kvp_ensure_api_key "$ROOT/live")" || {
        echo "ERROR: API key setup failed (see above)" >&2
        exit 1
      }
    fi
    export API_KEY

    # ---- KV_CACHE_DTYPE (checklist): pin explicitly. The recipe defaults to
    # auto (= bfloat16 for MiMo; the kv identity gate resolves auto the same
    # way, so the namespace is identical), but a pin removes the dependency on
    # the model's dtype resolution -- the warmed cache must keep its namespace.
    export KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-bfloat16}"

    # ---- the recipe's KV_PERSIST block needs the cache endpoint: pass the
    # SAME host/port the cache service listens on (the single codec decision
    # above is already exported as LMCACHE_L2_SERDE).
    export KV_PERSIST=1
    export KV_PERSIST_HOST="${KV_PERSIST_HOST:-${LMCACHE_HOST:-127.0.0.1}}"
    export KV_PERSIST_PORT="${KV_PERSIST_PORT:-${LMCACHE_PORT:-5555}}"
    export CONTAINER="$KVP_ENGINE_CONTAINER"

    # Round 77 (review): resolve the pinned image the same way the kv launcher
    # does (live/image) -- ipc-test.sh needs it.
    if [ -z "${IMAGE:-}" ] && [ -f "$ROOT/live/image" ]; then
      IMAGE="$(tr -d '[:space:]' < "$ROOT/live/image")"
    fi
    IMAGE="${IMAGE:-}"

    # Round 77 (review): the SAME pre-flights the kv launcher runs before
    # docker run -- lmcache RPC ping + CUDA IPC boundary test. Without them the
    # delegated path spends ~15 min loading 130 shards and dies at
    # register_kv_cache (CUDA_ERROR_MAP_FAILED) with no hint before the load.
    _kvp_pre_flights "$ROOT" "$KV_PERSIST_HOST" "$KV_PERSIST_PORT" \
      "${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}" "$IMAGE" \
      "$(id -u)" "$(id -g)" "0" "${KVP_LAUNCH_MODE:-host}" || {
        echo "ERROR: pre-flight failed -- refusing the delegated launch (nothing was started)" >&2
        exit 1
      }

    echo "KVP_ENGINE=recipe: launching the hardware-verified recipe launcher"
    echo "  container: $CONTAINER (recorded in live/engine_container)"
    echo "  KV_CACHE_DTYPE=$KV_CACHE_DTYPE (pinned; auto would resolve identically for MiMo)"
    echo "  cache endpoint: $KV_PERSIST_HOST:$KV_PERSIST_PORT, codec: $LMCACHE_L2_SERDE"
    # Foreground: the recipe launcher's exit code (activation check, smoke
    # test) is the code start-all.sh and kvp up report -- never swallowed.
    bash "$RECIPE_DIR/scripts/run-tp4-hotsplit.sh"
    ;;
  *)
    echo "ERROR: KVP_ENGINE=$KVP_ENGINE is not understood (use 'kv' or 'recipe')" >&2
    exit 2
    ;;
esac