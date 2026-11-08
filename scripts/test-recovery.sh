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
# Round 52: PA() must not always say "FAIL(A)" -- it is used in scenarios A
# and B alike. Pass the scenario tag explicitly.
PA(){ echo "FAIL(${1:-?}): ${*:2}"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/env.sh"
_kvp_load_env "$ROOT"
API="${API_BASE:-http://127.0.0.1:30007}"
KEY="${API_KEY:-}"; [ -f "$PWD/live/api_key" ] && [ -z "$KEY" ] && KEY="$(head -n1 "$PWD/live/api_key" | tr -d '[:space:]')"
# Round 63 (review): the bearer key must not appear in `ps` -- write it to a
# chmod-600 curl config file and use curl -K instead of -H on the command line.
_kvp_curl_header="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}/live/.curl-header-test"
mkdir -p "$(dirname "$_kvp_curl_header")"
umask 077; printf 'header = "Authorization: Bearer %s"\n' "$KEY" > "$_kvp_curl_header"; chmod 600 "$_kvp_curl_header"

[ -n "$KEY" ] || PA pre "API_KEY unavailable (live/api_key)"
CONTAINER="${CONTAINER:-mimo-pro-tp4-kvp}"
# Review fix: the served model name is deployment-specific, not a project
# constant. Default matches the reference MiMo launcher (SERVED_MODEL_NAME).
# EXPORTED: the ask() heredoc is shell-quoted (no expansion), so the Python
# block reads it from the environment via os.environ.
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-mimo-pro}"
export SERVED_MODEL_NAME
L2="$KVP_CACHE_ROOT"; LOG="$KVP_CACHE_ROOT/lmcache-server.log"   # adapter writes file_path=KVP_CACHE_ROOT directly
# Review fix (round 17): L2 store/load events are logged at DEBUG level in the
# pinned LMCache 0.5.6 (see l2_retrievals/l2_stored comment above). The cache
# service must therefore run with LMCACHE_LOG_LEVEL=DEBUG for the acceptance
# evidence to exist in the log at all. Exported here so every start-cache.sh
# invocation in this script inherits it.
export LMCACHE_LOG_LEVEL="${LMCACHE_LOG_LEVEL:-DEBUG}"
OUT="$ROOT/benchmarks/results/$(date -u +%Y%m%dT%H%M%SZ)-recovery-v2"
mkdir -p "$OUT"; echo "results -> $OUT"
V="0"   # hard failures (FAIL)
VI="0"  # inconclusive findings (INCONCLUSIVE)
# ---- evidence helpers --------------------------------------------------
log_mark(){ wc -c < "$LOG" 2>/dev/null || echo 0; }
new_log(){ tail -c +$(( $1 + 1 )) "$LOG" 2>/dev/null || true; }
L2_bytes(){ # Sum bytes of cache data files EXCEPT the service's own log/pid and
            # the tmp_guard registry dir, so growth (or absence of it) reflects
            # store activity only. Review fix (round 18): the registry markers
            # under .kvp-active-writes/ are transient bookkeeping, NOT cache
            # data -- counting them could produce a false "L2 shrank" when a
            # marker disappears between two samples.
  [ -d "$L2" ] || { echo 0; return; }
  find "$L2" -type f ! -name 'lmcache-server.log' ! -name 'lmcache-server.pid' \
       ! -path "$L2/.kvp-active-writes/*" \
       -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}'
}
wait_l2_quiet(){ # wait until L2_bytes stops changing (async stores must finish)
  local prev=-1 cur=0 i
  for i in $(seq 1 30); do
    cur=$(L2_bytes)
    [ "$cur" = "$prev" ] && { echo "$cur"; return; }
    prev=$cur
    sleep 2
  done
  echo "$cur"
}
# BOTH helpers require an explicit STORE/READ-from-L2 EVENT, not any line
# merely containing one of these words (review: the old l2_stored() matched any
# line with "l2", so it could never distinguish storage activity from a log
# line that just happened to mention L2). Tightened to action-shaped phrases.
# Review fix (round 17): the patterns must match the REAL strings of the pinned
# LMCache 0.5.6 -- verified against the source (AST scan of every logger call):
#   * "L2 store completed: adapter %d, %d succeeded, %d failed"
#       (lmcache/v1/mp_observability/subscribers/logging/l2.py, DEBUG level)
#   * "L2 prefetch load completed: request %d, %d loaded, %d failed"
#       (same file, DEBUG level)
#   * "Prefetch request completed (L1+L2): %d/%d retained keys (%d L1, %d L2)..."
#       (lmcache/v1/distributed/storage_manager.py, INFO level)
# The old patterns matched none of these (only error lines like "Failed to
# read from file..."), so L2 evidence was unreachable. Additionally these
# events are DEBUG level, so the cache service must run with
# LMCACHE_LOG_LEVEL=DEBUG -- ensured below via KVP_LMCACHE_LOG_LEVEL.
l2_retrievals(){ # read-from-L2 events appended after the scenario's log mark
  new_log "$1" | grep -cE 'L2 prefetch load completed: request [0-9]+, [1-9][0-9]* loaded|Prefetch request completed \(L1\+L2\): .* \([1-9][0-9]* L1, [1-9][0-9]* L2\)' || true
}
l2_stored(){ # write-to-L2 events appended after the scenario's log mark
  new_log "$1" | grep -cE 'L2 store completed: adapter [0-9]+, [1-9][0-9]* succeeded' || true
}
api_ready(){ echo "  waiting for the engine API (container=$CONTAINER, up to 30 min)"; for i in $(seq 1 180); do
    # Review fix (round 46): the loop used to wait 20 min on a DEAD container
    # (the engine crashed with code 1 minutes after restart). Check the
    # container state and abort as soon as it is not running.
    _st="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
    case "$_st" in
      running) : ;;
      *) echo "  ABORT: container $CONTAINER is '$_st' -- the engine is dead, the API will never come back" >&2
         echo "         check: docker logs --tail 60 $CONTAINER" >&2
         return 2 ;;
    esac
    curl -fsS --max-time 3 -K "$_kvp_curl_header" "$API/v1/models" >/dev/null 2>&1 && return 0
    [ $((i % 6)) -eq 0 ] && echo "  waiting for API... $((i*10))s (container $_st)"
    sleep 10; done
  return 1; }
# Review round 13: inconc() used to SET V=9, erasing hard-FAIL counts -- two
# hard FAILs plus one INCONCLUSIVE wrongly reported INCONCLUSIVE. Failures and
# inconclusive findings are now counted separately: ANY hard FAIL dominates.
fail(){ echo "FAIL: $*"; V=$((V+1)); echo "$*" >> "$OUT/failures.txt"; }
inconc(){ echo "INCONCLUSIVE: $*"; VI=$((VI+1)); echo "$*" >> "$OUT/failures.txt"; }

# Round 55 (user, VM + LUKS): resolve KVP_CACHE_DEVICE automatically via the
# shared helper (env.sh) when it is not pinned. The result is recorded in
# live/cache_device so later runs skip the lookup. findmnt handles VM disks,
# LUKS mapper nodes and NVMe partitions alike.
if [ -z "${KVP_CACHE_DEVICE:-}" ]; then
  _auto_dev="$(_kvp_resolve_cache_device "${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}" || true)"
  if [ -n "$_auto_dev" ]; then
    KVP_CACHE_DEVICE="$_auto_dev"
    export KVP_CACHE_DEVICE
    mkdir -p "$ROOT/live"
    [ "$(head -n1 "$ROOT/live/cache_device" 2>/dev/null)" = "$_auto_dev" ] || printf '%s\n' "$_auto_dev" > "$ROOT/live/cache_device"
    echo "cache device auto-detected: $KVP_CACHE_DEVICE (override: KVP_CACHE_DEVICE=... or live/cache_device)"
  else
    echo "[info] cache device could not be auto-detected -- NVMe byte counters will be unavailable (set KVP_CACHE_DEVICE or live/cache_device)"
  fi
fi

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
  # Review fix (round 18): QUOTED heredoc (<<'PYEOF') so the Python block is
  # never expanded by the shell -- a stray $ or backtick inside it would
  # otherwise be interpreted (and `stream_options` already contains braces
  # that happen to be harmless, but any future edit with $ would break).
  python3 - "$API" "$KEY" "$OUT/prefix.txt" "$OUT/$label.json" <<'PYEOF' || PA pre "request $label failed"
import json,sys,time,urllib.request,os
api,key,prefix_file,out=sys.argv[1:5]
body=json.dumps({"model":os.environ.get("SERVED_MODEL_NAME","mimo-pro"),"messages":[
  {"role":"system","content":"You are a careful assistant."},
  {"role":"user","content":open(prefix_file).read()+"\nQ: what is a sikkatu? one short sentence."}],
  "max_tokens":32,"temperature":0,"stream":True,
  "stream_options":{"include_usage":True}}).encode()
req=urllib.request.Request(api+"/v1/chat/completions",data=body,
  headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
ttft=None;usage={}
# Review fix (round 53): a refused connection used to dump a raw Python
# traceback at the operator. Print a short actionable message instead.
try:
  with urllib.request.urlopen(req,timeout=1800) as r:
    for raw in r:
      line=raw.decode().strip()
      if not line.startswith("data: "): continue
      p=line[6:]
      if p=="[DONE]": break
      o=json.loads(p)
      if o.get("choices") and o["choices"][0].get("delta",{}).get("content") and ttft is None: ttft=time.time()
      if o.get("usage"): usage=o["usage"]
except urllib.error.URLError as e:
  print(f"REQUEST FAILED: cannot reach the engine at {api}: {e}", file=sys.stderr)
  print("  engine still loading, or it is not running -- check: docker ps / docker logs " + os.environ.get("CONTAINER","mimo-pro-tp4-kvp"), file=sys.stderr)
  sys.exit(3)
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
# Review round 13: the prefix must be UNIQUE per run. The old fixed text meant
# a repeat run's "cold" request was already in L2 from the previous run and the
# "nothing was stored" check failed spuriously (and a warm hit could masquerade
# as a fresh cold store). A UTC nonce is prepended so every run probes a
# genuinely cold prefix; the nonce is printed so a rerun can be reproduced.
RUN_NONCE="$(date -u +%Y%m%dT%H%M%S)-$$"
python3 -c "import sys; nonce='Run '+sys.argv[1]+' '; fact='The Sumerian sikkatu was a clay cone used to mark the boundary of a building plot.\\n'; open(sys.argv[2],'w').write(nonce + fact*120)" "$RUN_NONCE" "$PREFIX"
echo "  run nonce (cold-prefix identity): $RUN_NONCE"

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
# Review fix (round 53): the cold request used to fire IMMEDIATELY. If the
# engine was still loading (or stopped after a previous run) the request
# died with Connection refused and the operator saw a raw Python traceback.
# Wait for the API exactly like scenarios A/B do.
api_ready || PA pre "API is not up before the cold baseline -- start the engine first (bash scripts/run-vllm-with-persist.sh), wait for it to load, then rerun"
M0=$(log_mark); B0=$(L2_bytes)
ask cold "$M0"
T_COLD=$(python3 -c "import json;d=json.load(open('$OUT/cold.summary.json'));print(d['ttft_s'])")
# Review fix (round 18): L2 store is ASYNCHRONOUS -- measure B0/B_WARM only after
# the directory goes quiet, otherwise the byte delta races the write and can
# report a false "nothing was stored".
B_WARM=$(wait_l2_quiet)
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
docker restart "$CONTAINER" >/dev/null 2>&1 || PA A "docker restart failed"
api_ready || PA A "API did not come back after vLLM restart (scenario impossible)"
ask afterA "$M_A0"
A_RETR=$(l2_retrievals "$M_A0"); A_CACHED=$(cached_of "$OUT/afterA.json")
T_A=$(python3 -c "import json;print(json.load(open('$OUT/afterA.summary.json'))['ttft_s'])")
echo "  post-restart L2 read events: $A_RETR, cached_tokens: $A_CACHED, ttft: ${T_A}s (cold: ${T_COLD}s)"
# Review fix (round 18): scenario A restarts vLLM ONLY -- the cache service and
# its L1 (host RAM) are STILL ALIVE, so a perfectly working system serves the
# request from L1 and logs ZERO L2/disk reads. That is the documented "memory
# hit" outcome of this taxonomy, NOT a failure. Only scenario B (full restart,
# empty L1) proves disk-backed recovery. Therefore A accepts a memory hit and
# must not hard-FAIL for missing L2 evidence.
if [ "${A_CACHED}" = "MISSING" ]; then
  inconc "A: cached_tokens metric unavailable (prompt_tokens_details null/absent) -- cannot say reuse/no-reuse from usage counters alone"
elif [ "${A_CACHED:-0}" -eq 0 ]; then
  fail "A: cached_tokens==0 after restart (no reuse at all -- even L1/RAM reuse is missing)"
elif [ "${A_RETR:-0}" -gt 0 ]; then
  echo "  [ok] A: reuse with L2/disk read evidence (best case)"
else
  echo "  [ok] A: reuse WITHOUT L2 read events -- served from L1/RAM (memory hit)."
  echo "        Expected: the cache service and its L1 survived the vLLM restart."
  echo "        Disk-backed proof is scenario B's job; A is PASS on memory hit."
fi
A_DISK1=$(disk_read_bytes)
if [ "$A_DISK0" = "-1" ] || [ "$A_DISK1" = "-1" ]; then
  inconc "A: KVP_CACHE_DEVICE unset/unreadable -- cannot independently prove NVMe-sourced reads (device counters unavailable)"
else
  A_DISK_DELTA=$((A_DISK1 - A_DISK0))
  echo "  physical NVMe bytes read during A: $A_DISK_DELTA"
  # Review fix (round 18): zero NVMe reads during A are EXPECTED when the request
  # was served from L1/RAM (memory hit) -- the cache service survived, so nothing
  # had to come from disk. A positive delta alone on a SHARED disk is not proof
  # of cache reads either (unrelated I/O). So A's disk signal is INFORMATIVE,
  # never a hard FAIL: only scenario B proves disk-backed recovery.
  if [ "$A_DISK_DELTA" -le 0 ]; then
    echo "  [info] A: no physical NVMe reads -- consistent with an L1/RAM memory hit"
  elif [ "${A_RETR:-0}" -eq 0 ]; then
    echo "  [info] A: physical bytes read from a SHARED disk without L2-read events"
    echo "         (unrelated I/O possible) -- not attributable to LMCache alone"
  fi
fi

echo "== SCENARIO B: restart BOTH services, NVMe kept =="
B_DISK0=$(disk_read_bytes)
B_BEFORE=$(wait_l2_quiet)
# Review fix (round 46): scenario B used to stop-cache/start-cache and
# `docker restart` WHILE the engine was still alive -- the engine died with
# a Python error and the test then waited 20 min on a dead container. The
# correct order is: STOP THE ENGINE FIRST, then restart the cache service,
# then bring the engine back up.
echo "  stopping the engine first (B: correct order -- engine down, cache restart, engine up)"
# Round 52: do NOT swallow the docker error -- the previous form hid the
# real cause. An already-stopped/removed container is NOT a failure here:
# the goal is a clean cache restart with the engine down.
_b_state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
echo "  engine container state before stop: $_b_state"
if [ "$_b_state" = "missing" ]; then
  echo "  [info] engine container already removed -- nothing to stop"
elif [ "$_b_state" = "exited" ] || [ "$_b_state" = "created" ]; then
  echo "  [info] engine container already stopped ($_b_state) -- skipping stop"
else
  if ! _b_stop_out="$(docker stop "$CONTAINER" 2>&1)"; then
    echo "  docker stop output: $_b_stop_out" >&2
    PA B "docker stop (B) failed -- see the docker output above"
  fi
fi
bash "$HERE/stop-cache.sh" >/dev/null 2>&1 || PA B "stop-cache.sh failed"
# Review fix (round 47): the GPU check runs HERE -- AFTER both services are
# stopped. Round 46 checked BEFORE the stop and refused on the engine's own
# legal VRAM (78 GiB per GPU). A STOPPED engine+server must leave the GPUs
# free; if VRAM is still held, the holders are leaked processes that would
# break the restart below.
if [ "${KVP_SKIP_GPU_CHECK:-0}" != "1" ]; then
  _kvp_gpu_preflight 2048 || PA B "GPU memory still held AFTER stop-engine + stop-cache -- leaked processes block the restart (see PIDs above)"
fi
bash "$HERE/start-cache.sh" >/dev/null 2>&1 || PA B "start-cache.sh failed (cache service did not restart)"
LIVE_PID="$(cat "$KVP_CACHE_ROOT/lmcache-server.pid" 2>/dev/null || echo 0)"
kill -0 "$LIVE_PID" 2>/dev/null || PA B "cache service is dead right after start-cache.sh (readiness not reached)"
docker start "$CONTAINER" >/dev/null 2>&1 || PA B "docker start (B) failed"
api_ready || PA B "API did not come back after full restart"
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
B_AFTER=$(wait_l2_quiet)
[ "$B_AFTER" -ge "$B_BEFORE" ] || fail "B: L2 shrank during restart -- data loss suspected ($B_BEFORE -> $B_AFTER)"
B_DISK1=$(disk_read_bytes)
if [ "$B_DISK0" = "-1" ] || [ "$B_DISK1" = "-1" ]; then
  inconc "B: KVP_CACHE_DEVICE unset/unreadable -- cannot independently prove NVMe-sourced reads (device counters unavailable)"
else
  B_DISK_DELTA=$((B_DISK1 - B_DISK0))
  echo "  physical NVMe bytes read during B: $B_DISK_DELTA"
  # Shared-VM-disk caveat (review P2-4), same as scenario A: positive delta
  # alone is not proof on a shared disk -- require matching L2-read events in
  # the same window, otherwise INCONCLUSIVE (unrelated I/O cannot be ruled out).
  if [ "$B_DISK_DELTA" -le 0 ]; then
    fail "B: zero physical NVMe bytes read during scenario -- L2 cannot be credited as the source"
  elif [ "${B_RETR:-0}" -eq 0 ]; then
    inconc "B: physical bytes read from a SHARED disk without matching L2-read events -- cannot attribute these bytes to LMCache (unrelated I/O possible). Set an isolated test device or corroborate with per-process I/O to make this PASS."
  fi
fi

# ---- exclude alternative explanations ----------------------------------
# Review fix (round 18): scenario A is EXPECTED to have zero L2 read events
# (memory hit from the surviving L1) -- only scenario B must show them. So the
# "reuse without any L2 evidence" concern applies to B alone.
B2=$(cached_of "$OUT/afterB.json")
if [ "${B2:-0}" -gt 0 ] && [ "${B_RETR:-0}" -eq 0 ]; then
  fail "B: cached_tokens>0 but zero L2-read log events: reuse may come from RAM/prefix cache only -- NOT proof of NVMe recovery"
fi

# ================= machine verdict =====================================
echo
echo "================ SUMMARY ================"
cat "$OUT"/*.summary.json 2>/dev/null || true
# Review round 13: verdict precedence -- ANY hard FAIL dominates an
# INCONCLUSIVE. Two hard FAILs plus one INCONCLUSIVE is FAIL, not INCONCLUSIVE
# (the old `inconc()` set V=9 and erased the FAIL count).
if [ "$V" -gt 0 ]; then
  echo "VERDICT: FAIL ($V failed check(s), $VI inconclusive finding(s)) -- see $OUT/failures.txt"
  cat "$OUT/failures.txt" 2>/dev/null || true
  exit 1
elif [ "$VI" -gt 0 ]; then
  echo "VERDICT: INCONCLUSIVE ($VI inconclusive finding(s), 0 hard failures) -- not all evidence sources were available to prove disk-sourced reads (see $OUT/failures.txt)."
  echo "Not a cache failure and not a PASS: set KVP_CACHE_DEVICE (and MODEL_DIR) so device counters and identity are verifiable, then rerun. An unproven disk source must never be reported as PASS."
  cat "$OUT/failures.txt" 2>/dev/null || true
  exit 5
else
  echo "VERDICT: PASS -- cache reuse proven after restarts (A: memory/L1 hit as expected for a vLLM-only restart; B: disk-backed KV recovery with L2 read events + physical NVMe reads after full restart, data retained)"
  exit 0
fi
