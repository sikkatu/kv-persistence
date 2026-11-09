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

# Round 49 (user): `lmcache` lives ONLY in ~/.venvs/lmcache -- every script
# needs it on PATH without a manual `export PATH=$HOME/.venvs/lmcache/bin:$PATH`
# in every terminal. Resolve the venv bin dir (same sudo-aware logic as
# _kvp_python) and prepend it to PATH once.
_kvp_venv_bin(){
  local _home="$HOME" _bin
  if [ -n "${SUDO_UID:-}" ] && [ "${SUDO_UID}" != "0" ]; then
    _home="$(getent passwd "${SUDO_UID}" 2>/dev/null | cut -d: -f6)"
    [ -n "$_home" ] || _home="$HOME"
  elif [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    _home="$(getent passwd "${SUDO_USER}" 2>/dev/null | cut -d: -f6)"
    [ -n "$_home" ] || _home="$HOME"
  fi
  for _bin in "$_home/.venvs/lmcache/bin" "${KVP_LMCACHE_VENV:-}/bin"; do
    if [ -n "$_bin" ] && [ -x "$_bin/lmcache" ]; then
      printf '%s' "$_bin"
      return 0
    fi
  done
  return 1
}
_kvp_venv_bin="$(_kvp_venv_bin || true)"
if [ -n "$_kvp_venv_bin" ]; then
  case ":$PATH:" in
    *":$_kvp_venv_bin:"*) : ;;
    *) PATH="$_kvp_venv_bin:$PATH" ;;
  esac
  export PATH
fi

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
  # Round 48: ONE semantic table shared by env.sh / identity.py /
  # verify-engine-identity.sh._fold_serde. "" (unset) means NO switch ->
  # fall through to configured/default; "0|no|false|none" is an EXPLICIT
  # raw opt-out -> "none".
  case "${USE_LOSSLESS_SERDE:-}" in
    1|yes|true|zstd) _kvp_switch_serde=zstd ;;
    lz4)             _kvp_switch_serde=lz4 ;;
    0|no|false|none) _kvp_switch_serde=none ;;
    "")              _kvp_switch_serde="" ;;   # no switch -> configured/default
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
    # switch present (zstd / lz4 / explicit none)
    LMCACHE_L2_SERDE="$_kvp_switch_serde"
  elif [ -n "${KVP_L2_SERDE:-}" ]; then
    LMCACHE_L2_SERDE="$KVP_L2_SERDE"
  else
    # Round 48 (user): the DEFAULT codec is lossless zstd -- no export
    # needed in every new terminal. Opt out to byte-exact raw explicitly:
    # USE_LOSSLESS_SERDE=0 or LMCACHE_L2_SERDE=none.
    LMCACHE_L2_SERDE="${LMCACHE_L2_SERDE:-zstd}"
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

# Round 51 (user): ONE MODEL_DIR resolver shared by run-vllm-with-persist.sh
# and verify-engine-identity.sh. A standalone gate in a fresh terminal used
# to report model_dir=unset (the resolution chain lived only in the launcher)
# -> num_kv_heads/head_dim UNAVAILABLE -> INCONCLUSIVE -> cache reuse blocked.
# Order: explicit $MODEL_DIR / $KVP_MODEL_DIR > live/model_dir (remembered at
# launch) > KVP_MODEL_DIR pin in identity.env.local > the single model dir
# under ${KVP_MODELS_ROOT:-$HOME/models}. Prints the path (or nothing).
_kvp_resolve_model_dir(){
  local _root="$1" _md="" _mc="${KVP_MODEL_CONFIG:-}" _d _cands=() _models_root
  _md="${MODEL_DIR:-${KVP_MODEL_DIR:-}}"
  if [ -z "$_md" ] && [ -f "$_root/live/model_dir" ]; then
    _md="$(head -n1 "$_root/live/model_dir" | tr -d '[:space:]')"
  fi
  if [ -z "$_md" ] && [ -z "$_mc" ]; then
    for _d in "$_root"/configs/*/; do
      [ -d "$_d" ] || continue
      case "$(basename "$_d")" in generic) continue ;; esac
      [ -f "$_d/identity.env" ] && _cands+=("$(basename "$_d")")
    done
    [ "${#_cands[@]}" -eq 1 ] && _mc="${_cands[0]}"
  fi
  if [ -z "$_md" ] && [ -n "$_mc" ]; then
    for _d in "$_root/configs/$_mc/identity.env.local" "$_root/configs/$_mc/identity.env"; do
      [ -f "$_d" ] || continue
      _md="$(sed -n 's/^KVP_MODEL_DIR=//p' "$_d" | tail -1 | tr -d '[:space:]')"
      [ -n "$_md" ] && break
    done
  fi
  if [ -z "$_md" ]; then
    _models_root="${KVP_MODELS_ROOT:-$HOME/models}"
    _cands=()   # reset: may hold stale config-dir names from the scan above
    for _d in "$_models_root"/*/; do
      [ -f "$_d/config.json" ] && _cands+=("${_d%/}")
    done
    [ "${#_cands[@]}" -eq 1 ] && _md="${_cands[0]}"
  fi
  [ -n "$_md" ] && [ -d "$_md" ] && printf '%s' "$_md"
  return 0
}

# Round 55 (user, VM + LUKS): resolve the block device that backs the cache
# root AUTOMATICALLY. The operator may not know (or want to guess) which
# device to pin -- VMs, LUKS-encrypted roots and NVMe partitions all show
# different names (/dev/mapper/cryptroot, /dev/vda1, /dev/nvme0n1p1).
# Order: explicit $KVP_CACHE_DEVICE > live/cache_device (remembered) >
# findmnt SOURCE of $KVP_CACHE_ROOT (resolved through readlink for mapper
# symlinks). Prints the device path or nothing.
_kvp_resolve_cache_device(){
  local _root="${1:-}" _dev=""
  [ -n "$_root" ] || _root="${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}"
  _dev="${KVP_CACHE_DEVICE:-}"
  if [ -z "$_dev" ] && [ -f "${KVP_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/live/cache_device" ]; then
    _dev="$(head -n1 "${KVP_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/live/cache_device" | tr -d '[:space:]')"
  fi
  if [ -z "$_dev" ] && command -v findmnt >/dev/null 2>&1; then
    _dev="$(findmnt -no SOURCE --target "$_root" 2>/dev/null | head -1)"
    # mapper / dm-crypt names show up as /dev/mapper/cryptroot or /dev/dm-0;
    # readlink resolves to the canonical node so lsblk PKNAME works.
    [ -n "$_dev" ] && _dev="$(readlink -f "$_dev" 2>/dev/null || echo "$_dev")"
  fi
  # Round 55b: an EXPLICIT pin (env var or live/cache_device) is printed as-is
  # -- it is the operator's choice even if the node check would fail (e.g.
  # LUKS mapper names appear only after unlock). Only auto-detected values
  # must exist on the filesystem.
  if [ -n "${KVP_CACHE_DEVICE:-}" ] || [ -f "${KVP_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/live/cache_device" ]; then
    [ -n "$_dev" ] && printf '%s' "$_dev" && return 0
  fi
  [ -n "$_dev" ] && [ -e "$_dev" ] && printf '%s' "$_dev"
  return 0
}

# Round 56 (user): ONE API key for ALL launchers (kv-persistence + recipes).
# Cross-repo compatibility pin (round 63): kv-persistence and the recipes
# repo are developed together; this number marks the compatible pair and the
# cross-repo contract test asserts BOTH sides carry the same value.
KVP_COMPAT_VERSION=1

# Canonical path: $HOME/.config/kvp/api_key. Legacy per-repo live/api_key is
# migrated on first use. The key is generated once and reused.
_kvp_api_key_file(){
  printf '%s' "${KVP_API_KEY_FILE:-$HOME/.config/kvp/api_key}"
}
# _kvp_gen_key -- generate a 48-hex API key (the one generator, round 71:
# both engine paths must produce keys the same way). Tries openssl, python3,
# /dev/urandom in that order; prints the key on stdout.
_kvp_gen_key(){
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(24))'
  else
    head -c 24 /dev/urandom | od -An -tx1 | tr -d '[:space:]'
  fi
}

# _kvp_ensure_api_key [<legacy-dir>] -- the key resolution/generation contract
# shared by BOTH engine paths (round 71, review probe C): read the canonical
# key, else generate ONE for a loopback bind and save it (canonical + legacy
# mirror). Prints the key. Returns 1 with a message on stderr when a
# non-loopback bind has no key (fail-safe: a public server is always
# authenticated -- never auto-generate for it).
_kvp_ensure_api_key(){
  _kvp_ek_bind="${BIND:-127.0.0.1}"
  _kvp_ek_key="$(_kvp_load_api_key "$1")"
  if [ -n "$_kvp_ek_key" ]; then
    printf '%s' "$_kvp_ek_key"
    return 0
  fi
  case "$_kvp_ek_bind" in
    127.0.0.1|\[::1\]|::1)
      _kvp_ek_key="$(_kvp_gen_key)"
      case "$_kvp_ek_key" in
        *[!0-9a-f]*|'')
          echo "ERROR: failed to generate an API key (openssl/python3/urandom all unavailable)" >&2
          return 1 ;;
      esac
      [ "${#_kvp_ek_key}" -ge 32 ] || {
        echo "ERROR: generated API key is too short (${#_kvp_ek_key} chars)" >&2
        return 1; }
      _kvp_save_api_key "$_kvp_ek_key" "$1"
      echo "API key auto-generated for the loopback server and saved to $(_kvp_api_key_file)" >&2
      printf '%s' "$_kvp_ek_key"
      return 0 ;;
    *)
      echo "ERROR: API_KEY required for a non-loopback bind ($_kvp_ek_bind) -- that is a public unauthenticated API.
Set API_KEY explicitly (openssl rand -hex 24) or bind loopback (BIND=127.0.0.1)." >&2
      return 1 ;;
  esac
}

# The hot-expert budget default -- ONE place (round 71, review probe A): both
# engine paths must plan the SAME hot bank (30 GiB per rank, the recipe's
# documented value). An EMPTY HOTSPLIT_HOT_GIB with full UVA offload plans an
# EMPTY hot set -- the split becomes a silent no-op.
_kvp_hotsplit_hot_gib_default(){
  printf '%s' "${HOTSPLIT_HOT_GIB:-30}"
}

_kvp_load_api_key(){ # prints the key (empty when none exists yet)
  _kvp_kf="$(_kvp_api_key_file)"
  if [ -f "$_kvp_kf" ]; then
    head -n1 "$_kvp_kf" | tr -d '[:space:]'
    return 0
  fi
  # legacy locations -- migrate: write the first legacy key to the canonical
  # file (round 63: the promise "migrate below" was never implemented, so two
  # repos with their own old keys kept using DIFFERENT keys).
  local _root="${1:-}" _cand _found=""
  for _cand in "$_root/live/api_key" "$HOME/kv-persistence/live/api_key"; do
    [ -n "$_cand" ] && [ -f "$_cand" ] || continue
    _found="$(head -n1 "$_cand" | tr -d '[:space:]')"
    break
  done
  if [ -n "$_found" ]; then
    mkdir -p "$(dirname "$_kvp_kf")"
    umask 077
    printf '%s\n' "$_found" > "$_kvp_kf"
    chmod 600 "$_kvp_kf"
    echo "API key migrated to the shared location: $_kvp_kf" >&2
    printf '%s' "$_found"
  fi
  return 0
}
_kvp_save_api_key(){ # _kvp_save_api_key <key> [<legacy-dir>]
  _kvp_kf="$(_kvp_api_key_file)"
  mkdir -p "$(dirname "$_kvp_kf")"
  printf '%s\n' "$1" > "$_kvp_kf"
  chmod 600 "$_kvp_kf"
  # keep a copy in the legacy location for older tooling
  if [ -n "${2:-}" ]; then
    mkdir -p "$2"
    printf '%s\n' "$1" > "$2/api_key"
    chmod 600 "$2/api_key"
  fi
}

# _kvp_seed_env_file <file> -- seed shell variables from an env-style file
# (tp4.env contract, round 64): values already in the environment WIN; the
# file only fills names that are not set. Exact semantics of the recipe's
# parser (run-tp4-hotsplit.sh):
#   * blank lines and comment lines (including INDENTED comments) are skipped;
#   * "export X=1" is accepted (the prefix is stripped);
#   * inline comments are cut only at " #" (a '#' inside a value is kept);
#   * trailing whitespace and surrounding double quotes are stripped;
#   * $HOME / ${HOME} are expanded;
#   * only VALID shell identifiers are exported -- anything else is skipped
#     silently (an indented comment like "  # X=1" must NOT abort the caller).
# kvp_curl [curl args...] -- curl with the API key sent as a stdin config.
# Round 65 (review): NO key file at all -- `printf` is a bash builtin (invisible
# in `ps`), and the key travels on the pipe, not on a command line or in a
# world-readable file. Without a key the request goes out unauthenticated (the
# documented ALLOW_NO_AUTH loopback mode).
kvp_curl(){
  if [ -n "${API_KEY:-}" ]; then
    # Round 66 (review): a curl config file parses " ..." quoted values --
    # an unescaped " or \ inside the key was silently mangled (abc"def -> abc).
    # Escape both so ANY key bytes reach the server intact.
    _kvp_esc="${API_KEY//\\/\\\\}"
    _kvp_esc="${_kvp_esc//\"/\\\"}"
    printf 'header = "Authorization: Bearer %s"\n' "$_kvp_esc" | curl -K - "$@"
  else
    curl "$@"
  fi
}

_kvp_seed_env_file(){
  _kvp_sf="$1"
  [ -f "$_kvp_sf" ] || return 0
  while IFS= read -r _kvp_line || [ -n "$_kvp_line" ]; do
    case "$_kvp_line" in
      ''|[[:space:]]*'#'*) continue ;;          # blanks + comment lines
    esac
    _kvp_line="${_kvp_line#export }"
    _kvp_name="${_kvp_line%%=*}"
    [ "$_kvp_name" = "$_kvp_line" ] && continue # not an assignment
    _kvp_name="$(printf '%s' "$_kvp_name" | tr -d ' \t')"
    case "$_kvp_name" in
      ''|'#'*) continue ;;
    esac
    printf '%s' "$_kvp_name" | grep -Eq '^[A-Za-z_][A-Za-z0-9_]*$' || continue
    if [ -z "${!_kvp_name:-}" ]; then
      _kvp_val="${_kvp_line#*=}"
      _kvp_val="${_kvp_val%% #*}"                       # inline comment at " #"
      _kvp_val="${_kvp_val#"${_kvp_val%%[![:space:]]*}"}"  # leading whitespace
      _kvp_val="${_kvp_val%"${_kvp_val##*[![:space:]]}"}"  # trailing whitespace
      _kvp_val="${_kvp_val//\"/}"                       # surrounding double quotes
      case "$_kvp_val" in
        *'$HOME'*|*'${HOME}'*)
          _kvp_val="${_kvp_val//\$HOME/$HOME}"
          _kvp_val="${_kvp_val//\$\{HOME\}/$HOME}" ;;
      esac
      export "$_kvp_name=$_kvp_val"
    fi
  done < "$_kvp_sf"
  return 0
}

# _kvp_pre_flights <kv-repo-root> <host> <port> <cache-root> <image> <uid> <gid> <same-uid> <launch-mode>
# Round 77 (review): the mp_ping RPC probe and the CUDA IPC boundary test were
# wired ONLY into the kv launcher -- after the default switches to
# KVP_ENGINE=recipe they would silently disappear (the round-76 config-check
# defect all over again). One shared function, called from BOTH engine paths.
# Fails (return 1) on a dead RPC channel or a broken IPC boundary, with the
# honest fix text: the boundary is between the cache server and the container;
# run the server in the container (KVP_LAUNCH_MODE=docker) or as the engine UID.
_kvp_pre_flights(){
  local _root="$1" _host="$2" _port="$3" _croot="$4" _image="$5"
  local _uid="${6:-}" _gid="${7:-}" _same="${8:-0}" _mode="${9:-host}"
  local _ping_out _ping_rc=0 _ipc_mode _ipc_pidf _ipc_pid _ipc_uid _ipc_pu
  # (a) lmcache RPC probe -- an open TCP port does not prove the RequestClient
  # protocol register_kv_caches rides. Exit 3 = lmcache not importable (honest
  # skip: the system python lacks it; use the venv python for a real probe).
  if [ -d "$_root/implementation" ]; then
    _ping_out="$(PYTHONPATH="$_root/implementation" python3 -m kvpersist.mp_ping "$_host" "$_port" 10 2>&1)" || _ping_rc=$?
    case "$_ping_rc" in
      0)
        echo "  $_ping_out" ;;
      3)
        echo "  $_ping_out"
        echo "  NOTE: lmcache not importable on the host python -- protocol ping skipped" ;;
      *)
        echo "ERROR: the cache server did not answer the RPC ping (register_kv_caches would hang):" >&2
        echo "  $_ping_out" >&2
        echo "  The server accepted TCP on $_host:$_port but its RPC channel is dead." >&2
        echo "  Fix: restart the cache service (bash scripts/stop-cache.sh && bash scripts/start-cache.sh)" >&2
        echo "       and check its log: tail -40 ${_croot}/lmcache-server.log" >&2
        return 1 ;;
    esac
  fi
  # (b) CUDA IPC boundary -- the EXACT step that killed register_kv_cache
  # (CUDA_ERROR_MAP_FAILED from cuIpcOpenEventHandle). The consumer must match
  # the SERVER PROCESS user; a root server needs the host-root consumer.
  if [ "${KVP_SKIP_IPC_TEST:-0}" = "1" ]; then
    echo "pre-flight: CUDA IPC test SKIPPED (KVP_SKIP_IPC_TEST=1)"
    return 0
  fi
  if [ ! -x "$_root/scripts/ipc-test.sh" ]; then
    echo "NOTE: scripts/ipc-test.sh not found -- CUDA IPC boundary test skipped"
    return 0
  fi
  _ipc_mode=host
  _ipc_pidf="$_croot/lmcache-server.pid"
  if [ -f "$_ipc_pidf" ]; then
    _ipc_pid="$(tr -d '[:space:]' < "$_ipc_pidf")"
    _ipc_uid="$(awk '/^Uid:/{print $2}' "/proc/$_ipc_pid/status" 2>/dev/null || echo '')"
    if [ "$_ipc_uid" = "0" ]; then _ipc_mode=host-root; fi
  fi
  if [ "$_mode" = "docker" ]; then _ipc_mode=container; fi
  echo "pre-flight: CUDA IPC boundary test (consumer=$_ipc_mode) -- can the cache"
  echo "            server open GPU memory created in the container? (~1 min)"
  _ipc_pu=""
  if [ "$_same" = "1" ] && [ -n "$_uid" ]; then _ipc_pu="$_uid:$_gid"; fi
  if IMAGE="$_image" CONSUMER="$_ipc_mode" PRODUCER_USER="$_ipc_pu" bash "$_root/scripts/ipc-test.sh"; then
    echo "pre-flight: CUDA IPC boundary OK (consumer=$_ipc_mode${_ipc_pu:+, producer UID=$_ipc_pu})"
  else
    echo "ERROR: CUDA IPC BOUNDARY FAILED (pre-flight): the cache server cannot open" >&2
    echo "GPU memory created in the container -- this is the EXACT step that killed" >&2
    echo "register_kv_cache (CUDA_ERROR_MAP_FAILED from cuIpcOpenEventHandle)." >&2
    echo "The model load would waste ~15 min and fail the same way. Fix the boundary FIRST:" >&2
    echo "  - run the cache server in the container (same environment as the engine):" >&2
    echo "      KVP_LAUNCH_MODE=docker USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh" >&2
    echo "    then rerun with KVP_LAUNCH_MODE=docker so this test uses the container consumer;" >&2
    echo "  - or run the cache server as the engine UID (KVP_SAME_UID=1), so both" >&2
    echo "    sides of the IPC boundary share one uid." >&2
    echo "Isolate the blocker: CONSUMER=host-root vs CONSUMER=container in ipc-test.sh." >&2
    echo "NOTE: a pre-flight catches a dead channel at start -- it cannot catch a" >&2
    echo "server that dies MID-load; the launcher still fail-fasts on server errors." >&2
    echo "To bypass this check: KVP_SKIP_IPC_TEST=1 (NOT recommended -- the failure returns)." >&2
    return 1
  fi
  return 0
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