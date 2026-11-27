#!/usr/bin/env bash
# restart-lib.sh -- the ONE shared full-restart routine for the kv-persistence
# stack (r32 R1). test-pr-checks.sh sources this file; test-recovery.sh keeps
# its inline copy (untouched here -- its rewrite is a separate task).
#
# Order is NOT arbitrary -- round 46: restarting the cache service while the
# engine is alive kills the engine later. Correct order: STOP THE ENGINE FIRST
# (docker stop -t 120, retried; a container that never stops is a DOCKER
# DAEMON failure -> INCONCLUSIVE, exit 5, never FAIL), stop-cache.sh,
# start-cache.sh, wait for the live server pid, IPC pre-flight BEFORE
# docker start (round 79: without it the engine dies at register_kv_cache
# after ~15 min of model loading) incl. the .Config.User same-UID check,
# docker start, wait for the API.
#
# Refusals of stop-cache.sh / start-cache.sh print their last 20 output lines
# -- the cause is never hidden. $KVP_ORDER_LOG (tests) receives one line per
# checkpoint. Runs as the operator, never as root.
KVP_LIB_DIR="${KVP_SCRIPTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
_kvp_rlog(){ [ -n "${KVP_ORDER_LOG:-}" ] && printf '%s\n' "$1" >> "$KVP_ORDER_LOG" || true; }
_kvp_rnote(){ echo "  $*"; }
_kvp_rtail(){ echo "    ---- last 20 lines of $1 ----" >&2; tail -n 20 "$2" >&2; }

# (1) stop the engine first, retried, daemon-robust (round 46 / r90 D1)
# Timing knobs (machine-free tests only; marked as TEST MODE by the caller):
#   KVP_STOP_RETRIES=3, KVP_STOP_WAIT_LOOPS=30, KVP_STOP_WAIT_INTERVAL=10
kvp_restart_stop_engine(){ # <container> -> 0 ok, 5 inconclusive
  local c="$1" state stopped=0 stop_out cur try w
  local T_RETRIES="${KVP_STOP_RETRIES:-3}" T_LOOPS="${KVP_STOP_WAIT_LOOPS:-30}"
  local T_INT="${KVP_STOP_WAIT_INTERVAL:-10}"
  state="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
  _kvp_rnote "engine container state before stop: $state"
  case "$state" in
    missing)  _kvp_rnote "[info] engine container already removed -- nothing to stop" ;;
    exited|created) _kvp_rnote "[info] engine container already stopped ($state) -- skipping stop" ;;
    *)
      for try in $(seq 1 "$T_RETRIES"); do
        stop_out=""; stop_rc=0
        stop_out="$(docker stop -t 120 "$c" 2>&1)" || stop_rc=$?
        if [ "$stop_rc" -eq 0 ]; then
          # r32.1 R1: a zero exit of `docker stop` is NOT proof of a stop -- the
          # container must reach exited/dead (or be gone) before we believe it.
          cur="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
          case "$cur" in exited|dead|missing) stopped=1; break ;;
            *) _kvp_rnote "docker stop returned 0 but the container state is '$cur' -- still waiting";;
          esac
        else
          _kvp_rnote "docker stop attempt $try/$T_RETRIES output: $stop_out" >&2
          cur="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
          case "$cur" in exited|dead|missing) stopped=1; break ;; esac
        fi
        sleep "$T_INT"
      done
      if [ "$stopped" = "0" ]; then
        _kvp_rnote "waiting up to $((T_LOOPS * T_INT))s for the container to reach exited..."
        for w in $(seq 1 "$T_LOOPS"); do
          cur="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)"
          case "$cur" in exited|dead|missing) stopped=1; break ;; esac
          sleep "$T_INT"
        done
      fi
      if [ "$stopped" = "0" ]; then
        echo "INCONCLUSIVE: container $c did NOT stop within 5 min (docker daemon failure). This is NOT a cache failure: rerun when docker ps shows the container gone. If it repeats: dmesg | tail -30 and nvidia-smi." >&2
        return 5
      fi ;;
  esac
  _kvp_rlog ENGINE_STOPPED
  return 0
}

# (2)+(3) cache service restart; a refusal prints the last 20 output lines
kvp_restart_cache(){ # <scripts-dir> -> 0 ok, 5 inconclusive
  local sd="$1" out rc=0
  out="$(bash "$sd/stop-cache.sh" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    _kvp_rtail "stop-cache.sh" <(printf '%s\n' "$out")
    echo "INCONCLUSIVE: stop-cache.sh failed (rc=$rc) -- see its output above." >&2
    return 5
  fi
  _kvp_rlog CACHE_STOPPED
  rc=0
  out="$(bash "$sd/start-cache.sh" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    _kvp_rtail "start-cache.sh" <(printf '%s\n' "$out")
    echo "INCONCLUSIVE: start-cache.sh failed (rc=$rc) -- see its output above." >&2
    return 5
  fi
  _kvp_rlog CACHE_STARTED
  return 0
}

# (4) wait up to 30s for the live server pid (kvp_server_pid, round 90d)
kvp_restart_wait_pid(){ # <cache-root> -> 0 ok, 5 inconclusive
  local root="$1" pid="" t
  for t in $(seq 1 15); do
    pid="$(kvp_server_pid "$root" 2>/dev/null || true)"
    case "$pid" in
      ''|*[!0-9]*) : ;;
      *) kill -0 "$pid" 2>/dev/null && { _kvp_rlog PID_OK; return 0; } ;;
    esac
    sleep 2
  done
  echo "INCONCLUSIVE: lmcache-server.pid not present/alive 30s after start-cache.sh (rerun once the pid file exists; check: ps aux | grep lmcache)." >&2
  return 5
}

# (5) IPC pre-flight BEFORE docker start (round 79) incl. the same-UID check
kvp_restart_ipc(){ # <scripts-dir> <container> -> 0 ok, 1 fail, 5 skipped/inconc
  local sd="$1" c="$2" cuser ipc_mode
  if [ "${KVP_SKIP_IPC_TEST:-0}" = "1" ] || [ ! -x "$sd/ipc-test.sh" ]; then
    _kvp_rnote "IPC pre-flight skipped (KVP_SKIP_IPC_TEST=$KVP_SKIP_IPC_TEST or ipc-test.sh missing)"
    _kvp_rlog IPC_SKIPPED
    return 0
  fi
  _kvp_rnote "pre-flight: CUDA IPC boundary (before docker start)"
  # docker start REUSES the container -- an old root container must not
  # silently come back (empty .Config.User means ROOT and must NOT pass)
  cuser="$(docker inspect -f '{{.Config.User}}' "$c" 2>/dev/null || echo '@missing@')"
  _kvp_rnote "container $c created as user: ${cuser:-<empty = root>}"
  if [ "${KVP_SAME_UID:-1}" = "1" ]; then
    case "$cuser" in
      "@missing@")
        echo "INCONCLUSIVE: container $c not found -- run bash scripts/start-all.sh first (it creates the engine same-UID), then rerun." >&2
        return 5 ;;
      "$(id -u):$(id -g)") : ;;
      *)
        echo "INCONCLUSIVE: container $c was created as '${cuser:-<empty = root>}' but same-UID mode needs $(id -u):$(id -g) -- docker start would bring back a broken-IPC engine. Recreate it: bash scripts/start-all.sh, then rerun." >&2
        return 5 ;;
    esac
  fi
  if [ -z "${IMAGE:-}" ] && [ -f "${KVP_LIVE_DIR:-${KVP_ROOT}/live}/image" ]; then
    IMAGE="$(tr -d '[:space:]' < "${KVP_LIVE_DIR:-${KVP_ROOT}/live}/image")"
    _kvp_rnote "IMAGE resolved from live/image: $IMAGE"
  fi
  ipc_mode="$(kvp_ipc_consumer_mode "${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}")"
  if IMAGE="${IMAGE:-}" CONSUMER="$ipc_mode" PRODUCER_USER="$(id -u):$(id -g)" bash "$sd/ipc-test.sh"; then
    _kvp_rnote "pre-flight: CUDA IPC boundary OK (consumer=$ipc_mode)"
    _kvp_rlog IPC_OK
    return 0
  fi
  echo "INCONCLUSIVE: pre-flight FAILED -- CUDA IPC boundary broken before docker start (the engine would die at register_kv_cache after a long model load). Same-UID: both sides $(id -u):$(id -g). Fix: bash scripts/start-all.sh, then rerun." >&2
  return 5
}

# (6)+(7) bring the engine back and wait for its API
kvp_restart_engine_up(){ # <container> <api-cmd> -> 0 ok, 5 inconclusive
  local c="$1" api_cmd="$2" t
  docker start "$c" >/dev/null 2>&1 || {
    echo "INCONCLUSIVE: docker start ($c) failed -- check docker ps / docker logs $c." >&2
    return 5; }
  _kvp_rlog ENGINE_STARTED
  _kvp_rnote "waiting for the engine API (up to 30 min)..."
  for t in $(seq 1 180); do
    if bash -c "$api_cmd" >/dev/null 2>&1; then
      _kvp_rlog API_OK
      _kvp_rnote "engine API is up"
      return 0
    fi
    sleep 10
  done
  echo "INCONCLUSIVE: the engine API did not come back within 30 min after docker start." >&2
  return 5
}

# THE one full restart used by both scenarios of test-pr-checks.sh (r32 R1).
# kvp_full_restart <scripts-dir> <container> <api-probe-cmd>
#   -> 0 ok, 5 any inconclusive stage (the stage already printed WHY)
kvp_full_restart(){
  local sd="$1" c="$2" api="$3" rc=0
  kvp_restart_stop_engine "$c"        || rc=$?
  [ "$rc" -eq 0 ] || return "$rc"
  kvp_restart_cache "$sd"             || return $?
  kvp_restart_wait_pid "${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}" || return $?
  kvp_restart_ipc "$sd" "$c"          || return $?
  kvp_restart_engine_up "$c" "$api"   || return $?
  return 0
}