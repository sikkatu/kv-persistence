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
  case "$line" in ''|'#'*) return 1 ;; esac
  line="${line#export }"
  _name="${line%%=*}"
  [ "$_name" = "$line" ] && return 1
  _name="$(printf '%s' "$_name" | tr -d ' ')"
  [ -z "$_name" ] && return 1
  _val="${line#*=}"
  _val="${_val%% #*}"
  _val="${_val%\"${_val##*[![:space:]]}\"}"
  _val="${_val//\"/}"
  case "$_val" in *'$HOME'*|*'${HOME}'*) _val="${_val//\$HOME/$HOME}"; _val="${_val//\$\{HOME\}/$HOME}" ;; esac
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

_kvp_resolve_l2_serde(){
  # Fold the operator switch USE_LOSSLESS_SERDE into the effective
  # LMCACHE_L2_SERDE and EXPORT the result, so EVERY consumer (start-cache.sh,
  # run-vllm-with-persist.sh, verify-engine-identity.sh and the Python identity
  # loader) computes the SAME cache-identity namespace. Without this a
  # `USE_LOSSLESS_SERDE=1` launch would store zstd chunks while the identity
  # gate still hashed the namespace as "none" -- two deployments sharing a
  # cache root would then silently cross-read each other's data.
  #
  # Accepted USE_LOSSLESS_SERDE values:
  #   1 / yes / true / zstd -> zstd
  #   lz4                   -> lz4
  #   0 / no / false / none / "" -> keep the configured LMCACHE_L2_SERDE
  case "${USE_LOSSLESS_SERDE:-}" in
    1|yes|true|zstd) LMCACHE_L2_SERDE=zstd ;;
    lz4)             LMCACHE_L2_SERDE=lz4 ;;
    0|no|false|none|"") : ;;
    *)
      echo "ERROR: USE_LOSSLESS_SERDE=${USE_LOSSLESS_SERDE} not understood (use 1 / zstd / lz4 / 0)" >&2
      return 1 ;;
  esac
  LMCACHE_L2_SERDE="$(printf '%s' "${LMCACHE_L2_SERDE:-none}" | tr '[:upper:]' '[:lower:]')"
  export LMCACHE_L2_SERDE
  return 0
}

_kvp_load_env(){ # _kvp_load_env <repo root>
  local root="$1"
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
  # Fold USE_LOSSLESS_SERDE into the exported LMCACHE_L2_SERDE so the
  # cache-identity namespace (kvpersist.identity) matches what the server
  # actually stores. Failure is fatal: a mismatched namespace would let one
  # codec's chunks be read back under another.
  _kvp_resolve_l2_serde || return 1
}