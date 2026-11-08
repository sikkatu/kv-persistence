#!/usr/bin/env bash
# stop-engine.sh — stop the vLLM engine container started by
# run-vllm-with-persist.sh. The cache server is NOT touched (use
# stop-cache.sh for it); persistent cache data is untouched either way.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/env.sh"
_kvp_load_env "$ROOT"
# Round 70 (review checklist): ONE container-name source. start-all.sh
# (KVP_ENGINE=recipe) records the effective name in live/engine_container --
# stop whatever was ACTUALLY started, not a hardcoded guess. Explicit
# CONTAINER= still wins; the kv launcher's default remains the fallback.
if [ -z "${CONTAINER:-}" ] && [ -f "$ROOT/live/engine_container" ]; then
  CONTAINER="$(head -n1 "$ROOT/live/engine_container" | tr -d '[:space:]')"
fi
CONTAINER="${CONTAINER:-mimo-pro-tp4-kvp}"
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found — cannot stop the engine container" >&2
  exit 1
fi
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "no engine container named $CONTAINER — nothing to stop"
  echo "(if the engine runs under another name: docker ps)"
  exit 0
fi
_state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)"
echo "stopping engine container $CONTAINER (state: $_state)"
echo "the cache server is NOT touched — stop it separately with: bash scripts/stop-cache.sh"
docker rm -f "$CONTAINER" >/dev/null 2>&1
echo "engine stopped. persistent cache data preserved under ${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}"
echo "verify with: bash scripts/budget-report.sh   (usage must be unchanged)"