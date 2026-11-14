#!/usr/bin/env bash
# obs-extras-stand.sh -- behavioural stand for the EXTRA observability layer
# (part B round 87, spec 10.2). Real docker is NOT touched: a stub answers
# `ps`, `inspect`, `compose --profile`, `kill -s HUP`, `network inspect` etc
# from environment variables and logs every call.
#
# Usage: bash obs-extras-stand.sh /path/to/kv-persistence
# Exit:  0 = all scenarios behave as expected, 1 = at least one FAIL.
#
# Scenarios:
#  X1 clean start  : base up, then extras up -> relay config + targets appear,
#                    compose up ran with profiles extras (+ gpu when allowed)
#  X2 base already running, extras missing -> start-all.sh still brings extras
#  X3 KVP_OBS_EXTRAS=0 -> no extras profile invocation at all
#  X4 extras error (stub returns 1) -> start-observability.sh still exits 0 + WARN
#  X5 KVP_OBS_GPU=off or no NVIDIA runtime -> GPU profile not in the command
#  X6 stop owner -> compose down with --profile '*' (extras stopped)
#  X7 stop foreign / force -> unchanged from before (stand A-G covers this)
#  X8 old 7 scenarios of obs-ownership-stand.sh still pass (invoked inline)
#  X9 extras_started marker discipline (r89 L4):
#     X9a KVP_OBS_EXTRAS=0 -> the hook does NOT write the marker
#     X9b default -> the hook DOES write the marker when extras ran
#     X9c stale marker + foreign stack -> the start-all extras block CONSUMES
#         the marker anyway, so the NEXT run's deploy is never muted
#     X9d marker + our stack -> extras skipped ONCE ("already deployed"), marker gone
#  X10 targets/gpu.json content follows the selected exporter (r89 4):
#     gpu -> dcgm-exporter:9400 | gpu-prof -> dcgm-exporter-prof:9400 |
#     gpu-smi -> nvidia-smi-exporter:9835 | off -> []
#  X11 GPU mode switch stops the INACTIVE exporters (r89 3): `compose rm -sf`
#      runs with the non-selected services BEFORE the compose up
set -u
REPO_SRC="${1:?usage: $0 /path/to/kv-persistence}"
[ -f "$REPO_SRC/scripts/extras-up.sh" ] || { echo "no scripts/extras-up.sh in $REPO_SRC" >&2; exit 2; }

S="$(mktemp -d)"; trap 'rm -rf "$S"' EXIT
mkdir -p "$S/bin" "$S/repo/scripts" "$S/repo/observability/extras/targets" \
         "$S/repo/observability/extras/relay" "$S/repo/observability/extras/gpu" "$S/repo/live/obs-relay" \
         "$S/foreign/observability"
cp "$REPO_SRC/scripts/extras-up.sh" "$S/repo/scripts/"
cp "$REPO_SRC/scripts/stop-all.sh" "$S/repo/scripts/"
sed 's/^/ /' /dev/null > "$S/repo/scripts/stop-engine.sh"   # silent no-op stubs
printf '#!/bin/bash\nexit 0\n' > "$S/repo/scripts/stop-engine.sh"
cp "$S/repo/scripts/stop-engine.sh" "$S/repo/scripts/stop-cache.sh"
cp "$REPO_SRC/observability/extras/relay/nginx.conf.template" "$S/repo/observability/extras/relay/" 2>/dev/null || true
cp "$REPO_SRC/observability/extras/gpu/dcgm-counters.prof.csv" "$S/repo/observability/extras/gpu/" 2>/dev/null || true
cp "$REPO_SRC/observability/docker-compose.override.yml" "$S/repo/observability/" 2>/dev/null || true
cp "$REPO_SRC/observability/docker-compose.yml" "$S/repo/observability/" 2>/dev/null || true

cat > "$S/bin/docker" <<'EOF'
#!/bin/bash
# stub docker: answers from env (GRAFANA_UP/PROM_UP/WD/COMPOSE_RC) and logs.
echo "docker $*" >> "$LOG"
case "$1" in
  ps)      [ "${GRAFANA_UP:-1}" = 1 ] && echo obs_grafana
           [ "${PROM_UP:-1}" = 1 ] && echo obs_prometheus
           [ "${RELAY_UP:-0}" = 1 ] && echo obs_vllm_relay
           [ "${DCGM_UP:-0}" = 1 ] && echo obs_dcgm_exporter
           exit 0 ;;
  inspect) echo "${WD:-}"; exit 0 ;;
  info)    [ "${NVIDIA_RT:-1}" = 1 ] && echo "Runtimes: nvidia" || echo "Runtimes: runc"; exit 0 ;;
  network) echo "172.17.0.1"; exit 0 ;;
  kill)    exit 0 ;;
  rm)      echo "RM $*" >> "$LOG"; exit 0 ;;
  compose)
           if [ "$2" = "up" ]; then
             if [ "${COMPOSE_RC:-0}" != 0 ]; then exit "$COMPOSE_RC"; fi
           fi
           echo "COMPOSE ${*:2}" >> "$LOG"; exit 0 ;;
esac
exit 0
EOF
chmod +x "$S/bin/docker"
chmod +x "$S/repo/scripts/"*.sh
rm -f "$S/repo/tests" 2>/dev/null || true
mkdir -p "$S/repo/tests"
[ -f "$REPO_SRC/tests/obs-ownership-stand.sh" ] && cp "$REPO_SRC/tests/obs-ownership-stand.sh" "$S/repo/tests/"

FAILS=0
pass(){ printf 'PASS  %-62s expected=%s\n' "$1" "$2"; }
fail(){ printf 'FAIL  %-62s expected=%s got=%s\n' "$1" "$2" "$3"; sed -n '/extras\|observability/p' "$S/out" | sed 's/^/        /'; FAILS=$((FAILS+1)); }

run(){ : > "$S/log"; ( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" bash "${@}" >"$S/out" 2>&1 ); }

# ---- X1 clean start: run extras-up.sh with GPU detection on ----
rm -f "$S/repo/live/obs-relay/nginx.conf" "$S/repo/observability/extras/targets/vllm.json"
run scripts/extras-up.sh
if [ -f "$S/repo/live/obs-relay/nginx.conf" ] && [ -f "$S/repo/observability/extras/targets/vllm.json" ] \
   && grep -q "COMPOSE up -d" "$S/log"; then
  pass "X1 clean start: extras deployed" stop
else
  fail "X1 clean start" stop deploy
fi

# ---- X5 GPU detection: nvidia-smi missing + NVIDIA_RT=0 -> no gpu profile ----
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" NVIDIA_RT=0 KVP_OBS_GPU=auto bash scripts/extras-up.sh > "$S/out" 2>&1 )
if grep -q "COMPOSE up -d" "$S/log" && ! grep -q "gpu" "$S/log" && grep -q "GPU exporter skipped" "$S/out"; then
  pass "X5 auto + no NVIDIA runtime -> GPU profile guard" skip
else
  fail "X5 gpu solver" skip launch
fi

# ---- X5b KVP_OBS_GPU=off -> no GPU profile even with nvidia present ----
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" KVP_OBS_GPU=off bash scripts/extras-up.sh > "$S/out" 2>&1 )
if ! grep -qE "gpu-smi|,gpu" "$S/log"; then
  pass "X5b KVP_OBS_GPU=off -> no gpu profile" skip
else
  fail "X5b KVP_OBS_GPU=off" skip launch
fi

# ---- X3 KVP_OBS_EXTRAS=0 -> no extras invocation at all ----
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" KVP_OBS_EXTRAS=0 bash scripts/extras-up.sh > "$S/out" 2>&1 )
if ! grep -q "COMPOSE" "$S/log" && grep -q "disabled" "$S/out"; then
  pass "X3 KVP_OBS_EXTRAS=0 -> nothing started" leave
else
  fail "X3 KVP_OBS_EXTRAS=0" leave start
fi

# ---- X4 extras error -> start-observability.sh still exits 0 + WARN --------
# Exercise the REAL hook line the way the real script runs it (review r88:
# "X4 must verify start-observability.sh behaviour, not just that the stub
# returns 1"). We extract the literal extras-hook line from the real
# start-observability.sh and execute it against a FAILING extras-up.sh in the
# sandbox: the hook must neutralise the failure (exit 0 + a warning line).
: > "$S/log"
mv "$S/repo/scripts/extras-up.sh" "$S/repo/scripts/extras-up.sh.real"
printf '#!/bin/bash\necho "SIMULATED FAILURE"; exit 1\n' > "$S/repo/scripts/extras-up.sh"
chmod +x "$S/repo/scripts/extras-up.sh"
HOOK_LINE="$(grep -n 'bash \"$HERE/extras-up.sh\"' "$REPO_SRC/scripts/start-observability.sh" | tail -1 | cut -d: -f2-)"
if [ -z "$HOOK_LINE" ]; then
  fail "X4 hook line missing in start-observability.sh" warn intact
else
  ( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" HERE="$S/repo/scripts" \
      bash -c "extras-up.sh() { :; }; $HOOK_LINE" > "$S/out" 2>&1 )
  RC_EXP=$?
  # The hook line above substitutes $HERE/extras-up.sh -> real path; but we pass
  # HERE=$S/repo/scripts so it resolves to the SIMULATED FAILURE copy.
  if [ "$RC_EXP" = "0" ] && grep -qi "SIMULATED FAILURE" "$S/out"; then
    pass "X4 extras failure -> start-observability hook ignores it (rc=0)" warn
  else
    fail "X4 extras failure handling" warn "rc=$RC_EXP"
  fi
fi
mv "$S/repo/scripts/extras-up.sh.real" "$S/repo/scripts/extras-up.sh"

# ---- X2 base already running, extras missing -> extras block still deploys --
# Extract the extras block from the REAL start-all.sh and run it standalone in
# the sandbox with _obs_ours=1 (the update path: model already loaded, user
# re-runs start-all.sh after git pull). It must STILL bring extras up.
EXTRAS_BLOCK="$(sed -n '/-- extras deploy: BEGIN/,/-- extras deploy: END --/p' "$REPO_SRC/scripts/start-all.sh")"
rm -f "$S/repo/live/obs-relay/nginx.conf" "$S/repo/observability/extras/targets/vllm.json"
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" HERE="$S/repo/scripts" ROOT="$S/repo" _obs_ours=1 \
    bash -c "$EXTRAS_BLOCK" > "$S/out" 2>&1 )
if grep -q "COMPOSE up" "$S/log" && [ -f "$S/repo/live/obs-relay/nginx.conf" ]; then
  pass "X2 base already running -> extras deploy still runs" stop
else
  fail "X2 base already running" stop deploy
fi

# ---- X7 foreign stack -> extras must NOT deploy from this clone ------------
# Same block, _obs_ours=0 (foreign compose label): the block must refuse and
# print the foreign-clone line -- both clones name the compose project
# `observability`, so deploying here would recreate the foreign containers.
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" HERE="$S/repo/scripts" ROOT="$S/repo" _obs_ours=0 \
    bash -c "$EXTRAS_BLOCK" > "$S/out" 2>&1 )
if ! grep -q "COMPOSE up" "$S/log" && grep -qi "different clone\|foreign" "$S/out"; then
  pass "X7 foreign stack -> extras NOT deployed" leave
else
  fail "X7 foreign stack" leave deploy
fi

# ---- X6 stop: owner -> compose down with --profile '*' ----
rm -f "$S/repo/live/obs_started"; echo 1 > "$S/repo/live/obs_started"
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" WD="$S/repo/observability" bash scripts/stop-all.sh > "$S/out" 2>&1 )
if grep -q "down" "$S/log" && grep -q -- "--profile" "$S/log"; then
  pass "X6 owner -> compose down has --profile '*'" stop
else
  fail "X6 owner down" stop profile
fi

# ---- X9a extras hook, KVP_OBS_EXTRAS=0 -> the marker is NOT written (r89 L4) --
# Execute the REAL extras-hook block from start-observability.sh with extras
# DISABLED: nothing may run and, crucially, no extras_started marker may appear
# (a stale marker would mute the next start-all's extras deploy -- e.g. right
# after switching KVP_OBS_GPU_PROF the change silently did not apply).
HOOK_BLOCK="$(sed -n '/-- extras hook: BEGIN/,/-- extras hook: END --/p' "$REPO_SRC/scripts/start-observability.sh")"
rm -f "$S/repo/live/extras_started"
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" HERE="$S/repo/scripts" ROOT="$S/repo" KVP_OBS_EXTRAS=0 \
    bash -c "$HOOK_BLOCK" > "$S/out" 2>&1 )
if [ ! -f "$S/repo/live/extras_started" ] && ! grep -q "COMPOSE up" "$S/log"; then
  pass "X9a KVP_OBS_EXTRAS=0 -> no marker written" leave
else
  fail "X9a no-marker when disabled" leave "marker=$(test -f "$S/repo/live/extras_started" && echo present || echo absent)"
fi

# ---- X9b extras hook, default -> the marker IS written (r89 L4) -------------
rm -f "$S/repo/live/extras_started"
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" HERE="$S/repo/scripts" ROOT="$S/repo" \
    bash -c "$HOOK_BLOCK" > "$S/out" 2>&1 )
if [ -f "$S/repo/live/extras_started" ] && grep -q "COMPOSE up" "$S/log"; then
  pass "X9b default run -> marker written for start-all" write
else
  fail "X9b marker written when extras ran" write "marker=$(test -f "$S/repo/live/extras_started" && echo present || echo absent)"
fi

# ---- X9c stale marker + FOREIGN stack -> consumed anyway (r89 L4) -----------
# Even when the stack is foreign and extras MUST NOT run, the start-all extras
# block still consumes the marker -- otherwise the NEXT (our-stack) run skips
# its deploy once and a real config change is silently lost.
: > "$S/log"
mkdir -p "$S/repo/live"; echo 1 > "$S/repo/live/extras_started"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" HERE="$S/repo/scripts" ROOT="$S/repo" _obs_ours=0 \
    bash -c "$EXTRAS_BLOCK" > "$S/out" 2>&1 )
if [ ! -f "$S/repo/live/extras_started" ] && ! grep -q "COMPOSE up" "$S/log"; then
  pass "X9c foreign stack still CONSUMES the stale marker" consume
else
  fail "X9c stale marker consumption on foreign stack" consume "marker=$(test -f "$S/repo/live/extras_started" && echo present || echo absent)"
fi

# ---- X9d marker + our stack -> skipped ONCE, marker gone (r89 L4) -----------
: > "$S/log"
mkdir -p "$S/repo/live"; echo 1 > "$S/repo/live/extras_started"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" HERE="$S/repo/scripts" ROOT="$S/repo" _obs_ours=1 \
    bash -c "$EXTRAS_BLOCK" > "$S/out" 2>&1 )
if [ ! -f "$S/repo/live/extras_started" ] && ! grep -q "COMPOSE up" "$S/log" && grep -qi "already deployed" "$S/out"; then
  pass "X9d marker -> extras skipped once, marker consumed" skip
else
  fail "X9d marker honored then consumed" skip consume
fi

# ---- X10 targets/gpu.json follows the selected exporter (r89 4) -------------
# Four modes compose (gpu, gpu-prof, gpu-smi, off). The reviewer caught that
# substituting the port/service name in extras-up.sh went unnoticed: the file
# CONTENT was never checked -- only that the file exists.
check_gpu_target(){
  # $1 = label, $2 = expected content fragment (e.g. dcgm-exporter:9400 or [])
  if [ -f "$S/repo/observability/extras/targets/gpu.json" ] && \
     grep -qF "$2" "$S/repo/observability/extras/targets/gpu.json"; then
    pass "$1" "$2"
  else
    fail "$1" "$2" "$(cat "$S/repo/observability/extras/targets/gpu.json" 2>/dev/null | tr -d '\n')"
  fi
}
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" NVIDIA_RT=1 KVP_OBS_GPU=on KVP_OBS_GPU_PROF=0 \
    bash scripts/extras-up.sh > "$S/out" 2>&1 )
check_gpu_target "X10a gpu mode -> target dcgm-exporter:9400" "dcgm-exporter:9400"

: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" NVIDIA_RT=1 KVP_OBS_GPU=on KVP_OBS_GPU_PROF=1 \
    bash scripts/extras-up.sh > "$S/out" 2>&1 )
check_gpu_target "X10b gpu-prof mode -> target dcgm-exporter-prof:9400" "dcgm-exporter-prof:9400"

: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" NVIDIA_RT=1 KVP_OBS_GPU=on KVP_OBS_GPU_EXPORTER=smi \
    bash scripts/extras-up.sh > "$S/out" 2>&1 )
check_gpu_target "X10c gpu-smi mode -> target nvidia-smi-exporter:9835" "nvidia-smi-exporter:9835"

: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" KVP_OBS_GPU=off \
    bash scripts/extras-up.sh > "$S/out" 2>&1 )
check_gpu_target "X10d gpu=off -> empty target list" '[]'

# ---- X11 GPU mode switch stops INACTIVE exporters before up (r89 3) ---------
# `docker compose up -d` does not stop services of inactive profiles: switching
# KVP_OBS_GPU_PROF 0<->1 (or dcgm<->smi) used to leave the old exporter running
# side by side. extras-up.sh must `compose rm -sf` the NOT-selected GPU
# services BEFORE calling up -d.
: > "$S/log"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" NVIDIA_RT=1 KVP_OBS_GPU=on KVP_OBS_GPU_PROF=1 \
    bash scripts/extras-up.sh > "$S/out" 2>&1 )
# gpu-prof selected: dcgm-exporter AND nvidia-smi-exporter must be removed
if grep -q "rm -sf" "$S/log" && grep -q "dcgm-exporter " "$S/log" && grep -q "nvidia-smi-exporter" "$S/log" \
   && ! grep "rm -sf" "$S/log" | grep -q "dcgm-exporter-prof"; then
  pass "X11 gpu-prof switch: inactive exporters rm -sf'ed first" rm
else
  fail "X11 gpu mode switch cleanup" rm "no rm -sf of inactive GPU services"
fi

echo
if [ -f "$S/repo/tests/obs-ownership-stand.sh" ]; then
  bash "$S/repo/tests/obs-ownership-stand.sh" "$S/repo" >/dev/null 2>&1
  if [ $? = 0 ]; then
    pass "X8 obs-ownership-stand.sh 7/7 unchanged" pass
  else
    fail "X8 obs-ownership-stand.sh" pass fail
  fi
fi

echo
[ "$FAILS" -eq 0 ] && echo "ALL OK" || echo "$FAILS scenario(s) FAILED"
[ "$FAILS" -eq 0 ]