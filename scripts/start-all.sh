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
# r24 F3: resolve the launch mode ONCE, early (default docker) and export it
# so the recipe launcher's own `:-host` default can never flip it back. The
# bare `bash scripts/start-all.sh` used to run the server in docker (the
# start-cache default) while the engine-side checks ran in the host branch ->
# a false "LMCACHE VERSION MISMATCH" refusal at step 3/3.
KVP_LAUNCH_MODE="$(_kvp_resolve_launch_mode)" || exit 1
# r25 G3: the assignment above runs the resolver in a SUBSHELL -- its own
# `export` is lost there. Without THIS line the recipe launcher (whose
# default is host) never sees the resolved value.
export KVP_LAUNCH_MODE
# r20 K1: remember whether IMAGE came from the ENVIRONMENT before tp4.env is
# seeded (the seed exports IMAGE=stock from tp4.env as the base pin -- the
# NOTE wording distinguishes the two sources).
_kvp_image_from_env=0
[ -n "${IMAGE:-}" ] && _kvp_image_from_env=1
# tp4.env seeds FIRST (its values beat the config files), then the config layer.
for _tp4 in "${RECIPE_DIR:-}/tp4.env" "$HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/tp4.env" "$HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/tp4.env"; do
  [ -f "$_tp4" ] && { _kvp_seed_env_file "$_tp4"; break; }
done
_kvp_load_env "$ROOT"

# r20 K1: ONE fail-fast resolution of the RUNTIME image BEFORE anything is
# started (step 1/3 observability must NOT come up before the launch is
# proven -- a refusal after 6 containers are up is a side effect for
# nothing). The stock IMAGE= from tp4.env is the BASE pin: it must match the
# base of the custom image recorded in live/image; the launch always runs
# the CUSTOM one. Refusal here exits before the first docker call.
# r20 K1 fix (round-81b regression): when persistence is OFF
# (KVP_PERSIST_ENABLED=false -- a stock recipe run without KV_PERSIST) the
# IMAGE question must NOT block the launch at all (contract: "при
# KV_PERSIST!=1 IMAGE не трогать"). The custom-image deep check applies only
# to the persistence path, and it runs in the recipe branch BEFORE step 1/3.
if [ "${KVP_PERSIST_ENABLED:-true}" = "false" ]; then
  echo "NOTE: KVP_PERSIST_ENABLED=false -- persistence is off, the runtime image is NOT resolved/checked (KV_PERSIST!=1 path)"
else
  _kvp_resolve_runtime_image "$ROOT" "$_kvp_image_from_env" || {
    echo "ERROR: runtime image resolution failed -- refusing to start (nothing was started)" >&2
    exit 1
  }
fi

# ONE codec decision (review round 58): resolve the effective L2 codec ONCE
# here and pass the SAME value to both children. start-cache.sh and
# the engine then never re-decides, so an lz4 (or raw) pin can
# never split the cache server and the engine into two codecs.
# Round 63 (review): seed the recipe's tp4.env BEFORE resolving the codec --
# a USE_LOSSLESS_SERDE=0 (or LMCACHE_L2_SERDE=lz4) set in tp4.env must reach the
# single codec decision, otherwise it is silently ignored (the engine launcher
# seeds later, after the value is already exported).
_kvp_resolve_l2_serde
EFFECTIVE_SERDE="${LMCACHE_L2_SERDE:-zstd}"
export LMCACHE_L2_SERDE="$EFFECTIVE_SERDE"
echo "codec: $EFFECTIVE_SERDE (resolved once, used by cache and engine)"
# r89c (stale-marker fix #2): wipe any marker live/extras_started BEFORE
# step 1/3. A marker leftover from a PREVIOUS -- possibly manual --
# start-observability.sh run must NEVER mute the extras deploy of THIS run
# (scenario: operator runs start-observability.sh by hand, then edits
# KVP_OBS_GPU_PROF and reruns start-all.sh with the base stack already up;
# start-observability.sh is skipped entirely, and a marker found below would
# wrongly signal "already deployed in this run").
# The marker written later, by start-observability.sh in step 1/3, is the
# ONLY marker that may silence this run's extras block -- it is created
# strictly after this wipe.
# -- extras marker pre-clean: BEGIN (tests extract and EXECUTE this block) --
rm -f "$ROOT/live/extras_started" 2>/dev/null || true
# -- extras marker pre-clean: END --
echo "== 1/3 observability (Grafana on :3000) =="
# Round 81 (review): the check used to look at obs_grafana only -- a running
# Grafana with a dead Prometheus/OTel silently skipped the whole stack. Now
# ALL services are checked and the compose file is (re)applied idempotently:
# a missing component is started, nothing running is touched down.
_obs_all=1
for _svc in obs_otel_collector obs_tempo obs_prometheus obs_grafana; do
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$_svc" || _obs_all=0
done
if [ "$_obs_all" = "1" ]; then
  echo "observability already running (all 4 services) -- skipping"
else
  bash "$HERE/start-observability.sh" || echo "WARNING: observability failed to start -- continuing without metrics (the cache works fine without it)"
fi
# Round 81 (review): record that THIS start-all launched the stack, so
# stop-all.sh stops it only when we own it (a foreign docker compose stack
# on the same host must not be touched).
# Round 83 (user report): the marker used to be written ONLY when the stack
# was just started -- if it was already up (earlier run / other script) the
# marker stayed absent and stop-all.sh refused to stop OUR OWN stack, and the
# next start-all "skipped" it again. Now the marker is written whenever the
# running stack is PROVEN ours (compose label working_dir) or we just
# started it, so the pair stop-all/start-all stays consistent.
# -- obs ownership marker: BEGIN (tests extract and EXECUTE this block) --
mkdir -p "$ROOT/live"   # live/ is gitignored -- absent on a fresh clone
# Round 83 (fixed twice after the behavioural stand obs-ownership-stand.sh):
# the marker is written when the running stack is PROVEN ours by the compose
# label (pwd -P on BOTH sides -- a symlink must not flip us to "foreign", the
# old label compare used logical pwd) or when this run just started it.
_obs_ours=0
[ "$_obs_all" = "0" ] && _obs_ours=1
if [ "$_obs_ours" = "0" ]; then
  _obs_wd=""
  for _svc in obs_grafana obs_prometheus obs_tempo obs_otel_collector; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$_svc"; then
      _obs_wd="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$_svc" 2>/dev/null || true)"
      [ -n "$_obs_wd" ] && break
    fi
  done
  if [ -n "$_obs_wd" ]; then
    [ -e "$_obs_wd" ] && _obs_wd="$(cd "$_obs_wd" 2>/dev/null && pwd -P || echo "$_obs_wd")"
    _obs_dir="$(cd "$HERE/../observability" 2>/dev/null && pwd -P || true)"
    [ -n "$_obs_dir" ] && [ "$_obs_wd" = "$_obs_dir" ] && _obs_ours=1
  fi
fi
if [ "$_obs_ours" = "1" ]; then
  printf '%s\n' "1" > "$ROOT/live/obs_started" 2>/dev/null || true
else
  rm -f "$ROOT/live/obs_started" 2>/dev/null || true
fi
# -- obs ownership marker: END --
# -- extras deploy: BEGIN (tests extract and EXECUTE this block) --
# Round 87 (part B) + r88 review M1: extras layer (kvp-inference dashboard,
# vLLM relay, optional GPU exporters). Runs whenever the stack is OURS (fresh
# start or owned update path). A FOREIGN stack (different clone) is NEVER
# touched -- both clones share the compose project name `observability`, so
# letting these two compose invocations race would recreate each other's
# containers. On foreign ownership: log one line and skip.
# NOTE (r88 M2): the FIRST extra-enabled run DOES recreate base containers
# obs_prometheus and obs_grafana once -- compose must remount the freshly
# added volumes. Their DATA volumes are untouched and survive; after that
# one-time restart, subsequent `up -d` is a no-op for unchanged services.
# r89 (stale-marker fix): consume the extras_started marker UNCONDITIONALLY
# before any branching -- no matter whether the stack is ours, foreign, or
# extras are disabled. A marker left behind (manual start-observability.sh,
# foreign stack, KVP_OBS_EXTRAS=0 half-run) must NEVER mute a later deploy:
# that is how "change KVP_OBS_GPU_PROF and the first start silently keeps
# the old wiring" happened. The marker only ever means "extras ran inside
# THIS start-all.sh, started by start-observability.sh earlier in step 1/3".
_extras_by_obs=0
if [ -f "$ROOT/live/extras_started" ]; then
  _extras_by_obs=1
  rm -f "$ROOT/live/extras_started"
fi
if [ "${KVP_OBS_EXTRAS:-1}" != "0" ]; then
  if [ "$_obs_ours" = "1" ]; then
    if [ "$_extras_by_obs" = "1" ]; then
      echo "[extras] already deployed by start-observability.sh in this run (L4) -- skipping"
    else
      bash "$HERE/extras-up.sh" 2>&1 || \
        echo "[extras] extras-up.sh failed (base stack unaffected)" >&2
    fi
  else
    echo "[extras] observability stack belongs to a DIFFERENT clone (foreign compose label) -- extras not deployed from here."
  fi
elif [ "${KVP_OBS_EXTRAS:-}" = "0" ]; then
  echo "[extras] disabled (KVP_OBS_EXTRAS=0) -- base stack retained as-is"
fi
# -- extras deploy: END --

echo "== 2/3 persistent cache service =="
# Probe the EFFECTIVE cache root (from cache.env[.local]), not the default.
if [ -f "$KVP_CACHE_ROOT/lmcache-server.pid" ] && \
   kill -0 "$(cat "$KVP_CACHE_ROOT/lmcache-server.pid" 2>/dev/null)" 2>/dev/null; then
  echo "cache service already running at $KVP_CACHE_ROOT -- skipping"
else
  bash "$HERE/start-cache.sh"
fi

echo "== 3/3 vLLM engine (model load takes ~13-16 min; Ctrl-C stops the launcher, use bash scripts/stop-engine.sh to stop the engine) =="
# Round 80b (cleanup after the hardware validation PASSED 2026-11-10): the
# recipe launcher (run-tp4-hotsplit.sh with KV_PERSIST=1) is the ONE engine
# path now. The legacy kv launcher (run-vllm-with-persist.sh) is DELETED --
# every shared check (identity gate, pre-flights, API key, same-UID, hotsplit
# budget, container-name record) lives in the shared modules both launchers
# called: env.sh / hotsplit_env.sh / verify-engine-identity.sh. Nothing is
# lost with the old script.
# Round 80 review (LIVE_DIR drift): the recipe launcher defaults LIVE_DIR to
# $PWD/live, so a start-all.sh from ANY other directory wrote the state
# (live/image, live/api_key mirror, live/engine_container) to the wrong place.
# Export ONE canonical LIVE_DIR -- the repo's own live/ dir.
export LIVE_DIR="$ROOT/live"
mkdir -p "$LIVE_DIR"
KVP_ENGINE="${KVP_ENGINE:-recipe}"
case "$KVP_ENGINE" in
  kv)
    echo "ERROR: KVP_ENGINE=kv was REMOVED in round 80 -- the legacy kv launcher" >&2
    echo "(run-vllm-with-persist.sh) is deleted. Use the default recipe engine:" >&2
    echo "  bash scripts/start-all.sh            (KVP_ENGINE=recipe is the default)" >&2
    echo "Its checks were NOT lost: identity gate, pre-flights, API-key, same-UID" >&2
    echo "and the hotsplit budget live in env.sh / hotsplit_env.sh (shared modules)." >&2
    exit 1
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
    # Round 78 (review 1): CONTAINER= from the environment WINS -- the old
    # code overwrote it with the KVP_ENGINE_CONTAINER default, so
    # `CONTAINER=my-custom KVP_ENGINE=recipe` silently launched the wrong
    # name. Priority: CONTAINER (explicit) > KVP_ENGINE_CONTAINER > default.
    KVP_ENGINE_CONTAINER="${CONTAINER:-${KVP_ENGINE_CONTAINER:-mimo-pro-tp4-hotsplit}}"
    # Round 71 (review probe A): the SAME hot-bank default as the kv path --
    # an empty HOTSPLIT_HOT_GIB with full UVA offload plans an EMPTY hot set
    # ("the split is a no-op"); the split must not be silently disabled here.
    export HOTSPLIT_HOT_GIB="$(_kvp_hotsplit_hot_gib_default)"
    [ -n "$HOTSPLIT_HOT_GIB" ] || { echo "ERROR: HOTSPLIT_HOT_GIB resolved empty -- refusing the delegated launch" >&2; exit 1; }
    mkdir -p "$ROOT/live"
    printf '%s\n' "$KVP_ENGINE_CONTAINER" > "$ROOT/live/engine_container"
    # Round 78: the same name goes to the recipe launcher (was exported as
    # CONTAINER= below -- now the variable already carries the effective name).
    export CONTAINER="$KVP_ENGINE_CONTAINER"

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

    # ---- KV_CACHE_DTYPE (checklist, round 82 honest wording): a value from
    # tp4.env WINS (the recipe's default there is `auto`), so this is NOT a
    # forced pin -- it is a fallback when nothing is set. `auto` resolves to
    # the model dtype (bfloat16 for MiMo) via the identity gate, and the
    # RESOLVED value is what the cache namespace hashes; switching values is
    # a safe cache miss, never a wrong read.
    export KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-bfloat16}"

    # ---- the recipe's KV_PERSIST block needs the cache endpoint: pass the
    # SAME host/port the cache service listens on (the single codec decision
    # above is already exported as LMCACHE_L2_SERDE).
    export KV_PERSIST=1
    # r90 C1: if a previous engine container exists and is NOT running (crash
    # leftover -- the round-90 EngineCore assert kill), snapshot its logs +
    # state BEFORE this launch recreates it. Never blocks the launch.
    if [ -n "${KVP_ENGINE_CONTAINER:-}" ] && command -v docker >/dev/null 2>&1; then
      _prev_state="$(docker inspect -f '{{.State.Status}}' "$KVP_ENGINE_CONTAINER" 2>/dev/null || echo none)"
      if [ "$_prev_state" = "exited" ] || [ "$_prev_state" = "dead" ] || [ "$_prev_state" = "restarting" ]; then
        echo "previous engine container $KVP_ENGINE_CONTAINER was $_prev_state -- collecting crash evidence before relaunch (r90 C1)"
        bash "$HERE/collect-crash-evidence.sh" "$KVP_ENGINE_CONTAINER" 2>&1 | sed 's/^/  /'
      fi
    fi
    # Round 81 (review): wire the SHARED MODEL_DIR resolver into the delegated
    # path -- the wiki promises zero-config MODEL_DIR (explicit > live/model_dir
    # > identity pin > the single model dir under ~/models) but the new engine
    # path never called _kvp_resolve_model_dir. Keep the explicit-value
    # priority: an operator-set MODEL_DIR/KVP_MODEL_DIR always wins.
    # Round 81b (review): an operator-set KVP_MODEL_DIR must become MODEL_DIR
    # -- the recipe launcher reads MODEL_DIR only, so a bare KVP_MODEL_DIR was
    # silently ignored. Explicit priority: MODEL_DIR > KVP_MODEL_DIR > auto.
    if [ -n "${KVP_MODEL_DIR:-}" ] && [ -z "${MODEL_DIR:-}" ]; then
      export MODEL_DIR="$KVP_MODEL_DIR"
      echo "MODEL_DIR taken from KVP_MODEL_DIR: $MODEL_DIR"
    fi
    if [ -z "${MODEL_DIR:-}" ]; then
      _resolved_model="$(_kvp_resolve_model_dir "$ROOT")"
      if [ -n "$_resolved_model" ] && [ -d "$_resolved_model" ]; then
        export MODEL_DIR="$_resolved_model"
        echo "MODEL_DIR resolved: $MODEL_DIR (override: MODEL_DIR=... or live/model_dir)"
      else
        echo "ERROR: MODEL_DIR is not set and could not be auto-resolved." >&2
        echo "  Tried: live/model_dir -> identity pin -> the single model dir under ${KVP_MODELS_ROOT:-\$HOME/models}" >&2
        echo "  Fix: export MODEL_DIR=<your-model-dir> (or run scripts/setup-identity.sh) and rerun." >&2
        exit 1
      fi
    fi
    # -- MODEL_DIR remember: BEGIN (tests extract and EXECUTE this block) --
    # Round 83 (review): live/model_dir was READ but never WRITTEN -- the
    # chain live/model_dir > identity pin > ~/models relied on a file nobody
    # wrote. Remember the EFFECTIVE MODEL_DIR here: covers both an explicit
    # value and the auto-resolved one.
    # Round 85 (review) SEMANTICS: this is a "LAST USED" memory, not a
    # pin written once. A later run with a different explicit MODEL_DIR
    # updates it -- the docs say exactly that ("the last MODEL_DIR used"),
    # so no run is silently hijacked by an undocumented rule. An explicit
    # MODEL_DIR wins for that run either way.
    if [ -n "${MODEL_DIR:-}" ]; then
      if [ -f "$ROOT/live/model_dir" ] && \
         [ "$(tr -d '[:space:]' < "$ROOT/live/model_dir")" = "$MODEL_DIR" ]; then
        :   # already remembered -- do not touch the file every run
      else
        mkdir -p "$ROOT/live"
        printf '%s\n' "$MODEL_DIR" > "$ROOT/live/model_dir" 2>/dev/null \
          || echo "WARNING: cannot write live/model_dir -- next runs will re-resolve" >&2
      fi
    fi
    # -- MODEL_DIR remember: END --
    export KV_PERSIST_HOST="${KV_PERSIST_HOST:-${LMCACHE_HOST:-127.0.0.1}}"
    export KV_PERSIST_PORT="${KV_PERSIST_PORT:-${LMCACHE_PORT:-5555}}"
    # Round 77 (review): resolve the pinned image the same way the kv launcher
    # does (live/image) -- ipc-test.sh needs it.
    # r20 K1: the runtime image is ALREADY resolved by _kvp_resolve_runtime_image
    # (fail-fast, before step 1/3) -- steps 2 and 3 must NOT decide the image
    # question on their own. Only assert the resolution is present here.
    [ -n "${IMAGE:-}" ] || die "IMAGE is empty after _kvp_resolve_runtime_image -- internal error (r20 K1)"

    # Round 77 (review): the SAME pre-flights the kv launcher runs before
    # docker run -- lmcache RPC ping + CUDA IPC boundary test. Without them the
    # delegated path spends ~15 min loading 130 shards and dies at
    # register_kv_cache (CUDA_ERROR_MAP_FAILED) with no hint before the load.
    _kvp_pre_flights "$ROOT" "$KV_PERSIST_HOST" "$KV_PERSIST_PORT" \
      "${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}" "$IMAGE" \
      "$(id -u)" "$(id -g)" "${KVP_SAME_UID:-1}" "${KVP_LAUNCH_MODE:-docker}" || {
        # r20 K3: the cache server was ALREADY started at step 2 -- 'nothing
        # was started' is false here. stop-cache.sh is only needed when the
        # CODEC changes; a rerun with the same codec reuses the running cache.
        echo "ERROR: pre-flight failed -- the engine was NOT started (the cache service" >&2
        echo "  from step 2/3 remains running; a rerun with the same codec reuses it)." >&2
        echo "  Run stop-cache.sh first ONLY if you are changing the codec." >&2
        exit 1
      }

    # Round 80 review: the recipe launcher runs the shared pre-flights itself
    # when launched directly -- mark them as done here so it does not probe
    # twice (the block above just ran them).
    export KVP_PREFLIGHTS_DONE=1
    echo "KVP_ENGINE=recipe: launching the hardware-verified recipe launcher"
    echo "  container: $CONTAINER (recorded in live/engine_container)"
    echo "  KV_CACHE_DTYPE=$KV_CACHE_DTYPE (from tp4.env/export or the bfloat16 fallback; auto resolves to the model dtype -- the identity gate enforces the resolved value)"
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