"""Part B round 87 (sub-commit 4b): behavioural tests for the
kvp-inference dashboard -- spec 7.5.

Not substring grep: the REAL JSON is loaded and every contract point is
enforced, and the SAME validator is executed against MUTATED copies of the
dashboard (the mutation must make the run FAIL -- proof the checker works):

  M1 insert a raw `vllm:num_requests_running` into a panel expr
  M2 insert an unknown `kvp:this_rule_missing` into a panel expr
  M3 point a panel datasource at a name-based (non-uid) reference
  M4 use a plugin panel type (e.g. `singlestat`)
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path

import pytest

KV = Path(__file__).resolve().parents[2]
DASH = KV / "observability" / "extras" / "grafana" / "dashboards" / "kvp-inference.json"
RULES = KV / "observability" / "extras" / "rules.d"

VALIDATOR = r'''
import json, pathlib, re, sys
d = json.loads(pathlib.Path(sys.argv[1]).read_text())
rd = pathlib.Path(sys.argv[2])
bad = []
if d.get("uid") != "kvp-inference":
    bad.append("uid")
if "version" in d or "meta" in d:
    bad.append("version/meta present must be removed (provisioning conflicts)")
if d.get("id") is not None:
    bad.append("id must be null")
rules = set()
for y in rd.glob("*.yml"):
    for m in re.finditer(r"record:\s*(\S+)", y.read_text()):
        rules.add(m.group(1))
ALLOWED = {"row", "stat", "timeseries", "gauge", "bargauge", "table"}
exprs = []
types = []
ds_bad = 0
def walk(x):
    global ds_bad
    if isinstance(x, dict):
        for k, v in x.items():
            if k == "expr" and isinstance(v, str):
                exprs.append(v)
            if k == "datasource" and isinstance(v, dict):
                if v.get("type") != "prometheus" or v.get("uid") != "prometheus":
                    ds_bad += 1
            walk(v)
    elif isinstance(x, list):
        for v in x:
            walk(v)
walk(d)
# panel types: only entries of `panels` lists (top level + nested in rows)
for top in d.get("panels", []):
    types.append(top.get("type"))
    for nested in top.get("panels", []) if isinstance(top.get("panels"), list) else []:
        types.append(nested.get("type"))
if ds_bad:
    bad.append(f"{ds_bad} datasource ref(s) not by prometheus uid")
for t in types:
    if t is not None and t not in ALLOWED:
        bad.append(f"panel type {t} not in the builtin set")
for e in exprs:
    if re.search(r"\bvllm:|\bDCGM_", e):
        bad.append(f"raw engine metric in expr: {e[:60]}")
    for name in re.findall(r"\b(kvp:[A-Za-z0-9_:]+)", e):
        if name not in rules:
            bad.append(f"undefined rule {name} in expr: {e[:60]}")
print("BAD=" + ";".join(bad) if bad else "OK")
'''


def check(dash_obj) -> tuple[bool, str]:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(dash_obj, f)
        p = f.name
    r = subprocess.run(["python3", "-", p, str(RULES)],
                       input=VALIDATOR, capture_output=True, text=True, timeout=30)
    out = (r.stdout + r.stderr).strip()
    return "OK" == out.splitlines()[-1] if out else False, out


@pytest.fixture()
def dash():
    assert DASH.is_file(), "kvp-inference.json is missing (sub-commit 4b)"
    return json.loads(DASH.read_text())


def test_dashboard_contract_is_clean(dash):
    ok, out = check(dash)
    assert ok, f"contract violation: {out}"


def _first_expr_target(obj):
    """Return (panel_dict, list_of_its_targets) of the first panel that has
    at least one non-`up{...}` expr so mutations target real rule names."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            if "targets" in x and isinstance(x["targets"], list):
                for t in x["targets"]:
                    e = t.get("expr", "")
                    if e and not e.startswith("up{") and not e.startswith("time()"):
                        found.append((x, x["targets"]))
                        return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    assert found, "no panel with a rule-based expr found"
    return found[0]


# ----------------------------- mutations ----------------------------------
def test_mutation_raw_vllm_metric_is_rejected(dash):
    """MUTATION M1: raw `vllm:num_requests_running` pasted into an expr --
    the validator must reject it (the dashboard depends only on kvp: rules)."""
    d2 = copy.deepcopy(dash)
    panel, targets = _first_expr_target(d2)
    targets[0]["expr"] = "vllm:num_requests_running"
    ok, out = check(d2)
    assert not ok, "a raw vllm:* metric in an expr must FAIL the contract"
    assert "raw engine metric" in out


def test_mutation_unknown_rule_is_rejected(dash):
    """MUTATION M2: an undefined `kvp:` rule name must FAIL."""
    d2 = copy.deepcopy(dash)
    panel, targets = _first_expr_target(d2)
    targets[0]["expr"] = "kvp:this_rule_does_not_exist"
    ok, out = check(d2)
    assert not ok, "an undefined kvp: rule must FAIL the contract"
    assert "undefined rule" in out


def test_mutation_name_based_datasource_is_rejected(dash):
    """MUTATION M3: datasource referenced by TEXT name (no uid) must FAIL."""
    d2 = copy.deepcopy(dash)
    panel, targets = _first_expr_target(d2)
    targets[0]["datasource"] = {"type": "prometheus", "uid": None, "name": "Prometheus"}
    ok, out = check(d2)
    assert not ok, "datasource must be referenced by uid 'prometheus'"
    assert "datasource" in out


def test_mutation_plugin_panel_type_is_rejected(dash):
    """MUTATION M4: a removed/plugin panel type must FAIL."""
    d2 = copy.deepcopy(dash)
    panel, targets = _first_expr_target(d2)
    panel["type"] = "singlestat"
    ok, out = check(d2)
    assert not ok, "non-builtin panel type must FAIL the contract"
    assert "panel type" in out