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
# MODEL_DIR is resolved EARLY: the shared effective-identity resolver below
# needs it to resolve KV_CACHE_DTYPE=auto via config.json.
MODEL_DIR="${MODEL_DIR:-${KVP_MODEL_DIR:-}}"
# Review fix (round 23): fold the EFFECTIVE engine config into the KVP_* env
# names BEFORE the merge's env-wins pass, using the SAME shared resolver the
# namespace derivation uses (kvpersist.cli effective-identity). The recipe
# launchers configure the engine with operator names (KV_CACHE_DTYPE,
# MAX_MODEL_LEN, TP_SIZE, SERVED_MODEL_NAME) and derive the namespace from
# them -- without this folding a non-default recipe run (fp8, 524288, ...)
# verified its namespace against stale identity.env pins and was blocked by
# MISMATCH. Explicit KVP_* in the operator's environment still wins (the
# env-wins loop below checks the environment first).
_eff_env=""
if [ -f "$ROOT/implementation/kvpersist/cli.py" ]; then
  _eff_env="$(PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m kvpersist.cli effective-identity ${MODEL_DIR:+--model-dir "$MODEL_DIR"} 2>/dev/null || true)"
fi
if [ -z "$_eff_env" ]; then
  # Fallback: kvpersist is not importable from here (recipe-only checkout).
  # Same resolution rules: auto -> model dtype (torch_dtype, then dtype);
  # moe_router_dtype is deliberately NOT a fallback (router dtype != KV dtype).
  _eff_env="$(python3 - "${MODEL_DIR:-}" <<'PYEFF'
import json, os, sys
def pick(*names, default=""):
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default
kv_dtype = pick("KVP_KV_CACHE_DTYPE", "KV_CACHE_DTYPE", default="auto").lower()
model_dir = sys.argv[1] if len(sys.argv) > 1 else ""
if kv_dtype == "auto" and model_dir:
    try:
        cfg = json.load(open(os.path.join(model_dir, "config.json")))
    except Exception:
        cfg = {}
    for key in ("torch_dtype", "dtype"):
        v = cfg.get(key)
        if isinstance(v, str) and v.strip():
            kv_dtype = v.strip().lower()
            break
print("KVP_SERVED_MODEL_NAME=%s" % pick("KVP_SERVED_MODEL_NAME", "SERVED_MODEL_NAME", default="mimo-pro"))
print("KVP_KV_CACHE_DTYPE=%s" % kv_dtype)
print("KVP_MAX_MODEL_LEN=%s" % pick("KVP_MAX_MODEL_LEN", "MAX_MODEL_LEN", default="1048576"))
# Review fix (round 26): bare BLOCK_SIZE deliberately NOT read (clashes with
# unrelated environments; KV_PERSIST_BLOCK_SIZE is the recipe knob).
print("KVP_BLOCK_SIZE=%s" % pick("KVP_BLOCK_SIZE", "KV_PERSIST_BLOCK_SIZE", default="16"))
print("KVP_TP_SIZE=%s" % pick("KVP_TP_SIZE", "TP_SIZE", default="4"))
PYEFF
)"
fi
# Review fix (round 37, HIGH): fold the effective value into the KVP_* name
# ONLY when the operator actually set a source name -- otherwise the resolver's
# synthesized default (TP_SIZE=4, KV_CACHE_DTYPE=auto->model dtype, ...) would
# override the identity PIN in identity.env(.local) via the env-wins pass below
# and a bare gate run reported a false MISMATCH (pin KVP_TP_SIZE=2 -> 4).
# A recipe launch sets the operator names, so its effective values still fold
# in (that is the round-23 intent); a bare run keeps the pins.
_has_src() { # _has_src <KVP_NAME> <OP_NAME>...
  [ -n "${!1:-}" ] && return 0
  shift
  local _n
  for _n in "$@"; do
    [ -n "${!_n:-}" ] && return 0
  done
  return 1
}
while IFS='=' read -r _eff_k _eff_v; do
  [ -n "$_eff_v" ] || continue
  case "$_eff_k" in
    KVP_SERVED_MODEL_NAME) _has_src KVP_SERVED_MODEL_NAME SERVED_MODEL_NAME || continue ;;
    KVP_KV_CACHE_DTYPE)    _has_src KVP_KV_CACHE_DTYPE KV_CACHE_DTYPE        || continue ;;
    KVP_MAX_MODEL_LEN)     _has_src KVP_MAX_MODEL_LEN MAX_MODEL_LEN          || continue ;;
    KVP_BLOCK_SIZE)        _has_src KVP_BLOCK_SIZE KV_PERSIST_BLOCK_SIZE BLOCK_SIZE || continue ;;
    KVP_TP_SIZE)           _has_src KVP_TP_SIZE TP_SIZE                      || continue ;;
    *) continue ;;
  esac
  export "$_eff_k=$_eff_v"
done <<EOF
$_eff_env
EOF
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
# Review fix (round 26): exported KVP_* names must enter `vals` even when the
# identity files do not define them (KVP_L2_SERDE is optional and ships
# absent) -- otherwise an explicit override is silently dropped.
for k in ("KVP_L2_SERDE", "KVP_ROPE_CONFIG_DIGEST", "KVP_LORA_ADAPTERS",
          "KVP_MULTIMODAL_COMPONENTS", "KVP_SPECULATIVE_CONFIG"):
    if k in os.environ and k not in vals:
        vals[k] = os.environ[k].strip()
with open(out_path, "w") as fh:
    for k, v in vals.items():
        fh.write(f"{k}={v}\n")
PY

python3 - "$ROOT" "$CONFIG" "$CONTAINER" "$MODEL_DIR" "$CHECK_CONFIG_ONLY" <<'PYEOF'
import json, os, re, subprocess, sys
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
    ("KVP_L2_SERDE", None, None),
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
    # Review fix (round 38): UNAVAILABLE (no MODEL_DIR / config.json) is a
    # MISSING source, not a value -- comparing "unavailable" against the pin
    # produced a false MISMATCH (exit 4) where the verdict must be
    # INCONCLUSIVE (exit 5): we cannot prove the dtype either way.
    if resolved in ("MISSING", "UNAVAILABLE"):
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
        # Review fix (round 46): in --check-config-only mode the engine log
        # does not exist yet -- the INCONCLUSIVE line only scares the
        # operator. Stay quiet there.
        if not check_config_only:
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
# Review fix (round 26): check the L2 codec (serde). It lives in the shell
# that started the cache server -- if the recipe shell has USE_LOSSLESS_SERDE=1
# and the server shell does not (or vice versa), the namespaces diverge and
# chunks written with one codec are read back with another. The effective
# value is resolved by env.sh / the shared resolver; compare against the pin.
# Review fix (round 27): the codec check must verify what the SERVER is
# actually using, NOT the gate's own shell env. start-cache.sh / the systemd
# wrapper write the effective codec into <cache-root>/l2-adapter.rendered.json
# (the "serde" sub-dict: "type": "kvp-<codec>"; absent = none/raw). Read it
# from there; fall back to the shell env only when the file is unavailable
# (pre-launch check), and report INCONCLUSIVE rather than guessing.
def _codec_from_adapter() -> str:
    cache_root = os.environ.get("KVP_CACHE_ROOT", "").strip()
    # Review fix (round 28/29): the recipe's manual-gate run may not export
    # KVP_CACHE_ROOT -- read it from the repo config files as a fallback
    # (.local over base, same layer order as env.sh). Round 29: parse through
    # the ONE shared parser (kvpersist.config.read_env_file), which handles
    # `export`, trailing comments, quotes and $HOME expansion exactly like
    # env.sh -- the hand-rolled regex left "$HOME/kv-cache" unexpanded and the
    # gate then failed with a misleading INCONCLUSIVE.
    if not cache_root:
        try:
            from kvpersist.config import read_env_file as _read_env
        except Exception:
            _read_env = None
        if _read_env is not None:
            for _rel in ("configs/generic/cache.env.local",
                         "configs/generic/cache.env"):
                _vals = _read_env(os.path.join(root, _rel))
                _v = (_vals.get("KVP_CACHE_ROOT") or "").strip()
                if _v:
                    cache_root = _v
                    break
    if not cache_root:
        return ""
    p = os.path.join(cache_root, "l2-adapter.rendered.json")
    try:
        spec = json.load(open(p))
    except Exception:
        return ""
    serde = spec.get("serde") or {}
    stype = (serde.get("type") or "").strip().lower()
    if stype.startswith("kvp-"):
        return stype[4:]
    return "none"  # no serde key = raw byte-exact chunks

def _fold_serde(switch: str, configured: str) -> str:
    switch = (switch or "").strip().lower()
    configured = (configured or "").strip().lower() or "none"
    if switch in ("1", "yes", "true", "zstd"):
        return "zstd"
    if switch == "lz4":
        return "lz4"
    return configured if configured not in ("raw", "") else "none"

_l2_missing_adapter = False
_l2_from_server = _codec_from_adapter()
if _l2_from_server:
    _l2_seen = _l2_from_server
else:
    _l2_seen = _fold_serde(os.environ.get("USE_LOSSLESS_SERDE", ""),
                           os.environ.get("LMCACHE_L2_SERDE", ""))
    # post-launch: the server SHOULD have written the adapter; without it we
    # cannot prove the codec -- flag for INCONCLUSIVE (never a silent pass).
    if not check_config_only:
        _l2_missing_adapter = True
# The EXPECTED side is what the NAMESPACE was hashed from: the identity pin
# (KVP_L2_SERDE) or LMCACHE_L2_SERDE from the config, with USE_LOSSLESS_SERDE
# folded in -- exactly like kvpersist.identity.identity_from_env_files. This
# is the documented contract (README: run the launcher and the server with the
# SAME USE_LOSSLESS_SERDE), so the gate verifies that the SERVER'S codec
# (seen, from the rendered adapter) matches the namespace the cache is filed
# under. Empty pin normalizes to "none" ("" / "none" / "raw" share one
# namespace).
# Review fix (round 46): when NEITHER the shell switch NOR the identity pin
# is set, read the EFFECTIVE codec the launcher recorded in
# $KVP_CACHE_ROOT/engine-identity.env. The codec used to live only in the
# launching shell -- a plain `bash scripts/verify-engine-identity.sh` in a
# fresh terminal reported identity=none != engine/config=zstd and refused
# cache reuse. Precedence: USE_LOSSLESS_SERDE switch > KVP_L2_SERDE pin >
# engine-identity.env (written at launch) > LMCACHE_L2_SERDE.
def _read_engine_identity():
    out = {}
    _root = os.environ.get("KVP_CACHE_ROOT", "/var/lib/kv-persistence/l2")
    try:
        with open(os.path.join(_root, "engine-identity.env")) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out
_engine_identity = _read_engine_identity()
_l2_switch = os.environ.get("USE_LOSSLESS_SERDE", "")
_l2_pin = vals.get("KVP_L2_SERDE", "")
# Review fix (round 46b): the file comes BEFORE the LMCACHE_L2_SERDE env.
# scripts/env.sh folds a default "none" into LMCACHE_L2_SERDE even when the
# operator chose nothing, and a caller like test-recovery.sh exports that
# default into the gate -- it used to outrank engine-identity.env and the
# gate reported identity=none != engine/config=zstd despite the recorded
# file. The recorded launch identity is the ground truth of the RUNNING
# engine; LMCACHE_L2_SERDE is only the last fallback.
if not _l2_switch and not _l2_pin:
    _l2_pin = _engine_identity.get("KVP_L2_SERDE", "")
if not _l2_switch and not _l2_pin:
    _l2_pin = os.environ.get("LMCACHE_L2_SERDE", "")
_l2_expect = _fold_serde(_l2_switch, _l2_pin)
engine_checks.append(("l2_serde", _l2_expect, _l2_seen))

bad, inconc = [], []
if _l2_missing_adapter:
    inconc.append("l2_serde: no l2-adapter.rendered.json under KVP_CACHE_ROOT -- "
                  "cannot prove which codec the server actually uses")
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
    # Review fix (round 40): name the concrete fix for the two common causes
    # instead of a generic "fix identity.env" -- an l2_serde mismatch means
    # the SERVER and the LAUNCH ran with different codec switches, and a
    # head_dim/num_kv_heads mismatch means the template geometry does not
    # match the served model.
    _bad_labels = [b.split(":", 1)[0] for b in bad]
    if "l2_serde" in _bad_labels:
        print("HINT (l2_serde): the RUNNING cache server and this launch disagree")
        print("  about the lossless codec. Use the SAME switch on both sides:")
        print("    USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh   # server on zstd")
        print("    USE_LOSSLESS_SERDE=1 bash scripts/run-vllm-with-persist.sh")
        print("  or restart the server WITHOUT the switch (raw/none) and relaunch.")
        print("  (The codec is part of the cache identity -- a change is a safe MISS.)")
    if any(l in _bad_labels for l in ("head_dim", "num_kv_heads")):
        print("HINT (KV geometry): the identity pins do not match the served model.")
        print("  Rerun: bash scripts/setup-identity.sh \"$MODEL_DIR\"")
        print("  (it now fills KVP_NUM_KV_HEADS / KVP_HEAD_DIM from config.json).")
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