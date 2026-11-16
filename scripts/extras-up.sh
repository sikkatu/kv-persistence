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
RELAY_PORT="${KVP_OBS_RELAY_PORT:-30008}"   # the relay listens here (L6)

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
      -e "s|@RELAY_PORT@|$RELAY_PORT|g" \
      -e "s|@LISTEN_IP@|$GW_IP|g" "$TPL" > "$RENDERED" 2>/dev/null \
    || warn "cannot render $RENDERED (permissions?) -- the relay will fail to start"
else
  warn "$TPL missing -- relay config not rendered"
fi

# ------------------------- 3. file_sd target: vLLM relay ------------------
mkdir -p "$EXTRAS/targets" 2>/dev/null || true
# L3: NO static engine/model labels -- vLLM exports its own (label collision = exported_engine)
printf '[{"targets": ["%s:%s"]}]\n' \
  "$GW_IP" "$RELAY_PORT" > "$EXTRAS/targets/vllm.json" 2>/dev/null \
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
# Profiling counters (round 90 A1/A3): KVP_OBS_GPU_PROF=1 (DEFAULT now) uses
# the dedicated dcgm-exporter-prof service (profile `gpu-prof`, cap_add
# SYS_ADMIN). The mounted CSV is GENERATED here: every row of
# dcgm-counters.csv PLUS the DCGM_FI_PROF_* tail of dcgm-counters.prof.csv --
# `-f` REPLACES the exporter's counter set, so a prof-only file would drop
# the base fields (power / temp / memory / util / clocks) and the whole GPU
# row went to zero in round 90. KVP_OBS_GPU_PROF=0 selects the plain
# dcgm-exporter (no extra caps).
if [ "$GPU_PROF" = "gpu" ] && [ "${KVP_OBS_GPU_PROF:-1}" = "1" ]; then
  GPU_PROF="gpu-prof"
  { grep -hv '^[[:space:]]*#' "$EXTRAS/gpu/dcgm-counters.csv" | grep -v '^[[:space:]]*$'
    grep -hv '^[[:space:]]*#' "$EXTRAS/gpu/dcgm-counters.prof.csv" | grep -v '^[[:space:]]*$'
  } > "$EXTRAS/gpu/dcgm-counters.live.csv" 2>/dev/null \
    || warn "cannot generate the profiling counters csv -- the prof exporter will fail its mount, check.sh reports FAIL"
fi

# file_sd target for the GPU exporter (inside the compose network).
# H2.1 (review 88): the target follows the ACTIVATED exporter -- dcgm for
# profile `gpu`, the profiling instance dcgm-exporter-prof for `gpu-prof`,
# nvidia-smi-exporter for `gpu-smi`, and an EMPTY list when the
# GPU row is disabled (an always-down gpu target would be a permanent FAIL).
if [ "$GPU_PROF" = "gpu" ]; then
  printf '[{"targets": ["dcgm-exporter:9400"]}]\n' > "$EXTRAS/targets/gpu.json" 2>/dev/null \
    || warn "cannot write extras/targets/gpu.json"
elif [ "$GPU_PROF" = "gpu-prof" ]; then
  printf '[{"targets": ["dcgm-exporter-prof:9400"]}]\n' > "$EXTRAS/targets/gpu.json" 2>/dev/null \
    || warn "cannot write extras/targets/gpu.json"
elif [ "$GPU_PROF" = "gpu-smi" ]; then
  printf '[{"targets": ["nvidia-smi-exporter:9835"]}]\n' > "$EXTRAS/targets/gpu.json" 2>/dev/null \
    || warn "cannot write extras/targets/gpu.json"
else
  printf '[]\n' > "$EXTRAS/targets/gpu.json" 2>/dev/null \
    || warn "cannot write extras/targets/gpu.json"
fi

# ------------------------- 5. compose up with profiles --------------------
PROFILES="extras"
[ -n "$GPU_PROF" ] && PROFILES="extras,$GPU_PROF"
# r89 (GPU mode switch): `docker compose up -d` does NOT stop services of
# INACTIVE profiles -- switching KVP_OBS_GPU_PROF 0<->1 (or dcgm<->smi) used
# to leave the old exporter running next to the new one (two DCGM instances
# on the same GPUs; in the prof->base direction a SYS_ADMIN container stayed
# alive). Grep targets/gpu.json only sees ONE, so dashboards never showed it.
# Stop+remove every GPU service that is NOT in the selected profile first.
_gpu_inactive=""
for _svc in dcgm-exporter dcgm-exporter-prof nvidia-smi-exporter; do
  case "$_svc" in
    dcgm-exporter)        _want="gpu" ;;
    dcgm-exporter-prof)   _want="gpu-prof" ;;
    nvidia-smi-exporter)  _want="gpu-smi" ;;
  esac
  [ "$GPU_PROF" = "$_want" ] || _gpu_inactive="$_gpu_inactive $_svc"
done
if [ -n "$_gpu_inactive" ]; then
  # shellcheck disable=SC2086 -- intentional word splitting of the service list
  # r89c: use the SAME `--profile '*'` flag syntax as stop-all.sh (docs of
  # Compose describe COMPOSE_PROFILES as a comma-separated NAME LIST -- the
  # wildcard `*` is documented for the --profile FLAG, not for the env var;
  # with the undocumented env form a failure was masked into the WARN below).
  ( cd "$OBS_DIR" && docker compose --profile '*' rm -sf $_gpu_inactive >/dev/null 2>&1 ) \
    || warn "could not stop inactive GPU exporter(s):$_gpu_inactive -- stop them manually (docker compose --profile '*' rm -sf <svc>)"
fi
echo "[extras] docker compose up -d (COMPOSE_PROFILES=$PROFILES) -- idempotent. NOTE (r88 M2): the FIRST run after adding extras recreates obs_prometheus/obs_grafana once (new volume mounts); their DATA volumes are preserved."
if ! ( cd "$OBS_DIR" && COMPOSE_PROFILES="$PROFILES" docker compose up -d 2>&1 ); then
  warn "extras compose step failed -- base stack is intact, extras partially missing (see above)"
fi
# --------------------- 5b. A4: prof exporter health + auto-fallback -------
# The profiling container is capability-gated (SYS_ADMIN) and CUPTI-gated: on
# a driver/build that denies profiling it crash-loops (round 90: obs_dcgm_exporter_prof
# in `Restarting (1)` with "wrong number of fields" -> EMPTY GPU row). NEVER
# let the GPU row die because of profiling: if the prof exporter is not Up
# and does not answer /metrics with 200 within 60s (probe INSIDE the
# container -- docker exec, not from the host), the base dcgm-exporter is
# brought up instead, targets/gpu.json is rewritten, and the reason +
# `docker logs --tail 20` of the failed container are printed BEFORE it is
# removed. Exit 0 whatever happens: base panels never stay empty.
_probe_metrics_inside() {
  # $1 = metrics URL inside the compose network. r90b fix: probe FROM THE
  # COMPOSE NETWORK via obs_prometheus (it has wget -- the exact scrape path,
  # no dependence on tooling inside the exporter image: dcgm-exporter 4.5.2
  # ships NO wget/curl per its Dockerfile, so probing inside it always failed
  # and every start fell back). r90c: no host-side /dev/tcp fallback -- the
  # host cannot resolve compose service names like dcgm-exporter-prof, so
  # that path was dead weight (a probe that can never succeed).
  docker exec obs_prometheus wget -q -T 5 -O /dev/null "$1" >/dev/null 2>&1 \
    || docker exec obs_prometheus curl -fsS --max-time 5 -o /dev/null "$1" >/dev/null 2>&1
}
if [ "$GPU_PROF" = "gpu-prof" ]; then
  _PROF_OK=0
  # r90c P1-1: probe over the COMPOSE network toward the service URL -- same
  # URL Prometheus scrapes (dcgm-exporter-prof:9400). No wget/curl in the
  # exporter image, so the probe runs from obs_prometheus (same subnet) with
  # a 12x5s budget (60s): a healthy profiling exporter on 4 Blackwell cards
  # may need more than a few seconds to start; the early exit below (container
  # not running -> break) keeps a crash-looping one at seconds, not minutes.
  for _try in $(seq 1 12); do
    if _probe_metrics_inside "http://dcgm-exporter-prof:9400/metrics"; then
      _PROF_OK=1
      break
    fi
    _st="$(docker inspect -f '{{.State.Status}}' obs_dcgm_exporter_prof 2>/dev/null || echo missing)"
    [ "$_st" = "running" ] || break     # container down -> fail fast, no full wait
    sleep 5
  done
  if [ "$_PROF_OK" = "0" ]; then
    echo "[extras] A4 AUTO-FALLBACK: obs_dcgm_exporter_prof not healthy (no 200 on /metrics via obs_prometheus within 60s) -- falling back to the base dcgm-exporter so the GPU row keeps data."
    docker logs --tail 20 obs_dcgm_exporter_prof 2>&1 | sed 's/^/    [dcgm-prof log] /' || true
    ( cd "$OBS_DIR" && docker compose --profile '*' rm -sf dcgm-exporter-prof >/dev/null 2>&1 ) || true
    GPU_PROF="gpu"
    PROFILES="extras,gpu"
    printf '[{"targets": ["dcgm-exporter:9400"]}]\n' > "$EXTRAS/targets/gpu.json" 2>/dev/null \
      || warn "cannot rewrite extras/targets/gpu.json after fallback"
    ( cd "$OBS_DIR" && COMPOSE_PROFILES="extras,gpu" docker compose up -d >/dev/null 2>&1 ) \
      || warn "fallback dcgm-exporter failed to start -- GPU row missing (see above)"
    echo "[extras] GPU target after fallback: dcgm-exporter:9400 (profiling OFF -- set KVP_OBS_GPU_PROF=0 to silence, or check the log above)"
  fi
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
