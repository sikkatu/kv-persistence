#!/usr/bin/env bash
# extras-up.sh -- AGENT: observability extras layer (part B round 87, 4d).
# INVARIANT (spec R8): extras can NEVER block or break the base stack --
# every step below is best-effort: any error prints WARN and the script
# keeps going (and the caller exits 0 whatever happens here).
# Because this script runs INDEPENDENTLY of the base stack bring-up, it is
# safe to run repeatedly (--idempotent) and safe to run when the base is
# already up: it never touches obs_prometheus containers that it did not
# start itself.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OBS_DIR="$ROOT/observability"
EXTRAS="$OBS_DIR/extras"

warn(){ echo "WARNING: $*" >&2; }

# ----------------------------- 0. enable flag -----------------------------
if [ "${KVP_OBS_EXTRAS:-1}" = "0" ]; then
  echo "[extras] disabled (KVP_OBS_EXTRAS=0) -- base stack unchanged"
  exit 0
fi
[ -d "$EXTRAS" ] || { warn "extras directory $EXTRAS missing -- nothing to do"; exit 0; }

# ------------------------- 1. engine port + gateway IP --------------------
ENGINE_PORT="${PORT:-30007}"
RPC_PORT="${KVP_OBS_RELAY_PORT:-30008}"

GW_IP="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
[ -n "$GW_IP" ] || GW_IP="172.17.0.1"
if [ "$GW_IP" = "0.0.0.0" ] || [ "$GW_IP" = "::" ]; then
  warn "docker bridge gateway inspect returned a WIDE bind ($GW_IP) -- forcing 172.17.0.1"
  GW_IP="172.17.0.1"
fi

# ------------------------- 2. render the nginx relay config ---------------
mkdir -p "$ROOT/live/obs-relay" 2>/dev/null || true
TPL="$EXTRAS/relay/nginx.conf.template"
RENDERED="$ROOT/live/obs-relay/nginx.conf"
if [ -f "$TPL" ]; then
  sed -e "s|@ENGINE_PORT@|$ENGINE_PORT|g" \
      -e "s|@RELAY_PORT@|$RPC_PORT|g" \
      -e "s|@LISTEN_IP@|$GW_IP|g" "$TPL" > "$RENDERED" 2>/dev/null \
    || warn "cannot render $RENDERED (permissions?) -- the relay will fail to start"
else
  warn "$TPL missing -- relay config not rendered"
fi

# ------------------------- 3. file_sd target: vLLM relay ------------------
mkdir -p "$EXTRAS/targets" 2>/dev/null || true
printf '[{"targets": ["%s:%s"], "labels": {"engine": "vllm"}}]\n' \
  "$GW_IP" "$RPC_PORT" > "$EXTRAS/targets/vllm.json" 2>/dev/null \
  || warn "cannot write extras/targets/vllm.json"

# engine down is NOT an error -- up=0 will show in dashboards later
curl -fsS --max-time 3 "http://127.0.0.1:$ENGINE_PORT/metrics" >/dev/null 2>&1 \
  || echo "[extras] engine not answering yet on :$ENGINE_PORT -- relay/targets deploy anyway (up=0 until it starts)"

# ------------------------- 4. GPU exporter autodetect ---------------------
# R6/X5: with no NVIDIA runtime we skip the GPU row honestly (one line),
# never half-start it. off -> no GPU profile at all; on -> force without
# detect; auto (default) -> detect.
GPU_PROF=""
case "${KVP_OBS_GPU:-auto}" in
  off)
    echo "[extras] GPU exporter skipped: KVP_OBS_GPU=off"
    ;;
  on)
    GPU_PROF="gpu"
    ;;
  *)
    HAVE_NVIDIA=0
    command -v nvidia-smi >/dev/null 2>&1 && HAVE_NVIDIA=1
    docker info 2>/dev/null | grep -qi "nvidia" && HAVE_NVIDIA=1
    if [ "$HAVE_NVIDIA" = "1" ]; then
      GPU_PROF="gpu"
    else
      echo "[extras] GPU exporter skipped: no nvidia-smi and no NVIDIA container runtime detected"
    fi
    ;;
esac
# fallback exporter selection (KVP_OBS_GPU_EXPORTER=smi swaps the profile)
if [ -n "$GPU_PROF" ] && [ "${KVP_OBS_GPU_EXPORTER:-dcgm}" = "smi" ]; then
  GPU_PROF="gpu-smi"
fi
# Opt-in profiling counters (SYSTEM ADMIN needed): extras/gpu/dcgm-counters.prof.csv
if [ -n "$GPU_PROF" ] && [ "${KVP_OBS_GPU_PROF:-0}" = "1" ] && [ "$GPU_PROF" = "gpu" ]; then
  cp "$EXTRAS/gpu/dcgm-counters.prof.csv" "$EXTRAS/gpu/dcgm-counters.live.csv" 2>/dev/null \
    || warn "cannot install the profiling counters csv -- running with base counters"
fi

# file_sd target for the GPU exporter (inside the compose network)
printf '[{"targets": ["dcgm-exporter:9400"]}]\n' > "$EXTRAS/targets/gpu.json" 2>/dev/null \
  || warn "cannot write extras/targets/gpu.json"

# ------------------------- 5. compose up with profiles --------------------
PROFILES="extras"
[ -n "$GPU_PROF" ] && PROFILES="extras,$GPU_PROF"
echo "[extras] docker compose up -d (COMPOSE_PROFILES=$PROFILES) -- idempotent, base untouched"
if ! ( cd "$OBS_DIR" && COMPOSE_PROFILES="$PROFILES" docker compose up -d 2>&1 ); then
  warn "extras compose step failed -- base stack is intact, extras partially missing (see above)"
fi

# ------------------------- 6. prometheus reload ---------------------------
# Prometheus only reads scrape_config_files / rule_files at start OR reload.
# We prefer SIGHUP via docker kill (standard and works without web.enable-lifecycle);
# if that fails, try the HTTP lifecycle endpoint; if that also fails, WARN with a
# manual instruction -- never silently "pretend ok".
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx obs_prometheus; then
  if docker kill -s HUP obs_prometheus >/dev/null 2>&1; then
    echo "[extras] prometheus config reloaded (SIGHUP)"
  elif curl -fsS --max-time 5 -X POST "http://127.0.0.1:9091/-/reload" >/dev/null 2>&1; then
    echo "[extras] prometheus config reloaded (HTTP /-/reload)"
  else
    warn "could not reload prometheus -- run: docker restart obs_prometheus, then check.sh"
  fi
fi

# ------------------------- 7. completion ---------------------------------
echo "[extras] done. dashboard: kv-persistence / Inference (vLLM + GPU) on http://localhost:3000"
echo "[extras] self-check: bash $EXTRAS/check.sh"
