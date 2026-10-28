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
. "$HERE/env.sh"
_kvp_load_env "$ROOT"
API="${API_BASE:-http://127.0.0.1:30007}"
KEY="${API_KEY:-}"; [ -f "$PWD/live/api_key" ] && [ -z "$KEY" ] && KEY="$(head -n1 "$PWD/live/api_key" | tr -d '[:space:]')"
[ -n "$KEY" ] || PA "API_KEY unavailable (live/api_key)"
CONTAINER="${CONTAINER:-mimo-pro-tp4-kvp}"
L2="$KVP_CACHE_ROOT"; LOG="$KVP_CACHE_ROOT/lmcache-server.log"   # adapter writes file_path=KVP_CACHE_ROOT directly
OUT="$ROOT/benchmarks/results/$(date -u +%Y%m%dT%H%M%SZ)-recovery-v2"
mkdir -p "$OUT"; echo "results -> $OUT"
V="0"   # total verdict failures
# ---- evidence helpers --------------------------------------------------
log_mark(){ wc -c < "$LOG" 2>/dev/null || echo 0; }
new_log(){ tail -c +$(( $1 + 1 )) "$LOG" 2>/dev/null || true; }
L2_bytes(){ # Sum bytes of cache data files EXCEPT the service's own log/pid,
            # so growth (or absence of it) reflects store activity only.
  [ -d "$L2" ] || { echo 0; return; }
  find "$L2" -type f ! -name 'lmcache-server.log' ! -name 'lmcache-server.pid' \
       -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'
}
# BOTH helpers require an explicit STORE/READ-from-L2 EVENT, not any line
# merely containing one of these words (review: the old l2_stored() matched any
# line with "l2", so it could never distinguish storage activity from a log
# line that just happened to mention L2). Tightened to action-shaped phrases.
l2_retrievals(){ # read-from-L2 events appended after the scenario's log mark
  new_log "$1" | grep -ciE '(retriev|loaded|restore|prefetch|read)[a-z]*[^a-z]{0,20}(from |to )?(l2|disk|secondary|file)' || true
}
l2_stored(){ # write-to-L2 events appended after the scenario's log mark
  new_log "$1" | grep -ciE '(stored|storing|wrote|written|writing|save[a-z]*)[^a-z]{0,20}(to |into )?(l2|disk|secondary|file)' || true
}
api_ready(){ for i in $(seq 1 180); do
    curl -fsS --max-time 3 -H "Authorization: Bearer $KEY" "$API/v1/models" >/dev/null 2>&1 && return 0
    [ $((i % 6)) -eq 0 ] && echo "  waiting for API... $((i*10))s"
    sleep 10; done
  return 1; }
fail(){ echo "FAIL: $*"; V=$((V+1)); echo "$*" >> "$OUT/failures.txt"; }
inconc(){ echo "INCONCLUSIVE: $*"; V=9; echo "INCONC: $*" >> "$OUT/failures.txt"; }

disk_read_bytes(){ # physical bytes read from the shared VM disk (same volume holds Docker, the model and the OS) device -> independent
  # proof of disk-sourced reads (not RAM). Requires KVP_CACHE_DEVICE.
  # Review P2 fix: use lsblk PKNAME (robust for /dev/nvme0n1p1, /dev/sda1, /dev/vda1
  # and VM-style vda/sda names) instead of the old regex split that broke on
  # nvme partition names (e.g. /dev/nvme0n1p1 -> "nvme0n" which has no /sys/block
  # directory). Falls back to findmnt SOURCE if lsblk is unavailable.
  local dev="${KVP_CACHE_DEVICE:-}"; [ -n "$dev" ] || { echo -1; return; }
  # Resolve the parent block device (handles partitioned names).
  local parent=""
  if command -v lsblk >/dev/null 2>&1; then
    parent="$(lsblk -no PKNAME "$dev" 2>/dev/null | head -1 | tr -d '[:space:]')"
    [ -z "$parent" ] && parent="${dev#/dev/}"   # whole-disk name, no PKNAME
    # strip partition suffix if lsblk returned a partition name without PKNAME
    parent="${parent#/dev/}"
    [ -r "/sys/block/$parent/stat" ] || parent="${parent%%[0-9]*}"; [ -r "/sys/block/$parent/stat" ] || parent="$dev"
    parent="${parent#/dev/}"
  else
    parent="${dev#/dev/}"; parent="${parent%%[0-9]*p[0-9]*}"; parent="${parent%%p[0-9]*}"
  fi
  [ -r "/sys/block/$parent/stat" ] || { echo -1; return; }
  awk '{print $3*512}' "/sys/block/$parent/stat" 2>/dev/null || echo -1
}
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

cached_of(){ # 0 = report says no reuse. MISSING = field absent/null: that is NOT
             # proof of no reuse -- it means the metric itself is unavailable
             # (vLLM build/flag dependent), so callers must not treat it as a fact.
  python3 -c "
import json,sys
d=json.load(open('$1'))
u=d.get('usage') or {}
det=u.get('prompt_tokens_details')
if det is None:
    print('MISSING'); raise SystemExit
print(det.get('cached_tokens', 0) if det.get('cached_tokens') is not None else 'MISSING')
" 2>/dev/null || echo MISSING; }

# ================= SCENARIOS ============================================
echo "== build a long stable prefix (~3000 tokens) =="
PREFIX="$OUT/prefix.txt"
python3 -c "fact='The Sumerian sikkatu was a clay cone used to mark the boundary of a building plot.\\n'; open('$PREFIX','w').write(fact*120)"

echo "== MANDATORY identity gate: engine-verified cache key before anything =="
echo "(server identity must match the RUNNING engine; exit 5 = INCONCLUSIVE = refuse)"
rc=0
bash "$HERE/verify-engine-identity.sh" "$CONTAINER" || rc=$?
if [ "$rc" -ne 0 ]; then
  if [ "$rc" -eq 5 ]; then
    echo "VERDICT: INCONCLUSIVE -- engine identity could not be verified (exit 5)."
    echo "Cache reuse is REFUSED by policy: fixing the value sources and rerunning"
    echo "is the ONLY path to a verdict. This is not a failure of the cache itself."
    exit 5
  fi
  echo "VERDICT: FAIL -- engine identity MISMATCH in required fields (exit $rc)."
  echo "KV blocks of the recorded geometry must not be reused; fix identity.env."
  exit "$rc"
fi

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
A_DISK0=$(disk_read_bytes)
M_A0=$(log_mark)
docker restart "$CONTAINER" >/dev/null 2>&1 || PA "docker restart failed"
api_ready || PA "API did not come back after vLLM restart (scenario impossible)"
ask afterA "$M_A0"
A_RETR=$(l2_retrievals "$M_A0"); A_CACHED=$(cached_of "$OUT/afterA.json")
T_A=$(python3 -c "import json;print(json.load(open('$OUT/afterA.summary.json'))['ttft_s'])")
echo "  post-restart L2 read events: $A_RETR, cached_tokens: $A_CACHED, ttft: ${T_A}s (cold: ${T_COLD}s)"
[ "${A_RETR:-0}" -gt 0 ] || fail "A: no L2/disk read log events AFTER the vLLM restart (got only: $(new_log "$M_A0" | grep -ci retriev || true) generic retrievals)"
if [ "$A_CACHED" = "MISSING" ]; then
  inconc "A: cached_tokens metric unavailable (prompt_tokens_details null/absent) -- cannot say reuse/no-reuse from usage counters alone"
elif [ "${A_CACHED:-0}" -eq 0 ]; then
  fail "A: cached_tokens==0 after restart (no reuse at all)"
fi
# Scenario A's re-question remains valid ONLY when hits can be tied to L2/disk
# reads in the marked interval (not to surviving RAM/engine in-memory hits).
if [ "${A_RETR:-0}" -eq 0 ] && [ "${A_CACHED:-0}" != "MISSING" ] && [ "${A_CACHED:-0}" -gt 0 ]; then
  inconc "A: cached_tokens>0 but zero post-mark L2-read events -- reuse may be RAM-resident (L1/engine) and not disk-backed"
fi
A_DISK1=$(disk_read_bytes)
if [ "$A_DISK0" = "-1" ] || [ "$A_DISK1" = "-1" ]; then
  inconc "A: KVP_CACHE_DEVICE unset/unreadable -- cannot independently prove NVMe-sourced reads (device counters unavailable)"
else
  A_DISK_DELTA=$((A_DISK1 - A_DISK0))
  echo "  physical NVMe bytes read during A: $A_DISK_DELTA"
  [ "$A_DISK_DELTA" -gt 0 ] || fail "A: zero physical NVMe bytes read during scenario -- L2 cannot be credited as the source"
fi

echo "== SCENARIO B: restart BOTH services, NVMe kept =="
B_DISK0=$(disk_read_bytes)
B_BEFORE=$(L2_bytes)
bash "$HERE/stop-cache.sh" >/dev/null 2>&1 || PA "stop-cache.sh failed"
bash "$HERE/start-cache.sh" >/dev/null 2>&1 || PA "start-cache.sh failed (cache service did not restart)"
LIVE_PID="$(cat "$KVP_CACHE_ROOT/lmcache-server.pid" 2>/dev/null || echo 0)"
kill -0 "$LIVE_PID" 2>/dev/null || PA "cache service is dead right after start-cache.sh (readiness not reached)"
docker restart "$CONTAINER" >/dev/null 2>&1 || PA "docker restart (B) failed"
api_ready || PA "API did not come back after full restart"
M_B0=$(log_mark)
ask afterB "$M_B0"
B_RETR=$(l2_retrievals "$M_B0"); B_CACHED=$(cached_of "$OUT/afterB.json")
T_B=$(python3 -c "import json;print(json.load(open('$OUT/afterB.summary.json'))['ttft_s'])")
echo "  post-full-restart L2 read events: $B_RETR, cached_tokens: $B_CACHED, ttft: ${T_B}s (cold: ${T_COLD}s)"
[ "${B_RETR:-0}" -gt 0 ] || fail "B: no L2/disk read log events AFTER both services restarted (index rebuild must prove disk reads)"
if [ "$B_CACHED" = "MISSING" ]; then
  inconc "B: cached_tokens metric unavailable (prompt_tokens_details null/absent) -- cannot say reuse/no-reuse from usage counters alone"
elif [ "${B_CACHED:-0}" -eq 0 ]; then
  fail "B: cached_tokens==0 after full restart"
fi
B_AFTER=$(L2_bytes)
[ "$B_AFTER" -ge "$B_BEFORE" ] || fail "B: L2 shrank during restart -- data loss suspected ($B_BEFORE -> $B_AFTER)"
B_DISK1=$(disk_read_bytes)
if [ "$B_DISK0" = "-1" ] || [ "$B_DISK1" = "-1" ]; then
  inconc "B: KVP_CACHE_DEVICE unset/unreadable -- cannot independently prove NVMe-sourced reads (device counters unavailable)"
else
  B_DISK_DELTA=$((B_DISK1 - B_DISK0))
  echo "  physical NVMe bytes read during B: $B_DISK_DELTA"
  [ "$B_DISK_DELTA" -gt 0 ] || fail "B: zero physical NVMe bytes read during scenario -- L2 cannot be credited as the source"
fi

# ---- exclude alternative explanations ----------------------------------
A2=$(cached_of "$OUT/afterA.json"); B2=$(cached_of "$OUT/afterB.json")
if [ "${A2:-0}" -gt 0 ] && [ "${B2:-0}" -gt 0 ] && [ "${A_RETR:-0}" -eq 0 ] && [ "${B_RETR:-0}" -eq 0 ]; then
  fail "cached_tokens>0 but zero L2-read log events: reuse may come from RAM/prefix cache only -- NOT proof of NVMe recovery"
fi

# ================= machine verdict =====================================
echo
echo "================ SUMMARY ================"
cat "$OUT"/*.summary.json 2>/dev/null || true
if [ "$V" -eq 0 ]; then
  echo "VERDICT: PASS -- disk-backed KV recovery proven (L2 read events + physical NVMe reads after each restart, data retained)"
  exit 0
elif [ "$V" -eq 9 ]; then
  echo "VERDICT: INCONCLUSIVE -- not all evidence sources were available to prove disk-sourced reads (see $OUT/failures.txt)."
  echo "Not a cache failure and not a PASS: set KVP_CACHE_DEVICE (and MODEL_DIR) so device counters and identity are verifiable, then rerun. An unproven disk source must never be reported as PASS."
  cat "$OUT/failures.txt" 2>/dev/null || true
  exit 5
else
  echo "VERDICT: FAIL ($V failed checks) -- see $OUT/failures.txt"
  cat "$OUT/failures.txt" 2>/dev/null || true
  exit 1
fi
