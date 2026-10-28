#!/usr/bin/env bash
# run-vllm-with-persist.sh -- vLLM TP4 for MiMo-V2.6-Pro WITH the persistent KV connector.
# Separate launcher of kv-persistence: NEVER edits the production recipe.
# Recipe patch files are mounted READ-ONLY; all state lives under KVP_CACHE_ROOT.
# docker args mirror run-tp4-uva.sh one-to-one + --kv-transfer-config to the
# running lmcache server. ROLLBACK: run the recipe launcher; NVMe data is kept.
#
# MANDATORY identity gate (review): verify-engine-identity.sh must pass BEFORE
# persistent cache is attached to vLLM. If it returns exit 4 (MISMATCH) or 5
# (INCONCLUSIVE), cache reuse is refused -- this launcher stops instead of
# silently starting with unverified identity.
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
RECIPE="${RECIPE_DIR:-$HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit}"
. "$HERE/env.sh"
_kvp_load_env "$ROOT"
[ -d "$RECIPE" ] || die "production recipe not found at $RECIPE (set RECIPE_DIR)"
IMAGE="${IMAGE:?IMAGE must be pinned with @sha256:<64 hex>}"
printf '%s' "$IMAGE" | grep -Eq '@sha256:[0-9a-f]{64}$' || die "IMAGE must be pinned by digest"
MODEL_DIR="${MODEL_DIR:?set MODEL_DIR to the local model directory}"
[ -d "$MODEL_DIR" ] || die "MODEL_DIR not found: $MODEL_DIR"
API_KEY="${API_KEY:-}"
[ -f "$PWD/live/api_key" ] && [ -z "$API_KEY" ] && API_KEY="$(head -n1 "$PWD/live/api_key" | tr -d '[:space:]')"
[ -n "$API_KEY" ] || die "API_KEY required (live/api_key; non-loopback binds always need it)"
BIND="${BIND:-127.0.0.1}"; PORT="${PORT:-30007}"
CONTAINER="${CONTAINER:-mimo-pro-tp4-kvp}"
# NOTE on REQUESTED settings from the task brief vs the VERIFIED recipe:
#  KV_CACHE_DTYPE: the brief says fp16; the production deployment effectively
#  runs bf16 until re-measured. Set what is ACTUALLY in effect (identity too).
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-bf16}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-1048576}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-2}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-8192}"
CPU_OFFLOAD_GB="${CPU_OFFLOAD_GB:-128}"; GPU_UTIL="${GPU_UTIL:-0.85}"
MOE_BACKEND="${MOE_BACKEND:-humming}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"; SITE="${SITE:-}"
PIDF="$KVP_CACHE_ROOT/lmcache-server.pid"
[ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null \
  || die "lmcache server not running -- start it first: bash scripts/start-cache.sh"
# Identity gate: engine vs config must match before cache is trusted.
echo "-- identity gate: verifying engine config matches identity pins --"
rc=0
bash "$HERE/verify-engine-identity.sh" "$CONTAINER" || rc=$?
if [ "$rc" -ne 0 ]; then
  if [ "$rc" -eq 5 ]; then
    die "INCONCLUSIVE: engine identity could not be verified (see output above).
Cache reuse is REFUSED by policy -- fix value sources and rerun, or set
KVP_PERSIST_ENABLED=0 to launch without persistent cache."
  fi
  die "ENGINE IDENTITY MISMATCH (exit $rc): cache must NOT be reused. Fix
configs/${KVP_MODEL_CONFIG}/identity.env(.local) and rerun, or launch without
persistent cache (KVP_PERSIST_ENABLED=0)."
fi
echo "identity verified -- proceeding to attach persistent cache"
# Review P1-1 fix: KVP_PERSIST_ENABLED=0 is a REAL bypass -- the connector flag
# is genuinely omitted (not just documented as if it were). When disabled, the
# identity gate above may still fail and stop the launcher: that is intended,
# because even without the connector a mismatch means the pins are stale and
# silently starting with wrong config is worse than a clear stop.
KV_TRANSFER_CONFIG=""
KV_TRANSFER_ARGS=()
if [ "${KVP_PERSIST_ENABLED:-true}" = "true" ]; then
  KV_TRANSFER_CONFIG="{\"kv_connector\":\"LMCacheMPConnector\",\"kv_connector_module_path\":\"lmcache.integration.vllm.lmcache_mp_connector\",\"kv_role\":\"kv_both\",\"kv_connector_extra_config\":{\"lmcache.mp.host\":\"tcp://$LMCACHE_HOST\",\"lmcache.mp.port\":\"$LMCACHE_PORT\"}}"
  KV_TRANSFER_ARGS=(--kv-transfer-config "$KV_TRANSFER_CONFIG")
  echo "persistent cache: attached (KV_TRANSFER_CONFIG=$KV_TRANSFER_CONFIG)"
else
  echo "KVP_PERSIST_ENABLED=0 -- launching WITHOUT persistent KV connector (--kv-transfer-config omitted)"
fi
MOUNTS=(-v "$HF_CACHE:/root/.cache/huggingface" -v "$MODEL_DIR:/model:ro")
if [ -d "$RECIPE/patches" ]; then
  ( cd "$RECIPE/patches" && sha256sum -c MANIFEST.sha256 >/dev/null 2>&1 ) \
    || die "recipe patches do not match MANIFEST.sha256 - refusing to run"
  PYBIN=python3
  docker run --rm --entrypoint python3 "$IMAGE" -c 'import sys; print(sys.version)' >/dev/null 2>&1 || PYBIN=python
  [ -n "$SITE" ] || SITE="$(docker run --rm --entrypoint $PYBIN "$IMAGE" -c 'import importlib.util,os
s=importlib.util.find_spec("vllm")
locs=list(s.submodule_search_locations or [])
pkg = locs[0] if locs else os.path.dirname(s.origin)
print(os.path.dirname(pkg))' | tail -1 | tr -d '\r')"
  case "$SITE" in */site-packages|*/dist-packages) : ;; *) die "SITE detection failed: '$SITE'" ;; esac
  MOUNTS+=(
    -v "$RECIPE/patches/mimo_v2_hotsplit.py:$SITE/vllm/model_executor/models/mimo_v2.py:ro"
    -v "$RECIPE/patches/mimo_v2_omni.py:$SITE/vllm/model_executor/models/mimo_v2_omni.py:ro"
  )
fi
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
echo "starting $CONTAINER with persistent KV connector (lmcache $LMCACHE_HOST:$LMCACHE_PORT)"
docker run -d --name "$CONTAINER" --restart no \
  --gpus all --ipc host --network host \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -e HF_HOME=/root/.cache/huggingface \
  -e VLLM_USE_DEEP_GEMM=0 \
  -e VLLM_WEIGHT_OFFLOADING_DISABLE_PIN_MEMORY=1 \
  -e "VLLM_API_KEY=$API_KEY" \
  "${MOUNTS[@]}" "$IMAGE" \
  --model /model \
  --served-model-name mimo-pro \
  --trust-remote-code \
  --tensor-parallel-size 4 \
  --disable-custom-all-reduce \
  --offload-backend uva \
  --cpu-offload-gb "$CPU_OFFLOAD_GB" \
  --cpu-offload-params routed_experts.w13_weight routed_experts.w2_weight \
  --moe-backend "$MOE_BACKEND" \
  --max-model-len "$MAX_MODEL_LEN" \
  --max-num-seqs "$MAX_NUM_SEQS" \
  --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS" \
  --kv-cache-dtype "$KV_CACHE_DTYPE" \
  --enable-prefix-caching \
  --gpu-memory-utilization "$GPU_UTIL" \
  ${KV_TRANSFER_ARGS[@]+"${KV_TRANSFER_ARGS[@]}"} \
  --generation-config vllm \
  --reasoning-parser mimo \
  --tool-call-parser mimo \
  --enable-auto-tool-choice \
  --host "$BIND" --port "$PORT"
echo "launched; model load takes ~13-16 min. progress every 10s:"
URLHOST=127.0.0.1; case "$BIND" in 0.0.0.0) URLHOST=127.0.0.1 ;; *:*) URLHOST="[$BIND]" ;; *) URLHOST="$BIND" ;; esac
LOG_SHOWN=0; START_TS=$(date +%s)
for _ in $(seq 1 300); do
  STATE="$(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' "$CONTAINER" 2>/dev/null || echo gone)"
  case "$STATE" in running*) : ;; *)
    echo "ERROR: container not running ($STATE)"; docker logs --tail 100 "$CONTAINER" >&2; exit 1 ;; esac
  LOG_ALL="$(docker logs "$CONTAINER" 2>&1)"; LOG_TOTAL=$(printf '%s\n' "$LOG_ALL" | wc -l)
  if [ "$LOG_TOTAL" -gt "$LOG_SHOWN" ]; then
    printf '[%3ds] %d new log line(s)\n' "$(( $(date +%s) - START_TS ))" "$(( LOG_TOTAL - LOG_SHOWN ))"
    LOG_SHOWN=$LOG_TOTAL
  else
    printf '[%3ds] still loading (no new log lines)\n' "$(( $(date +%s) - START_TS ))"
  fi
  if curl -fsS --max-time 3 -H "Authorization: Bearer $API_KEY" "http://$URLHOST:$PORT/v1/models" >/dev/null 2>&1; then
    echo "API is up with the persistent KV connector attached."; exit 0
  fi
  sleep 10
done
echo "startup timed out"; docker logs --tail 200 "$CONTAINER" >&2; exit 1
