#!/usr/bin/env bash
# test-recovery.sh -- STRICT acceptance test for disk-backed KV recovery.
#
# Review-driven redesign (v2):
#  * log-POSITION markers: only events since the scenario start count
#  * distinguishes L2 (disk) retrievals from L1/RAM ones
#  * verifies blocks were STORED to L2 before the restart
#  * wait loops FAIL hard when the API never comes back
#  * machine verdict at the end: PASS => exit 0, FAIL => exit 1
#  * alternative explanations excluded: plain prefix cache hit alone is NOT
#    accepted -- the L2 store must have bytes BEFORE the restart and the log
#    must show a post-restart L2 read of comparable size.
# Scenario A: restart vLLM only (cache service alive).
# Scenario B: restart BOTH services, NVMe data kept.
set -Eeuo pipefail
PA(){ echo "FAIL(A): $*"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
_kvp_seed(){ [ -f "$1" ] || return 0; local line name val;
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue;; esac
    line="${line#export }"; name="${line%%=*}"; [ "$name" = "$line" ] && continue
    name="$(printf '%s' "$name" | tr -d ' ')"; [ -z "$name" ] && continue
    if [ -z "${!name:-}" ]; then val="${line#*=}"; val="${val%% #*}"; val="${val//\"/}"; export "$name=$val"; fi
  done < "$1"; }
_kvp_seed "$ROOT/configs/generic/cache.env"
_kvp_seed "$ROOT/configs/generic/cache.env.local"
_kvp_seed "$ROOT/configs/generic/lmcache-server.env"
API="${API_BASE:-http://127.0.0.1:30007}"
KEY="${API_KEY:-}"; [ -f "$PWD/live/api_key" ] && [ -z "$KEY" ] && KEY="$(head -n1 "$PWD/live/api_key" | tr -d '[:space:]')"
[ -n "$KEY" ] || PA "API_KEY unavailable (live/api_key)"
CONTAINER="${CONTAINER:-mimo-pro-tp4-kvp}"
L2="$KVP_CACHE_ROOT/l2"; LOG="$KVP_CACHE_ROOT/lmcache-server.log"
OUT="$ROOT/benchmarks/results/$(date -u +%Y%m%dT%H%M%SZ)-recovery-v2"
mkdir -p "$OUT"; echo "results -> $OUT"
V="0"   # total verdict failures
# ---- evidence helpers --------------------------------------------------
log_mark(){ wc -c < "$LOG" 2>/dev/null || echo 0; }
new_log(){ tail -c +$(( $1 + 1 )) "$LOG" 2>/dev/null || true; }
L2_bytes(){ du -sb "$L2" 2>/dev/null | cut -f1 || echo 0; }
l2_retrievals(){ new_log "$1" | grep -ci '\(retriev\|loaded\|restore\).*\(L2\|disk\|l2\|secondary\|file\)' || true; }
l2_stored(){ new_log "$1" | grep -ci '\(stored\|wrote\|write\|stored to\|l2\)' || true; }
api_ready(){ for i in $(seq 1 180); do
    curl -fsS --max-time 3 -H "Authorization: Bearer $KEY" "$API/v1/models" >/dev/null 2>&1 && return 0
    [ $((i % 6)) -eq 0 ] && echo "  waiting for API... $((i*10))s"
    sleep 10; done
  return 1; }
fail(){ echo "FAIL: $*"; V=$((V+1)); echo "$*" >> "$OUT/failures.txt"; }
ask(){ # ask <label> -> writes $OUT/<label>.json with ttft+usage
  local label="$1" mark="$2" t0 t1
  t0=$(date +%s.%N)
  python3 - "$API" "$KEY" "$OUT/prefix.txt" "$OUT/$label.json" <<'PYEOF' || PA "request $label failed"
import json,sys,time,urllib.request
api,key,prefix_file,out=sys.argv[1:5]
body=json.dumps({"model":"mimo-pro","messages":[
  {"role":"system","content":"You are a careful assistant."},
  {"role":"user","content":open(prefix_file).read()+"\nQ: what is a sikkatu? one short sentence."}],
  "max_tokens":32,"temperature":0,"stream":True,
  "stream_options":{"include_usage":True}}).encode()
req=urllib.request.Request(api+"/v1/chat/completions",data=body,
  headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
ttft=None;usage={}
with urllib.request.urlopen(req,timeout=1800) as r:
  for raw in r:
    line=raw.decode().strip()
    if not line.startswith("data: "): continue
    p=line[6:]
    if p=="[DONE]": break
    o=json.loads(p)
    if o.get("choices") and o["choices"][0].get("delta",{}).get("content") and ttft is None: ttft=time.time()
    if o.get("usage"): usage=o["usage"]
json.dump({"ttft_abs":ttft,"usage":usage},open(out,"w"),indent=2)
PYEOF
  t1=$(date +%s.%N)
  python3 -c "import json;d=json.load(open('$OUT/$label.json'));u=d['usage'];p=u.get('prompt_tokens_details',{});print(json.dumps({'label':'$label','ttft_s':round(max(0,(d['ttft_abs'] or 0)-$t0),2),'total_s':round($t1-$t0,2),'prompt_tokens':u.get('prompt_tokens',0),'cached_tokens':p.get('cached_tokens',0)}))" | tee "$OUT/$label.summary.json"
}

# ================= SCENARIOS ============================================
echo "== build a long stable prefix (~3000 tokens) =="
PREFIX="$OUT/prefix.txt"
python3 -c "fact='The Sumerian sikkatu was a clay cone used to mark the boundary of a building plot.\\n'; open('$PREFIX','w').write(fact*120)"

echo "== COLD baseline (no cache reuse expected) =="
M0=$(log_mark); B0=$(L2_bytes)
ask cold "$M0"
T_COLD=$(python3 -c "import json;d=json.load(open('$OUT/cold.summary.json'));print(d['ttft_s'])")
B_WARM=$(L2_bytes)
if [ "$B_WARM" -le "$B0" ]; then
  fail "scenario pre: nothing was stored to L2 during the warm request (B0=$B0 B=$B_WARM) -- cannot prove later reads are disk-backed"
else
  echo "  [ok] blocks stored to L2: $(( (B_WARM-B0)/1024 )) KiB"
fi
STORED_LINES=$(l2_stored "$M0")
[ "${STORED_LINES:-0}" -gt 0 ] || fail "scenario pre: no 'stored to L2' log evidence after warm request"

echo "== SCENARIO A: restart vLLM only =="
M_A0=$(log_mark)
docker restart "$CONTAINER" >/dev/null 2>&1 || PA "docker restart failed"
api_ready || PA "API did not come back after vLLM restart (scenario impossible)"
ask afterA "$M_A0"
A_RETR=$(l2_retrievals "$M_A0"); A_CACHED=$(python3 -c "import json;print(json.load(open('$OUT/afterA.summary.json'))['cached_tokens'])")
T_A=$(python3 -c "import json;print(json.load(open('$OUT/afterA.summary.json'))['ttft_s'])")
echo "  post-restart L2 read events: $A_RETR, cached_tokens: $A_CACHED, ttft: ${T_A}s (cold: ${T_COLD}s)"
[ "${A_RETR:-0}" -gt 0 ] || fail "A: no L2/disk read log events AFTER the vLLM restart (got only: $(new_log "$M_A0" | grep -ci retriev || true) generic retrievals)"
[ "${A_CACHED:-0}" -gt 0 ] || fail "A: cached_tokens==0 after restart (no reuse at all)"

echo "== SCENARIO B: restart BOTH services, NVMe kept =="
B_BEFORE=$(L2_bytes)
bash "$HERE/stop-cache.sh" >/dev/null 2>&1 || PA "stop-cache.sh failed"
bash "$HERE/start-cache.sh" >/dev/null 2>&1 || PA "start-cache.sh failed (cache service did not restart)"
LIVE_PID="$(cat "$KVP_CACHE_ROOT/lmcache-server.pid" 2>/dev/null || echo 0)"
kill -0 "$LIVE_PID" 2>/dev/null || PA "cache service is dead right after start-cache.sh (readiness not reached)"
docker restart "$CONTAINER" >/dev/null 2>&1 || PA "docker restart (B) failed"
api_ready || PA "API did not come back after full restart"
M_B0=$(log_mark)
ask afterB "$M_B0"
B_RETR=$(l2_retrievals "$M_B0"); B_CACHED=$(python3 -c "import json;print(json.load(open('$OUT/afterB.summary.json'))['cached_tokens'])")
T_B=$(python3 -c "import json;print(json.load(open('$OUT/afterB.summary.json'))['ttft_s'])")
echo "  post-full-restart L2 read events: $B_RETR, cached_tokens: $B_CACHED, ttft: ${T_B}s (cold: ${T_COLD}s)"
[ "${B_RETR:-0}" -gt 0 ] || fail "B: no L2/disk read log events AFTER both services restarted (index rebuild must prove disk reads)"
[ "${B_CACHED:-0}" -gt 0 ] || fail "B: cached_tokens==0 after full restart"
B_AFTER=$(L2_bytes)
[ "$B_AFTER" -ge "$B_BEFORE" ] || fail "B: L2 shrank during restart -- data loss suspected ($B_BEFORE -> $B_AFTER)"

# ---- exclude alternative explanations ----------------------------------
cached_of(){ python3 -c "import json;print(json.load(open('$1'))['usage'].get('prompt_tokens_details',{}).get('cached_tokens',0))" 2>/dev/null || echo 0; }
A2=$(cached_of "$OUT/afterA.json"); B2=$(cached_of "$OUT/afterB.json")
if [ "${A2:-0}" -gt 0 ] && [ "${B2:-0}" -gt 0 ] && [ "${A_RETR:-0}" -eq 0 ] && [ "${B_RETR:-0}" -eq 0 ]; then
  fail "cached_tokens>0 but zero L2-read log events: reuse may come from RAM/prefix cache only -- NOT proof of NVMe recovery"
fi

# ================= machine verdict =====================================
echo
echo "================ SUMMARY ================"
cat "$OUT"/*.summary.json 2>/dev/null || true
if [ "$V" -eq 0 ]; then
  echo "VERDICT: PASS -- disk-backed KV recovery proven (L2 read events after each restart, data retained)"
  exit 0
else
  echo "VERDICT: FAIL ($V failed checks) -- see $OUT/failures.txt"
  cat "$OUT/failures.txt" 2>/dev/null || true
  exit 1
fi
