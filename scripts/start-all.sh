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

echo "== 1/3 observability (Grafana on :3000) =="
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q obs_grafana; then
  echo "observability already running -- skipping"
else
  bash "$HERE/start-observability.sh" || echo "WARNING: observability failed to start -- continuing without metrics (the cache works fine without it)"
fi

echo "== 2/3 persistent cache service =="
if [ -f "${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}/lmcache-server.pid" ] && \
   kill -0 "$(cat "${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}/lmcache-server.pid" 2>/dev/null)" 2>/dev/null; then
  echo "cache service already running -- skipping"
else
  USE_LOSSLESS_SERDE="${USE_LOSSLESS_SERDE:-1}" bash "$HERE/start-cache.sh"
fi

echo "== 3/3 vLLM engine (model load takes ~13-16 min; Ctrl-C stops the launcher, use bash scripts/stop-engine.sh to stop the engine) =="
KVP_SAME_UID="${KVP_SAME_UID:-1}" bash "$HERE/run-vllm-with-persist.sh"