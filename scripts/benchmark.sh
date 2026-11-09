#!/usr/bin/env bash
# benchmark.sh -- measure TTFT / prefill across the 5 required configurations:
#   1 baseline in-memory only, 2 cold persistent, 3 warm same process,
#   4 after vLLM restart, 5 after both services restart (if supported).
# Writes one JSON line per measurement to benchmarks/results/<ts>/bench.jsonl.
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$ROOT/configs/generic/cache.env"
[ -f "$ROOT/configs/generic/cache.env.local" ] && . "$ROOT/configs/generic/cache.env.local"
API="${API_BASE:-http://127.0.0.1:30007}"
KEY="${API_KEY:-}"
[ -f "$PWD/live/api_key" ] && [ -z "$KEY" ] && KEY="$(head -n1 "$PWD/live/api_key" | tr -d '[:space:]')"
[ -n "$KEY" ] || die "API_KEY unavailable"
SCENARIO="${1:-warm}"
TOKENS="${2:-3000}"   # approx prompt scale in tokens; content is generated fact-repeat
OUT="$ROOT/benchmarks/results/$(date -u +%Y%m%dT%H%M%SZ)-bench"
mkdir -p "$OUT"; OUT="$OUT/bench.jsonl"
PROMPT="$(python3 -c "fact='The Sumerian sikkatu was a clay cone used to mark the boundary of a building plot.\n'; n=max(1,$TOKENS//10); print(fact*n)")"
# Review fix (round 37): a huge PROMPT via an env var can exceed the exec
# environment limit for large TOKENS -- write it to a temp file instead.
PROMPT_FILE="$(mktemp /tmp/kvp-bench-prompt.XXXXXX)"
printf '%s' "$PROMPT" > "$PROMPT_FILE"
trap 'rm -f "$PROMPT_FILE"' EXIT
PROMPT_FILE="$PROMPT_FILE" SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-mimo-pro}" python3 - "$API" "$KEY" "$SCENARIO" "$OUT" <<'PYEOF'
import json,sys,time,urllib.request
api,key,scenario,out=sys.argv[1:5]
prompt=open('/dev/stdin','r').read() if False else None
body=json.dumps({"model":__import__('os').environ.get("SERVED_MODEL_NAME","mimo-pro"),"messages":[
  {"role":"system","content":"You are a careful assistant."},
  {"role":"user","content":(open(__import__('os').environ.get('PROMPT_FILE','/dev/null')).read() or 'sikkatu')+
   "\nQ: one short sentence defining a sikkatu."}],
  "max_tokens":32,"temperature":0,"stream":True,
  "stream_options":{"include_usage":True}}).encode()
req=urllib.request.Request(api+"/v1/chat/completions",data=body,
  headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
t0=time.time(); ttft=None; usage={}
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
t1=time.time()
rec={"ts":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"scenario":scenario,
     "prompt_tokens":usage.get('prompt_tokens',0),
     "cached_tokens":usage.get('prompt_tokens_details',{}).get('cached_tokens',0),
     "ttft_s":round((ttft or t1)-t0,3),"total_s":round(t1-t0,3)}
open(out,'a').write(json.dumps(rec)+'\n')
print(json.dumps(rec))
PYEOF
echo "recorded -> $OUT"
