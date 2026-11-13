#!/usr/bin/env bash
# extras/check.sh -- self-check for the observability/extras block.
# Modes:
#   --static   no docker/network: YAML/JSON/pin/contract checks -> exit 0 when clean
#   --live     (default when docker is up) static + Prometheus targets + relay
#              behaviour + metric drift + Grafana loading.  exit 0 clean / 1 FAIL / 2 WARN
#   --json     machine-readable JSON report; combined: `--json --static`
# NEVER changes anything.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRAS="$HERE"
ROOT="$(cd "$HERE/../.." && pwd)"

MODE=auto; JSON=0
for a in "$@"; do
  case "$a" in
    --static) MODE=static ;;
    --live)   MODE=live ;;
    --json)   JSON=1 ;;
  esac
done
if [ "$MODE" = auto ]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then MODE=live; else MODE=static; fi
fi

RESULTS=()
_ok()    { RESULTS+=("OK|$1"); }
_warn()  { RESULTS+=("WARN|$1"); }
_fail()  { RESULTS+=("FAIL|$1"); }

# ======================== helpers: run python, parse markers ==============
# Each inline python block prints lines like  FAIL: message  /  WARN: msg  /
# OK: msg -- bash converts them into the same counters the rest uses.
run_py() {
  # drop a leading '-' if the caller passed one inline convention marker
  [ "${1:-}" = "-" ] && shift
  local _out
  _out="$(python3 - "$@" 2>&1)"
  local _rc=$?
  if [ $_rc -ne 0 ]; then _fail "python check crashed: $(printf '%s' "$_out" | tail -1)"; return; fi
  while IFS= read -r line; do
    case "$line" in
      FAIL:*) _fail "${line#FAIL: }" ;;
      WARN:*) _warn "${line#WARN: }" ;;
      OK:*)   _ok   "${line#OK: }" ;;
      *)      ;;
    esac
  done <<< "$_out"
}

# ======================== STATIC checks ==================================
# 1) YAML syntax of scrape.d & rules.d
run_py - "$EXTRAS" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
try:
    import yaml
except ImportError:
    print("WARN: pyyaml module not installed -- YAML syntax not validated (parsed by promtool when available)")
    sys.exit(0)
for sub in ("scrape.d", "rules.d"):
    for p in sorted((root / sub).glob("*.yml")):
        try:
            yaml.safe_load(p.read_text())
            print(f"OK: {sub}/{p.name} YAML parses")
        except Exception as e:
            print(f"FAIL: {sub}/{p.name} YAML broken: {e}")
PY

# 2) dashboard contract (7.5)
DASH="$EXTRAS/grafana/dashboards/kvp-inference.json"
if [ -f "$DASH" ]; then
  run_py - "$DASH" "$EXTRAS/rules.d" <<'PY'
import json, pathlib, re, sys
d = json.loads(pathlib.Path(sys.argv[1]).read_text())
rd = pathlib.Path(sys.argv[2])
bad = []
if d.get("uid") != "kvp-inference": bad.append("uid must be kvp-inference")
if d.get("id") is not None: bad.append("id must be null")
if "version" in d or "meta" in d: bad.append("version/meta must be removed")
rules = set()
for y in rd.glob("*.yml"):
    for m in re.finditer(r"record:\s*(\S+)", y.read_text()):
        rules.add(m.group(1))
ALLOWED = {"row", "stat", "timeseries", "gauge", "bargauge", "table"}
exprs, ds_bad = [], 0
types = []
for top in d.get("panels", []):
    types.append(top.get("type"))
    for nested in (top.get("panels") or []):
        types.append(nested.get("type"))
def walk(x):
    global ds_bad
    if isinstance(x, dict):
        if x.get("expr") and isinstance(x["expr"], str):
            exprs.append(x["expr"])
        if isinstance(x.get("datasource"), dict):
            if x["datasource"].get("type") != "prometheus" or x["datasource"].get("uid") != "prometheus":
                ds_bad += 1
        for v in x.values(): walk(v)
    elif isinstance(x, list):
        for v in x: walk(v)
walk(d)
if ds_bad: bad.append(f"{ds_bad} datasource ref(s) not uid=prometheus")
for t in types:
    if t is not None and t not in ALLOWED: bad.append(f"panel type {t!r} not in builtin set")
for e in exprs:
    if re.search(r"(?<![:\w])(vllm:|DCGM_)", e): bad.append(f"raw engine metric in expr: {e[:60]}")
    for name in re.findall(r"\b(kvp:[A-Za-z0-9_:]+)", e):
        if name not in rules: bad.append(f"undefined rule {name} in expr: {e[:60]}")
for b in bad: print(f"FAIL: {b}")
if not bad: print("OK: dashboard contract clean (7.5)")
PY
else
  _warn "dashboard kvp-inference.json missing (sub-commit 4b not present)"
fi

# 3) pinned images in compose files (no :latest, tag present)
run_py - "$ROOT" <<'PY'
import pathlib, re, sys
root = pathlib.Path(sys.argv[1])
for fn in ("observability/docker-compose.yml",
           "observability/docker-compose.override.yml"):
    p = root / fn
    if not p.is_file(): continue
    for ref in re.findall(r"^\s+image:\s*(\S+)", p.read_text(), re.M):
        if ":latest" in ref:
            print(f"FAIL: :latest in {fn}: {ref}")
        elif not re.search(r":[\w][\w.\-]*$", ref):
            print(f"FAIL: unpinned (no tag) in {fn}: {ref}")
print("OK: all image refs pinned")
PY

# 4) no published host ports in overrides (R7)
OVR="$ROOT/observability/docker-compose.override.yml"
if [ -f "$OVR" ]; then
  run_py - "$OVR" <<'PY'
import re, sys, pathlib
t = pathlib.Path(sys.argv[1]).read_text()
# every `ports:` section is a violation in the override (services stay inside compose network)
for i, line in enumerate(t.splitlines(), 1):
    if re.match(r"^\s+ports:\s*$", line):
        print(f"FAIL: published port block at override line {i} -- nothing may bind host (R7)")
print("OK: no host-published ports in docker-compose.override.yml")
PY
fi

# 5) promtool check rules + config (skip gracefully)
if command -v promtool >/dev/null 2>&1; then
  for y in "$EXTRAS"/rules.d/*.yml; do
    if promtool check rules "$y" >/dev/null 2>&1; then
      _ok "promtool check rules: $(basename "$y")"
    else
      _fail "promtool check rules REJECTED $(basename "$y"): $(promtool check rules "$y" 2>&1 | tail -3)"
    fi
  done
  if [ -f "$ROOT/observability/prometheus.yml" ]; then
    if promtool check config "$ROOT/observability/prometheus.yml" >/dev/null 2>&1; then
      _ok "promtool check config: prometheus.yml"
    else
      _fail "promtool check config REJECTED prometheus.yml"
    fi
  fi
else
  _warn "promtool not installed -- rule/config syntax checked on the operator stand only"
fi

# ======================== LIVE checks ====================================
if [ "$MODE" = static ]; then
  [ "$JSON" = "1" ] && { printf '{"mode":"static","results":['; for r in "${RESULTS[@]}"; do printf '{"level":"%s","msg":%s},' "${r%%|*}" "\"${r#*|}\""; done | sed 's/,$//'; printf ']}\n'; }
  if [ "$JSON" = "0" ]; then for r in "${RESULTS[@]}"; do echo "${r%%|*}  ${r#*|}"; done; fi
  if printf '%s\n' "${RESULTS[@]}" | grep -q '^FAIL'; then
    [ "$JSON" = "1" ] || echo "check.sh: mode=static FAIL"
    exit 1
  fi
  [ "$JSON" = "1" ] || echo "check.sh: mode=static all clean"
  exit 0
fi

# Prometheus reachability
if curl -fsS --max-time 5 http://127.0.0.1:9091/-/ready >/dev/null 2>&1; then
  _ok "prometheus answering on :9091"
else
  _fail "prometheus not reachable on :9091"
fi

# Targets health: jobs vllm + gpu
run_py - <<'PY'
import json, sys
try:
    data = json.load(__import__('urllib.request', fromlist=['urlopen']).urlopen("http://127.0.0.1:9091/api/v1/targets", timeout=8))
except Exception as e:
    print(f"FAIL: cannot query prometheus targets API: {e}")
    sys.exit(0)
for job in ("vllm", "gpu"):
    found = [t for t in data.get("data", {}).get("activeTargets", []) if t.get("labels", {}).get("job") == job]
    if not found:
        print(f"WARN: job={job} has no registered target")
        continue
    for t in found:
        h = t.get("health", "unknown")
        le = t.get("lastError", "")
        if h == "up":
            print(f"OK: job={job} target {t.get('scrapeUrl','?')} is UP")
        elif h == "unknown":
            print(f"WARN: job={job} target {t.get('scrapeUrl','?')} unknown/not yet scraped")
        else:
            print(f"FAIL: job={job} target {t.get('scrapeUrl','?')} is {h}: {le}")
PY

# Relay behaviour (200 on /metrics, 404/405 elsewhere) -- only if relay container is up
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx obs_vllm_relay; then
  RELAY_PORT="${KVP_OBS_RELAY_PORT:-30008}"
  GW="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo 172.17.0.1)"
  for probe in "/metrics GET 200" "/v1/models GET 404" "/metrics POST 405"; do
    set -- $probe
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$2" "http://$GW:$RELAY_PORT$1" 2>/dev/null || echo "000")
    if [ "$code" = "$3" ]; then _ok "relay $2 $1 -> $code"; else _fail "relay $2 $1 -> $code (expected $3)"; fi
  done
else
  _warn "obs_vllm_relay not running -- relay probe skipped"
fi

# Metric drift: call relay on its REAL listen address (bridge GW), not 127.0.0.1
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx obs_vllm_relay; then
  RELAY_PORT="${KVP_OBS_RELAY_PORT:-30008}"
  GW="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo 172.17.0.1)"
  live_metrics="$(curl -fsS --max-time 8 "http://$GW:$RELAY_PORT/metrics" 2>/dev/null | grep -oE '^vllm:[A-Za-z0-9_]+' | sort -u || true)"
  if [ -n "$live_metrics" ]; then
    _ok "metric drift check ran through the relay on $GW:$RELAY_PORT"
  else
    _warn "engine metrics not reachable through the relay on $GW:$RELAY_PORT (engine down? -- drift check skipped)"
  fi
fi

# Grafana + dashboard loading
if curl -fsS --max-time 5 http://127.0.0.1:3000/api/health >/dev/null 2>&1; then
  _ok "grafana healthy"
  if curl -fsS --max-time 8 "http://127.0.0.1:3000/api/dashboards/uid/kvp-inference" >/dev/null 2>&1; then
    _ok "dashboard kvp-inference loaded by provisioning"
  else
    _warn "dashboard kvp-inference not found in grafana (check provisioning path / folder)"
  fi
else
  _fail "grafana not reachable on :3000"
fi

# ======================== report =========================================
FAILS=0; WARNS=0; OKS=0
for r in "${RESULTS[@]}"; do
  case "${r%%|*}" in
    FAIL) FAILS=$((FAILS+1));;
    WARN) WARNS=$((WARNS+1));;
    OK)   OKS=$((OKS+1));;
  esac
done
if [ "$JSON" = "1" ]; then
  printf '{"mode":"%s","ok":%d,"warn":%d,"fail":%d,"results":[' "$MODE" "$OKS" "$WARNS" "$FAILS"
  first=1
  for r in "${RESULTS[@]}"; do
    [ $first = 0 ] && printf ','
    first=0
    printf '{"level":"%s","msg":%s}' "${r%%|*}" "\"$(printf '%s' "${r#*|}" | sed 's/"/\\"/g')\""
  done
  printf ']}\n'
elif [ "$JSON" = "0" ]; then
  for r in "${RESULTS[@]}"; do echo "${r%%|*}  ${r#*|}"; done
  echo "check.sh: mode=$MODE  ok=$OKS  warn=$WARNS  fail=$FAILS"
fi
if [ "$FAILS" -gt 0 ]; then exit 1; fi
if [ "$WARNS" -gt 0 ]; then exit 2; fi
exit 0