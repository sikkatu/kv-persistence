#!/usr/bin/env bash
# test-pr-checks.sh -- hardware checks proving the downstream PRs do THEIR
# jobs: #4136 (retrieve/abort race) and #5972 (incomplete-object recovery).
# r32 rewrite: same contract as test-recovery.sh (strict mode, key from the
# canonical file, container from live/engine_container), ONE shared restart
# from scripts/restart-lib.sh, disk-backed retrieve actually proven, honest
# verdicts (INCONCLUSIVE=5 where evidence is missing).
#
# Order:
#   warmup: N long unique prefixes + the D prompts (T0/T1), each once;
#           wait for L2 store events;
#   one FULL restart (engine first -- round 46; IPC pre-flight -- round 79);
#   C (#4136): round k aborts 4 streams over prefix k at random 0..X ms,
#           ENGINE-log checks + L2 checks + stuck requests + same answer,
#           and disk evidence: L2 prefetch-load events must be > 0 or the
#           scenario is INCONCLUSIVE (no L2 reads = the retrieve never hit
#           disk = nothing was proven);
#   D (#5972): T2 over the SAME prompts after the restart, token-level
#           comparison (logprobs when available, words otherwise), FAIL when
#           the first divergence is inside 32 units, WARN later; zero L2
#           reads after the restart -> INCONCLUSIVE; L2 store failures -> FAIL.
#
# Verdicts: FAIL=1, INCONCLUSIVE=5, PASS/WARN=0. Report: a timestamped file
# PLUS live/pr-checks-report.md as the latest copy (old reports kept).
# Key never reaches argv (env/file only). Root never used.
set -Eeuo pipefail
PA(){ echo "FAIL(${1:-?}): ${*:2}"; exit 1; }
INCONC(){ echo "INCONCLUSIVE: ${*}"; exit 5; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/env.sh"
_kvp_load_env "$ROOT"
. "$HERE/restart-lib.sh"          # r32 R1: the ONE shared restart routine
# ---- r32.1 R7/R1: TEST MODE flags are computed and PRINTED FIRST (before the
# API key check and any docker call) -- the banner is literally the first line
# of the run and of the report whenever any test mode applies. A missing or
# skipped IPC pre-flight is TEST MODE too (a bypassed CUDA-IPC boundary is
# not a hardware proof; see the same rule in restart-lib.sh).
TEST_MODE_VARS=""
for _v in PR_CHECKS_SKIP_RESTART PR_CHECKS_ROUNDS PR_CHECKS_SETTLE \
          PR_CHECKS_ABORT_MAX_MS PR_CHECKS_MIN_EARLY_ABORT_PCT; do
  if [ -n "${!_v:-}" ]; then
    TEST_MODE_VARS="$TEST_MODE_VARS $_v"
  fi
done
if [ "${KVP_SKIP_IPC_TEST:-0}" = "1" ]; then
  TEST_MODE_VARS="$TEST_MODE_VARS IPC_PRE_FLIGHT_SKIPPED(KVP_SKIP_IPC_TEST=1)"
fi
if [ ! -x "${KVP_SCRIPTS_DIR:-$HERE}/ipc-test.sh" ]; then
  TEST_MODE_VARS="$TEST_MODE_VARS IPC_PRE_FLIGHT_MISSING(no ipc-test.sh)"
fi
if [ -n "$TEST_MODE_VARS" ]; then
  echo "TEST MODE:$(for _v in $TEST_MODE_VARS; do printf ' %s' "$_v"; done) set -- this run is NOT a hardware proof"
fi
# ---- API key: env wins, then ~/.., then live mirror; key in env file only --
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
# r32 R5: the key travels ONLY via the environment of the child python
# (KVP_KEY), never as an argv element (argv is world-readable in ps). The
# prompts travel via files/stdin for the same reason (argv is capped).
export KVP_KEY="$KEY"
unset KEY
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
API="${API_BASE:-http://127.0.0.1:30007}"
# both API and KVP_KEY are read inside the exported api_ok() probe (the
# shared restart routine waits for the engine in a `bash -c`) -- export both
export API
# ---- r32 R7: TEST MODE flags are computed and printed at the TOP of this
# script (literally the first lines of the run and of the report). SKIP_RESTART
# additionally makes the D scenario SKIPPED (mem-hit T2 would be a fake
# proof), never PASS.
C_ROUNDS="${PR_CHECKS_ROUNDS:-30}"
C_SETTLE="${PR_CHECKS_SETTLE:-60}"
C_ABORT_MAX_MS="${PR_CHECKS_ABORT_MAX_MS:-400}"
MIN_EARLY_PCT="${PR_CHECKS_MIN_EARLY_ABORT_PCT:-50}"
# ---- paths: OUTDIR for evidence, timestamped report + latest copy (R8) -----
OUTDIR="${PR_CHECKS_OUTDIR:-${KVP_LIVE_DIR:-$ROOT/live}/pr-checks-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUTDIR"
LIVE_DIR="${KVP_LIVE_DIR:-$ROOT/live}"
# r32.1 R8: collision-safe report name -- two runs inside the same UTC second
# must NOT overwrite each other (the old fixed <ts>.md could be clobbered).
REPORT_TS_BASE="$LIVE_DIR/pr-checks-report-$(date -u +%Y%m%dT%H%M%SZ)"
REPORT_TS="$REPORT_TS_BASE.md"
_r321_n=1
# r32.2 R8: atomic filename reservation -- O_EXCL create (via noclobber)
# proves uniqueness in the kernel, so two concurrent runs in the same UTC
# second can NEVER pick the same timestamped report name.
while :; do
  if ( set -C; : > "$REPORT_TS" ) 2>/dev/null; then
    break                     # reserved (file created empty, will be filled later)
  fi
  REPORT_TS="${REPORT_TS_BASE}-${_r321_n}.md"
  _r321_n=$((_r321_n + 1))
done
REPORT_LATEST="$LIVE_DIR/pr-checks-report.md"
REPORT="$REPORT_TS"     # everything writes here; at the end cp -> latest
# ---- evidence marks -------------------------------------------------------
LOG="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}/lmcache-server.log"
if [ -f "$LOG" ]; then M0="$(wc -c < "$LOG")"; else M0=0; fi
D_MARK=0
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-mimo-pro}"
export SERVED_MODEL_NAME
ENG_SINCE=""
eng_since_now(){ ENG_SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; }
log_since(){ docker logs --since "$ENG_SINCE" "$CONTAINER" 2>&1 || true; }
srv_since(){ tail -c +"$(( $1 + 1 ))" "$LOG" 2>/dev/null || true; }
# ---- tiny API helpers: the key comes from $KVP_KEY (env), prompts from files
api_ok(){
  KVP_URL="$API" python3 - <<'PYEOF'
import os,sys,urllib.request
req=urllib.request.Request(os.environ["KVP_URL"]+"/v1/models",
    headers={"Authorization":"Bearer "+os.environ["KVP_KEY"]})
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
# chat(): non-stream single-turn probe. The prompt travels via FILE (argv is
# capped at 128 KiB and world-readable); answer + usage land in <path>.json.
chat(){ # chat <out.json> <prompt-file> <phase> [max_tokens] [logprobs]
  local out="$1" pfile="$2" phase="$3" mt="${4:-64}" lp="${5:-false}"
  KVP_URL="$API" KVP_PFILE="$pfile" KVP_OUT="$out" KVP_PHASE="$phase" \
  KVP_MT="$mt" KVP_LP="$lp" python3 - <<'PYEOF' 2>/dev/null
import json,os,sys,urllib.request
api=os.environ["KVP_URL"]
prompt=open(os.environ["KVP_PFILE"]).read()
body=json.dumps({
  "model":os.environ.get("SERVED_MODEL_NAME","mimo-pro"),
  "messages":[{"role":"user","content":prompt}],
  "max_tokens":int(os.environ["KVP_MT"]),
  "temperature":0,"seed":0,"stream":False,
  "logprobs": os.environ["KVP_LP"]=="true","top_logprobs":1,
  "metadata":{"phase": os.environ.get("KVP_PHASE","")},
}).encode()
req=urllib.request.Request(api+"/v1/chat/completions",data=body,
  headers={"Authorization":"Bearer "+os.environ["KVP_KEY"],
           "Content-Type":"application/json"})
try:
    with urllib.request.urlopen(req,timeout=3600) as r:
        o=json.loads(r.read())
    ch=o.get("choices",[{}])[0]
    txt=(ch.get("message") or {}).get("content") or ""
    toks=[]
    lp=ch.get("logprobs")
    if isinstance(lp,dict):
        toks=[(c.get("token") or "") for c in (lp.get("content") or [])]
    usage=o.get("usage") or {}
    open(os.environ["KVP_OUT"],"w").write(json.dumps({
        "text":txt,"tokens":toks,
        "completion_tokens":usage.get("completion_tokens"),
        "prompt_tokens":usage.get("prompt_tokens"),
        "cached_tokens":(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
    }))
    print(txt)
except Exception as e:
    import traceback; traceback.print_exc(file=sys.stderr)
    sys.exit(1)
PYEOF
}
# abort_stream(): ONE streaming request, CLIENT aborts at a real DEADLINE
# (r32.1 R3): the abort is deadline-driven and independent of the response
# -- it fires even if the server has sent NO headers yet (the old
# `conn.getresponse()` blocked until the headers and the "abort" happened
# after them). Everything is read from ONE raw socket (no http.client /
# file-object buffering anywhere, so no buffered bytes are ever bypassed).
# One line per abort:
#   ABRT before_first_byte=0|1 ms=<real time-to-close> body_bytes=<bytes after headers>
# A python crash prints NOTHING and the abort counts as lost (R3).
abort_stream(){ # abort_stream <prompt-file> <ms> <abort-log>
  local pfile="$1" ms="$2" af="$3"
  KVP_URL="$API" KVP_PFILE="$pfile" KVP_MS="$ms" KVP_AF="$af" python3 - <<'PYEOF'
import json,os,select,socket,sys,time,urllib.parse
api=os.environ["KVP_URL"]; u=urllib.parse.urlparse(api)
prompt=open(os.environ["KVP_PFILE"]).read()
body=json.dumps({"model":os.environ.get("SERVED_MODEL_NAME","mimo-pro"),
  "messages":[{"role":"user","content":prompt}],
  "max_tokens":256,"temperature":0,"seed":0,"stream":True}).encode()
hdr=(f"POST /v1/chat/completions HTTP/1.1\r\nHost: {u.hostname}\r\n"
     f"Authorization: Bearer {os.environ['KVP_KEY']}\r\n"
     "Content-Type: application/json\r\nConnection: close\r\n"
     f"Content-Length: {len(body)}\r\n\r\n").encode()
win=int(os.environ["KVP_MS"])/1000.0
tol=float(os.environ.get("KVP_ABORT_TOL_MS","250"))/1000.0
t0=time.monotonic()                      # r32.2 R3: monotic, never wall clock
deadline=t0+win                          # ONE absolute deadline for everything
sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
sock.setblocking(False)                  # r32.2: neither connect nor send may
                                         # exceed the deadline by blocking
phase="connect"
got=0; buf=b""; hdr_end=-1       # r32.2: defined up front -- any phase may exit early
try:
    try:
        sock.connect((u.hostname,u.port))
    except BlockingIOError:
        pass
    while True:                          # connect completion, deadline-bounded
        left=deadline-time.monotonic()
        if left<=0:
            raise TimeoutError("connect")
        _,w,_=select.select([], [sock], [], left)
        if w:
            err=sock.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)
            if err!=0:
                raise OSError(err,"connect")
            break
    phase="send"
    out=hdr+body                          # request transmission, also bounded
    while out:
        left=deadline-time.monotonic()
        if left<=0:
            raise TimeoutError("send")
        _,w,_=select.select([], [sock], [], left)
        if w:
            n=sock.send(out[:65536])
            out=out[n:]
    phase="read"
    while True:
        left=deadline-time.monotonic()
        if left<=0:
            break
        readable,_,_=select.select([sock],[],[],left)
        if not readable:
            break
        try:
            chunk=sock.recv(4096)
        except Exception:
            break
        if not chunk:
            break
        buf+=chunk
        if hdr_end<0:
            k=buf.find(b"\r\n\r\n")
            if k>=0:
                hdr_end=k+4
        if hdr_end>=0:
            got=len(buf)-hdr_end
except TimeoutError:
    pass            # the deadline said stop -- whatever phase we were in
except OSError:
    pass            # connection reset / refused while we were timing it
closed=time.monotonic()
try:
    sock.close()
except Exception:
    pass
early=1 if hdr_end<0 else 0
ms_close=(closed-t0)*1000.0
over=ms_close-win*1000.0
# r32.2 R3: an abort that closed LATER than its window plus tolerance is
# recorded "late=1" and NEVER counts as a successful early abort -- a false
# timing proof (delayed connect or stalled send) must not produce before_first_byte=1
late=1 if over > tol*1000.0 else 0
if late:
    early=0
with open(os.environ["KVP_AF"],"a") as fh:
    fh.write("ABRT before_first_byte=%d ms=%.0f body_bytes=%d late=%d\n"
             % (early,ms_close,got,late))
print("ABRT early=%d late=%d" % (early,late))
PYEOF
}
# ---------------------------------------------------------------------------
# shared checks (engine log, server log, metrics, text compare)
# ---------------------------------------------------------------------------
engine_alive(){ docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; }
traceback_count(){ log_since | grep -ac 'Traceback (most recent call last)' || true; }
# r32 R4: the allow-list is a FILE of FIXED substrings (grep -Fv), no BRE
# asterisks -- an "ERROR ... client disconnected by peer" line must really be
# allowed. Kept explicit on purpose.
C_ALLOWED="$OUTDIR/allowed-errors.txt"
cat > "$C_ALLOWED" <<'ALLEOF'
client disconnected
Client disconnected
connection reset
broken pipe
AbortError
Request aborted
request aborted
aborted by client
disconnected by peer
ALLEOF
engine_errors(){ log_since | grep -aE '(^|[^A-Za-z])ERROR([^A-Za-z]|$)' | grep -Fv -f "$C_ALLOWED" | grep -v '^$' || true; }
l2_failures(){ # kotlin-store scanner over the cache-server log since a mark
  local _r=0
  kvp_scan_l2_failures "$LOG" "$1" >/dev/null 2>&1 || _r=$?
  [ "$_r" -eq 1 ] && echo 1 || echo 0
}
l2_stores_ok(){ # L2 store success events since a mark (warmup proof)
  srv_since "$1" | grep -ac 'L2 store completed: adapter [0-9]*, [1-9][0-9]* succeeded, 0 failed' || true
}
l2_reads(){ # L2 (disk -> memory) read events since a mark
  srv_since "$1" | grep -ac 'L2 prefetch load completed: request [0-9]*, [1-9][0-9]* loaded' || true
}
stuck_requests(){ KVP_URL="$API" python3 - <<'PYEOF'
import os,sys,urllib.request
req=urllib.request.Request(os.environ["KVP_URL"]+"/metrics",
    headers={"Authorization":"Bearer "+os.environ["KVP_KEY"]})
try:
    with urllib.request.urlopen(req,timeout=15) as r:
        m=r.read().decode()
    run=wait=0
    for line in m.splitlines():
        if line.startswith("vllm:num_requests_running{"):
            run=float(line.rsplit(" ",1)[1])
        elif line.startswith("vllm:num_requests_waiting{"):
            wait=float(line.rsplit(" ",1)[1])
    print(f"{int(run)} {int(wait)}")
except Exception:
    print("NA NA")
PYEOF
}
# d_compare(): token-level comparison (R6c). Uses logprobs tokens when the
# server returned them, WORDS otherwise (threshold 24 units then). Unequal
# lengths with a matching common part: divergence index = length of the
# SHORTER answer (never -1). Prints a report line and a verdict word.
d_compare(){ # d_compare <tag> <t0.json> <t1.json> <t2.json> [<prompt-tokens>]
  local tag="$1" f0="$2" f1="$3" f2="$4" pt="${5:-?}"
  KVP_F0="$f0" KVP_F1="$f1" KVP_F2="$f2" KVP_TAG="$tag" KVP_PT="$pt" python3 - <<'PYEOF'
import json,os
def load(p):
    return json.load(open(p))
t0,t1,t2=load(os.environ["KVP_F0"]),load(os.environ["KVP_F1"]),load(os.environ["KVP_F2"])
tag,pt=os.environ["KVP_TAG"],os.environ["KVP_PT"]
def units(x):
    if x.get("tokens"):
        return x["tokens"], "token"
    return (x.get("text") or "").split(), "word"
# r32.1 R6: use token comparison ONLY when ALL THREE answers carry logprobs;
# if any of T0/T1/T2 lacks them, fall back to WORDS for all three (mixing
# token-level for some and word-level for others gives non-comparable indices).
_have=[bool(t.get("tokens")) for t in (t0,t1,t2)]
use_tokens=all(_have)
def maybe(x):
    if use_tokens:
        return x.get("tokens") or [], "token"
    return (x.get("text") or "").split(), "word"
a,kind=maybe(t0); b,_=maybe(t1); c,_=maybe(t2)
THR=32 if kind=="token" else 24
def firstdiff(x,y):
    n=min(len(x),len(y))
    for i in range(n):
        if x[i]!=y[i]:
            return i
    return min(len(x),len(y))   # one answer is a prefix of the other
d_ab=firstdiff(a,b) if a!=b else None
d_ac=firstdiff(a,c) if a!=c else None
w=None
for d in (d_ab,d_ac):
    if d is None: continue
    if w is None or d<w: w=d
if w is None:
    print(f"| {tag} | {pt} | {len(a)} {kind}s | identical ({kind}) | PASS |")
else:
    verdict="FAIL" if w<THR else "WARN"
    print(f"| {tag} | {pt} | {len(a)} {kind}s | first diff {kind} {w} | {verdict} |")
PYEOF
}
# ---------------------------------------------------------------------------
# prompt generation (R6a/R6b): the unique marker goes at the BEGINNING so T0
# is cold on every run; the LENGTH is calibrated by usage.prompt_tokens from
# a live answer (chars are not tokens) against the model's sliding_window
# (2x) or, without that field, 4x the LMCache chunk size from the config.
# ---------------------------------------------------------------------------
# token target from sliding_window / chunk (the report keeps which one)
D_TARGET_SRC=""; D_TOKEN_TARGET=0
MODEL_CFG="$(find "$ROOT/live" "$ROOT/configs" -name 'config.json' -print 2>/dev/null | head -1 || true)"
D_SW=""
if [ -n "$MODEL_CFG" ] && grep -q '"sliding_window"' "$MODEL_CFG" 2>/dev/null; then
  D_SW="$(python3 - "$MODEL_CFG" <<'PYEOF'
import json,sys
print(json.load(open(sys.argv[1])).get("sliding_window") or "")
PYEOF
)"
fi
if [ -n "$D_SW" ] && [ "$D_SW" != "None" ] && [ "$D_SW" -gt 0 ] 2>/dev/null; then
  D_TOKEN_TARGET=$(( D_SW * 2 ))
  D_TARGET_SRC="2 x sliding_window ($D_SW)"
else
  D_CHUNK="${LMCACHE_CHUNK_SIZE:-}"
  [ -z "$D_CHUNK" ] && D_CHUNK="$(grep -h 'LMCACHE_CHUNK_SIZE=' "$ROOT/live"/*.env "$ROOT/configs"/*/*.env 2>/dev/null | head -1 | sed 's/.*=//' | tr -d '[:space:]')"
  # r32.1 R6: NO hardcoded fallback -- if the chunk size cannot be obtained
  # from configuration, the token target is unknown and D is INCONCLUSIVE.
  if [ -z "$D_CHUNK" ] || ! [ "$D_CHUNK" -gt 0 ] 2>/dev/null; then
    INCONC "cannot determine the LMCache chunk size (LMCACHE_CHUNK_SIZE not set, not found in live/*.env or configs/*/*.env, no sliding_window in the model config) -- the D token target is unknown, recovery cannot be validated"
  fi
  D_TOKEN_TARGET=$(( D_CHUNK * 4 ))
  D_TARGET_SRC="4 x lmc_chunk ($D_CHUNK)"
fi
echo "  D token target: $D_TOKEN_TARGET tokens ($D_TARGET_SRC, from prompt_tokens)"
# ---------------------------------------------------------------------------
# WARMUP (R2): one long unique prefix per C round (unique at the FIRST word,
# so every run is cold), sent exactly once; the D prompts T0+T1 too. Then
# the one full restart (R1) -- after it every prefix lives on DISK only.
# ---------------------------------------------------------------------------
echo "== WARMUP: $C_ROUNDS unique prefixes + D prompts (T0/T1) =="
eng_since_now
W_MARK=0
if [ -f "$LOG" ]; then W_MARK="$(wc -c < "$LOG")"; fi
mk_prefix(){ # mk_prefix <idx> <nwords> <outfile>  (marker at the START)
  KVP_IDX="$1" KVP_N="$2" KVP_OUT="$3" python3 - <<'PYEOF'
import os
idx=os.environ["KVP_IDX"]
n=int(os.environ["KVP_N"])
body=("sikkatu body %s. " % idx)*max(1,n//6)
open(os.environ["KVP_OUT"],"w").write(
  f"RUN{idx}-{os.urandom(8).hex()[:8]} tag-unique-start. "
  f"Task background: {body}Remember this text.\n"
  f"Q: short note {idx}.")
PYEOF
}
# one probe to learn bytes-per-token, then size every prompt by prompt_tokens
PROBE="$OUTDIR/w-probe.txt"
mk_prefix 0 240 "$PROBE"
chat "$OUTDIR/w-probe.json" "$PROBE" warmup || INCONC "the warm-up probe request failed -- is the engine healthy?"
PT="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("prompt_tokens") or 0)' "$OUTDIR/w-probe.json")"
[ "${PT:-0}" -gt 0 ] || INCONC "usage.prompt_tokens is missing from the engine answer -- cannot calibrate prompt length in tokens at all (R6a)"
BYTES="$(wc -c < "$PROBE")"
PER_TOK=$(( BYTES / PT ))
[ "$PER_TOK" -ge 1 ] || PER_TOK=1
echo "  calibration: $PT prompt_tokens for $BYTES chars -> ~$PER_TOK chars/token"
# C prefixes: sized 2x the token target (longer than any window)
C_NWORDS=$(( (D_TOKEN_TARGET * 2 * PER_TOK) / 6 + 240 ))
for k in $(seq 1 "$C_ROUNDS"); do
  mk_prefix "$k" "$C_NWORDS" "$OUTDIR/c-prefix-$k.txt"
done
# WARMUP-C: each prefix is warmed exactly ONCE (r32.1 R2: prefix 1 used to be
# sent twice -- once as the quiet probe and once as round 1). The before-answer
# for the post-C comparison comes from the SAME single round-1 warm-up reply.
for k in $(seq 1 "$C_ROUNDS"); do
  chat "$OUTDIR/w-c-$k.json" "$OUTDIR/c-prefix-$k.txt" warmup 64 >/dev/null \
    || INCONC "warm-up request (prefix $k) failed"
done
C_BEFORE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["text"])' "$OUTDIR/w-c-1.json")"
# WARMUP-D: D prompts T0+T1 (unique marker at the START -> T0 is cold here)
D_PTS=""
for i in 1 2 3; do
  mk_prefix "D$i" $(( D_TOKEN_TARGET * 2 / 6 + 240 )) "$OUTDIR/d-$i-prompt.txt"
  chat "$OUTDIR/d-$i-t0.json" "$OUTDIR/d-$i-prompt.txt" d0 64 true >/dev/null \
    || INCONC "D T0 request $i failed"
  chat "$OUTDIR/d-$i-t1.json" "$OUTDIR/d-$i-prompt.txt" d1 64 true >/dev/null \
    || INCONC "D T1 request $i failed"
  _pt="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("prompt_tokens") or 0)' "$OUTDIR/d-$i-t0.json")"
  D_PTS="$D_PTS $_pt"
done
echo "  warm-up done; waiting for L2 store events (up to 120s)..."
# r32.1 R2: the FULL expected number of successful stores must be seen, not
# merely "> 0" -- every warm-up request (1 probe + C_ROUNDS prefixes + 3 D T0
# + 3 D T1) is unique and must land one successful L2 store.
WARMUP_EXPECT=$(( C_ROUNDS + 7 ))
STORES=0
for w in $(seq 1 24); do
  STORES="$(l2_stores_ok "$W_MARK")"
  [ "${STORES:-0}" -ge "$WARMUP_EXPECT" ] && break
  sleep 5
done
echo "  L2 store success events after warm-up: $STORES"
[ "${STORES:-0}" -ge "$WARMUP_EXPECT" ] || INCONC "only $STORES of $WARMUP_EXPECT expected L2 store events after the warm-up -- the warmup is INCOMPLETE, disk retrieve cannot be proven later"
# ---- ONE full restart (R1) -- after it the prefixes live on DISK only -----
echo "== FULL RESTART (engine first, then cache -- round 46 order) =="
if [ "${PR_CHECKS_SKIP_RESTART:-0}" = "1" ]; then
  echo "TEST MODE: PR_CHECKS_SKIP_RESTART=1 set -- the restart is SKIPPED, D cannot be proven"
  D_SKIPPED=1
else
  D_SKIPPED=0
  # r32: the restart routine is shared (restart-lib.sh); SCRIPTS_DIR lets the
  # machine-free tests swap in stub stop-cache/start-cache/ipc-test scripts,
  # and api_ok is exported as a function so the lib's `bash -c` probe sees it.
  SCRIPTS_DIR="${KVP_SCRIPTS_DIR:-$HERE}"
  export -f api_ok
  kvp_full_restart "$SCRIPTS_DIR" "$CONTAINER" "api_ok" || exit $?
fi
if [ -f "$LOG" ]; then M0="$(wc -c < "$LOG")"; else M0=0; fi
# ---------------------------------------------------------------------------
# SCENARIO C (#4136): abort a stream request DURING a disk-backed retrieve.
# Round k aborts 4 streams over prefix k at random 0..C_ABORT_MAX_MS; after
# the restart the prefix is only on DISK, so the aborts land inside the
# retrieve window. Disk proof (R2): > 0 L2 prefetch-load events during C or
# the scenario is INCONCLUSIVE -- without them nothing about the retrieve was
# proven. Early-abort proof (R3): >= MIN_EARLY_PCT% of the abort lines say
# before_first_byte=1 and every abort printed its line.
# ---------------------------------------------------------------------------
echo "== SCENARIO C (#4136): abort during retrieve over $C_ROUNDS disk prefixes =="
eng_since_now
C_MARK="$M0"
AF="$OUTDIR/c-aborts.txt"
: > "$AF"
C_LAUNCHED=0            # r32.1 R7: REAL abort launches, counted per spawn
for round in $(seq 1 "$C_ROUNDS"); do
  pids=()
  for k in 1 2 3 4; do
    ms=$(( $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % (C_ABORT_MAX_MS + 1) ))
    abort_stream "$OUTDIR/c-prefix-$round.txt" "$ms" "$AF" >/dev/null 2>/dev/null &
    pids+=("$!")
    C_LAUNCHED=$((C_LAUNCHED + 1))   # r32.1 R7: count REAL launches, not just the plan
  done
  wait "${pids[@]}" 2>/dev/null || true
  if [ $(( round % 5 )) -eq 0 ] || [ "$round" = 1 ]; then
    printf '[C] round %d/%s done, lines so far: %s\n' "$round" "$C_ROUNDS" \
      "$(wc -l < "$AF" 2>/dev/null | tr -d ' ')"
  fi
done
C_TOTAL=$(( C_ROUNDS * 4 ))
C_LINES="$(wc -l < "$AF" 2>/dev/null | tr -d ' ')"
# r32.2 R3: an abort is EARLY only when it closed BEFORE the first byte AND
# within its deadline tolerance -- `late=1` lines are stripped from the count.
C_EARLY="$(grep '^ABRT before_first_byte=1 ' "$AF" 2>/dev/null | grep -c ' late=0$' || true)"
C_EARLY="${C_EARLY:-0}"
C_LATE="$(grep -c ' late=1$' "$AF" 2>/dev/null || true)"
C_LATE="${C_LATE:-0}"
C_DIST=0
[ "$C_TOTAL" -gt 0 ] && C_DIST=$(( C_EARLY * 100 / C_TOTAL ))
echo "  abort lines: $C_LINES/$C_TOTAL, early (before first byte): $C_EARLY ($C_DIST%), late (missed deadline): $C_LATE"
C_READS="$(l2_reads "$C_MARK")"
echo "  L2 disk-read events during C: $C_READS"
echo "  waiting ${C_SETTLE}s for any stuck request to settle..."
sleep "$C_SETTLE"
C_VERDICT="PASS"; C_NOTES=""
# (R3) lost aborts / early-share gate
if [ "${C_LINES:-0}" -lt "$C_TOTAL" ]; then
  C_VERDICT="INCONCLUSIVE"; C_NOTES="$C_NOTES
- $((C_TOTAL - C_LINES)) of $C_TOTAL aborts printed no line (lost -- python crash or early socket death)"
fi
if [ "$C_DIST" -lt "$MIN_EARLY_PCT" ]; then
  C_VERDICT="INCONCLUSIVE"; C_NOTES="$C_NOTES
- only $C_DIST% of the aborts landed before the first response byte (threshold $MIN_EARLY_PCT%) -- the retrieve window was not engaged"
fi
# (r32.2 R3) a late abort is INVALID timing evidence: a stream that connected
# or transmitted past its window proves nothing about the retrieve, so any
# late abort invalidates the whole early-abort proof -> INCONCLUSIVE.
if [ "${C_LATE:-0}" -gt 0 ]; then
  C_VERDICT="INCONCLUSIVE"; C_NOTES="$C_NOTES
- $C_LATE of $C_TOTAL aborts closed AFTER their deadline tolerance (delayed connect or stalled transmission) -- their timing is invalid, the early-abort proof cannot stand"
fi
# (R2) disk proof
if [ "${C_READS:-0}" -le 0 ]; then
  C_VERDICT="INCONCLUSIVE"; C_NOTES="$C_NOTES
- zero L2 prefetch-load events during C -- the retrieve did NOT come from disk, nothing was proven"
fi
# engine-log + request checks (r32 R10: exact strings, S3/S5/S6/S8/S9)
if ! engine_alive; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- the engine container '$CONTAINER' is DEAD after the abort series"
fi
C_TB="$(traceback_count)"
if [ "${C_TB:-0}" -gt 0 ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- $C_TB 'Traceback' occurrences in the engine log since scenario C started"
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
- kvp_scan_l2_failures reports an L2 store failure during scenario C"
fi
read -r C_RUN C_WAIT <<<"$(stuck_requests)"
if [ "$C_RUN" = "NA" ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- /metrics did not answer (cannot prove no stuck requests)"
elif [ "$C_RUN" != "0" ] || [ "$C_WAIT" != "0" ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- stuck requests ${C_SETTLE}s after the aborts: running=$C_RUN waiting=$C_WAIT (must be 0 0)"
fi
chat "$OUTDIR/c-after.json" "$OUTDIR/c-prefix-1.txt" cafter 16 >/dev/null \
  || PA C "the post-scenario plain request failed"
C_AFTER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["text"])' "$OUTDIR/c-after.json")"
if [ "$C_AFTER" != "$C_BEFORE" ]; then
  C_VERDICT="FAIL"; C_NOTES="$C_NOTES
- the plain answer CHANGED after the abort series (before: $(printf '%.60s' "$C_BEFORE")...; after: $(printf '%.60s' "$C_AFTER")...)"
fi
echo "  C result: $C_VERDICT (aborts $C_LINES/$C_TOTAL, early $C_EARLY/$C_TOTAL after-byte $((C_TOTAL-C_EARLY)), disk reads $C_READS)"
[ -n "$C_NOTES" ] && printf '%s\n' "$C_NOTES" | sed 's/^/  /' || true
# ---------------------------------------------------------------------------
# SCENARIO D (#5972): T2 (disk-backed) must regenerate T0 exactly. T0/T1 were
# captured in the warm-up (before the restart); T2 uses the SAME prompts now.
# Length proof (R6a): each prompt's prompt_tokens must reach the D token
# target; shorter prompts -> INCONCLUSIVE. Disk proof (R6d): > 0 L2 reads
# after the restart -> else INCONCLUSIVE. Divergence (R6c): tokens (or words
# when logprobs are missing), FAIL inside 32/24 units, WARN later. L2 store
# failures during D (R6e) -> FAIL. SKIP_RESTART -> SKIPPED (never PASS).
# ---------------------------------------------------------------------------
echo "== SCENARIO D (#5972): incomplete-object recovery (T2 disk-backed) =="
eng_since_now
D_MARK="$M0"
D_VERDICT="PASS"; D_NOTES=""; D_LINES=""
D_PTS_ARR=($D_PTS)
idx=0
for i in 1 2 3; do
  idx=$(( idx + 1 ))
  pt="${D_PTS_ARR[$((idx-1))]:-0}"
  if [ "${pt:-0}" -lt "$D_TOKEN_TARGET" ]; then
    D_VERDICT="INCONCLUSIVE"; D_NOTES="$D_NOTES
- prompt $i: only $pt prompt_tokens (target $D_TOKEN_TARGET) -- the prompt is NOT longer than the window, recovery is not exercised"
  fi
  chat "$OUTDIR/d-$i-t2.json" "$OUTDIR/d-$i-prompt.txt" d2 64 true >/dev/null \
    || PA D "T2 (disk-backed) request $i failed"
  line="$(d_compare "prompt-$i" "$OUTDIR/d-$i-t0.json" "$OUTDIR/d-$i-t1.json" "$OUTDIR/d-$i-t2.json" "$pt")"
  D_LINES="$D_LINES
$line"
  case "$line" in
    *FAIL*) D_VERDICT="FAIL"; D_NOTES="$D_NOTES
- prompt $i: divergence inside the fail window (corrupt recovery)" ;;
    *WARN*)
      [ "$D_VERDICT" = "PASS" ] && D_VERDICT="WARN"
      D_NOTES="$D_NOTES
- prompt $i: later divergence -- possible numeric drift" ;;
  esac
done
D_READS="$(l2_reads "$D_MARK")"
D_CACHED="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("cached_tokens"))' "$OUTDIR/d-1-t2.json")"
echo "  L2 disk-read events during D: $D_READS, cached_tokens (T2 #1): $D_CACHED"
if [ "$D_SKIPPED" = "1" ]; then
  D_VERDICT="SKIPPED"; D_NOTES="$D_NOTES
- PR_CHECKS_SKIP_RESTART=1: the restart was skipped, T2 is a memory hit -- recovery of INCOMPLETE objects is NOT tested (this must never be a PASS)"
elif [ "${D_READS:-0}" -le 0 ]; then
  D_VERDICT="INCONCLUSIVE"; D_NOTES="$D_NOTES
- zero L2 prefetch-load events after the restart -- T2 did NOT come from disk, nothing was proven"
fi
D_L2FAIL="$(l2_failures "$D_MARK")"
if [ "$D_L2FAIL" != "0" ]; then
  D_VERDICT="FAIL"; D_NOTES="$D_NOTES
- kvp_scan_l2_failures reports an L2 store failure during scenario D"
fi
echo "  D result: $D_VERDICT (L2 disk reads: $D_READS)"
[ -n "$D_NOTES" ] && printf '%s\n' "$D_NOTES" | sed 's/^/  /' || true
# ---------------------------------------------------------------------------
# REPORT (R8). Wheel = the lmcache version INSIDE the running engine
# container (the first vendor entry is a directory name, not a wheel);
# hardware = GPU names via nvidia-smi; PR heads come from SOURCES.md (never
# hardcoded). Timestamped file + latest copy, old reports kept. The control
# line is honest: the image identity gate refuses unpatched images, so this
# run claims only "no failure WITH the patches".
# ---------------------------------------------------------------------------
WHEEL="$(docker exec "$CONTAINER" python3 -c 'import importlib.metadata as m; print(m.version("lmcache"))' 2>/dev/null | head -1 || true)"
[ -n "$WHEEL" ] || WHEEL="unknown"
GPU_INFO="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | sort | uniq -c | awk '{cnt=$1; $1=""; sub(/^ /,""); printf "%sx %s; ", cnt, $0}' | sed 's/; $//' || true)"
[ -n "$GPU_INFO" ] || GPU_INFO="unknown"
SHA_4136="$(grep -A 10 '## PR #4136' "$ROOT/vendor/patches/lmcache/SOURCES.md" 2>/dev/null | grep 'PR head' | grep -oE '\b[0-9a-f]{40}\b' | head -1 || true)"
SHA_5972="$(grep -A 10 '## PR #5972' "$ROOT/vendor/patches/lmcache/SOURCES.md" 2>/dev/null | grep 'PR head' | grep -oE '\b[0-9a-f]{40}\b' | head -1 || true)"
[ -n "$SHA_4136" ] || SHA_4136="unknown"
[ -n "$SHA_5972" ] || SHA_5972="unknown"
{
  # r32.1 R7: TEST MODE is the ACTUAL first line of the report (before the
  # title), so nobody can mistake a test run for a hardware proof at a glance.
  if [ -n "$TEST_MODE_VARS" ]; then
    echo "TEST MODE:$(for _v in $TEST_MODE_VARS; do printf ' %s' "$_v"; done) set -- this run is NOT a hardware proof"
  fi
  echo "# PR #4136 / #5972 hardware checks"
  echo
  echo "- date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "- wheel (lmcache version in $CONTAINER): $WHEEL"
  echo "- hardware: $GPU_INFO"
  echo "- PR #4136 head: $SHA_4136"
  echo "- PR #5972 head: $SHA_5972"
  echo "- image label org.kvp.lmc-patch-4136: $(docker inspect -f '{{index .Config.Labels "org.kvp.lmc-patch-4136"}}' "$CONTAINER" 2>/dev/null || echo '<absent>')"
  echo "- image label org.kvp.lmc-patch-5972: $(docker inspect -f '{{index .Config.Labels "org.kvp.lmc-patch-5972"}}' "$CONTAINER" 2>/dev/null || echo '<absent>')"
  echo "- engine container: $CONTAINER"
  echo "- requests: warm-up $((1 + C_ROUNDS)) + D T0/T1 x6 + D T2 x3 + C abort streams expected $((C_ROUNDS * 4)) / launched $C_LAUNCHED / metric lines $C_LINES + C before/after"
  echo
  echo "## Scenario C (#4136, abort during a disk-backed retrieve)"
  echo
  echo "- result: **$C_VERDICT** (exit code: $([ "$C_VERDICT" = FAIL ] && echo 1 || { [ "$C_VERDICT" = INCONCLUSIVE ] && echo 5 || echo 0; }))"
  echo "- aborts: $C_LINES lines / $C_TOTAL sent, early (before first byte, on time): $C_EARLY ($C_DIST%), late (missed deadline): $C_LATE, after/into the body: $((C_TOTAL - C_EARLY - C_LATE))"
  echo "- L2 disk-read events during C: $C_READS"
  [ -n "$C_NOTES" ] && printf '%s\n' "$C_NOTES"
  echo
  echo "## Scenario D (#5972, incomplete-object recovery)"
  echo
  echo "- result: **$D_VERDICT** (exit code: $([ "$D_VERDICT" = FAIL ] && echo 1 || { { [ "$D_VERDICT" = INCONCLUSIVE ] || [ "$D_VERDICT" = SKIPPED ]; } && echo 5 || echo 0; }))"
  echo "- token target: $D_TOKEN_TARGET ($D_TARGET_SRC); prompt tokens: $D_PTS"
  echo "- L2 disk-read events during D: $D_READS, cached_tokens (T2 #1): $D_CACHED"
  echo
  echo "| prompt | prompt tokens | answer | first divergence | verdict |"
  echo "| --- | --- | --- | --- | --- |"
  printf '%s\n' "$D_LINES" | grep '^|' || echo "| (none) | | | | |"
  [ -n "$D_NOTES" ] && printf '%s\n' "$D_NOTES"
  echo
  echo "## Control"
  echo
  echo "- control: none -- the image identity gate refuses unpatched images; the result claims only 'no failure with the patches', not that the PRs fix anything"
} > "$REPORT_TS"
cp "$REPORT_TS" "$REPORT_LATEST"
echo
echo "== report written to $REPORT_TS (latest copy: $REPORT_LATEST) =="
cat "$REPORT_TS"
# ---- final exit: FAIL=1 beats everything, INCONCLUSIVE/SKIPPED=5, else 0 ---
rc=0
[ "$C_VERDICT" = "FAIL" ] && rc=1
[ "$D_VERDICT" = "FAIL" ] && rc=1
if [ "$rc" = "0" ]; then
  [ "$C_VERDICT" = "INCONCLUSIVE" ] && rc=5
  { [ "$D_VERDICT" = "INCONCLUSIVE" ] || [ "$D_VERDICT" = "SKIPPED" ]; } && rc=5
fi
exit "$rc"