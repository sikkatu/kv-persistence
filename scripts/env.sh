#!/usr/bin/env bash
# env.sh -- ONE config-loading implementation for ALL kv-persistence scripts.
#
# Precedence (fixes the review bug where cache.env.local edits were silently
# ignored by preflight/test-recovery while start-cache honoured them):
#
#   1. already-exported environment   (always wins -- documented contract)
#   2. <file>.local                   (site override, e.g. cache.env.local)
#   3. <file>                         (shipped defaults)
#
# Overlay order across files: cache.env -> lmcache-server.env -> identity.env,
# with each file's .local applied over its base. Every script MUST use
# _kvp_load_env instead of plain `source`: `source` lets a file overwrite an
# already-exported variable, which is exactly how the bug happened.
#
# Usage in a script:
#   HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   ROOT="$(cd "$HERE/.." && pwd)"
#   . "$HERE/env.sh"
#   _kvp_load_env "$ROOT"

_kvp_parse_line(){ # sets _name / _val; returns 1 if not an assignment
  local line="$1"
  # Review fix (round 32): trim the WHOLE line first, exactly like the
  # python parsers (raw.strip() before removeprefix("export ")) --
  # ` export KEY=value` used to be rejected here but accepted by python.
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  case "$line" in ''|'#'*) return 1 ;; esac
  line="${line#export }"
  _name="${line%%=*}"
  [ "$_name" = "$line" ] && return 1
  # Review fix (round 31): ONE strict format for all parsers. The name is
  # trimmed at the edges (NOT stripped of inner spaces -- `FOO BAR=x` is not
  # an assignment, matching python name.isidentifier()) and must be an ASCII
  # identifier. The old `tr -d ' '` silently turned `FOO BAR=x` into FOOBAR=x.
  _name="${_name#"${_name%%[![:space:]]*}"}"
  _name="${_name%"${_name##*[![:space:]]}"}"
  case "$_name" in
    ''|*[!A-Za-z0-9_]*|[0-9]*) return 1 ;;
  esac
  _val="${line#*=}"
  # Review fix (round 30): cut a trailing comment at a space OR a TAB
  # before '#' -- byte-for-byte the Python parsers (config._parse_env_file,
  # identity._read_env_config), which split at " #" and "\t#". The old
  # ${_val%% #*} only cut at a SPACE, so `KEY=value<TAB># comment` kept the
  # comment here while Python dropped it (the "MUST match" contract).
  _val="${_val%% #*}"
  _val="${_val%%$'\t'#*}"
  # Review fix (round 31): trim the value after the comment cut and BEFORE
  # quote stripping -- byte-for-byte the python order
  # (comment cut -> val.strip() -> quote strip). Without this `KEY= value`
  # kept a leading space here while python stripped it.
  _val="${_val#"${_val%%[![:space:]]*}"}"
  _val="${_val%"${_val##*[![:space:]]}"}"
  # Review fix: strip ONE matching outer quote pair (single or double), exactly
  # like `kvpersist/config.py::_parse_env_file`. The old code only removed
  # double quotes, so `KVP_X='abc'` came out as `'abc'` here but `abc` in
  # Python -- the two loaders disagreed despite the "MUST match" contract.
  if [ "${#_val}" -ge 2 ]; then
    case "$_val" in
      \"*\") _val="${_val#\"}"; _val="${_val%\"}" ;;
      \'*\') _val="${_val#\'}"; _val="${_val%\'}" ;;
    esac
  fi
  # Review fix (round 33): expand ${HOME} BEFORE $HOME -- canonical safe
  # order, same as config._parse_env_file / identity._read_env_config.
  case "$_val" in *'$HOME'*|*'${HOME}'*) _val="${_val//\$\{HOME\}/$HOME}"; _val="${_val//\$HOME/$HOME}" ;; esac
  return 0
}

die(){ echo "ERROR: $*" >&2; exit 1; }

_kvp_seed(){ # _kvp_seed <file> <pre>: export assignments; skip names listed
             # in $pre (newline-delimited, from BEFORE loading began) and any
             # name already exported by a later-priority layer.
  [ -f "$1" ] || return 0
  local pre="$2" line
  while IFS= read -r line || [ -n "$line" ]; do
    _kvp_parse_line "$line" || continue
    case "$pre" in *$'\n'"$_name"$'\n'*) continue ;; esac
    export "$_name=$_val"
  done < "$1"
}

# Review fix (round 46): GPU free-memory pre-flight. A crashed engine or a
# dead cache-server can leave compute processes (and their VRAM) behind, so
# the NEXT engine start fails with a confusing allocation error. Check every
# GPU BEFORE starting the engine; refuse to start when holdouts are found and
# name the PIDs to kill.
# Usage: _kvp_gpu_preflight [threshold_mib]   (default 2048 MiB)
_kvp_gpu_preflight(){
  local _th="${1:-2048}"
  command -v nvidia-smi >/dev/null 2>&1 || return 0
  local _bad=""
  local _line _idx _used
  while IFS=, read -r _idx _used; do
    [ -n "$_idx" ] || continue
    _used="${_used%%.*}"
    if [ "${_used:-0}" -gt "$_th" ]; then
      _bad="$_bad  GPU $_idx: ${_used} MiB busy (threshold ${_th} MiB)\n"
    fi
  done < <(nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits 2>/dev/null)
  if [ -z "$_bad" ]; then
    echo "pre-flight: GPU memory is free (all GPUs below ${_th} MiB)"
    return 0
  fi
  echo "ERROR: GPU memory is NOT free -- a dead process is holding VRAM:" >&2
  printf '%b' "$_bad" >&2
  echo "Compute processes still on the GPUs:" >&2
  nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader 2>&1 | head -10 >&2 || true
  echo "Fix: stop the engine (bash scripts/stop-engine.sh) and RESTART the cache" >&2
  echo "service (bash scripts/stop-cache.sh && bash scripts/start-cache.sh) -- a" >&2
  echo "crashed engine leaves VRAM on the GPU and the cache server holds it too." >&2
  echo "Then rerun. To bypass (not recommended): KVP_SKIP_GPU_CHECK=1." >&2
  return 1
}

_kvp_resolve_l2_serde(){
  # Fold the operator switch USE_LOSSLESS_SERDE into the effective
  # LMCACHE_L2_SERDE and EXPORT the result, so EVERY consumer (start-cache.sh,
  # run-vllm-with-persist.sh, verify-engine-identity.sh and the Python identity
  # loader) computes the SAME cache-identity namespace. Without this a
  # `USE_LOSSLESS_SERDE=1` launch would store zstd chunks while the identity
  # gate still hashed the namespace as "none" -- two deployments sharing a
  # cache root would then silently cross-read each other's data.
  #
  # Precedence (the documented "env wins" contract):
  #   1. LMCACHE_L2_SERDE / KVP_L2_SERDE exported in the ENVIRONMENT
  #      (snapshot taken at the top of _kvp_load_env) -- always wins.
  #   2. USE_LOSSLESS_SERDE (operator shorthand) -- only when (1) is empty.
  #   3. LMCACHE_L2_SERDE from the config files.
  #   4. "none".
  #
  # Accepted USE_LOSSLESS_SERDE values:
  #   1 / yes / true / zstd -> zstd
  #   lz4                   -> lz4
  #   0 / no / false / none / "" -> keep the configured LMCACHE_L2_SERDE
  case "${USE_LOSSLESS_SERDE:-}" in
    1|yes|true|zstd) _kvp_switch_serde=zstd ;;
    lz4)             _kvp_switch_serde=lz4 ;;
    0|no|false|none|"") _kvp_switch_serde="" ;;
    *)
      echo "ERROR: USE_LOSSLESS_SERDE=${USE_LOSSLESS_SERDE} not understood (use 1 / zstd / lz4 / 0)" >&2
      return 1 ;;
  esac
  # Review fix (round 28, item 2): the KVP_L2_SERDE PIN in identity.env(.local)
  # is a real source of the effective codec -- it must reach the namespace,
  # or a zstd pin silently lands under the "none" namespace while the gate
  # accepts it (the reviewer's divergence). Precedence: explicit env export >
  # USE_LOSSLESS_SERDE switch > KVP_L2_SERDE file pin > LMCACHE_L2_SERDE from
  # files > none.
  if [ -n "${_kvp_env_serde:-}" ]; then
    # Rule 1: explicit env export beats the shorthand switch.
    LMCACHE_L2_SERDE="$_kvp_env_serde"
  elif [ -n "$_kvp_switch_serde" ]; then
    LMCACHE_L2_SERDE="$_kvp_switch_serde"
  elif [ -n "${KVP_L2_SERDE:-}" ]; then
    LMCACHE_L2_SERDE="$KVP_L2_SERDE"
  else
    LMCACHE_L2_SERDE="${LMCACHE_L2_SERDE:-none}"
  fi
  LMCACHE_L2_SERDE="$(printf '%s' "$LMCACHE_L2_SERDE" | tr '[:upper:]' '[:lower:]')"
  export LMCACHE_L2_SERDE
  return 0
}

# Round 45f: resolve the lmcache venv python EVEN UNDER SUDO. `sudo` resets
# HOME and PATH, so `python3` would miss ~/.venvs/lmcache -- and the cache
# server must be able to run as root (the ipc-test proved root is required
# to open CUDA IPC memory created by the engine container). Under sudo we
# derive the ORIGINAL user's home from SUDO_USER/SUDO_UID; otherwise $HOME
# is used. Prints the python path; falls back to plain python3.
_kvp_python(){
  local _home="$HOME" _py
  if [ -n "${SUDO_UID:-}" ] && [ "${SUDO_UID}" != "0" ]; then
    _home="$(getent passwd "${SUDO_UID}" 2>/dev/null | cut -d: -f6)"
    [ -n "$_home" ] || _home="$HOME"
  elif [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    _home="$(getent passwd "${SUDO_USER}" 2>/dev/null | cut -d: -f6)"
    [ -n "$_home" ] || _home="$HOME"
  fi
  for _py in "$_home/.venvs/lmcache/bin/python" "$_home/.venvs/lmcache/bin/python3"; do
    if [ -x "$_py" ]; then
      printf '%s' "$_py"
      return 0
    fi
  done
  command -v python3 2>/dev/null || echo python3
}

_kvp_load_env(){ # _kvp_load_env <repo root>
  local root="$1"
  # Remember whether LMCACHE_L2_SERDE / KVP_L2_SERDE were pre-exported in the
  # ENVIRONMENT (precedence rule 1: env wins over files AND over the
  # USE_LOSSLESS_SERDE shorthand). Without this snapshot a
  # `LMCACHE_L2_SERDE=lz4` export would be silently overridden by
  # `USE_LOSSLESS_SERDE=1`, breaking the documented "env wins" contract.
  _kvp_env_serde="${LMCACHE_L2_SERDE-}"
  [ -z "$_kvp_env_serde" ] && _kvp_env_serde="${KVP_L2_SERDE-}"
  # Universal model-config selector (review: no hardcoded model dir).
  # KVP_MODEL_CONFIG=<dirname> picks configs/<dirname>/ identity files.
  # If unset and exactly ONE non-generic config dir has identity.env, that one
  # is used; ambiguous layouts must set KVP_MODEL_CONFIG explicitly.
  local model_cfg="${KVP_MODEL_CONFIG:-}"
  if [ -z "$model_cfg" ]; then
    local candidates=()
    for d in "$root"/configs/*/; do
      [ -d "$d" ] || continue
      case "$(basename "$d")" in generic) continue ;; esac
      [ -f "$d/identity.env" ] && candidates+=("$(basename "$d")")
    done
    case "${#candidates[@]}" in
      0) model_cfg="generic" ;;
      1) model_cfg="${candidates[0]}" ;;
      *) die "_kvp_load_env: multiple model config dirs found (${candidates[*]}) -- set KVP_MODEL_CONFIG explicitly" ;;
    esac
  fi
  # Export for callers (start-cache.sh uses $KVP_MODEL_CONFIG to locate the
  # L2 adapter template in configs/<model>/l2-adapter.json.in).
  KVP_MODEL_CONFIG="$model_cfg"
  # names exported BEFORE loading wins over every file (precedence rule 1)
  local pre=$'\n'"$(compgen -e | sort)"$'\n'
  # base layers first, then .local layers on top -- .local overrides .base for
  # names not pre-exported (precedence rule 2 > 3).
  _kvp_seed "$root/configs/generic/cache.env"                 "$pre"
  _kvp_seed "$root/configs/generic/lmcache-server.env"        "$pre"
  _kvp_seed "$root/configs/$model_cfg/identity.env"           "$pre"
  _kvp_seed "$root/configs/generic/cache.env.local"           "$pre"
  _kvp_seed "$root/configs/generic/lmcache-server.env.local"  "$pre"
  _kvp_seed "$root/configs/$model_cfg/identity.env.local"     "$pre"
  # Review fix (round 26): PROPAGATE the KVP_* duplicates into their LMCACHE_*
  # counterparts so cache.env(.local) edits actually take effect. The server
  # reads only LMCACHE_*; setting KVP_LMCACHE_PORT=6000 or KVP_L1_SIZE_GB=32
  # in cache.env.local used to be silently ignored (server started with the
  # defaults from lmcache-server.env). Precedence: explicit LMCACHE_* env still
  # wins (documented "env wins" contract).
  for _pair in "KVP_LMCACHE_HOST:LMCACHE_HOST" "KVP_LMCACHE_PORT:LMCACHE_PORT" \
               "KVP_LMCACHE_HTTP_PORT:LMCACHE_HTTP_PORT" "KVP_L1_SIZE_GB:LMCACHE_L1_SIZE_GB" \
               "KVP_CHUNK_SIZE:LMCACHE_CHUNK_SIZE" \
               "KVP_EVICT_TRIGGER_WATERMARK:LMCACHE_EVICTION_TRIGGER_WATERMARK" \
               "KVP_EVICT_RATIO:LMCACHE_EVICTION_RATIO"; do
    _kv_name="${_pair%%:*}"; _lm_name="${_pair##*:}"
    # an LMCACHE_* explicitly exported BEFORE the files were loaded wins
    # (the documented "env wins" contract)
    case "$pre" in
      *$'\n'"$_lm_name"$'\n'*) continue ;;
    esac
    # otherwise the KVP_* value is authoritative and overwrites the file value
    # (lmcache-server.env always ships LMCACHE_PORT=5555, so an "only if empty"
    # rule would silently ignore a KVP_LMCACHE_PORT edit)
    if [ -n "${!_kv_name:-}" ]; then
      export "$_lm_name=${!_kv_name}"
    fi
  done
  # Review fix (round 26/27): call config.verify() so conflicting duplicate
  # knobs are reported at startup. Non-fatal. Review fix (round 27): a
  # conflict the PROPAGATION above already resolved in favour of KVP_* is NOT
  # a problem -- suppress those warnings (they are a false alarm: the effective
  # value is correct, the files just carry both names).
  if command -v python3 >/dev/null 2>&1; then
    _kvp_conflicts="$(PYTHONPATH="$root/implementation${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -c 'from kvpersist.config import verify; import sys
resolved = set()
for pair in sys.argv[2:]:
    kv, lm = pair.split(":", 1)
    resolved.add((kv, lm))
msgs = verify(sys.argv[1])
for m in msgs:
    skip = False
    for kv, lm in resolved:
        if kv in m and lm in m:
            skip = True
            break
    if not skip:
        print("CONFIG WARNING: " + m)' "$root" \
      "KVP_LMCACHE_HOST:LMCACHE_HOST" "KVP_LMCACHE_PORT:LMCACHE_PORT" \
      "KVP_LMCACHE_HTTP_PORT:LMCACHE_HTTP_PORT" "KVP_L1_SIZE_GB:LMCACHE_L1_SIZE_GB" \
      "KVP_CHUNK_SIZE:LMCACHE_CHUNK_SIZE" \
      "KVP_EVICT_TRIGGER_WATERMARK:LMCACHE_EVICTION_TRIGGER_WATERMARK" \
      "KVP_EVICT_RATIO:LMCACHE_EVICTION_RATIO" 2>/dev/null || true)"
    [ -n "$_kvp_conflicts" ] && printf '%s\n' "$_kvp_conflicts" >&2
  fi
  # Fold USE_LOSSLESS_SERDE into the exported LMCACHE_L2_SERDE so the
  # cache-identity namespace (kvpersist.identity) matches what the server
  # actually stores. Failure is fatal: a mismatched namespace would let one
  # codec's chunks be read back under another.
  _kvp_resolve_l2_serde || return 1
}