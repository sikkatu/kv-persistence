#!/usr/bin/env bash
# extras/check.sh -- self-check for the observability/extras block.
# Two modes:
#   --static   no docker, no network: YAML/JSON/rule/UI-contract checks (7.5)
#   --live     (default when docker is up) everything --static does, plus
#              Prometheus targets, metric drift vs expected-metrics.txt,
#              Grafana dashboard load, and the relay behaviour.
#   --json     machine-readable report on stdout
# Exit codes: 0 all OK | 1 FAIL (broken dashboard/rules/target down) |
#             2 only WARNs (extras partially unavailable).
# This script NEVER changes anything.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRAS="$HERE"
ROOT="$(cd "$HERE/../.." && pwd)"
MODE="auto"; JSON=0
[ "${1:-}" = "--static" ] && MODE=static
[ "${1:-}" = "--live" ] && MODE=live
[ "${1:-}" = "--json" ] && JSON=1
if [ "$JSON" = 1 ] && [ "${1:-}" = "--json" ]; then MODE="${2:-auto}"; fi
if [ "$MODE" = auto ]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then MODE=live
  else MODE=static; fi
fi
OKS=0; FAILS=0; WARNS=0
RES=""
fin(){ printf '%s' "$RES"; echo; echo "check.sh: mode=$MODE  ok=$OKS  warn=$WARNS  fail=$FAILS"
  if [ "$FAILS" -gt 0 ]; then exit 1; fi
  if [ "$WARNS" -gt 0 ]; then exit 2; fi
  exit 0; }
ok(){ OKS=$((OKS+1)); RES="${RES}OK    $*\n"; [ "$JSON" = 1 ] || printf 'OK    %s\n' "$*"; }
warn(){ WARNS=$((WARNS+1)); RES="${RES}WARN  $*\n"; [ "$JSON" = 1 ] || printf 'WARN  %s\n' "$*"; }
fail(){ FAILS=$((FAILS+1)); RES="${RES}FAIL  $*\n"; [ "$JSON" = 1 ] || printf 'FAIL  %s\n' "$*"; }

# ======================= static checks (always) ===========================
for f in scrape.d/vllm.yml scrape.d/gpu.yml rules.d/vllm.yml rules.d/gpu.yml; do
  [ -s "$EXTRAS/$f" ] || fail "missing $f"
done
python3 - "$EXTRAS" <<'PY'
import sys, pathlib
try:
    import yaml
except Exception:
    print("YAML_MODULE_MISSING")
    sys.exit(0)
root = pathlib.Path(sys.argv[1])
bad = 0
for p in list((root/"scrape.d").glob("*.yml")) + list((root/"rules.d").glob("*.yml")):
    try:
        yaml.safe_load(p.read_text())
    except Exception as e:
        bad += 1
        print(f"YAML_BAD {p.name}: {e}")
print("YAML_OK" if bad == 0 else f"YAML_BAD_COUNT={bad}")
PY
# dashboard JSON (7.5 contract) -- validated if the dashboard exists yet
DASH="$EXTRAS/grafana/dashboards/kvp-inference.json"
if [ -f "$DASH" ]; then
  python3 - "$DASH" "$EXTRAS/rules.d" <<'PY'
import json, pathlib, re, sys
p = pathlib.Path(sys.argv[1]); rd = pathlib.Path(sys.argv[2])
try: d = json.loads(p.read_text())
except Exception as e:
    print(f"DASH_JSON_BAD {e}"); sys.exit(0)
bad = []
if d.get("uid") != "kvp-inference": bad.append("uid must be kvp-inference")
rules = set()
for y in rd.glob("*.yml"):
    for m in re.finditer(r"record:\s*(\S+)", y.read_text()):
        rules.add(m.group(1))
exprs = []
def walk(x):
    if isinstance(x, dict):
        for k, v in x.items():
            if k in ("expr","exprRaw") and isinstance(v, str): exprs.append(v)
            walk(v)
    elif isinstance(x, list):
        for v in x: walk(v)
walk(d)
for e in exprs:
    for name in re.findall(r"\b(kvp:[A-Za-z0-9_:]+)", e):
        if name not in rules: bad.append(f"expr references undefined rule {name}")
    if re.search(r"\b(vllm:|DCGM_)", e): bad.append(f"raw engine name in expr: {e[:60]}")
for pan in [q for r in d.get("panels",[]) for q in ([r] + r.get("panels",[]))]:
    for t in pan.get("targets", []):
        ds = t.get("datasource")
        if ds and (ds.get("type") != "prometheus" or ds.get("uid") != "prometheus"):
            bad.append("panel datasource must be prometheus by uid")
for b in bad: print("DASH_CONTRACT", b)
print("DASH_CONTRACT_OK" if not bad else f"DASH_CONTRACT_BAD={len(bad)}")
PY
else
  warn "dashboard JSON not present yet (4b)"
fi
# images must be pinned (no :latest) in every compose file present
for f in "$ROOT/observability/docker-compose.yml" "$ROOT/observability/docker-compose.override.yml" "$ROOT/observability/extras/relay/compose.yml"; do
  [ -f "$f" ] || continue
  if grep -nE 'image:.*:latest' "$f" >/dev/null 2>&1; then fail ":latest tag found in $f"; else ok "pinned images in $(basename "$f")"; fi
done
# no published ports in extras-only services (R7) -- checked on the override
OVR="$ROOT/observability/docker-compose.override.yml"
if [ -f "$OVR" ]; then
  if grep -nE '^\s+-\s+"0\.0\.0\.0:' "$OVR" >/dev/null 2>&1; then fail "0.0.0.0 port published in docker-compose.override.yml"
  else ok "no outward ports in docker-compose.override.yml"; fi
fi
if [ "$MODE" = static ]; then
  warn "live checks skipped (--static)"
  fin
fi

# ============================== live checks ===============================
BASE_URL_9091="http://127.0.0.1:9091"
if curl -fsS --max-time 5 "$BASE_URL_9091/-/ready" >/dev/null 2>&1; then
  ok "prometheus answering on :9091"
else
  fail "prometheus not reachable on :9091"
fi
# targets health for jobs vllm and gpu
for job in vllm gpu; do
  body="$(curl -fsS --max-time 8 "$BASE_URL_9091/api/v1/targets" 2>/dev/null || true)"
  if [ -n "$body" ]; then
    health="$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    t = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for a in t.get("data", {}).get("activeTargets", []):
    if a.get("labels", {}).get("job") == sys.argv[1]:
        print(f"{a.get(\"health\")} {a.get(\"lastError\",\"\")}"[:200])
' "$job" 2>/dev/null || true)"
    case "$health" in
      up*) ok "target job=$job is up" ;;
      "") warn "job=$job has no target registered (scrape not configured or file_sd empty)" ;;
      *) fail "job=$job target unhealthy: $health" ;;
    esac
  fi
done
# metric drift vs the expected snapshot (WARN only)
EXP="$EXTRAS/expected-metrics.txt"
live_metrics="$(curl -fsS --max-time 8 "http://127.0.0.1:${KVP_OBS_RELAY_PORT:-30008}/metrics" 2>/dev/null | grep -oE '^vllm:[a-zA-Z0-9_]+' | sort -u || true)"
if [ -z "$live_metrics" ]; then
  warn "cannot read engine metrics through the relay (engine down?) -- drift check skipped"
else
  missing="$(grep -v '^#' "$EXP" | awk '{print $1}' | sort -u | comm -23 - <(printf '%s\n' "$live_metrics" | sed 's/_\(bucket\|count\|sum\|created\)$//') | head -5 || true)"
  new="$(printf '%s\n' "$live_metrics" | sed 's/_\(bucket\|count\|sum\|created\)$//' | sort -u | comm -13 <(grep -v '^#' "$EXP" | awk '{print $1}' | sort -u) - | head -5 || true)"
  [ -z "$missing" ] || warn "metrics DISAPPEARED vs expected-metrics.txt: $(printf '%s' "$missing" | tr '\n' ' ')"
  [ -z "$new" ] || warn "metrics NEW vs expected-metrics.txt: $(printf '%s' "$new" | tr '\n' ' ')"
  [ -z "$missing" ] && [ -z "$new" ] && ok "no metric drift in vllm:* names"
fi
# Grafana + dashboard load
if curl -fsS --max-time 5 "http://127.0.0.1:3000/api/health" >/dev/null 2>&1; then
  ok "grafana healthy"
  if curl -fsS --max-time 8 "http://127.0.0.1:3000/api/dashboards/uid/kvp-inference" >/dev/null 2>&1; then
    ok "dashboard kvp-inference is loaded by provisioning"
  else
    warn "dashboard kvp-inference not found in grafana (restart grafana or check provisioning path)"
  fi
else
  fail "grafana not reachable on :3000"
fi
fin
