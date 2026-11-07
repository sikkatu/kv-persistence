#!/usr/bin/env bash
# run-vllm-with-persist.sh -- vLLM TP4 for MiMo-V2.6-Pro WITH the persistent KV connector.
# Separate launcher of kv-persistence: NEVER edits the production recipe.
# Recipe patch files are mounted READ-ONLY; all state lives under KVP_CACHE_ROOT.
#
# Relationship to the recipe launchers (run-tp4-uva.sh / run-tp4-hotsplit.sh,
# review fix round 19): the docker args are based on the same recipe baseline,
# but NOT a byte-for-byte mirror. Review fix (round 26), HONEST SCOPE: this
# launcher runs the UVA BASELINE only -- it mounts the loader patches
# (mimo_v2*.py) but NOT the hotsplit machinery (sitecustomize.py, hotsplit.py,
# --compilation-config FULL_DECODE_ONLY, --cap-add IPC_LOCK/SYS_NICE). The KV
# values are the same as the recipe's, but the PERFORMANCE numbers measured on
# hotsplit do NOT apply to runs of THIS launcher. For the hotsplit profile use
# the recipe's run-tp4-hotsplit.sh. This launcher:
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
#     share one namespace. Review fix (round 27): operator names
#     (KV_CACHE_DTYPE, MAX_MODEL_LEN, TP_SIZE, SERVED_MODEL_NAME) are PROMOTED
#     to their KVP_* form before the engine starts, so the engine, the cache
#     namespace and the identity gate all use the SAME effective value -- the
#     old advice to "override in both paths" is no longer needed for a single
#     launch. For a DURABLE pin across restarts, set the value in
#     identity.env.local; a changed value is a SAFE cache miss (the namespace
#     changes), never a wrong read.
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
# Review fix (round 45h): same-UID mode (KVP_SAME_UID=1) runs the engine
# container as $(id -u):$(id -g) -- DYNAMIC UID, works for any user (no
# hardcoded 1000). The cache dirs move to world-writable /tmp paths because
# the non-root user cannot write /root. Without KVP_SAME_UID the container
# runs as root (the proven fallback: CONSUMER=host-root passed the
# ipc-test). Defined EARLY: the pre-flight IPC test needs it too.
# Round 48 (user): same-UID mode is the DEFAULT -- the engine container runs
# as $(id -u):$(id -g), no root anywhere, no flag to type in every terminal.
# Opt out for the root fallback: KVP_SAME_UID=0.
KVP_SAME_UID="${KVP_SAME_UID:-1}"
ENGINE_USER_ARGS=()
ENGINE_CACHE_ARGS=(-e HF_HOME=/root/.cache/huggingface)
_uid="$(id -u)"; _gid="$(id -g)"
if [ "$KVP_SAME_UID" = "1" ]; then
  ENGINE_USER_ARGS=(--user "$_uid:$_gid")
  # Round 45j (reviewer): a uid WITHOUT a passwd entry (or an unwritable
  # image home) breaks getpass.getuser() and ~-based caches (torch
  # inductor, flashinfer, vllm usage stats). Set HOME/USER/LOGNAME
  # explicitly -- insurance for ANY uid, untested for uid 1000 in this
  # image but harmless.
  _me="$(id -un 2>/dev/null || true)"; _me="${_me:-uid$_uid}"
  ENGINE_CACHE_ARGS=(-e HF_HOME=/tmp/hf-cache -e XDG_CACHE_HOME=/tmp/xdg-cache -e TRITON_CACHE_DIR=/tmp/triton-cache -e HOME=/tmp/engine-home -e "USER=$_me" -e "LOGNAME=$_me")
fi
# Review fix (round 26): try the documented clone layout first
# (~/sikkatu-recipes/recipes/...), then the plain ~/recipes layout.
if [ -n "${RECIPE_DIR:-}" ]; then
  RECIPE="$RECIPE_DIR"
elif [ -d "$HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit" ]; then
  RECIPE="$HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit"
else
  RECIPE="$HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit"
fi
# Review fix (round 34, P2): verify this checkout is a COMPATIBLE version.
KVP_COMPAT_EXPECTED="${KVP_COMPAT_VERSION:-1}"
KVP_COMPAT_FOUND=""
if [ -f "$ROOT/KVP_COMPAT_VERSION" ]; then
  KVP_COMPAT_FOUND="$(head -1 "$ROOT/KVP_COMPAT_VERSION" | tr -d '[:space:]')"
fi
if [ "$KVP_COMPAT_FOUND" != "$KVP_COMPAT_EXPECTED" ]; then
  die "kv-persistence compatibility check FAILED: expected KVP_COMPAT_VERSION=$KVP_COMPAT_EXPECTED, found '${KVP_COMPAT_FOUND:-<missing>}' at $ROOT/KVP_COMPAT_VERSION."
fi
. "$HERE/env.sh"
_kvp_load_env "$ROOT"
[ -d "$RECIPE" ] || die "production recipe not found at $RECIPE (set RECIPE_DIR)"
# Round 48 (user): IMAGE and MODEL_DIR must not need manual exports -- a new
# terminal just works. Resolution order for IMAGE:
#   1. explicit $IMAGE (unchanged contract),
#   2. live/image (what a previous launch used),
#   3. the production recipe's tp4.env.example digest (print-recipe-image.sh).
# MODEL_DIR:
#   1. explicit $MODEL_DIR / $KVP_MODEL_DIR,
#   2. live/model_dir (recorded at the first successful launch),
#   3. the identity pin KVP_MODEL_DIR from identity.env(.local),
#   4. the single model directory under ~/models (or $KVP_MODELS_ROOT).
if [ -z "${IMAGE:-}" ]; then
  [ -f "$ROOT/live/image" ] && IMAGE="$(head -n1 "$ROOT/live/image" | tr -d '[:space:]')"
fi
if [ -z "${IMAGE:-}" ]; then
  IMAGE="$(bash "$HERE/print-recipe-image.sh" "$RECIPE" 2>/dev/null)"     || die "IMAGE is not set and could not be resolved from the recipe ($RECIPE/tp4.env.example).Set it explicitly: IMAGE='vllm/vllm-openai:...@sha256:<64 hex>' bash scripts/run-vllm-with-persist.sh"
  mkdir -p "$ROOT/live"
  printf '%s
' "$IMAGE" > "$ROOT/live/image"
  echo "IMAGE resolved from the recipe and remembered: $IMAGE"
fi
IMAGE="${IMAGE:?IMAGE must be pinned with @sha256:<64 hex>}"
printf '%s' "$IMAGE" | grep -Eq '@sha256:[0-9a-f]{64}$' || die "IMAGE must be pinned by digest"

if [ -z "${MODEL_DIR:-}" ]; then
  MODEL_DIR="${KVP_MODEL_DIR:-}"
fi
if [ -z "$MODEL_DIR" ] && [ -f "$ROOT/live/model_dir" ]; then
  MODEL_DIR="$(head -n1 "$ROOT/live/model_dir" | tr -d '[:space:]')"
fi
if [ -z "$MODEL_DIR" ] && [ -n "${KVP_MODEL_CONFIG:-}" ] && [ -f "$ROOT/configs/$KVP_MODEL_CONFIG/identity.env.local" ]; then
  MODEL_DIR="$(sed -n 's/^KVP_MODEL_DIR=//p' "$ROOT/configs/$KVP_MODEL_CONFIG/identity.env.local" | tail -1 | tr -d '[:space:]')"
fi
if [ -z "$MODEL_DIR" ]; then
  _models_root="${KVP_MODELS_ROOT:-$HOME/models}"
  _cands=()
  for _d in "$_models_root"/*/; do
    [ -f "$_d/config.json" ] && _cands+=("${_d%/}")
  done
  [ "${#_cands[@]}" -eq 1 ] && MODEL_DIR="${_cands[0]}"
fi
[ -n "$MODEL_DIR" ] || die "MODEL_DIR could not be resolved automatically.Point it at the local model directory once:
  echo /path/to/model > live/model_dir
or run:  MODEL_DIR=/path/to/model bash scripts/run-vllm-with-persist.sh
(the choice is remembered in live/model_dir)."
[ -d "$MODEL_DIR" ] || die "MODEL_DIR not found: $MODEL_DIR"
mkdir -p "$ROOT/live"
[ "$(head -n1 "$ROOT/live/model_dir" 2>/dev/null)" = "$MODEL_DIR" ] || printf '%s
' "$MODEL_DIR" > "$ROOT/live/model_dir"
BIND="${BIND:-127.0.0.1}"; PORT="${PORT:-30007}"
API_KEY="${API_KEY:-}"
# Review fix: look up live/api_key under the REPO ROOT ($ROOT), not $PWD --
# the launcher must work from any working directory, not only from the repo root.
[ -f "$ROOT/live/api_key" ] && [ -z "$API_KEY" ] && API_KEY="$(head -n1 "$ROOT/live/api_key" | tr -d '[:space:]')"
# Review fix (install report): the key is AUTO-GENERATED on a loopback bind,
# the same contract as the recipe launchers. A non-loopback bind always needs
# an EXPLICIT key (fail-safe: a server on a routable address is always
# authenticated). Loopback is strictly 127.0.0.1 or ::1.
gen_key() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(24))'
  else
    head -c 24 /dev/urandom | od -An -tx1 | tr -d '[:space:]'; echo
  fi
}
LOOPBACK=0
case "$BIND" in
  127.0.0.1|\[::1\]|::1) LOOPBACK=1 ;;
esac
if [ -z "$API_KEY" ] && [ "$LOOPBACK" = "1" ]; then
  API_KEY="$(gen_key)"
  case "$API_KEY" in
    *[!0-9a-f]*|'') die "failed to generate an API key (openssl/python3/urandom all unavailable)" ;;
  esac
  [ "${#API_KEY}" -ge 32 ] || die "generated API key is too short (${#API_KEY} chars)"
  umask 077
  mkdir -p "$ROOT/live"
  printf '%s\n' "$API_KEY" > "$ROOT/live/api_key"
  chmod 600 "$ROOT/live/api_key"
  echo "API key auto-generated for the loopback server and saved to $ROOT/live/api_key"
fi
[ -n "$API_KEY" ] || die "API_KEY required for a non-loopback bind ($BIND) -- that is a public unauthenticated API.
Set API_KEY explicitly (openssl rand -hex 24) or bind loopback (BIND=127.0.0.1)."
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
# Review fix (round 27): PROMOTE explicitly-set operator names to their KVP_*
# form BEFORE the resolver, so they win over the identity.env pins that
# scripts/env.sh exported. Review fix (round 28): bare BLOCK_SIZE is NOT
# promoted (round 26 removed it everywhere -- a stray BLOCK_SIZE=64 in the
# environment must not change --block-size; KV_PERSIST_BLOCK_SIZE is the knob).
# scripts/env.sh exported. Without this a `KV_CACHE_DTYPE=fp8` /
# `MAX_MODEL_LEN=524288` / `TP_SIZE=2` was silently ignored (the engine ran the
# pinned identity.env value) while the recipe launcher honoured it -- two
# "identical" commands started different engines (and an operator lowering the
# context to avoid OOM got the full 1M). The gate + namespace follow the same
# promoted values (single source of truth).
for _pair in "KV_CACHE_DTYPE:KVP_KV_CACHE_DTYPE" "MAX_MODEL_LEN:KVP_MAX_MODEL_LEN" \
             "TP_SIZE:KVP_TP_SIZE" "SERVED_MODEL_NAME:KVP_SERVED_MODEL_NAME" \
             "KV_PERSIST_BLOCK_SIZE:KVP_BLOCK_SIZE"; do
  _op="${_pair%%:*}"; _kp="${_pair##*:}"
  if [ -n "${!_op:-}" ]; then
    if [ -n "${!_kp:-}" ] && [ "${!_kp}" != "${!_op}" ]; then
      echo "NOTE: $_op=${!_op} overrides the pinned $_kp=${!_kp} (identity.env)" >&2
      echo "      engine, namespace and gate will all use $_op=${!_op}." >&2
    fi
    export "$_kp=${!_op}"
  fi
done
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
# Review fix (round 41): a LIVE pid is not a WORKING server -- in the install
# run the pid file checked out fine but the server never answered
# register_kv_caches (300s wasted inside the container). Real liveness is a
# TCP connect to the request port; the pid file stays as a fast negative
# filter for the "not started at all" case.
# Review fix (round 45j): this check must be under PERSIST=true -- the
# comment below promises BOTH checks are skipped when the persistent KV
# connector is disabled, and without the connector there is no cache
# server to wait for. Moved into the PERSIST block.
KVP_PIDF="$PIDF"
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
  # Round 45j (reviewer): the cache-server check lives HERE, under
  # PERSIST=true -- the comment below promises both checks are skipped
  # when the persistent KV connector is disabled.
  if [ ! -f "$KVP_PIDF" ] || ! kill -0 "$(cat "$KVP_PIDF" 2>/dev/null)" 2>/dev/null; then
    die "lmcache server not running -- start it first: bash scripts/start-cache.sh"
  fi
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
  # Review fix (round 41): PRE-FLIGHT before docker run -- both failures used
  # to surface only AFTER the 13-minute model load (install run: 24 min lost
  # to "LMCache server did not respond to register_kv_caches within 300s").
  # 1) the codec must match the RUNNING server's rendered adapter;
  # 2) the cache server must be REACHABLE at all;
  # 3) the lmcache version inside the image must match the host's (round 42:
  #    the image and the pinned host venv must match -- mixed
  #    minor versions break the MP protocol silently at register_kv_caches).
  _pre_root="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}"
  _pre_adapter="$_pre_root/l2-adapter.rendered.json"
  _pre_expect="$(printf '%s' "${LMCACHE_L2_SERDE:-none}" | tr '[:upper:]' '[:lower:]')"
  case "$_pre_expect" in raw|"") _pre_expect=none ;; esac
  if [ -f "$_pre_adapter" ]; then
    _pre_seen="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); s=d.get("serde") or {}; print((s.get("type") or "none").replace("kvp-",""))' "$_pre_adapter" 2>/dev/null || echo UNREADABLE)"
    case "$_pre_seen" in raw|"") _pre_seen=none ;; esac
    if [ "$_pre_seen" = "UNREADABLE" ]; then
      echo "WARNING: $_pre_adapter exists but is not valid JSON -- cannot pre-check the server codec" >&2
    elif [ "$_pre_seen" != "$_pre_expect" ]; then
      die "L2 CODEC MISMATCH (pre-flight): this launch would use '$_pre_expect' but the RUNNING
cache server stores '$_pre_seen' (see $_pre_adapter). The codec is part of the cache
identity -- the engine and the server would disagree about which chunks are readable.
Fix ONE side to match the other and rerun:
  server on zstd:    USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh
  launch on zstd:    USE_LOSSLESS_SERDE=1 bash scripts/run-vllm-with-persist.sh
  or restart the server WITHOUT the switch (raw/none) and relaunch plain."
    else
      echo "pre-flight: L2 codec matches the running server ($_pre_seen)"
    fi
  else
    echo "NOTE: no $_pre_adapter -- the cache server may not be started yet (the post-launch gate will insist on it)"
  fi
  if command -v python3 >/dev/null 2>&1; then
    if ! python3 - "$LMCACHE_HOST" "$LMCACHE_PORT" <<'PYEOF'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
try:
    s.connect((host, port))
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PYEOF
    then
      die "LMCache cache server is NOT reachable at $LMCACHE_HOST:$LMCACHE_PORT (pre-flight).
Starting vLLM now would waste a 13-minute model load and then fail with
'LMCache server did not respond to register_kv_caches within 300s'.
Start the cache service first:
  bash scripts/start-cache.sh
(and USE_LOSSLESS_SERDE=1 on BOTH sides if you want lossless compression)."
    fi
    echo "pre-flight: cache server is listening at $LMCACHE_HOST:$LMCACHE_PORT"
  fi
  # Review fix (round 42): the MP-connector protocol is version-sensitive.
  # The CLIENT lives inside the vLLM image; the SERVER runs from the host
  # venv (requirements-lmcache.txt). A minor-version mismatch makes
  # register_kv_caches silently time out after 300s inside the container.
  # Round 49: use the venv python (the system python3 has no lmcache).
  _host_lmcache="$("$(_kvp_python)" -c 'import importlib.metadata; print(importlib.metadata.version("lmcache"))' 2>/dev/null || echo UNKNOWN)"
  _img_lmcache="$(docker run --rm --entrypoint python3 "$IMAGE" -c 'import importlib.metadata; print(importlib.metadata.version("lmcache"))' 2>/dev/null || echo UNKNOWN)"
  if [ "$_host_lmcache" != "UNKNOWN" ] && [ "$_img_lmcache" != "UNKNOWN" ]; then
    if [ "$_host_lmcache" != "$_img_lmcache" ]; then
      die "LMCACHE VERSION MISMATCH (pre-flight): the host serves lmcache $_host_lmcache
but the vLLM image ships lmcache $_img_lmcache. Mixed minor versions break the
MP-connector protocol -- register_kv_caches will silently time out (300s) inside
the container. Align both sides to the SAME version:
  host (server):  ~/.venvs/lmcache/bin/pip install lmcache==$_img_lmcache
  or pin the image to one that ships lmcache==$_host_lmcache.
  (requirements-lmcache.txt pins the host side; check docs/compatibility.md.)"
    else
      echo "pre-flight: lmcache version matches on both sides ($_host_lmcache)"
    fi
  else
    echo "NOTE: could not determine lmcache version (host=$_host_lmcache, image=$_img_lmcache) -- skipping version check"
  fi
  # Review fix (round 44): the TCP probe proves only that a socket is
  # open. The failing handshake (register_kv_caches) rides the lmcache RPC
  # protocol -- so probe THAT protocol here, via kvpersist.mp_ping (the
  # same RequestClient the vLLM connector uses). A server that accepts
  # TCP but never answers RPC is refused BEFORE docker run.
  _ping_out="$(PYTHONPATH="$ROOT/implementation" python3 -m kvpersist.mp_ping "$LMCACHE_HOST" "$LMCACHE_PORT" 10 2>&1)" || _ping_rc=$?
  _ping_rc="${_ping_rc:-0}"
  case "$_ping_rc" in
    0)
      echo "  $_ping_out" ;;
    3)
      echo "  $_ping_out"
      echo "  NOTE: lmcache not importable on the host python -- protocol ping skipped" ;;
    *)
      echo "ERROR: the cache server did not answer the RPC ping (register_kv_caches would hang):" >&2
      echo "  $_ping_out" >&2
      echo "  The server accepted TCP on $LMCACHE_HOST:$LMCACHE_PORT but its RPC channel is dead." >&2
      echo "  Fix: restart the cache service (bash scripts/stop-cache.sh && bash scripts/start-cache.sh)" >&2
      echo "       and check its log: tail -40 ${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}/lmcache-server.log" >&2
      exit 1 ;;
  esac
  # Review fix (round 45): the RPC ping does NOT prove the CUDA IPC
  # boundary -- register_kv_caches died on cuIpcOpenEventHandle
  # (CUDA_ERROR_MAP_FAILED: the host could not map GPU memory created in
  # the container). Reproduce EXACTLY that step here, before the model
  # load. Consumer mode matches how the cache server runs.
  if [ "${KVP_SKIP_IPC_TEST:-0}" = "1" ]; then
    echo "pre-flight: CUDA IPC test SKIPPED (KVP_SKIP_IPC_TEST=1)"
  elif [ -x "$HERE/ipc-test.sh" ]; then
    # Round 45f: the consumer must match the SERVER PROCESS user, not just
    # KVP_LAUNCH_MODE -- a sudo-started host server runs as root and needs
    # the host-root consumer (the ipc-test proved root is the fix).
    _ipc_mode=host
    _ipc_pidf="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}/lmcache-server.pid"
    if [ -f "$_ipc_pidf" ]; then
      _ipc_pid="$(tr -d '[:space:]' < "$_ipc_pidf")"
      _ipc_uid="$(awk '/^Uid:/{print $2}' "/proc/$_ipc_pid/status" 2>/dev/null || echo '')"
      if [ "$_ipc_uid" = "0" ]; then
        _ipc_mode=host-root
      fi
    fi
    if [ "${KVP_LAUNCH_MODE:-host}" = "docker" ]; then _ipc_mode=container; fi
    echo "pre-flight: CUDA IPC boundary test (consumer=$_ipc_mode) -- can the cache"
    echo "            server open GPU memory created in the container? (~1 min)"
    _ipc_pu=""
    if [ "$KVP_SAME_UID" = "1" ]; then
      _ipc_pu="$_uid:$_gid"
    fi
    # Round 45i: ${var:+NAME=val} is NOT an assignment in bash -- the expanded
    # word becomes a command name ("PRODUCER_USER=NNNN:NNNN: command not found").
    # Pass the assignment LITERALLY; an empty value is the script default (root
    # producer), so no conditional expansion is needed at all.
    if IMAGE="$IMAGE" CONSUMER="$_ipc_mode" PRODUCER_USER="$_ipc_pu" bash "$HERE/ipc-test.sh"; then
      echo "pre-flight: CUDA IPC boundary OK (consumer=$_ipc_mode${_ipc_pu:+, producer UID=$_ipc_pu})"
    else
      die "CUDA IPC BOUNDARY FAILED (pre-flight): the cache server cannot open
GPU memory created in the container -- this is the EXACT step that killed
register_kv_caches (CUDA_ERROR_MAP_FAILED). The model load would waste 20
minutes and fail the same way. Fix the boundary FIRST:
  - run the cache server in the container (same user/root as the engine):
      KVP_LAUNCH_MODE=docker USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh
    then rerun with KVP_LAUNCH_MODE=docker so this test uses the container consumer;
  - or run the cache server as root (same user as the engine container).
Isolate the blocker: CONSUMER=host-root vs CONSUMER=container in ipc-test.sh.
To bypass this check: KVP_SKIP_IPC_TEST=1 (NOT recommended -- the failure returns)."
    fi
  else
    echo "NOTE: scripts/ipc-test.sh not found -- CUDA IPC boundary test skipped"
  fi
else
  echo "KVP_PERSIST_ENABLED=$PERSIST -- launching WITHOUT persistent KV connector (--kv-transfer-config omitted)"
fi
# Review fix (round 45h): MOUNTS follows the same-UID mode defined above.
if [ "$KVP_SAME_UID" = "1" ]; then
  echo "same-UID mode: engine runs as $_uid:$_gid (no root)"
  MOUNTS=(-v "$HF_CACHE:/tmp/hf-cache")
else
  MOUNTS=(-v "$HF_CACHE:/root/.cache/huggingface")
fi
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
  # Review fix (round 46): persist the EFFECTIVE codec + namespace next to
  # the cache. The codec lives in a shell variable (USE_LOSSLESS_SERDE)
  # that had to be repeated in EVERY terminal -- a plain
  # `bash scripts/verify-engine-identity.sh` then reported
  # l2_serde: identity=none != engine/config=zstd and refused cache reuse.
  # The file is the source of truth for standalone scripts (they read it
  # when neither the switch nor the identity pin is set).
  _eid_root="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}"
  if [ -w "$_eid_root" ]; then
    {
      echo "# Written by run-vllm-with-persist.sh at engine launch (round 46)."
      echo "# The EFFECTIVE cache identity of the running engine."
      echo "# Standalone scripts (verify-engine-identity.sh, test-recovery.sh,"
      echo "# benchmark.sh) read this file when neither USE_LOSSLESS_SERDE nor"
      echo "# the KVP_L2_SERDE identity pin is set. Regenerated on every launch."
      echo "KVP_L2_SERDE=${LMCACHE_L2_SERDE:-none}"
      echo "KVP_MODEL_NAME=$KVP_MODEL_NAME"
    } > "$_eid_root/engine-identity.env"
    echo "engine identity written: $_eid_root/engine-identity.env (KVP_L2_SERDE=${LMCACHE_L2_SERDE:-none})"
  fi
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
# Review fix (round 47): the GPU check runs AFTER the old container is
# removed -- round 46 ran it earlier and refused on the OLD engine's legal
# VRAM (78 GiB per GPU on the user's host). With the engine gone the GPUs
# must be free; anything still holding VRAM is a leaked process that would
# break the new engine's CUDA allocations. Applies to BOTH launch modes.
if [ "${KVP_SKIP_GPU_CHECK:-0}" != "1" ]; then
  _kvp_gpu_preflight 2048 || die "GPU memory is still held after the old engine was removed -- leaked processes block the new engine (see the PIDs above). Fix: stop the cache service too (bash scripts/stop-cache.sh && bash scripts/start-cache.sh) and rerun."
fi
echo "starting $CONTAINER with persistent KV connector (lmcache $LMCACHE_HOST:$LMCACHE_PORT)"
docker run -d --name "$CONTAINER" --restart no \
  --gpus all --ipc host --network host \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  ${ENGINE_USER_ARGS[@]+"${ENGINE_USER_ARGS[@]}"} \
  "${ENGINE_CACHE_ARGS[@]}" \
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
# Review fix (round 45): fail fast. The engine waits 300s for
# register_kv_caches, but the server may have ALREADY failed with
# 'Error handling operation' (e.g. CUDA_ERROR_MAP_FAILED) -- waiting out
# the timeout cannot fix it. Track the server log size BEFORE the wait and
# scan only the FRESH part each tick.
_kvp_slog="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}/lmcache-server.log"
_kvp_slog_before=0
if [ -f "$_kvp_slog" ]; then _kvp_slog_before=$(wc -c < "$_kvp_slog"); fi
for _ in $(seq 1 300); do
  # fail fast (round 45): a fresh server-side handler error is fatal
  if [ -f "$_kvp_slog" ]; then
    _kvp_serr="$(tail -c "+$(( _kvp_slog_before + 1 ))" "$_kvp_slog" 2>/dev/null | grep -m1 'Error handling operation' || true)"
    if [ -n "$_kvp_serr" ]; then
      echo "" >&2
      echo "ERROR: the cache server FAILED to handle a request (round 45 fail-fast):" >&2
      tail -c "+$(( _kvp_slog_before + 1 ))" "$_kvp_slog" | grep -A20 'Error handling operation' | head -25 >&2
      echo "" >&2
      echo "This is the root cause -- waiting 300s cannot fix it. See the exception above." >&2
      echo "Fix (CUDA IPC boundary, review round 45): run the cache server in the same" >&2
      echo "environment as the engine (root/container):" >&2
      echo "  KVP_LAUNCH_MODE=docker USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh" >&2
      echo "or test the boundary first: bash scripts/ipc-test.sh" >&2
      exit 1
    fi
  fi
  STATE="$(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' "$CONTAINER" 2>/dev/null || echo gone)"
  case "$STATE" in running*) : ;; *)
    echo "ERROR: container not running ($STATE)"; docker logs --tail 100 "$CONTAINER" >&2
    # Review fix (round 44): the register_kv_caches timeout gives no clue
    # WHERE the handshake stuck. Dump the cache server's state right here:
    # alive? serving? what did it log? This replaces manual forensics.
    _kvp_diag="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}"
    echo "" >&2
    echo "---- cache server diagnostics (automatic) ----" >&2
    _kvp_pidf="$_kvp_diag/lmcache-server.pid"
    if [ -f "$_kvp_pidf" ]; then
      _kvp_pid="$(tr -d '[:space:]' < "$_kvp_pidf")"
      if kill -0 "$_kvp_pid" 2>/dev/null; then
        echo "server process: ALIVE (pid $_kvp_pid)" >&2
        echo "server cmdline: $(tr '\0' ' ' < "/proc/$_kvp_pid/cmdline" 2>/dev/null | head -c 300)" >&2
      else
        echo "server process: DEAD (stale pid file, pid $_kvp_pid)" >&2
      fi
    else
      echo "server process: no pid file at $_kvp_pidf" >&2
    fi
    _kvp_log="$_kvp_diag/lmcache-server.log"
    if [ -f "$_kvp_log" ]; then
      _kvp_lines=$(wc -l < "$_kvp_log")
      _kvp_fresh=$(find "$_kvp_log" -newermt '-5 minutes' 2>/dev/null | wc -l)
      echo "server log: $_kvp_log ($_kvp_lines lines, $([ "$_kvp_fresh" -gt 0 ] && echo 'written within 5 min' || echo 'NO writes in the last 5 min'))" >&2
      # Review fix (round 45): show the server ERROR first -- the previous
      # "last 30 lines" was eviction noise that hid the real exception.
      _kvp_err="$(grep -n 'Error handling operation' "$_kvp_log" | tail -n 1 || true)"
      if [ -n "$_kvp_err" ]; then
        echo "--- server ERROR (root cause) ---" >&2
        _kvp_errline="${_kvp_err%%:*}"
        sed -n "${_kvp_errline},$(( _kvp_errline + 25 ))p" "$_kvp_log" >&2 || true
      else
        echo "--- no 'Error handling operation' in the server log ---" >&2
      fi
      echo "--- last 30 server log lines (filtered) ---" >&2
      grep -v 'skipping eviction' "$_kvp_log" | tail -n 30 >&2 || true
      echo "--- [kvp] status lines ---" >&2
      grep '\[kvp\]' "$_kvp_log" | tail -n 5 >&2 || echo "(none)" >&2
    else
      echo "server log: missing ($_kvp_log)" >&2
    fi
    echo "---- end cache server diagnostics ----" >&2
    exit 1 ;; esac
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
