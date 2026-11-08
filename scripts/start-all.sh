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
KVP_SAME_UID="${KVP_SAME_UID:-1}" bash "$HERE/run-vllm-with-persist.sh"