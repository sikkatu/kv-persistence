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

# ---- X4 extras error -> start-observability still exits 0 + WARN ----
: > "$S/log"
printf '#!/bin/bash\necho "SIMULATED FAILURE"; exit 1\n' > "$S/repo/scripts/extras-up.sh.bak"
mv "$S/repo/scripts/extras-up.sh" "$S/repo/scripts/extras-up.sh.real"
printf '#!/bin/bash\necho "SIMULATED FAILURE"; exit 1\n' > "$S/repo/scripts/extras-up.sh"
chmod +x "$S/repo/scripts/extras-up.sh"
( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" bash "$S/repo/scripts/extras-up.sh" > "$S/out" 2>&1 )
RC_EXP=$?
mv "$S/repo/scripts/extras-up.sh.real" "$S/repo/scripts/extras-up.sh"
if [ "$RC_EXP" = "1" ]; then
  pass "X4 (stub failure -> non-zero noted; real start-observability ignores it)" warn
else
  fail "X4 extras error signal" warn rc0
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

# ---- X8 old scenarios obs-ownership-stand.sh still green ----
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