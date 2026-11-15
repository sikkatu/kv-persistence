#!/usr/bin/env bash
# collect-crash-evidence.sh -- round 90 C1: snapshot EVERYTHING needed to
# diagnose an engine crash (LMCache vLLM adapter race, OOM, CUDA/Xid, ORM
# dead server) BEFORE the crashed container is recreated/removed.
#
# Why: the round 90 incident (`EngineCore` -> `assert req_id in self.requests`
# -> `EngineDeadError`) left NO artifacts -- by the time anyone looked, the
# container was recreated and the only clue was a RESTART reason vLLM showed
# nowhere. This script fixes that: it is called by the recipe launchers and
# stop-all/start-engine paths on any non-operator stop (or before a recreate),
# dumps evidence to live/crash-<UTC-TIME>/ and rotates to the newest 5 dirs.
#
# Usage: bash scripts/collect-crash-evidence.sh [CONTAINER] [LMCACHE_LOG]
#   CONTAINER    engine container name (default: live/engine_container, then
#                $CONTAINER, then mimo-pro-tp4-hotsplit)
#   LMCACHE_LOG  path to lmcache-server.log (default: $KVP_CACHE_ROOT/
#                lmcache-server.log, KVP_CACHE_ROOT=/var/lib/kv-persistence/l2)
# Exit 0 in any case -- evidence collection must never kill a start/stop flow.
# Prints the evidence dir path on stdout as the LAST line.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
warn(){ echo "WARNING: $*" >&2; }

CONTAINER="${1:-${CONTAINER:-}}"
if [ -z "$CONTAINER" ] && [ -f "$ROOT/live/engine_container" ]; then
  CONTAINER="$(tr -d '[:space:]' < "$ROOT/live/engine_container")"
fi
CONTAINER="${CONTAINER:-mimo-pro-tp4-hotsplit}"

KVP_CACHE_ROOT="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}"
LMCACHE_LOG="${2:-$KVP_CACHE_ROOT/lmcache-server.log}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CRASH_DIR="$ROOT/live/crash-$STAMP"
if ! mkdir -p "$CRASH_DIR" 2>/dev/null; then
  warn "cannot create $CRASH_DIR -- skipping evidence collection"
  exit 0
fi

# --- 1. engine container logs + state ------------------------------------
if command -v docker >/dev/null 2>&1; then
  docker logs "$CONTAINER" > "$CRASH_DIR/engine-docker.log" 2>&1 \
    || warn "docker logs $CONTAINER failed ($CRASH_DIR/engine-docker.log will be empty)"
  docker inspect "$CONTAINER"    > "$CRASH_DIR/engine-inspect.json" 2>&1 \
    || warn "docker inspect $CONTAINER failed"
  # exit reason in one grep-able line (state + error + finished time)
  docker inspect -f 'State={{.State.Status}} ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} Error={{.State.Error}} FinishedAt={{.State.FinishedAt}}' \
    "$CONTAINER" > "$CRASH_DIR/engine-state.txt" 2>/dev/null || true
else
  warn "docker not found -- only log files will be collected"
fi

# --- 2. lmcache-server.log: tail + grep with line numbers ----------------
if [ -f "$LMCACHE_LOG" ]; then
  tail -n 500 "$LMCACHE_LOG" > "$CRASH_DIR/lmcache-server-tail500.log" 2>/dev/null || true
  grep -nE 'ERROR|Traceback|Exception|not found during retrieve|Cannot store keys' "$LMCACHE_LOG" \
    > "$CRASH_DIR/lmcache-errors-numbered.log" 2>/dev/null \
    || echo "(no ERROR/Traceback/Exception lines matched)" > "$CRASH_DIR/lmcache-errors-numbered.log"
else
  warn "lmcache log not found at $LMCACHE_LOG (pass the path as \$2 if it lives elsewhere)"
  echo "(file missing at collection time: $LMCACHE_LOG)" > "$CRASH_DIR/lmcache-server-tail500.log"
fi

# --- 3. host resource + GPU state at crash time --------------------------
{
  echo "### date -u"; date -u
  echo "### nvidia-smi -q -d MEMORY"
  nvidia-smi -q -d MEMORY 2>&1 || echo "(nvidia-smi unavailable)"
  echo "### free -h"
  free -h 2>&1 || echo "(free unavailable)"
  echo "### dmesg (Xid|NVRM|oom)"
  dmesg 2>/dev/null | grep -iE 'Xid|NVRM|oom|Out of memory' | tail -40 || echo "(dmesg unavailable or filtered empty)"
} > "$CRASH_DIR/host-state.txt" 2>&1

# --- 4. rotation: keep only the newest 5 crash-* dirs -------------------
# (round 90 C1: storage hygiene, no human wants 100 crash dirs in live/)
CRASH_ROOT="$(dirname "$CRASH_DIR")"
if [ -d "$CRASH_ROOT" ]; then
  # newest 5 stay; any older crash-* dir is removed (inclusive of files)
  mapfile -t _all < <(ls -1dt "$CRASH_ROOT"/crash-* 2>/dev/null)
  for (( i=5; i<${#_all[@]}; i++ )); do
    rm -rf "${_all[$i]}" 2>/dev/null || true
  done
fi

echo "[crash-evidence] engine evidence collected in: $CRASH_DIR"
echo "$CRASH_DIR"