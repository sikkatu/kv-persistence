#!/usr/bin/env bash
# verify-engine-identity.sh -- MANDATORY gate before cache data is stored or reused.
# Exit codes: 0 = verified, 4 = MISMATCH, 5 = INCONCLUSIVE
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CONTAINER="${1:-mimo-pro-tp4-kvp}"
# --check-config-only (review round 13): verify the identity CONFIG alone,
# without docker. Used by the launcher BEFORE the container exists; the
# engine-side (container-args) check runs right after `docker run`.
CHECK_CONFIG_ONLY=0
if [ "$CONTAINER" = "--check-config-only" ]; then
  CHECK_CONFIG_ONLY=1
  CONTAINER=""
fi
# Universal model-config selector + .local overlay (review P1: setup writes to
# identity.env.local; the gate MUST verify the same source, not the stale
# tracked file).
_model_cfg="${KVP_MODEL_CONFIG:-}"
if [ -z "$_model_cfg" ]; then
  _candidates=()
  for _d in "$ROOT"/configs/*/; do
    [ -d "$_d" ] || continue
    case "$(basename "$_d")" in generic) continue ;; esac
    [ -f "$_d/identity.env" ] && _candidates+=("$(basename "$_d")")
  done
  case "${#_candidates[@]}" in
    0) _model_cfg="generic" ;;
    1) _model_cfg="${_candidates[0]}" ;;
    *) echo "ERROR: multiple model config dirs found (${_candidates[*]}) -- set KVP_MODEL_CONFIG" >&2; exit 5 ;;
  esac
fi
# Overlay: .local values (what setup-identity.sh wrote) win over the tracked base.
CONFIG_LIVE="$ROOT/configs/$_model_cfg/identity.env.local"
CONFIG_BASE="$ROOT/configs/$_model_cfg/identity.env"
# Merge base + .local into a temp .env file so downstream Python reads the
# SAME effective values setup-identity.sh wrote (not the stale tracked file).
CONFIG="$(mktemp /tmp/kvp-identity-XXXXXX.env)"
trap 'rm -f "$CONFIG"' EXIT
python3 - "$CONFIG_BASE" "$CONFIG_LIVE" "$CONFIG" <<'PY'
import os, sys
base_path, live_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

# Parsing MUST match scripts/env.sh _kvp_parse_line and
# kvpersist/config.py _parse_env_file (the documented "MUST match" contract):
#   * optional "export " prefix,
#   * trailing comment cut at " #" / tab+# (whitespace + hash),
#   * ONE matching outer quote pair stripped (single or double),
#   * $HOME expansion.
def parse_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    line = line.removeprefix("export ").strip()
    if "=" not in line:
        return None
    name, val = line.split("=", 1)
    name = name.strip()
    if not name:
        return None
    for sep in (" #", "\t#"):
        if sep in val:
            val = val.split(sep, 1)[0]
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    val = os.path.expanduser(val.replace("${HOME}", "~").replace("$HOME", "~"))
    return name, val

vals = {}
for path in (base_path, live_path):
    try:
        for line in open(path):
            kv = parse_line(line)
            if kv:
                vals[kv[0]] = kv[1]
    except FileNotFoundError:
        pass
# Review fix (round 22): the documented precedence is "env wins over files"
# (scripts/env.sh _kvp_load_env, kvpersist.config.load_config and
# identity_from_env_files all honour it). The gate read ONLY the files, so an
# operator override like MAX_MODEL_LEN=524288 or KV_CACHE_DTYPE=fp8 changed
# the NAMESPACE while the gate still compared the container args against the
# stale file pins -> guaranteed MISMATCH. Exported KVP_* vars now override
# the file values here too.
for k in list(vals):
    if k in os.environ:
        vals[k] = os.environ[k].strip()
with open(out_path, "w") as fh:
    for k, v in vals.items():
        fh.write(f"{k}={v}\n")
PY
MODEL_DIR="${MODEL_DIR:-${KVP_MODEL_DIR:-}}"
python3 - "$ROOT" "$CONFIG" "$CONTAINER" "$MODEL_DIR" "$CHECK_CONFIG_ONLY" <<'PYEOF'
import json, re, subprocess, sys
root, config, container, model_dir = sys.argv[1:5]
check_config_only = sys.argv[5] == "1"
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
# ---- config-side checks (always, incl. --check-config-only) -------------
# Review round 13: the gate used to verify only 5 of 13 identity fields
# (max_model_len, tp_size, kv_cache_dtype, num_kv_heads, head_dim). The
# remaining fields are verified here from their authoritative sources:
#   model_revision / tokenizer_revision / chat_template_digest -> the identity
#     config itself must carry well-formed values (setup-identity.sh writes
#     them; this gate refuses empty/malformed ones).
#   block_size -> engine arg --block-size when present, else config.json's
#     `block_size`-related keys, else "auto(default)".
#   connector / connector_version -> engine arg --kv-transfer-config must
#     reference the pinned connector when persistence is attached.
#   model_id / tokenizer_name -> presence + non-empty (provenance fields).
format_checks = []   # (label, expect, found) -- found from config/model only
def require(label, expect, pattern, why):
    v = vals.get(label, "")
    if not v:
        format_checks.append((label, v, "EMPTY (identity must pin %s)" % why))
    elif pattern and not re.match(pattern, v):
        format_checks.append((label, v, "MALFORMED (want %s)" % why))
    else:
        format_checks.append((label, v, v))
require("KVP_MODEL_ID", vals.get("KVP_MODEL_ID", ""), None, "a non-empty model id")
require("KVP_TOKENIZER_NAME", vals.get("KVP_TOKENIZER_NAME", ""), None, "a non-empty tokenizer name")
require("KVP_MODEL_REVISION", vals.get("KVP_MODEL_REVISION", ""), r"^[0-9a-f]{40}$", "a 40-hex revision")
require("KVP_TOKENIZER_REVISION", vals.get("KVP_TOKENIZER_REVISION", ""), r"^[0-9a-f]{40}$", "a 40-hex revision")
require("KVP_CHAT_TEMPLATE_DIGEST", vals.get("KVP_CHAT_TEMPLATE_DIGEST", ""), r"^[0-9a-f]{64}$", "a 64-hex sha256")
require("KVP_CONNECTOR", vals.get("KVP_CONNECTOR", ""), None, "a connector class name")
require("KVP_CONNECTOR_VERSION", vals.get("KVP_CONNECTOR_VERSION", ""), None, "a connector version tag")
# Advanced identity fields (review fix): OPTIONAL -- empty is legal (means
# "not applicable"), but a set value must be well-formed and must be part of
# the cache namespace (kvpersist.identity reads them now).
for _k, _pat, _why in (
    ("KVP_ROPE_CONFIG_DIGEST", r"^[0-9a-f]{64}$", "a 64-hex sha256"),
    ("KVP_LORA_ADAPTERS", None, None),
    ("KVP_MULTIMODAL_COMPONENTS", None, None),
    ("KVP_SPECULATIVE_CONFIG", None, None),
):
    _v = vals.get(_k, "")
    if _v and _pat and not re.match(_pat, _v):
        format_checks.append((_k, _v, "MALFORMED (want %s)" % _why))
    elif _v:
        format_checks.append((_k, _v, _v))
    else:
        format_checks.append((_k, "", "(unset -- not applicable, does not split namespace)"))

engine_checks = []   # (label, expect, found) -- found from engine/config.json
engine_checks.append(("max_model_len", vals.get("KVP_MAX_MODEL_LEN"), arg(cmd, "--max-model-len")))
engine_checks.append(("tp_size", vals.get("KVP_TP_SIZE"), arg(cmd, "--tensor-parallel-size")))
kvd = arg(cmd, "--kv-cache-dtype") if cmd is not None else "UNAVAILABLE"
if kvd == "UNAVAILABLE":
    kvd_seen = "UNAVAILABLE"
elif kvd in ("auto", "MISSING"):
    # Review fix (round 19): `auto` is resolved by the engine to the MODEL's
    # dtype, so the gate must compare the EFFECTIVE dtype, not the string
    # "auto" against the pin. Read the served model's config.json dtype
    # (torch_dtype / dtype) and use that as the resolved value.
    resolved = model_cfg("torch_dtype")
    if resolved == "MISSING":
        resolved = model_cfg("dtype")
    if resolved == "MISSING":
        kvd_seen = "MISSING"
    else:
        kvd_seen = str(resolved).lower()
        print("  [info] kv_cache_dtype 'auto' resolved to the model's %s" % kvd_seen)
else:
    kvd_seen = kvd.lower()
engine_checks.append(("kv_cache_dtype", vals.get("KVP_KV_CACHE_DTYPE"), kvd_seen))
bs = arg(cmd, "--block-size") if cmd is not None else "UNAVAILABLE"
if bs in ("UNAVAILABLE", "MISSING") and model_dir:
    # Review fix (round 20): do NOT guess the resolved block size. When the
    # engine is started without --block-size, the pinned build picks a value at
    # runtime (DEFAULT_BLOCK_SIZE=16, but platforms/interface.py
    # get_preferred_block_size can override it and the engine logs
    # "Setting kv cache block size to N"). Two sources, in order:
    #   1. the model config (`block_size` / `hybrid_block_size`), then
    #   2. the container's engine log line "Setting kv cache block size to N".
    # If neither is available we report INCONCLUSIVE -- the old behaviour of
    # substituting "16" claimed a fact we did not observe.
    bs = "MISSING"
    try:
        cfg = json.load(open(model_dir + "/config.json"))
        b = cfg.get("block_size")
        if b is None:
            b = cfg.get("hybrid_block_size")
        if b is not None:
            bs = str(b)
    except Exception:
        pass
    if bs == "MISSING" and container:
        try:
            log = subprocess.run(
                ["docker", "logs", "--tail", "500", container],
                capture_output=True, text=True, timeout=20,
            )
            m = re.search(r"[Ss]etting kv cache block size to (\d+)", log.stdout)
            if m:
                bs = m.group(1)
                print("  [info] block_size read from the engine log: %s" % bs)
        except Exception:
            pass
    if bs == "MISSING":
        print("  [info] block_size could not be determined (no --block-size arg, "
              "no config.json value, no engine log line) -- reporting INCONCLUSIVE")
engine_checks.append(("block_size", vals.get("KVP_BLOCK_SIZE"), bs))
nh = model_cfg("num_key_value_heads")
hd = model_cfg("head_dim")
if hd == "MISSING":
    hidden = model_cfg("hidden_size")
    nh_all = model_cfg("num_attention_heads")
    hd = hidden // nh_all if isinstance(hidden, int) and isinstance(nh_all, int) else "UNAVAILABLE"
engine_checks.append(("num_kv_heads", vals.get("KVP_NUM_KV_HEADS"), nh))
engine_checks.append(("head_dim", vals.get("KVP_HEAD_DIM"), hd))
# Review fix: the connector check promised by the comment above ("engine arg
# --kv-transfer-config must reference the pinned connector") was never
# implemented -- only the config-side KVP_CONNECTOR/KVP_CONNECTOR_VERSION
# strings were format-checked. Verify the ENGINE ARG actually references the
# pinned connector class when persistence is attached.
kvtc = arg(cmd, "--kv-transfer-config") if cmd is not None else "UNAVAILABLE"
want_conn = vals.get("KVP_CONNECTOR", "")
if kvtc == "UNAVAILABLE":
    connector_seen = "UNAVAILABLE"
elif kvtc == "MISSING":
    # No connector attached at all: fine when persistence is disabled, a
    # MISMATCH when the identity pins a connector.
    connector_seen = "MISSING" if want_conn else "(none, and none pinned)"
else:
    # Review fix (round 22, blocking): on a match, report the PIN (what we
    # compare against), not the whole --kv-transfer-config JSON. The old code
    # put the full JSON into `found`, which then never equaled the short
    # `KVP_CONNECTOR=LMCacheMPConnector` pin -- the post-launch gate ALWAYS
    # returned exit 4 and the launcher refused to start with cache attached.
    connector_seen = (
        want_conn if (want_conn and want_conn in kvtc)
        else f"--kv-transfer-config does not reference {want_conn!r}"
    )
engine_checks.append(("connector", want_conn, connector_seen))

bad, inconc = [], []
for label, expect, found in format_checks:
    if found == expect:
        continue
    # Review fix (blocking bug): an OPTIONAL field left empty is legal ("not
    # applicable"), and the check above reports it with the "(unset ..."
    # marker instead of the raw value. Comparing that marker against the
    # empty expect always mismatched (exit 4 on the standard config).
    # Treat the "(unset" marker as an explicit pass for empty values.
    if found.startswith("(unset"):
        continue
    if found.startswith("EMPTY") or found.startswith("MALFORMED"):
        bad.append(f"{label}: {found} -- identity={expect!r}")
    else:
        bad.append(f"{label}: identity={expect!r} != config={found!r}")
if not check_config_only:
    for label, expect, found in engine_checks:
        if found == "UNAVAILABLE":
            inconc.append(f"{label}: source unavailable (no container/model_dir) -- identity={expect}")
            continue
        if found == "MISSING":
            inconc.append(f"{label}: engine/config has no value for this field -- identity={expect}")
            continue
        if str(found) != str(expect):
            bad.append(f"{label}: identity={expect} != engine/config={found}")
print("engine identity check (container=%s, model_dir=%s, config_only=%s)" % (container or "none", model_dir or "unset", check_config_only))
for label, expect, found in format_checks:
    print("  [config] %s: identity=%s  config=%s" % (label, expect, found))
if not check_config_only:
    for label, expect, found in engine_checks:
        print("  [engine] %s: identity=%s  engine/config=%s" % (label, expect, found))
if bad:
    print("\nENGINE IDENTITY MISMATCH -- cache reuse is FORBIDDEN:")
    for b in bad:
        print("  !!! " + b)
    print("Fix configs/*/identity.env(.local) (scripts/setup-identity.sh), rerun.")
    sys.exit(4)
if inconc:
    print("\nINCONCLUSIVE -- values could not be verified; cache reuse is REFUSED by policy:")
    for u in inconc:
        print("  ??? " + u)
    print("Set MODEL_DIR and/or start the container, then rerun. Passing requires")
    print("verified values, not the absence of errors.")
    sys.exit(5)
if check_config_only:
    print("\nidentity config complete and well-formed (engine-side check runs after launch)")
else:
    print("\nidentity verified against the CONTAINER'S STARTUP ARGUMENTS and the model's config.json")
    print("-- NOT against a live engine introspection. This gate compares what the engine")
    print("WAS STARTED WITH (docker inspect .Config.Cmd) and the served model's config.json,")
    print("not runtime state. Cache reuse allowed within that scope.")
PYEOF