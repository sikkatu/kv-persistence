#!/usr/bin/env bash
# run-vllm-with-persist.sh -- vLLM TP4 for MiMo-V2.6-Pro WITH the persistent KV connector.
# Separate launcher of kv-persistence: NEVER edits the production recipe.
# Recipe patch files are mounted READ-ONLY; all state lives under KVP_CACHE_ROOT.
#
# Relationship to the recipe launchers (run-tp4-uva.sh / run-tp4-hotsplit.sh,
# review fix round 19): the docker args are based on the same recipe baseline,
# but NOT a byte-for-byte mirror. This launcher:
#   * mounts the model at /models/<cache-identity namespace> (not /model) --
#     the recipe's KV_PERSIST=1 branch now does the same, but the plain
#     recipe run does NOT (it intentionally has no cache identity);
#   * passes --kv-transfer-config (same JSON shape as the recipe) and
#     --enable-prompt-tokens-details + --block-size (also in the recipe's
#     KV_PERSIST=1 branch);
#   * verifies engine identity BEFORE and AFTER launch (verify-engine-identity.sh)
#     and REFUSES to attach the cache on mismatch -- the recipe's branch only
#     reminds the operator to run the gate manually;
#   * keeps the recipe's engine defaults compatible (MAX_NUM_SEQS=2,
#     KV_CACHE_DTYPE spelling `bfloat16`, MAX_MODEL_LEN=1048576, block-size 16)
#     so BOTH paths derive compatible KV. Review fix: the RECIPE's own default
#     for KV_CACHE_DTYPE is `auto` (not `bfloat16`), and `auto` resolves to the
#     MODEL's dtype -- this launcher resolves `auto` the same way before hashing
#     the namespace, so an `auto` recipe run and a `bfloat16` run of a bf16 model
#     share one namespace. If you override any of these in one path, override
#     them in the other too -- a different value is a SAFE cache miss (the
#     namespace changes), never a wrong read, but it silently stops reuse
#     between paths.
# ROLLBACK: run the recipe launcher; NVMe data is kept.
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
# Review fix: look up live/api_key under the REPO ROOT ($ROOT), not $PWD --
# the launcher must work from any working directory, not only from the repo root.
[ -f "$ROOT/live/api_key" ] && [ -z "$API_KEY" ] && API_KEY="$(head -n1 "$ROOT/live/api_key" | tr -d '[:space:]')"
[ -n "$API_KEY" ] || die "API_KEY required (live/api_key under the repo root; non-loopback binds always need it)"
BIND="${BIND:-127.0.0.1}"; PORT="${PORT:-30007}"
CONTAINER="${CONTAINER:-mimo-pro-tp4-kvp}"
# NOTE on REQUESTED settings from the task brief vs the VERIFIED recipe:
#  KV_CACHE_DTYPE: BFLOAT16 is the effective dtype of this project. The value
#  MUST be the vLLM CacheDType spelling `bfloat16` (review fix): `bf16` is NOT
#  in the pinned build's CacheDType Literal (vllm 8e2beaa3, vllm/config/cache.py)
#  and the engine refuses to start ("invalid choice"). KV dtype is part of the
#  cache identity, so a dtype change is a cache MISS.
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-bfloat16}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-1048576}"
# Review fix (round 22/23): export the EFFECTIVE engine settings as KVP_* so
# the identity gate compares the container args against the SAME values this
# launcher uses, not against stale file pins. Round 23: resolved through the
# ONE shared code path (kvpersist.cli effective-identity) that the recipe
# launchers and the gate itself also use -- the consumers cannot drift apart.
# The gate honours "env wins over files", so these exports make
# MAX_MODEL_LEN=524288 / KV_CACHE_DTYPE=fp8 launches verify clean.
_EFF_ENV="$(PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m kvpersist.cli effective-identity --model-dir "$MODEL_DIR")" \
  || die "cannot resolve the effective engine identity (kvpersist.cli effective-identity)"
while IFS='=' read -r _eff_k _eff_v; do
  case "$_eff_k" in
    KVP_SERVED_MODEL_NAME|KVP_KV_CACHE_DTYPE|KVP_MAX_MODEL_LEN|KVP_BLOCK_SIZE|KVP_TP_SIZE)
      export "$_eff_k=$_eff_v" ;;
  esac
done <<EOF
$_EFF_ENV
EOF
[ -n "${KVP_KV_CACHE_DTYPE:-}" ] || die "empty effective KV dtype -- refusing to guess"
# Review fix (round 24): SINGLE SOURCE OF TRUTH -- the engine is started with
# EXACTLY these KVP_* values (see the docker run line below). A conflicting
# KVP_* export no longer desyncs namespace/gate from the running engine: the
# resolver's output IS what the engine runs. The gate compares the container
# args against the same values, so a namespace can never claim settings the
# engine does not have.
MAX_NUM_SEQS="${MAX_NUM_SEQS:-2}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-8192}"
CPU_OFFLOAD_GB="${CPU_OFFLOAD_GB:-128}"; GPU_UTIL="${GPU_UTIL:-0.85}"
MOE_BACKEND="${MOE_BACKEND:-humming}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"; SITE="${SITE:-}"
# Review fix: reasoning/tool-call parsers are MODEL-specific, not a project
# constant. The reference MiMo deployment needs the mimo parsers; other models
# set VLLM_EXTRA_ARGS to their own (or to nothing).
# VLLM_EXTRA_ARGS is a SPACE-SEPARATED string of extra vLLM flags (an env var
# cannot carry an array), e.g.
#   export VLLM_EXTRA_ARGS="--reasoning-parser my-model --tool-call-parser my-model"
# It is split into a real array here so flags with spaces survive.
if [ "${KVP_MODEL_CONFIG:-mimo-v2.6-pro}" = "mimo-v2.6-pro" ] && [ -z "${VLLM_EXTRA_ARGS:-}" ]; then
  VLLM_EXTRA_ARGS="--reasoning-parser mimo --tool-call-parser mimo --enable-auto-tool-choice"
fi
# Review fix (critical): without --enable-prompt-tokens-details the engine
# returns usage.prompt_tokens_details = None, so `cached_tokens` -- the ONLY
# machine-readable reuse counter -- is unavailable and scripts/test-recovery.sh
# can never PASS (it reads cached_tokens there). This flag is model-independent
# (it only controls response verbosity), so it is added for every model, not
# just MiMo. The pinned build (vllm 8e2beaa3) accepts it; if a future build
# renames it, drop it here and in benchmark.sh's metric extraction.
PROMPT_TOKENS_DETAILS_FLAG="--enable-prompt-tokens-details"
VLLM_EXTRA_ARGS_ARR=()
if [ -n "${VLLM_EXTRA_ARGS:-}" ]; then
  read -r -a VLLM_EXTRA_ARGS_ARR <<< "$VLLM_EXTRA_ARGS"
fi
PIDF="$KVP_CACHE_ROOT/lmcache-server.pid"
[ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null \
  || die "lmcache server not running -- start it first: bash scripts/start-cache.sh"
# Review fix (round 13): the identity gate inspects the RUNNING container. On a
# first run there is no container yet -- gate AFTER launch instead of failing
# with INCONCLUSIVE. On reruns the gate checks the container that is about to
# be REPLACED (it holds the OLD args), so the meaningful check is against the
# args we are about to pass, plus the container as a secondary signal. The
# gate is therefore split: pre-launch config check (always), post-launch
# container-args check (after docker run).
# KVP_PERSIST_ENABLED=0 genuinely bypasses both gates: without the connector
# there is no cache to mistrust, so refusing to launch would be wrong.
PERSIST="${KVP_PERSIST_ENABLED:-true}"
if [ "$PERSIST" != "true" ] && [ "$PERSIST" != "false" ] && [ "$PERSIST" != "0" ] && [ "$PERSIST" != "1" ]; then
  die "KVP_PERSIST_ENABLED must be true/false (got '$PERSIST')"
fi
case "$PERSIST" in
  false|0) PERSIST=false ;;
  *) PERSIST=true ;;
esac
# Identity gate (pre-launch, config-side): engine-independent fields must be
# present and well-formed BEFORE anything is launched. The engine-side match
# (container args) runs right after docker run below.
if [ "$PERSIST" = "true" ]; then
  echo "-- identity gate (pre-launch): verifying identity config is complete --"
  bash "$HERE/verify-engine-identity.sh" --check-config-only \
    || die "identity config incomplete/invalid -- fix configs/${KVP_MODEL_CONFIG}/identity.env(.local)"
else
  echo "KVP_PERSIST_ENABLED=$PERSIST -- launching WITHOUT persistent KV connector (--kv-transfer-config omitted); identity gate skipped (no cache to mistrust)"
fi
# Review P1-1 fix: KVP_PERSIST_ENABLED=false is a REAL bypass -- the connector
# flag is genuinely omitted (not just documented as if it were). The pre-launch
# gate above is also skipped when disabled: without the connector there is no
# cache to mistrust.
KV_TRANSFER_CONFIG=""
KV_TRANSFER_ARGS=()
if [ "$PERSIST" = "true" ]; then
  KV_TRANSFER_CONFIG="{\"kv_connector\":\"LMCacheMPConnector\",\"kv_connector_module_path\":\"lmcache.integration.vllm.lmcache_mp_connector\",\"kv_role\":\"kv_both\",\"kv_connector_extra_config\":{\"lmcache.mp.host\":\"tcp://$LMCACHE_HOST\",\"lmcache.mp.port\":\"$LMCACHE_PORT\"}}"
  KV_TRANSFER_ARGS=(--kv-transfer-config "$KV_TRANSFER_CONFIG")
  echo "persistent cache: attached (KV_TRANSFER_CONFIG=$KV_TRANSFER_CONFIG)"
  # Lossless L2 codec must match the RUNNING cache server: the codec is part
  # of the cache-identity namespace (kvpersist.identity l2_serde), so a
  # mismatch here means the engine and the server would disagree about which
  # chunks are readable. Both read the same LMCACHE_L2_SERDE (folded from
  # USE_LOSSLESS_SERDE by scripts/env.sh), so just report it.
  echo "L2 codec: ${LMCACHE_L2_SERDE:-none} (lossless; expected in the cache server too)"
else
  echo "KVP_PERSIST_ENABLED=$PERSIST -- launching WITHOUT persistent KV connector (--kv-transfer-config omitted)"
fi
MOUNTS=(-v "$HF_CACHE:/root/.cache/huggingface")
# Review round 13 (critical): LMCache derives its cache identity (ObjectKey and
# the L2 file name) from vLLM's `model_config.model`, which is whatever `--model`
# was passed. Mounting every model at the SAME path `/model` meant all
# deployments registered one identical name and shared/collided on the same L2
# objects -- the identity namespace never reached the storage layer.
#
# Fix: mount the model under a namespace-coded path so `--model <path>` is
# unique per identity. The name comes from the SAME identity values the gate
# verifies (kvpersist.identity.lmcache_model_name), so the storage namespace
# and the verified pins cannot drift apart.
export KVP_REPO_ROOT="$ROOT"
export PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}"
if [ "$PERSIST" = "true" ]; then
  # Review fix (round 20): the served-model-name prefix of the namespace MUST
  # match the recipe's KV_PERSIST=1 path, which passes
  # KVP_SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-mimo-pro}". The old default
  # here was `basename "$MODEL_DIR"` (e.g. "MiMo-V2.6-Pro-MOPD"), so the two
  # launchers produced DIFFERENT model_names for the SAME identity and never
  # shared cached blocks. Default now follows SERVED_MODEL_NAME (what the
  # engine is actually called via --served-model-name) exactly.
  export KVP_SERVED_MODEL_NAME="${KVP_SERVED_MODEL_NAME:-${SERVED_MODEL_NAME:-mimo-pro}}"
  # Review fix (round 20/23): the KVP_* values above are the REAL engine
  # settings (resolved by the shared effective-identity code) -- the namespace
  # therefore follows what the engine is actually started with, exactly like
  # the recipe KV_PERSIST=1 branch.
  KVP_MODEL_NAME="$(python3 -m kvpersist.cli model-name ${KVP_TENANT_DISCRIMINATOR:+--discriminator "$KVP_TENANT_DISCRIMINATOR"})" \
    || die "cannot derive LMCache model_name from the identity config"
  [ -n "$KVP_MODEL_NAME" ] || die "empty LMCache model_name -- identity config incomplete"
  # `/models/<name>` where <name> encodes the identity namespace (no '/' in it).
  MODEL_MOUNT="/models/$KVP_MODEL_NAME"
  MOUNTS+=(-v "$MODEL_DIR:$MODEL_MOUNT:ro")
  echo "LMCache identity namespace: $KVP_MODEL_NAME"
  echo "model mounted at: $MODEL_MOUNT (so model_config.model is namespace-coded)"
else
  MODEL_MOUNT="/model"
  MOUNTS+=(-v "$MODEL_DIR:$MODEL_MOUNT:ro")
fi
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
  # Review fix: the MiMo model patches are model-specific, not a project
  # constant. Mount them only when the configured model config is the
  # reference MiMo one; other models bring their own loader patches (if any)
  # via RECIPE_DIR. Mounting mimo_v2*.py over a different model would corrupt
  # its loader.
  case "${KVP_MODEL_CONFIG:-mimo-v2.6-pro}" in
    mimo-v2.6-pro*)
      MOUNTS+=(
        -v "$RECIPE/patches/mimo_v2_hotsplit.py:$SITE/vllm/model_executor/models/mimo_v2.py:ro"
        -v "$RECIPE/patches/mimo_v2_omni.py:$SITE/vllm/model_executor/models/mimo_v2_omni.py:ro"
      )
      ;;
    *)
      echo "KVP_MODEL_CONFIG=${KVP_MODEL_CONFIG} is not the reference MiMo config -- skipping MiMo model patches (provide your own via RECIPE_DIR if needed)"
      ;;
  esac
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
  --model "$MODEL_MOUNT" \
  --served-model-name "$KVP_SERVED_MODEL_NAME" \
  --trust-remote-code \
  --tensor-parallel-size "$KVP_TP_SIZE" \
  --block-size "$KVP_BLOCK_SIZE" \
  --disable-custom-all-reduce \
  --offload-backend uva \
  --cpu-offload-gb "$CPU_OFFLOAD_GB" \
  --cpu-offload-params routed_experts.w13_weight routed_experts.w2_weight \
  --moe-backend "$MOE_BACKEND" \
  --max-model-len "$KVP_MAX_MODEL_LEN" \
  --max-num-seqs "$MAX_NUM_SEQS" \
  --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS" \
  --kv-cache-dtype "$KVP_KV_CACHE_DTYPE" \
  --enable-prefix-caching \
  --gpu-memory-utilization "$GPU_UTIL" \
  ${KV_TRANSFER_ARGS[@]+"${KV_TRANSFER_ARGS[@]}"} \
  --generation-config vllm \
  "$PROMPT_TOKENS_DETAILS_FLAG" \
  ${VLLM_EXTRA_ARGS_ARR[@]+"${VLLM_EXTRA_ARGS_ARR[@]}"} \
  --host "$BIND" --port "$PORT"
# Post-launch identity gate (review round 13): now the container EXISTS and
# carries the args we just passed -- verify them against the identity pins
# before any cache data is trusted. INCONCLUSIVE/MISMATCH here stops the run.
if [ "$PERSIST" = "true" ]; then
  echo "-- identity gate (post-launch): verifying engine args match identity pins --"
  rc=0
  bash "$HERE/verify-engine-identity.sh" "$CONTAINER" || rc=$?
  if [ "$rc" -ne 0 ]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    if [ "$rc" -eq 5 ]; then
      die "INCONCLUSIVE: engine identity could not be verified (see output above).
Cache reuse is REFUSED by policy -- fix value sources and rerun."
    fi
    die "ENGINE IDENTITY MISMATCH (exit $rc): cache must NOT be reused. Fix
configs/${KVP_MODEL_CONFIG}/identity.env(.local) and rerun."
  fi
  echo "identity verified -- persistent cache attached and trusted"
fi
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
