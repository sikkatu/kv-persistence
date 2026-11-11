#!/usr/bin/env bash
# obs-ownership-stand.sh -- behavioural stand for scripts/stop-all.sh (round 83).
# Real docker is NOT touched: a stub `docker` answers `ps`, `inspect -f` and
# `compose down` from environment variables and logs what was called.
#
# Usage:  bash obs-ownership-stand.sh /path/to/kv-persistence
# Exit:   0 = all scenarios behave as expected, 1 = at least one FAIL.
#
# Each scenario states the EXPECTED behaviour (what a correct stop-all must do).
# On round-83 code scenarios E, F, G FAIL -- these are the open defects:
#   E  KVP_STOP_OBS=1 does nothing when obs_grafana is down but others are up
#   F  repo reached through a symlinked path is treated as a FOREIGN stack
#   G  a stale live/obs_started marker overrides a label that proves "foreign"
set -u
REPO_SRC="${1:?usage: $0 /path/to/kv-persistence}"
[ -f "$REPO_SRC/scripts/stop-all.sh" ] || { echo "no scripts/stop-all.sh in $REPO_SRC" >&2; exit 2; }

S="$(mktemp -d)"; trap 'rm -rf "$S"' EXIT
mkdir -p "$S/bin" "$S/repo/scripts" "$S/repo/observability" "$S/foreign/observability"
cp "$REPO_SRC/scripts/stop-all.sh" "$S/repo/scripts/"
# stop-all first stops the engine and the cache -- stub both out
printf '#!/bin/bash\nexit 0\n' > "$S/repo/scripts/stop-engine.sh"
cp "$S/repo/scripts/stop-engine.sh" "$S/repo/scripts/stop-cache.sh"

cat > "$S/bin/docker" <<'EOF'
#!/bin/bash
# GRAFANA_UP / PROM_UP: which containers `docker ps` lists
# WD: value of the com.docker.compose.project.working_dir label
case "$1" in
  ps)      [ "${GRAFANA_UP:-1}" = 1 ] && echo obs_grafana
           [ "${PROM_UP:-0}" = 1 ] && echo obs_prometheus
           exit 0 ;;
  inspect) echo "${WD:-}"; exit 0 ;;
  compose) echo "COMPOSE $2" >> "$LOG"; exit 0 ;;
  rm)      echo "RM $*" >> "$LOG"; exit 0 ;;
esac
exit 0
EOF
chmod +x "$S/bin/docker" "$S/repo/scripts/"*.sh

FAILS=0
# run_case <name> <expect: stop|leave> [VAR=value ...]
run_case() {
  local name="$1" expect="$2"; shift 2
  : > "$S/log"
  ( cd "$S/repo" && env PATH="$S/bin:$PATH" LOG="$S/log" "$@" bash scripts/stop-all.sh >"$S/out" 2>&1 )
  local got=leave
  grep -q '^COMPOSE down\|^RM ' "$S/log" && got=stop
  if [ "$got" = "$expect" ]; then
    printf 'PASS  %-62s expected=%s\n' "$name" "$expect"
  else
    printf 'FAIL  %-62s expected=%s got=%s\n' "$name" "$expect" "$got"
    sed -n '/observability/p' "$S/out" | sed 's/^/        /'
    FAILS=$((FAILS+1))
  fi
}

rm -f "$S/repo/live/obs_started"
run_case "A  running, label == this repo, no marker"            stop  WD="$S/repo/observability"
run_case "B  running, FOREIGN label, no marker"                 leave WD="$S/foreign/observability"
run_case "C  running, foreign, KVP_STOP_OBS=1 (force)"          stop  WD="$S/foreign/observability" KVP_STOP_OBS=1
run_case "D  nothing running"                                   leave GRAFANA_UP=0
run_case "E  grafana down, prometheus up, KVP_STOP_OBS=1"       stop  GRAFANA_UP=0 PROM_UP=1 KVP_STOP_OBS=1
ln -sfn "$S/repo" "$S/link"
run_case "F  label path goes through a SYMLINK to this repo"    stop  WD="$S/link/observability"
mkdir -p "$S/repo/live"; echo 1 > "$S/repo/live/obs_started"
run_case "G  stale marker, but label proves the stack is FOREIGN" leave WD="$S/foreign/observability"
rm -f "$S/repo/live/obs_started"

echo
[ "$FAILS" -eq 0 ] && echo "ALL OK" || echo "$FAILS scenario(s) FAILED"
[ "$FAILS" -eq 0 ]
