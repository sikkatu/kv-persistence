#!/usr/bin/env bash
# test-recovery.sh -- Scenarios A/B: prove KV blocks are recovered from NVMe
# after restarts. The acceptance core of the project: a speed-up WITHOUT proof
# of disk-backed recovery does NOT count as a pass.
#
# Scenario A: vLLM restarts, cache service stays up.
# Scenario B: BOTH services restart, NVMe data kept.
# In both: warm cache -> restart -> resend SAME prompt -> verify recovery via
# (1) LMCache log lines 'Retrieved ... from L2/disk', (2) usage.cached_tokens > 0,
# (3) measured TTFT drop vs cold baseline.
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$ROOT/configs/generic/cache.env"
[ -f "$ROOT/configs/generic/cache.env.local" ] && . "$ROOT/configs/generic/cache.env.local"
. "$ROOT/configs/generic/lmcache-server.env"
API="${API_BASE:-http://127.0.0.1:30007}"
KEY="${API_KEY:-}"
[ -f "$PWD/live/api_key" ] && [ -z "$KEY" ] && KEY="$(head -n1 "$PWD/live/api_key" | tr -d '[:space:]')"
[ -n "$KEY" ] || die "API_KEY unavailable (live/api_key)"
CONTAINER="${CONTAINER:-mimo-pro-tp4-kvp}"
OUT="$ROOT/benchmarks/results/$(date -u +%Y%m%dT%H%M%SZ)-recovery"
mkdir -p "$OUT"; echo "results -> $OUT"
# long stable prefix (~3000 tokens) + deterministic tail
PREFIX="$OUT/prefix.txt"
python3 -c "fact='The Sumerian sikkatu was a clay cone used to mark the boundary of a building plot.\n'; open('$PREFIX','w').write(fact*120)"
send(){ # send <label> -> json per call in $OUT
  local label="$1" t0 t1
  t0=$(date +%s.%N)
  python3 - "$API" "$KEY" "$PREFIX" "$OUT/$label.json" <<'PYEOF'
import json,sys,time,urllib.request
api,key,prefix_file,out=sys.argv[1:5]
body=json.dumps({"model":"mimo-pro","messages":[
  {"role":"system","content":"You are a careful assistant."},
  {"role":"user","content":open(prefix_file).read()+"\nQ: what is a sikkatu? one short sentence."}],
  "max_tokens":32,"temperature":0,"stream":True,
  "stream_options":{"include_usage":True}}).encode()
req=urllib.request.Request(api+"/v1/chat/completions",data=body,
  headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
ttft=None; usage={}
with urllib.request.urlopen(req,timeout=1800) as r:
  for raw in r:
    line=raw.decode().strip()
    if not line.startswith("data: "): continue
    p=line[6:]
    if p=="[DONE]": break
    o=json.loads(p)
    if o.get("choices") and o["choices"][0].get("delta",{}).get("content") and ttft is None:
      ttft=time.time()
    if o.get("usage"): usage=o["usage"]
json.dump({"ttft_abs":ttft,"usage":usage},open(out,"w"),indent=2)
PYEOF
  t1=$(date +%s.%N)
  python3 -c "import json; d=json.load(open('$OUT/$label.json')); u=d['usage']; print(json.dumps({'label':'$label','ttft_s':round(max(0,(d['ttft_abs'] or 0)-$t0),2),'total_s':round($t1-$t0,2),'prompt_tokens':u.get('prompt_tokens',0),'cached_tokens':u.get('prompt_tokens_details',{}).get('cached_tokens',0)}))" | tee "$OUT/$label.summary.json"
}
cache_bytes(){ du -sb "$KVP_CACHE_ROOT/l2" 2>/dev/null | cut -f1 || echo 0; }
echo "== warm run (cold persistent cache expected) =="
send warm
B1=$(cache_bytes); echo "cache bytes after warm: $B1"
echo "== Scenario A: restart vLLM only (cache service alive) =="
docker restart "$CONTAINER" >/dev/null 2>&1 || die "docker restart failed"
for i in $(seq 1 180); do
  curl -fsS --max-time 3 -H "Authorization: Bearer $KEY" "$API/v1/models" >/dev/null 2>&1 && break
  [ $((i % 6)) = 0 ] && echo "  waiting for API... $((i*10))s"
  sleep 10
done
send afterA
B2=$(cache_bytes); echo "cache bytes after A: $B2 (delta $((B2-B1)))"
grep -ci 'retriev' "$KVP_CACHE_ROOT/lmcache-server.log" > "$OUT/scenarioA.retrieved_linecount" || echo 0 > "$OUT/scenarioA.retrieved_linecount"
echo "lmcache 'Retrieved' lines so far: $(cat "$OUT/scenarioA.retrieved_linecount")"
echo "== Scenario B: restart BOTH services, NVMe kept =="
bash "$HERE/stop-cache.sh" >/dev/null 2>&1 || true
bash "$HERE/start-cache.sh" >/dev/null 2>&1 || die "cache restart failed"
docker restart "$CONTAINER" >/dev/null 2>&1 || die "vllm restart failed"
for i in $(seq 1 180); do
  curl -fsS --max-time 3 -H "Authorization: Bearer $KEY" "$API/v1/models" >/dev/null 2>&1 && break
  [ $((i % 6)) = 0 ] && echo "  waiting for API... $((i*10))s"
  sleep 10
done
send afterB
B3=$(cache_bytes); echo "cache bytes after B: $B3 (delta $((B3-B2)))"
cat "$OUT"/*.summary.json
echo "VERDICT (manual rule): PASS only if cached_tokens in afterA/afterB > 0 AND"
echo "  the lmcache log shows retrieval-from-disk entries after each restart."
echo "  A speed-up alone is NOT proof. See docs/benchmarks.md for the matrix."
