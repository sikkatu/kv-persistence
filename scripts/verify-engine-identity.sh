#!/usr/bin/env bash
# verify-engine-identity.sh -- MANDATORY gate before cache data is stored or reused.
# Exit codes: 0 = verified, 4 = MISMATCH, 5 = INCONCLUSIVE
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CONTAINER="${1:-mimo-pro-tp4-kvp}"
CONFIG="$ROOT/configs/mimo-v2.6-pro/identity.env"
MODEL_DIR="${MODEL_DIR:-${KVP_MODEL_DIR:-}}"
python3 - "$ROOT" "$CONFIG" "$CONTAINER" "$MODEL_DIR" <<'PYEOF'
import json, re, subprocess, sys
root, config, container, model_dir = sys.argv[1:5]
sys.path.insert(0, root + "/implementation")
vals = {}
for line in open(config):
    m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
    if m:
        vals[m.group(1)] = m.group(2).strip()

def docker_args():
    try:
        out = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Cmd}}", container],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            return json.loads(out.stdout.strip() or "[]") or []
    except Exception:
        pass
    return None  # None = could not read container

def arg(cmd, name):
    if cmd is None:
        return "UNAVAILABLE"
    for i, a in enumerate(cmd):
        if a == name and i + 1 < len(cmd):
            return cmd[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return "MISSING"

def model_cfg(field):
    if not model_dir:
        return "UNAVAILABLE"
    try:
        cfg = json.load(open(model_dir + "/config.json"))
        return cfg.get(field, "MISSING")
    except Exception:
        return "UNAVAILABLE"

cmd = docker_args()
checks = []
checks.append(("max_model_len", vals.get("KVP_MAX_MODEL_LEN"), arg(cmd, "--max-model-len")))
checks.append(("tp_size", vals.get("KVP_TP_SIZE"), arg(cmd, "--tensor-parallel-size")))
kvd = arg(cmd, "--kv-cache-dtype") if cmd is not None else "UNAVAILABLE"
if kvd == "UNAVAILABLE":
    kvd_seen = "UNAVAILABLE"
elif kvd in ("auto", "MISSING"):
    kvd_seen = "auto(default)"
else:
    kvd_seen = kvd.lower()
checks.append(("kv_cache_dtype", vals.get("KVP_KV_CACHE_DTYPE"), kvd_seen))
nh = model_cfg("num_key_value_heads")
hd = model_cfg("head_dim")
if hd == "MISSING":
    hidden = model_cfg("hidden_size")
    nh_all = model_cfg("num_attention_heads")
    hd = hidden // nh_all if isinstance(hidden, int) and isinstance(nh_all, int) else "UNAVAILABLE"
checks.append(("num_kv_heads", vals.get("KVP_NUM_KV_HEADS"), nh))
checks.append(("head_dim", vals.get("KVP_HEAD_DIM"), hd))

bad, inconc = [], []
for label, expect, found in checks:
    if found == "UNAVAILABLE":
        inconc.append(f"{label}: source unavailable (no container/model_dir) -- identity={expect}")
        continue
    if found == "MISSING":
        inconc.append(f"{label}: engine/config has no value for this field -- identity={expect}")
        continue
    if str(found) != str(expect):
        bad.append(f"{label}: identity={expect} != engine/config={found}")

print(f"engine identity check (container={container}, model_dir={model_dir or 'unset'})")
for label, expect, found in checks:
    print(f"  {label}: identity={expect}  engine/config={found}")
if bad:
    print("\nENGINE IDENTITY MISMATCH -- cache reuse is FORBIDDEN:")
    for b in bad:
        print("  !!! " + b)
    print("Fix configs/mimo-v2.6-pro/identity.env (scripts/setup-identity.sh), rerun.")
    sys.exit(4)
if inconc:
    print("\nINCONCLUSIVE -- values could not be verified; cache reuse is REFUSED by policy:")
    for u in inconc:
        print("  ??? " + u)
    print("Set MODEL_DIR and/or start the container, then rerun. Passing requires")
    print("verified values, not the absence of errors.")
    sys.exit(5)
print("\nidentity verified against running engine -- cache reuse allowed")
PYEOF