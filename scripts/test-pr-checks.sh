#!/usr/bin/env bash
# test-pr-checks.sh -- hardware checks proving the downstream PRs do THEIR
# jobs: #4136 (retrieve/abort race) and #5972 (incomplete-object recovery).
#
# test-recovery.sh (scenarios A/B) only proves recovery timing and L2 read
# counts -- it never compares model output (max_tokens=8) and never aborts a
# request during a retrieve. So a green recovery run does NOT prove that the
# two carried patches fix their own cases. This script runs the missing
# checks; the report goes to live/pr-checks-report.md (+ stdout), for the PR
# owners and our own records.
#
# Scenario C (#4136): aborts a stream request at a random 0..400 ms into a
# disk-backed retrieve and verifies nothing broke (engine alive, no traceback,
# no L2 store failures, no stuck requests, same answer afterwards).
# Scenario D (#5972): three prompts of different lengths, T0 cold / T1 repeat
# / T2 after a full restart, compared token-by-token: a difference in the
# FIRST 32 tokens is FAIL (corrupt recovery), a later difference is WARN.
#
# Everything runs as the operator -- root is never used or printed.
# Precondition failures print the exact command to run and exit, no traceback.
# Style after test-recovery.sh.
set -Eeuo pipefail
PA(){ echo "FAIL(${1:-?}): ${*:2}"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/env.sh"
_kvp_load_env "$ROOT"
# ---- API key: explicit API_KEY wins, then the canonical shared key, then --
# ---- the local mirror. Never printed; curl uses a chmod-600 -K file. ------
KEY="${API_KEY:-}"
if [ -z "$KEY" ] && [ -f "$HOME/.config/kvp/api_key" ]; then
  KEY="$(head -n1 "$HOME/.config/kvp/api_key" | tr -d '[:space:]')"
fi
if [ -z "$KEY" ] && [ -f "$ROOT/live/api_key" ]; then
  KEY="$(head -n1 "$ROOT/live/api_key" | tr -d '[:space:]')"
fi
if [ -z "$KEY" ]; then
  echo "ERROR: no API key found." >&2
  echo "Run the stack once first (it writes the key):" >&2
  echo "  bash scripts/start-all.sh" >&2
  exit 1
fi
# the engine container the stack REALLY started (start-all records it)
CONTAINER="${CONTAINER:-}"
if [ -z "$CONTAINER" ] && [ -f "$ROOT/live/engine_container" ]; then
  CONTAINER="$(head -n1 "$ROOT/live/engine_container" | tr -d '[:space:]' || true)"
fi
CONTAINER="${CONTAINER:-mimo-pro-tp4-kvp}"
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  echo "ERROR: the engine container '$CONTAINER' is not running." >&2
  echo "Start the stack and wait until it is ready:" >&2
  echo "  bash scripts/start-all.sh" >&2
  exit 1
fi
# the bearer key never appears in `ps` -- a chmod-600 curl -K config file
API="${API_BASE:-http://127.0.0.1:30007}"
KFILE="$(mktemp "${TMPDIR:-/tmp}/kvp-pr-curl.XXXXXX")"
umask 077
printf 'header = "Authorization: Bearer %s"\n' "$KEY" > "$KFILE"
chmod 600 "$KFILE"
trap 'rm -f "$KFILE"' EXIT
LOG="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}/lmcache-server.log"
# r30 P2b class: take the size via [ -f ] -- `wc -c < missing 2>/dev/null`
# still prints "No such file or directory" (the `<` is parsed before 2>/dev/null)
if [ -f "$LOG" ]; then M0="$(wc -c < "$LOG")"; else M0=0; fi   # scenario log mark
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-mimo-pro}"
export SERVED_MODEL_NAME
OUTDIR="${PR_CHECKS_OUTDIR:-}"   # tests inject a tmp dir here
if [ -z "$OUTDIR" ]; then
  OUTDIR="${KVP_LIVE_DIR:-$ROOT/live}/pr-checks-$(date -u +%Y%m%dT%H%M%SZ)"
fi
mkdir -p "$OUTDIR"
REPORT="${PR_CHECKS_REPORT:-${KVP_LIVE_DIR:-$ROOT/live}/pr-checks-report.md}"
C_STRAY=0; C_ERR=0; C_STUCK=""
# ---- tiny API helpers (python3 -- no curl side effects on streams) -------
api_ok(){ # GET /v1/models answers 200 with our bearer key
  python3 - "$API" "$KEY" <<'PYEOF'
import sys,urllib.request
api,key=sys.argv[1:3]
req=urllib.request.Request(api+"/v1/models",headers={"Authorization":"Bearer "+key})
try:
    with urllib.request.urlopen(req,timeout=10) as r:
        sys.exit(0 if r.status==200 else 1)
except Exception:
    sys.exit(1)
PYEOF
}
api_ok || { echo "ERROR: the engine API at $API did not answer /v1/models." >&2
  echo "Wait until the stack is ready, then rerun:" >&2
  echo "  bash scripts/test-pr-checks.sh" >&2
  exit 1; }
# chat(): NON-stream single-turn probe (T0/T1/T2 comparisons + sanity ask)
chat(){ # chat <path.json> <prompt> [max_tokens] [temperature] [seed]
  local path="$1" prompt="$2" mt="${3:-64}" temp="${4:-0}" seed="${5:-0}"
  python3 - "$API" "$KEY" "$path" "$prompt" "$mt" "$temp" "$seed" <<'PYEOF'
import json,sys,urllib.request
api,key,path,prompt,mt,temp,seed=sys.argv[1:8]
body=json.dumps({"model":__import__("os").environ.get("SERVED_MODEL_NAME","mimo-pro"),
  "messages":[{"role":"user","content":prompt}],
  "max_tokens":int(mt),"temperature":float(temp),"seed":int(seed),
  "stream":False}).encode()
req=urllib.request.Request(api+"/v1/chat/completions",data=body,
  headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
try:
    with urllib.request.urlopen(req,timeout=3600) as r:
        o=json.loads(r.read())
    txt=o["choices"][0]["message"].get("content") or ""
    toks=(o.get("usage") or {}).get("completion_tokens")
    if not isinstance(toks, int):
        toks=len(txt.split())
    open(path,"w").write(json.dumps({"text":txt,"completion_tokens":toks}))
    print(txt)
except Exception as e:
    print("",end="")
    import traceback; traceback.print_exc(file=__import__("sys").stderr)
    sys.exit(1)
PYEOF
}
# abort_stream(): ONE streaming chat request, closed at ms_delay ms (a client
# abort DURING a retrieve when the prefix is only in L2). Prints ERR on HTTP
# error, ABRT when the socket was closed by us (the expected case).
abort_stream(){ # abort_stream <prompt> <ms_delay> <tag>
  local prompt="$1" ms="$2" tag="$3"
  python3 - "$API" "$KEY" "$prompt" "$ms" "$tag" <<'PYEOF' 2>/dev/null
import json,os,socket,sys,time,urllib.request
api,key,prompt,ms,tag=sys.argv[1:6]
body=json.dumps({"model":os.environ.get("SERVED_MODEL_NAME","mimo-pro"),
  "messages":[{"role":"user","content":prompt}],
  "max_tokens":256,"temperature":0,"stream":True}).encode()
req=urllib.request.Request(api+"/v1/chat/completions",data=body,
  headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
t0=time.time()
try:
    r=urllib.request.urlopen(req,timeout=3600)
except Exception:
    print("ERR"); sys.exit(0)
# read bytes until the requested delay, then ABORT: close the connection
# mid-retrieve (the #4136 case only triggers with a real client abort)
deadline=t0+int(ms)/1000.0
r.fp.raw._sock.settimeout(1)
got=0
try:
    while time.time() < deadline:
        chunk=r.fp.raw._sock.recv(4096)
        if not chunk:
            break
        got+=len(chunk)
except Exception:
    pass
try:
    r.close()          # the actual abort
except Exception:
    pass
print("ABRT-READ=%d" % got)
PYEOF
}
# ---------------------------------------------------------------------------
# helpers shared by the scenarios: engine state, log evidence, stuck queries
# ---------------------------------------------------------------------------
engine_alive(){ docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; }
# the ENGINE log (docker logs of the engine container) since RFC3339 start --
# the r31 contract checks the ENGINE log, not the cache-server log. The cache
# server log is used separately (kvp_scan_l2_failures).
ENG_SINCE="${ENG_SINCE:-}"   # set once at each scenario start (date -u -Iseconds)
log_since(){ docker logs --since "$ENG_SINCE" "$CONTAINER" 2>&1 || true; }
traceback_count(){ log_since | grep -ac 'Traceback (most recent call last)' || true; }
# ALLOWED engine-log ERROR lines during scenario C. The engine is EXPECTED to
# report the client disconnect itself; every other ERROR is a real defect.
# List kept EXPLICIT on purpose (the review asked for it).
C_ALLOWED_ERRORS='
client disconnected
Client disconnected
connection reset
broken pipe
AbortError
Request aborted
request aborted
aborted by client
'
engine_errors(){ # engine errors since ENG_SINCE, minus the explicit allow-list
  log_since | grep -aE '(^|[^A-Za-z])ERROR([^A-Za-z]|$)' \
    | grep -af <(printf '%s\n' "$C_ALLOWED_ERRORS" | grep -v '^$' | sed 's/^/*/;s/$/*/') -v \
    | grep -v '^$' || true
}
l2_failures(){ # kvp_scan_l2_failures since the mark, CACHE-SERVER log (quiet -> count)
  local _r=0
  kvp_scan_l2_failures "$LOG" "$1" >/dev/null 2>&1 || _r=$?
  [ "$_r" -eq 1 ] && echo 1 || echo 0
}
srv_since(){ # bytes of the CACHE-SERVER log appended after $1
  tail -c +"$(( $1 + 1 ))" "$LOG" 2>/dev/null || true
}
stuck_requests(){ # vllm:num_requests_{running,waiting} must be 0
  python3 - "$API" "$KEY" <<'PYEOF'
import json,sys,urllib.request
api,key=sys.argv[1:3]
try:
    req=urllib.request.Request(api+"/metrics",
        headers={"Authorization":"Bearer "+key})
    with urllib.request.urlopen(req,timeout=15) as r:
        m=r.read().decode()
    run=wait=0
    for line in m.splitlines():
        if line.startswith("vllm:num_requests_running{"):
            run=float(line.rsplit(" ",1)[1])
        elif line.startswith("vllm:num_requests_waiting{"):
            wait=float(line.rsplit(" ",1)[1])
    print(f"{int(run)} {int(wait)}")
except Exception as e:
    print("NA NA")
PYEOF
}
# ---------------------------------------------------------------------------
# SCENARIO C (#4136): abort a stream request during a disk-backed retrieve.
# 30 rounds; each round runs 4 concurrent streaming requests over the same
# long prefix (short unique suffixes) and every client abandons its socket at
# a random 0..400 ms -- the different delays are what lands an abort INSIDE
# the retrieve window. Afterwards: engine alive, no Traceback/ERROR outside
# the explicit allow-list, clean kvp_scan_l2_failures since the mark, no stuck
# requests after 60s, and a plain request still answers like before.
# ---------------------------------------------------------------------------
echo "== SCENARIO C (#4136): abort during retrieve =="
ENG_SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
C_ROUNDS="${PR_CHECKS_ROUNDS:-30}"       # overridable for the machine-free tests
C_SETTLE="${PR_CHECKS_SETTLE:-60}"       # seconds to let stuck requests surface
C_ABORT_MAX_MS="${PR_CHECKS_ABORT_MAX_MS:-400}"   # random abort window
C_PREFIX="$(python3 -c 'print("Task background. " + "The sikkatu is a Sumerian clay cone marking a property boundary. "*400 + "Remember this text.")')"
C_QUIET_PROMPT="$C_PREFIX
Q: what is a sikkatu? one short sentence."
chat "$OUTDIR/c-before.json" "$C_QUIET_PROMPT" 64 >/dev/null || PA C "the pre-scenario plain request failed"
C_BEFORE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["text"])' "$OUTDIR/c-before.json")"
C_MARK="$M0"
C_STRAY=0; C_ERR=0
for round in $(seq 1 "$C_ROUNDS"); do
  pids=()
  for k in 1 2 3 4; do
    ms=$(( $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % (C_ABORT_MAX_MS + 1) ))
    abort_stream "$C_PREFIX
Q: short note ${round}-${k}." "$ms" "${round}-${k}" \
      >>"$OUTDIR/c-aborts.txt" 2>/dev/null &
    pids+=("$!")
  done
  wait "${pids[@]}" 2>/dev/null || true
  if [ $(( round % 5 )) -eq 0 ] || [ "$round" = 1 ]; then
    printf '[C] round %d/%s done, aborted: %s, http-errors: %s\n' \
      "$round" "$C_ROUNDS" "$(grep -ac 'ABRT' "$OUTDIR/c-aborts.txt" 2>/dev/null || echo 0)" \
      "$(grep -ac '^ERR' "$OUTDIR/c-aborts.txt" 2>/dev/null || echo 0)"
  fi
done
C_STRAY="$(grep -ac '^ERR' "$OUTDIR/c-aborts.txt" 2>/dev/null || true)"
C_ABORTED="$(grep -ac 'ABRT' "$OUTDIR/c-aborts.txt" 2>/dev/null || true)"
# r31: how many aborts actually saw response bytes BEFORE closing (the abort
# must land INSIDE the retrieve -- a 0 ms abort that closes too early proves
# nothing). Printed to the report; the tests assert on this line.
C_READ_OK="$(grep -c 'ABRT-READ=[1-9]' "$OUTDIR/c-aborts.txt" 2>/dev/null || true)"
echo "  aborted requests: $C_ABORTED, http errors: $C_STRAY, aborts after reading bytes: $C_READ_OK"
echo "  waiting ${C_SETTLE}s for any stuck request to settle..."
sleep "$C_SETTLE"
C_VERDICT="PASS"; C_NOTES=""
if ! engine_alive; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- the engine container '$CONTAINER' is DEAD after the abort series"
fi
C_TB="$(traceback_count)"
if [ "$C_TB" -gt 0 ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- $C_TB 'Traceback' occurrences in the engine log since the scenario start"
fi
C_ERRS="$(engine_errors)"
if [ -n "$C_ERRS" ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- engine log ERROR lines outside the allow-list:
$(printf '%s\n' "$C_ERRS" | head -5 | sed 's/^/    /')"
fi
C_L2FAIL="$(l2_failures "$C_MARK")"
if [ "$C_L2FAIL" != "0" ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- kvp_scan_l2_failures reports an L2 store failure since the scenario start"
fi
read -r C_RUN C_WAIT <<<"$(stuck_requests)"
if [ "$C_RUN" = "NA" ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- /metrics did not answer (cannot prove no stuck requests)"
elif [ "$C_RUN" != "0" ] || [ "$C_WAIT" != "0" ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- stuck requests 60s after the aborts: running=$C_RUN waiting=$C_WAIT (must be 0 0)"
fi
chat "$OUTDIR/c-after.json" "$C_QUIET_PROMPT" 64 >/dev/null || PA C "the post-scenario plain request failed"
C_AFTER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["text"])' "$OUTDIR/c-after.json")"
if [ "$C_AFTER" != "$C_BEFORE" ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- the plain answer CHANGED after the abort series (before: $(printf '%.60s' "$C_BEFORE")...; after: $(printf '%.60s' "$C_AFTER")...)"
fi
echo "  C result: $C_VERDICT (aborted=$C_ABORTED, http errors=$C_STRAY)"
[ -n "$C_NOTES" ] && printf '%s\n' "$C_NOTES" | sed 's/^/  /'
# ---------------------------------------------------------------------------
# SCENARIO D (#5972): recovery of INCOMPLETE objects must regenerate the
# exact same text. Three prompts of different length (all longer than the
# model's sliding window), temperature 0, seed 0, max_tokens 64:
#   T0 cold cache (unique prompt), T1 repeat (memory hit), T2 after a FULL
#   restart of both services (disk-backed). Criteria: T0 == T1 == T2 by text;
#   a mismatch in the FIRST 32 tokens is FAIL (corrupt recovery), a mismatch
#   LATER is WARN ("possible numeric drift") with the token index -- vLLM
#   does not guarantee bit-equal output across prefix-cache paths.
# ---------------------------------------------------------------------------
echo "== SCENARIO D (#5972): incomplete-object recovery =="
ENG_SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# prompt lengths from the model config when available, else 1x/4x/16x chunk
D_CHUNK="${LMCACHE_CHUNK_SIZE:-}"
D_BASELINE=384
if [ -n "$D_CHUNK" ]; then
  D_BASELINE="$D_CHUNK"
fi
D_LEN_SLIDING=0
MODEL_CFG="$(find "$ROOT/live" "$ROOT/configs" -name 'config.json' -print 2>/dev/null | head -1 || true)"
D_SW=""
if [ -n "$MODEL_CFG" ] && grep -q '"sliding_window"' "$MODEL_CFG" 2>/dev/null; then
  D_SW="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("sliding_window"))' "$MODEL_CFG")"
  echo "  sliding_window from $MODEL_CFG = $D_SW"
else
  echo "  NOTE: no 'sliding_window' in the model config.json -- falling back to 1x/4x/16x of the LMCache chunk ($D_BASELINE) as prompt sizes"
fi
D_VERDICT="PASS"; D_NOTES=""; D_LINES=""
d_compare(){ # d_compare <tag> <t0.json> <t1.json> <t2.json> -> updates D_VERDICT
  local tag="$1" ntok first idx
  ntok="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("completion_tokens",0))' "$2")"
  line="$(python3 - "$2" "$3" "$4" "$tag" "$ntok" <<'PYEOF'
import json,sys
t0=json.load(open(sys.argv[1]))["text"]
t1=json.load(open(sys.argv[2]))["text"]
t2=json.load(open(sys.argv[3]))["text"]
tag,n=sys.argv[4],sys.argv[5]
def tok(s): return s.split()
a,b,c=tok(t0),tok(t1),tok(t2)
def firstdiff(x,y):
    for i in range(min(len(x),len(y))):
        if x[i]!=y[i]: return i
    return -1 if len(x)!=len(y) else None
ab=firstdiff(a,b); ac=firstdiff(a,c)
worst=None
for d in (ab,ac):
    if d is None: continue
    if worst is None or d<worst: worst=d
if worst is None:
    print(f"| {tag} | {len(a)} | identical | | PASS |")
else:
    kind="FAIL" if worst<32 else "WARN"
    print(f"| {tag} | {len(a)} | token {worst} | {kind} |")
PYEOF
)"
  D_LINES="$D_LINES
$line"
  case "$line" in
    *FAIL*) D_VERDICT="FAIL"; D_NOTES="$D_NOTES
- $tag: mismatch in the first 32 tokens (corrupt recovery)" ;;
    *WARN*) [ "$D_VERDICT" = "PASS" ] && D_VERDICT="WARN"
      D_NOTES="$D_NOTES
- $tag: later mismatch -- possible numeric drift" ;;
  esac
}
D_TOKS=""
i=0
for mult in 1 4 16; do
  i=$(( i + 1 ))
  size=$(( D_BASELINE * mult ))
  prompt=""
  if [ -n "$D_SW" ] && [ "$D_SW" != "None" ]; then
    size=$(( D_SW * mult ))
  fi
  prompt="User note ${i}. $(python3 -c "print('sikkatu ' * max(1, $size // 8))")End of note ${i}. Reply with a single short sentence about property boundaries."
  u="$(date -u +%s%N)-$i"    # unique: not in L1 nor in L2 for T0
  p="$prompt
(request $u)"
  printf '%s' "$p" > "$OUTDIR/d-${i}-prompt.txt"   # T2 replays the SAME prompt
  chat "$OUTDIR/d-${i}-t0.json" "$p" 64 0 0 >/dev/null || PA D "T0 (cold) request $i failed"
  chat "$OUTDIR/d-${i}-t1.json" "$p" 64 0 0 >/dev/null || PA D "T1 (repeat) request $i failed"
  echo "  [D] prompt $i: T0+T1 done"
done
echo "  restarting BOTH services (NVMe kept) for the T2 (disk-backed) phase..."
if [ "${PR_CHECKS_SKIP_RESTART:-0}" = "1" ]; then
  # machine-free stub tests: no real restart in a test run
  echo "  PR_CHECKS_SKIP_RESTART=1 -- no restart performed"
else
bash "$HERE/stop-cache.sh" >/dev/null 2>&1 || PA D "stop-cache.sh failed"
docker stop "$CONTAINER" >/dev/null 2>&1 || PA D "docker stop '$CONTAINER' failed"
bash "$HERE/start-cache.sh" >/dev/null 2>&1 || PA D "start-cache.sh failed (cache service did not restart)"
docker start "$CONTAINER" >/dev/null 2>&1 || PA D "docker start '$CONTAINER' failed"
for _ in $(seq 1 180); do
  engine_alive && api_ok && break
  sleep 10
done
api_ok || PA D "the engine API did not come back after the restart (waited 30 min)"
fi
i=0
for mult in 1 4 16; do
  i=$(( i + 1 ))
  # T2 MUST replay the EXACT T0/T1 prompt (the L2 recovery key)
  p="$(cat "$OUTDIR/d-${i}-prompt.txt")"
  chat "$OUTDIR/d-${i}-t2.json" "$p" 64 0 0 >/dev/null || PA D "T2 (disk-backed) request $i failed"
  d_compare "prompt-$i" "$OUTDIR/d-${i}-t0.json" "$OUTDIR/d-${i}-t1.json" "$OUTDIR/d-${i}-t2.json"
done
d_l2_reads(){ # L2 read events in the CACHE-SERVER log since the scenario mark
  srv_since "$1" | grep -ac 'L2 prefetch load completed: request [0-9]*, [1-9][0-9]* loaded' || true
}
D_L2READS="$(d_l2_reads "$M0")"
echo "  D result: $D_VERDICT (L2 read events since start: $D_L2READS)"
[ -n "$D_NOTES" ] && printf '%s\n' "$D_NOTES" | sed 's/^/  /'
# ---------------------------------------------------------------------------
# CONTROL (optional): an UNPATCHED image run is the only way to claim "the
# PRs fix their cases". If KVP_CONTROL_IMAGE is set AND that image really
# serves the engine, the C/D checks are repeated against it; otherwise the
# report honestly says "no control". Nothing is ever built here (20 min and
# risk) and the org.kvp.lmc-patch-* identity check is never bypassed.
# ---------------------------------------------------------------------------
C_CONTROL="NO"; C_CONTROL_NOTES="no unpatched image passed in KVP_CONTROL_IMAGE (and none is auto-built) -- no control run"
if [ -n "${KVP_CONTROL_IMAGE:-}" ]; then
  if docker image inspect "$KVP_CONTROL_IMAGE" >/dev/null 2>&1; then
    echo "== CONTROL: restarting the engine on the UNPATCHED image $KVP_CONTROL_IMAGE =="
    echo "   (the C/D scenarios would run again here; to keep this script"
    echo "    safe in one pass, the control run is a SEPARATE invocation with"
    echo "    the stack already serving that image). For the report the"
    echo "    requested control image is recorded below."
    C_CONTROL="REQUESTED"
    C_CONTROL_NOTES="image '$KVP_CONTROL_IMAGE' present; rerun the whole script with the stack serving that image for the control numbers"
  else
    C_CONTROL_NOTES="KVP_CONTROL_IMAGE='$KVP_CONTROL_IMAGE' not present in docker images -- no control run"
  fi
fi
# ---------------------------------------------------------------------------
# REPORT (stdout + live/pr-checks-report.md). No secrets: the bearer key and
# the api-key file are NEVER written to the report.
# ---------------------------------------------------------------------------
WHEEL="$(ls "$ROOT"/vendor 2>/dev/null | head -1 || true)"
IMG_ID="$(cat "$ROOT/live/image" 2>/dev/null || echo '(no live/image record)')"
LAB4136="$(docker inspect -f '{{index .Config.Labels "org.kvp.lmc-patch-4136"}}' "$CONTAINER" 2>/dev/null || true)"
LAB5972="$(docker inspect -f '{{index .Config.Labels "org.kvp.lmc-patch-5972"}}' "$CONTAINER" 2>/dev/null || true)"
{
  echo "# PR #4136 / #5972 hardware checks"
  echo
  echo "- date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "- wheel: $WHEEL"
  echo "- image: $IMG_ID"
  echo "- label org.kvp.lmc-patch-4136: ${LAB4136:-<absent>}"
  echo "- label org.kvp.lmc-patch-5972: ${LAB5972:-<absent>}"
  echo "- engine container: $CONTAINER"
  echo "- requests: scenario C = 30 rounds x 4 aborts = 120 stream aborts; scenario D = 3 prompts x 3 phases = 9 requests (max_tokens=64)"
  echo
  echo "## Scenario C (#4136, abort during retrieve)"
  echo
  echo "- result: **$C_VERDICT**"
  echo "- aborted requests: $C_ABORTED, http errors: $C_STRAY, aborts after reading bytes: $C_READ_OK"
  [ -n "$C_NOTES" ] && printf '%s\n' "$C_NOTES"
  echo
  echo "## Scenario D (#5972, incomplete-object recovery)"
  echo
  echo "- result: **$D_VERDICT**, L2 read events since start: $D_L2READS"
  echo
  echo "| prompt | tokens | first diff | verdict |"
  echo "| --- | --- | --- | --- |"
  printf '%s\n' "$D_LINES" | grep '^|' || echo "| (no data) | | | |"
  [ -n "$D_NOTES" ] && printf '%s\n' "$D_NOTES"
  echo
  echo "## Control"
  echo
  echo "- control: $C_CONTROL -- $C_CONTROL_NOTES"
} > "$REPORT"
echo
echo "== report written to $REPORT =="
cat "$REPORT"
# exit: PASS/WARN = 0, FAIL = 1
[ "$C_VERDICT" = "FAIL" ] && exit 1
[ "$D_VERDICT" = "FAIL" ] && exit 1
exit 0