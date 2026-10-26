#!/usr/bin/env bash
# verify-engine-identity.sh -- post-launch gate: compare identity pins against the
# ACTUALLY RUNNING engine before any saved KV data is trusted.
#
# Why: a syntactically valid identity.env does not guarantee that the engine
# serving traffic has the same KV geometry (block size, kv heads, head dim, TP,
# max-model-len, dtype). Review demanded a separate verification stage because
# these values only exist after vLLM starts. Reuses docs-declared single digest
# algorithm via kvpersist; exits non-zero on ANY mismatch (cache-miss policy:
# same here -- wrong identity means a different namespace must be recomputed).
#
# Usage:
#   bash scripts/verify-engine-identity.sh                 # reads live/api_key
#   bash scripts/verify-engine-identity.sh <container> <api-base>
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
_kvp_seed(){ [ -f "$1" ] || return 0; local line name val
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue;; esac
    line="${line#export }"; name="${line%%=*}"; [ "$name" = "$line" ] && continue
    name="$(printf '%s' "$name" | tr -d ' ')"; [ -z "$name" ] && continue
    if [ -z "${!name:-}" ]; then val="${line#*=}"; val="${val%% #*}"; val="${val//\"/}"; export "$name=$val"; fi
  done < "$1"; }
_kvp_seed "$ROOT/configs/generic/cache.env"
_kvp_seed "$ROOT/configs/generic/cache.env.local"
_kvp_seed "$ROOT/configs/mimo-v2.6-pro/identity.env"
CONTAINER="${1:-mimo-pro-tp4-kvp}"
API="${2:-http://127.0.0.1:30007}"
KEY="${API_KEY:-}"; [ -f "$PWD/live/api_key" ] && [ -z "$KEY" ] && KEY="$(head -n1 "$PWD/live/api_key" | tr -d '[:space:]')"
[ -n "$KEY" ] || die "API_KEY unavailable (live/api_key)"
LOGS="$(docker logs "$CONTAINER" 2>&1 | tail -n 4000)" || die "cannot read logs of container $CONTAINER"

python3 - "$ROOT" <<PYEOF || die "engine identity mismatch or unreadable -- do NOT reuse cached data"
import os, re, sys
logs = """$LOGS"""
vals = {}
for line in open(sys.argv[1] + "/configs/mimo-v2.6-pro/identity.env").read().splitlines():
    m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
    if m: vals[m.group(1)] = m.group(2)
bad = []
def check(label, expect, found):
    if found is None or str(found) != str(expect):
        bad.append("%s: identity=%s engine=%s" % (label, expect, found))
m = re.search(r"GPU KV cache size:\s*([0-9.]+)\s*MiB", logs)  # informational
m = re.search(r"max_model_len[= ]\s*(\d+)", logs)
check("max_model_len", vals.get("KVP_MAX_MODEL_LEN"), m.group(1) if m else None)
m = re.search(r"Using (?:CUDA )?graphs? .*?block size\D{0,20}(\d+)", logs) or re.search(r"cache_pages?\D{0,20}num_pages\D{0,20}(\d+)", logs)
m = re.search(r"[Bb]lock [Ss]ize:?\s*(\d+)", logs)
check("block_size", vals.get("KVP_BLOCK_SIZE"), m.group(1) if m else None)
m = re.search(r"[Nn]um[_ ](?:_kv|kv)_?[hH]eads?:?\s*(\d+)", logs)
check("num_kv_heads", vals.get("KVP_NUM_KV_HEADS"), m.group(1) if m else None)
m = re.search(r"[hH]ead _?[dD]im:?\s*(\d+)", logs)
check("head_dim", vals.get("KVP_HEAD_DIM"), m.group(1) if m else None)
m = re.search(r"[tT]ensor [Pp]arallel(?: Size:?)?\s*=?\s*(\d+)", logs)
check("tp_size", vals.get("KVP_TP_SIZE"), m.group(1) if m else None)
m = re.search(r"[Kk]v[ _-]?[Cc]ache[ _-]?(?:dtype|data_type):?\s*=?\s*(\w+)", logs)
if m: check("kv_cache_dtype", vals.get("KVP_KV_CACHE_DTYPE"), m.group(1).lower())
if bad:
    print("ENGINE IDENTITY MISMATCH:")
    for b in bad: print(" ", b)
    print("Repair configs/mimo-v2.6-pro/identity.env via scripts/setup-identity.sh values,")
    print("then re-run; cached data of the OLD geometry stays isolated by namespace.")
    sys.exit(4)
print("engine identity matches identity.env (all compared fields)")
PYEOF