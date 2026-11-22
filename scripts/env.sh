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
  # start-all.sh, verify-engine-identity.sh and the Python identity
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
# HOME and PATH, so `python3` would miss ~/.venvs/lmcache. Round 27 (review):
# root is NOT required for the cache server -- CUDA IPC opens when both sides
# (server and engine) share ONE UID (proven on hardware: 1000+1000 PASS,
# root+1000 FAIL). Under sudo we derive the ORIGINAL user's home from
# SUDO_USER/SUDO_UID; otherwise $HOME is used. Prints the python path; falls
# back to plain python3.
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
# ---------------------------------------------------------------------------
# r20 K6.3: ONE source for the cache-server launch command. Both
# scripts/start-cache.sh (production) and the image-build server smoke
# (scripts/build-image.sh) must run the server with the SAME arguments and
# the SAME rendered L2 adapter -- otherwise the smoke proves nothing about
# the real launch. Never duplicate these arg lists.
# ---------------------------------------------------------------------------
_kvp_render_l2_adapter() { # _kvp_render_l2_adapter <out.json> <repo-root> <cache-root> <budget-gb> <serde> [model-config]
  local _out="$1" _repo="$2" _root="$3" _budget="$4" _serde="$5" _mc="${6:-${KVP_MODEL_CONFIG:-mimo-v2.6-pro}}"
  local _serde_json="" _tpl
  case "$_serde" in
    none|raw|"") _serde_json="" ;;
    zstd|lz4)
      _serde_json="\"serde\": {\"type\": \"kvp-$_serde\", \"level\": \"${KVP_L2_COMPRESS_LEVEL:-3}\", \"align_bytes\": \"4096\"}," ;;
    *)
      echo "ERROR(_kvp_render_l2_adapter): codec '$_serde' is not lossless-supported (none / zstd / lz4)" >&2
      return 1 ;;
  esac
  _tpl="$_repo/configs/${_mc}/l2-adapter.json.in"
  [ -f "$_tpl" ] || { echo "ERROR(_kvp_render_l2_adapter): template not found: $_tpl" >&2; return 1; }
  sed -e "s|__KVP_CACHE_ROOT__|$_root|" \
      -e "s|__KVP_CACHE_BUDGET_GB__|$_budget|" \
      -e "s|__KVP_EVICT_TRIGGER_WATERMARK__|${LMCACHE_EVICTION_TRIGGER_WATERMARK:-${KVP_EVICT_TRIGGER_WATERMARK:-0.9}}|" \
      -e "s|__KVP_EVICT_RATIO__|${LMCACHE_EVICTION_RATIO:-${KVP_EVICT_RATIO:-0.1}}|" \
      -e "s|__KVP_L2_SERDE_JSON__|$_serde_json|" \
      "$_tpl" \
    | python3 -c 'import json,re,sys; s=sys.stdin.read(); s=re.sub(r"^\s*//.*$","",s,flags=re.M); json.loads(s); sys.stdout.write(s)' \
    > "$_out"
}
_kvp_normalize_eviction_policy() { # _kvp_normalize_eviction_policy <value> -> canonical LRU|ARC|IsolatedLRU|noop
  # r22 B1: the pin parser (lmcache/v1/distributed/config.py:984) accepts
  # EXACTLY LRU|ARC|IsolatedLRU|noop (case-sensitive). Normalize before
  # passing to the server; refuse unknown values with all four choices.
  # r23 D6: case-INSENSITIVE match -> the pin's canonical spelling
  # (LRU / ARC / IsolatedLRU / noop).
  local _p="${1:-LRU}"
  case "${_p,,}" in
    lru) printf 'LRU' ;;
    arc) printf 'ARC' ;;
    isolatedlru) printf 'IsolatedLRU' ;;
    noop) printf 'noop' ;;
    *)
      echo "ERROR(_kvp_server_argv): LMCACHE_EVICTION_POLICY=$_p is not a valid policy." >&2
      echo "  Allowed values (case-insensitive): LRU, ARC, IsolatedLRU, noop" >&2
      echo "  Set LMCACHE_EVICTION_POLICY to one of these, or leave it unset (default: LRU)." >&2
      return 1 ;;
  esac
}
_kvp_server_argv_read() {
  # _kvp_server_argv_read <array-name> <host> <port> <l1-gb> <chunk-size> <adapter-json> [http-host]
  #
  # r22 B4 / r23 D4: ONE consumer-side builder+reader of the NUL-separated
  # argv (the three former copies -- smoke, docker and host branches of
  # start-cache.sh -- all call this; the line-splitting defect cannot live in
  # more than one place). The argv is written to a temp file and its exit
  # status is CHECKED (process substitution would swallow a rc=1 from
  # _kvp_server_argv -- e.g. an invalid LMCACHE_EVICTION_POLICY -- and the
  # caller would launch the server with an EMPTY argv).
  # Result: the caller's array holds ("server" ...); empty or a non-"server"
  # first element is a hard error -> return 1 BEFORE any launch.
  # r23 D4 fix: a nameref (`local -n`) -- `mapfile -t "$name"` only WRITES
  # through a name; READING (`${arr[0]}`) needs the nameref, otherwise the
  # validation compares the literal name string and always refuses.
  local -n _kvp_argv_ref="$1"; shift
  local _tmp
  _tmp="$(mktemp /tmp/kvp-server-argv.XXXXXX)" || return 1
  if ! _kvp_server_argv "$@" > "$_tmp"; then
    rm -f "$_tmp"
    echo "ERROR(_kvp_server_argv_read): the server argv could not be built" >&2
    echo "  (see the message above -- e.g. an invalid LMCACHE_EVICTION_POLICY;" >&2
    echo "  allowed values: LRU, ARC, IsolatedLRU, noop)" >&2
    return 1
  fi
  mapfile -d '' -t _kvp_argv_ref < "$_tmp"
  rm -f "$_tmp"
  if [ "${#_kvp_argv_ref[@]}" -eq 0 ] || [ "${_kvp_argv_ref[0]}" != "server" ]; then
    echo "ERROR(_kvp_server_argv_read): the server argv is empty or malformed" >&2
    echo "  (expected argv[0]=server, got '${_kvp_argv_ref[0]:-<empty>}') -- refusing to launch." >&2
    return 1
  fi
  return 0
}
_kvp_server_argv() { # _kvp_server_argv <host> <port> <l1-gb> <chunk-size> <adapter-json> [http-host]
  # Prints the `kvpersist.launch server ...` arguments separated by NUL
  # (r22 B4: the adapter JSON carries spaces AND newlines -- never word-split
  # or line-split it in the caller; read with `mapfile -d '' -t`).
  # docker mode binds 0.0.0.0 INSIDE the container (the -p mapping is the
  # host-side gate); host mode binds the loopback -- pass the right host.
  local _host="$1" _port="$2" _l1="$3" _chunk="$4" _adapter="$5" _http_host="${6:-}"
  local _wm="${LMCACHE_EVICTION_TRIGGER_WATERMARK:-${KVP_EVICT_TRIGGER_WATERMARK:-0.9}}"
  local _ratio="${LMCACHE_EVICTION_RATIO:-${KVP_EVICT_RATIO:-0.1}}"
  local _policy
  _policy="$(_kvp_normalize_eviction_policy "${LMCACHE_EVICTION_POLICY:-LRU}")" || return 1
  # r22 B4: compact the adapter JSON to a single line (the pretty-printed
  # file is kept for inspection; the argv value must be ONE element).
  local _adapter_compact
  _adapter_compact="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])),separators=(",",":")))' "$_adapter")" \
    || { echo "ERROR(_kvp_server_argv): cannot compact adapter JSON: $_adapter" >&2; return 1; }
  printf '%s\0' \
    "server" \
    "--host" "$_host" \
    "--port" "$_port" \
    "--l1-size-gb" "$_l1" \
    "--eviction-policy" "$_policy" \
    "--eviction-trigger-watermark" "$_wm" \
    "--eviction-ratio" "$_ratio" \
    "--chunk-size" "$_chunk" \
    "--l2-adapter" "$_adapter_compact"
  # Optional flags: keep only those THIS build's --help understands.
  [ -n "$_http_host" ] && printf '%s\0' "--http-host" "$_http_host"
  [ -n "${LMCACHE_HTTP_PORT:-}" ] && printf '%s\0' "--http-port" "$LMCACHE_HTTP_PORT"
  [ -n "${LMCACHE_OTLP_ENDPOINT:-}" ] && printf '%s\0' "--otlp-endpoint" "$LMCACHE_OTLP_ENDPOINT" "--enable-tracing"
  return 0
}

# Round 51 (user): ONE MODEL_DIR resolver shared by start-all.sh
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
      _kvp_val="${_kvp_val//\'/}"                       # r20 K1: single quotes too (all tp4.env forms parse the same)
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

# r24 F3: ONE launch-mode resolver for every consumer. The default MUST be
# docker everywhere: start-cache.sh defaulted to docker while start-all.sh and
# _kvp_pre_flights defaulted to host -- a bare `bash scripts/start-all.sh` then
# started the server IN A CONTAINER but checked readiness through the HOST
# branch (host-venv lmcache version vs image version -> a false
# "LMCACHE VERSION MISMATCH" refusal). Accepts host|docker, refuses anything
# else with a clear message. Prints the resolved mode on stdout and exports
# KVP_LAUNCH_MODE so a later default (e.g. the recipe launcher's :-host) can
# never silently flip it back.
_kvp_resolve_launch_mode(){
  local _mode="${KVP_LAUNCH_MODE:-docker}"
  case "$_mode" in
    host|docker) ;;
    *)
      echo "ERROR(_kvp_resolve_launch_mode): KVP_LAUNCH_MODE must be 'host' or 'docker', got '$_mode'" >&2
      echo "  Unset KVP_LAUNCH_MODE to use the default (docker), or set it explicitly." >&2
      return 1 ;;
  esac
  KVP_LAUNCH_MODE="$_mode"
  export KVP_LAUNCH_MODE
  printf '%s' "$_mode"
}
# ---------------------------------------------------------------------------
# r28 L9: ONE server-pid resolution shared by test-recovery.sh. In docker
# mode start-cache.sh writes `echo $!` (the HOST docker-run client) into the
# pid file -- reading /proc/<that>/io measured the CLIENT's I/O, not the
# cache server's. The real server pid is the container's State.Pid (host
# namespace); host mode keeps the pid file.
# ---------------------------------------------------------------------------
kvp_server_pid(){ # kvp_server_pid [cache-root]
  # Prints the PID of the REAL cache server (host namespace), or nothing
  # when it cannot be resolved.
  local _croot="${1:-${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}}"
  local _cname="${KVP_CACHE_CONTAINER:-lmcache-kvp-server}" _pidf _pid
  _pid="$(docker inspect -f '{{.State.Pid}}' "$_cname" 2>/dev/null || echo '')"
  case "$_pid" in
    ""|0|*[!0-9]*) ;;  # no container / no pid -> fall back to the pid file
    *) echo "$_pid"; return 0 ;;
  esac
  _pidf="$_croot/lmcache-server.pid"
  _pid="$(tr -d '[:space:]' < "$_pidf" 2>/dev/null || echo '')"
  case "$_pid" in
    ""|*[!0-9]*) return 0 ;;
  esac
  echo "$_pid"
}
# ---------------------------------------------------------------------------
# r28 L8: ONE consumer-mode decision shared by the pre-flight and
# test-recovery.sh. The docker-mode server runs in a CONTAINER (start-cache.sh
# launches `docker run`), so the boundary to test is "server container <-> engine
# container" -- the old test-recovery.sh only knew host/host-root and probed
# the WRONG boundary (host process <-> engine container), which could pass
# while the real one was broken.
# ---------------------------------------------------------------------------
kvp_ipc_consumer_mode(){ # kvp_ipc_consumer_mode [cache-root]
  # Prints host | host-root | container:
  #   container  -- the cache server is a running docker container;
  #   host-root  -- host server whose pid file points at a root process;
  #   host       -- anything else.
  local _croot="${1:-${KVP_CACHE_ROOT:-/var/lib/kv-persistence/l2}}"
  local _cname="${KVP_CACHE_CONTAINER:-lmcache-kvp-server}"
  local _pidf _pid _uid
  if [ "$(docker inspect -f '{{.State.Running}}' "$_cname" 2>/dev/null)" = "true" ]; then
    echo container
    return 0
  fi
  _pidf="$_croot/lmcache-server.pid"
  _pid="$(tr -d '[:space:]' < "$_pidf" 2>/dev/null || echo '')"
  if [ -n "$_pid" ]; then
    _uid="$(awk '/^Uid:/{print $2}' "/proc/$_pid/status" 2>/dev/null || echo '')"
    if [ "$_uid" = "0" ]; then
      echo host-root
      return 0
    fi
  fi
  echo host
}
# ---------------------------------------------------------------------------
# r28 L7: the L2 store failure must be LOUD. The hardware run wrote every
# failure only into lmcache-server.log ("Serde task N (SERIALIZE) failed",
# "Store task N to adapter 0 failed", "L2 store completed: adapter 0, 0
# succeeded, 20 failed") while start-all.sh, vLLM and the readiness gate all
# looked healthy -- the operator learned about it only by reading the log.
# ---------------------------------------------------------------------------
kvp_scan_l2_failures(){ # kvp_scan_l2_failures <log> [from_byte]
  # Prints the first error block + "L2 STORE FAILING" and returns 1 when the
  # log (from from_byte) contains an L2 store failure; silent, return 0
  # otherwise. Regexes are mawk-safe (no {n} intervals).
  local _log="${1:-}" _from="${2:-0}" _fresh _hit _line _block
  [ -n "$_log" ] && [ -f "$_log" ] || return 0
  _fresh="$(tail -c "+$(( _from + 1 ))" "$_log" 2>/dev/null || true)"
  [ -n "$_fresh" ] || return 0
  _hit="$(printf '%s\n' "$_fresh" | grep -anE \
    'Serde task [0-9]+ \((SERIALIZE|DESERIALIZE)\) failed|Store task [0-9]+ to adapter [0-9]+ failed|L2 store completed: adapter [0-9]+, [0-9]+ succeeded, [1-9][0-9]* failed' \
    | head -n 1 || true)"
  [ -n "$_hit" ] || return 0
  _line="${_hit%%:*}"
  # Limited selection (same rule as the r28 L2 traceback fix): from the found
  # line to the NEXT timestamped line, at most 40 lines -- the server keeps
  # logging DEBUG noise ("skipping eviction") after the error.
  _block="$(printf '%s\n' "$_fresh" | awk -v start="$_line" '
    NR == start {f=1}
    f && NR > start && /^\[20[0-9][0-9]-/ {exit}
    f {print}' | head -n 40)"
  printf '%s\n' "$_block"
  echo "L2 STORE FAILING: the cache server cannot write to NVMe. L1 (RAM) hits still work, so the service looks healthy, but nothing survives a restart."
  return 1
}
# r28 L7: one-line summary for the `kvp status` subcommand.
kvp_l2_writes_status(){ # kvp_l2_writes_status <log> [from_byte]
  local _log="${1:-}" _from="${2:-0}" _fresh _n
  [ -n "$_log" ] && [ -f "$_log" ] || { echo "L2 writes: OK"; return 0; }
  _fresh="$(tail -c "+$(( _from + 1 ))" "$_log" 2>/dev/null || true)"
  _n="$(printf '%s\n' "$_fresh" | grep -acE \
    'Serde task [0-9]+ \((SERIALIZE|DESERIALIZE)\) failed|Store task [0-9]+ to adapter [0-9]+ failed|L2 store completed: adapter [0-9]+, [0-9]+ succeeded, [1-9][0-9]* failed' \
    || true)"
  if [ "${_n:-0}" -gt 0 ]; then
    echo "L2 writes: FAILING ($_n failed)"
    return 1
  fi
  echo "L2 writes: OK"
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
  local _uid="${6:-}" _gid="${7:-}" _same="${8:-0}" _mode="${9:-docker}"
  local _ping_out _ping_rc=0 _ipc_mode _ipc_pidf _ipc_pid _ipc_uid _ipc_pu
  # ---- Round 80b: three pre-flight checks preserved from the legacy kv
  # launcher (they lived ONLY in the legacy kv launcher and would vanish
  # with it -- the round-76 defect class). All refuse BEFORE the model load.
  # (c0) reachability -- a clear "server not reachable" beats a 300 s
  #      register_kv_caches timeout after 15 min of loading.
  # r24 F2: in docker mode the TCP probe alone is NOT proof -- the Docker
  # proxy publishes the port as soon as the container starts. Check the
  # CONTAINER is alive FIRST (its name is the same default start-cache.sh
  # uses); then the TCP probe as before.
  if [ "$_mode" = "docker" ]; then
    local _pf_cname="${KVP_CACHE_CONTAINER:-lmcache-kvp-server}"
    if [ "$(docker inspect -f '{{.State.Running}}' "$_pf_cname" 2>/dev/null)" != "true" ]; then
      echo "ERROR: the cache container '$_pf_cname' is NOT running (pre-flight, docker mode)." >&2
      echo "The engine would start against a dead cache service. Start it first:" >&2
      echo "  bash scripts/start-cache.sh" >&2
      return 1
    fi
    echo "pre-flight: cache container '$_pf_cname' is running"
    # Round 27 (K3): the RUNNING server's UID must match the engine's. The
    # hardware run showed a root server + UID-1000 engine fails at
    # register_kv_cache (CUDA_ERROR_MAP_FAILED): the engine starts only when
    # both sides share ONE UID. docker Config.User: empty / 0 / 0:0 / root
    # all mean root. Expected: operator UID:GID when _same=1, root when 0.
    local _srv_user _srv_uid _srv_gid _exp_uid _exp_gid
    _srv_user="$(docker inspect -f '{{.Config.User}}' "$_pf_cname" 2>/dev/null || echo '')"
    case "$_srv_user" in
      ""|0|0:0|root) _srv_uid=0; _srv_gid=0 ;;
      [0-9]*:[0-9]*)
        case "$_srv_user" in
          *[!0-9:]* ) _srv_uid=""; _srv_gid="" ;;   # garbage inside -> unparsed
          *) _srv_uid="${_srv_user%%:*}"; _srv_gid="${_srv_user##*:}" ;;
        esac ;;
      [0-9]*) _srv_uid="$_srv_user"; _srv_gid="$_srv_user" ;;
      *) _srv_uid=""; _srv_gid="" ;;                # unparsed: not a proven mismatch
    esac
    if [ -z "$_srv_uid" ]; then
      echo "NOTE: cannot parse the cache server's docker Config.User ('$_srv_user') -- the server/engine UID check is skipped (pre-flight)"
    else
      if [ "$_same" = "1" ]; then
        _exp_uid="${_uid:-$(id -u)}"; _exp_gid="${_gid:-$(id -g)}"
      else
        _exp_uid=0; _exp_gid=0
      fi
      if [ "$_srv_uid" != "$_exp_uid" ] || [ "$_srv_gid" != "$_exp_gid" ]; then
        echo "ERROR: the cache server runs as UID $_srv_uid:$_srv_gid, the engine would run as $_exp_uid:$_exp_gid." >&2
        echo "CUDA IPC will NOT open across mismatched UIDs (register_kv_cache fails with CUDA_ERROR_MAP_FAILED)." >&2
        echo "Run: bash scripts/stop-cache.sh and rerun bash scripts/start-all.sh" >&2
        return 1
      fi
      echo "pre-flight: cache server UID matches the engine ($_srv_uid:$_srv_gid)"
    fi
  fi
  # Round 80b: bash /dev/tcp (NOT python3) -- test stubs replace python3 and
  # would break a python-based probe; /dev/tcp has no interpreter dependency.
  if timeout 3 bash -c "exec 3<>/dev/tcp/$_host/$_port" 2>/dev/null; then
    echo "pre-flight: cache server is listening at $_host:$_port"
  else
    echo "ERROR: LMCache cache server is NOT reachable at $_host:$_port (pre-flight)." >&2
    echo "Starting the engine now would waste a ~15-min model load and then fail with" >&2
    echo "'LMCache server did not respond to register_kv_caches within 300s'." >&2
    echo "Start the cache service first: bash scripts/start-cache.sh" >&2
    return 1
  fi
  # (c1) L2 codec match -- the codec is part of the cache-identity namespace;
  # a mismatch means engine and server disagree about which chunks are readable.
  local _pre_adapter="$_croot/l2-adapter.rendered.json"
  local _pre_expect="$(printf '%s' "${LMCACHE_L2_SERDE:-none}" | tr '[:upper:]' '[:lower:]')"
  case "$_pre_expect" in raw|"") _pre_expect=none ;; esac
  if [ -f "$_pre_adapter" ]; then
    local _pre_seen
    _pre_seen="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); s=d.get("serde") or {}; print((s.get("type") or "none").replace("kvp-",""))' "$_pre_adapter" 2>/dev/null || echo UNREADABLE)"
    case "$_pre_seen" in raw|"") _pre_seen=none ;; esac
    if [ "$_pre_seen" = "UNREADABLE" ]; then
      echo "WARNING: $_pre_adapter exists but is not valid JSON -- cannot pre-check the server codec" >&2
    elif [ "$_pre_seen" != "$_pre_expect" ]; then
      echo "ERROR: L2 CODEC MISMATCH (pre-flight): this launch would use '$_pre_expect' but the RUNNING" >&2
      echo "cache server stores '$_pre_seen' (see $_pre_adapter). The codec is part of the cache" >&2
      echo "identity -- the engine and the server would disagree about which chunks are readable." >&2
      echo "Fix ONE side to match the other and rerun:" >&2
      echo "  server on zstd:    USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh" >&2
      echo "  then relaunch the engine with the same codec." >&2
      return 1
    else
      echo "pre-flight: L2 codec matches the running server ($_pre_seen)"
    fi
  else
    echo "NOTE: no $_pre_adapter -- the cache server may not be started yet (the post-launch gate will insist on it)"
  fi
  # (c2) lmcache version match -- mixed minor versions break the MP-connector
  # protocol silently at register_kv_caches (round 42).
  # r20 K3: the check MODE-DEPENDS. In docker mode (the default) the cache
  # server runs in a container from the SAME image as the engine -- the
  # host-venv copy is only the mp_ping client (requirements-lmcache.txt calls
  # it "diagnostic"). Comparing the host-venv version against the image
  # version always refused (host 0.5.6 vs image 0.5.8rc1.post48+kvp...) with
  # an unrunnable `pip install lmcache==...+kvp...` hint (no such package on
  # PyPI). In docker mode the REAL invariant is "server and engine run ONE
  # image" -- verify it, and run the RPC ping INSIDE the cache container.
  local _kvp_cache_container="${KVP_CACHE_CONTAINER:-lmcache-kvp-server}"
  if [ "$_mode" = "docker" ]; then
    # (c2-d) server image == engine image?
    local _srv_img="" _eng_img=""
    _srv_img="$(docker inspect --format '{{.Image}}' "$_kvp_cache_container" 2>/dev/null || true)"
    _eng_img="$(docker inspect --format '{{.Id}}' "$_image" 2>/dev/null || true)"
    if [ -z "$_srv_img" ]; then
      if timeout 2 bash -c "exec 3<>/dev/tcp/$_host/$_port" 2>/dev/null; then
        echo "NOTE: cache container '$_kvp_cache_container' not found but the TCP port is open -- server/engine image check skipped"
      else
        :  # (c0) already refused the dead server above
      fi
    elif [ -n "$_eng_img" ] && [ "$_srv_img" != "$_eng_img" ]; then
      echo "ERROR: the cache server and the engine run DIFFERENT images (pre-flight):" >&2
      echo "  cache container '$_kvp_cache_container' image: $_srv_img" >&2
      echo "  engine image '$_image': $_eng_img" >&2
      echo "  The MP server and every worker must run the SAME custom build -- mixed" >&2
      echo "  builds break the MP-connector protocol and can corrupt the cache." >&2
      echo "  Fix: bash scripts/stop-cache.sh && bash scripts/start-cache.sh" >&2
      return 1
    elif [ -n "$_eng_img" ]; then
      echo "pre-flight: cache server and engine run the same image (${_eng_img:0:24}...)"
    else
      echo "NOTE: cannot read the engine image id ($_image) -- server/engine image check skipped"
    fi
  else
    # host mode: the host DOES serve lmcache -- the version must match the
    # image (the round-42 check, unchanged in logic)
    local _kvp_py="$(_kvp_python)"
    local _host_lmcache
    _host_lmcache="$("$_kvp_py" -c 'import importlib.metadata; print(importlib.metadata.version("lmcache"))' 2>/dev/null || echo UNKNOWN)"
    local _img_lmcache
    _img_lmcache="$(docker run --rm --entrypoint python3 "$_image" -c 'import importlib.metadata; print(importlib.metadata.version("lmcache"))' 2>/dev/null || echo UNKNOWN)"
    if [ "$_host_lmcache" != "UNKNOWN" ] && [ "$_img_lmcache" != "UNKNOWN" ]; then
      if [ "$_host_lmcache" != "$_img_lmcache" ]; then
        # r20 K3: the fix hint must be RUNNABLE -- `pip install lmcache==<+kvp
        # version>` names a package that does not exist on PyPI. Point at the
        # EXACT wheel this repo built (the only place that version exists).
        local _kvp_wheel=""
        _kvp_wheel="$(ls "${KVP_LIVE_DIR:-$_root/live}/lmcache-build/wheels/"*.whl 2>/dev/null | head -1 || true)"
        echo "ERROR: LMCACHE VERSION MISMATCH (pre-flight): this launch serves lmcache $_host_lmcache" >&2
        echo "  from the host venv but the vLLM image ships lmcache $_img_lmcache. Mixed minor" >&2
        echo "  versions break the MP-connector protocol -- register_kv_caches will silently" >&2
        echo "  time out (300s)." >&2
        if [ -n "$_kvp_wheel" ]; then
          echo "  Align the host copy to the built wheel:" >&2
          echo "    ~/.venvs/lmcache/bin/pip install --force-reinstall --no-deps $_kvp_wheel" >&2
        else
          echo "  Build the wheel first (bash scripts/build-lmcache.sh), then:" >&2
          echo "    ~/.venvs/lmcache/bin/pip install --force-reinstall --no-deps <the single *.whl in live/lmcache-build/wheels/>" >&2
        fi
        echo "  (host mode serves lmcache from the host venv; in docker mode this check is" >&2
        echo "  replaced by the server/engine same-image check)" >&2
        return 1
      fi
      echo "pre-flight: lmcache version matches on both sides ($_host_lmcache)"
    else
      echo "NOTE: could not determine lmcache version (host=$_host_lmcache, image=$_img_lmcache) -- skipping version check"
    fi
  fi
  # (a) lmcache RPC probe -- an open TCP port does not prove the RequestClient
  # protocol register_kv_caches rides. Exit 3 = lmcache not importable (honest
  # skip: the system python lacks it; use the venv python for a real probe).
  # r20 K3: in docker mode run the ping INSIDE the cache container -- the
  # image carries our wheel (PYTHONPATH=/kvp-impl is mounted, lmcache is our
  # build); exit 3 there is a REFUSAL too (an unreadable lmcache in the image
  # is a hidden defect, never an honest skip).
  if [ -d "$_root/implementation" ]; then
    if [ "$_mode" = "docker" ]; then
      _ping_out="$(docker exec "$_kvp_cache_container" \
        python3 -m kvpersist.mp_ping "$_host" "$_port" 10 2>&1)" || _ping_rc=$?
      case "$_ping_rc" in
        0)
          echo "  $_ping_out" ;;
        3)
          echo "  $_ping_out" >&2
          echo "ERROR: lmcache is NOT importable inside the cache container (pre-flight):" >&2
          echo "  'skipped' would hide a broken image -- the wheel must import there." >&2
          echo "  Fix: rebuild/redeploy the image (bash scripts/build-lmcache.sh && bash scripts/build-image.sh)" >&2
          return 1 ;;
        *)
          echo "ERROR: the cache server did not answer the RPC ping (register_kv_caches would hang):" >&2
          echo "  $_ping_out" >&2
          echo "  The server accepted TCP on $_host:$_port but its RPC channel is dead." >&2
          echo "  Fix: restart the cache service (bash scripts/stop-cache.sh && bash scripts/start-cache.sh)" >&2
          echo "       and check its log: tail -40 ${_croot}/lmcache-server.log" >&2
          return 1 ;;
      esac
    else
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
  _ipc_mode="$(kvp_ipc_consumer_mode "$_croot")"
  echo "pre-flight: CUDA IPC boundary test (consumer=$_ipc_mode) -- can the cache"
  echo "            server open GPU memory created in the container? (~1 min)"
  _ipc_pu=""
  if [ "$_same" = "1" ] && [ -n "$_uid" ]; then _ipc_pu="$_uid:$_gid"; fi
  local _ipc_rc=0 _ipc_tmp _c_err
  # r28 L5.2: show the ipc-test output LIVE (the 10s/20s handle-wait counter
  # used to stay hidden for 60-90s inside $(...)); tee captures it for the
  # L3 error-line extraction. rc comes from PIPESTATUS[0], never from tee.
  _ipc_tmp="$(mktemp "${TMPDIR:-/tmp}/kvp-ipc-out.XXXXXX")"
  IMAGE="$_image" CONSUMER="$_ipc_mode" PRODUCER_USER="$_ipc_pu" CONSUMER_USER="$_ipc_pu" \
    bash "$_root/scripts/ipc-test.sh" 2>&1 | tee "$_ipc_tmp"
  _ipc_rc=${PIPESTATUS[0]}
  if [ "$_ipc_rc" -eq 0 ]; then
    rm -f "$_ipc_tmp"
    echo "pre-flight: CUDA IPC boundary OK (consumer=$_ipc_mode${_ipc_pu:+, producer UID=$_ipc_pu})"
    return 0
  fi
  # r28 L3.2: rc=3 is the test's own "TEST INVALID BY CONSTRUCTION" verdict --
  # it says NOTHING about the CUDA boundary. Never present it as a boundary
  # failure, never print UID advice for it.
  if [ "$_ipc_rc" -eq 3 ]; then
    rm -f "$_ipc_tmp"
    echo "ERROR: the IPC pre-flight test is INVALID (producer and consumer share a PID) -- a test-construction bug, NOT a CUDA failure" >&2
    return 1
  fi
  echo "ERROR: CUDA IPC BOUNDARY FAILED (pre-flight): the cache server cannot open" >&2
  echo "GPU memory created in the container -- this is the EXACT step that killed" >&2
  echo "register_kv_cache (CUDA_ERROR_MAP_FAILED from cuIpcOpenEventHandle)." >&2
  echo "The model load would waste ~15 min and fail the same way. Fix the boundary FIRST:" >&2
  if [ "$_mode" = "docker" ]; then
    # K7: name BOTH sides' UIDs -- the mismatch is the whole story.
    local _f_srv_user _f_srv_uid
    _f_srv_user="$(docker inspect -f '{{.Config.User}}' "${KVP_CACHE_CONTAINER:-lmcache-kvp-server}" 2>/dev/null || echo '?')"
    case "$_f_srv_user" in ""|0|0:0|root) _f_srv_uid=0:0 ;; *) _f_srv_uid="$_f_srv_user" ;; esac
    echo "  - the cache server and the engine must run under ONE UID (round 27 review)." >&2
    echo "    UIDs: cache server=$_f_srv_uid, engine=${_ipc_pu:-$(id -u):$(id -g)}" >&2
    echo "    Check: docker inspect -f '{{.Config.User}}' ${KVP_CACHE_CONTAINER:-lmcache-kvp-server}" >&2
    echo "    Then: bash scripts/stop-cache.sh && bash scripts/start-cache.sh" >&2
  else
    echo "  - the cache server and the engine must run under ONE UID." >&2
    echo "    Run the server as the same user as the engine (KVP_SAME_UID=1)," >&2
    echo "    or both as root (KVP_SAME_UID=0). Mixed pairs never work." >&2
    echo "  - or run the cache server in the container too:" >&2
    echo "      KVP_LAUNCH_MODE=docker USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh" >&2
  fi
  # r28 L3.1: the REAL consumer error line, extracted from the OUTPUT (the old
  # `tail -n 1` of everything showed the VERDICT tail, e.g. "check GPU drivers
  # / ECC", which is exactly the wrong hint at the wrong moment).
  _c_err="$(grep -a 'OPEN FAILED' "$_ipc_tmp" | tail -n 1 || true)"
  [ -n "$_c_err" ] || _c_err="$(grep -aE 'CUDA_ERROR|AcceleratorError|Returning [0-9]+' "$_ipc_tmp" | tail -n 1 || true)"
  rm -f "$_ipc_tmp"
  echo "Consumer error: ${_c_err:-(no consumer error line)}" >&2
  echo "NOTE: a pre-flight catches a dead channel at start -- it cannot catch a" >&2
  echo "server that dies MID-load; the launcher still fail-fasts on server errors." >&2
  echo "To bypass this check: KVP_SKIP_IPC_TEST=1 (NOT recommended -- the failure returns)." >&2
  return 1
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

# ---------------------------------------------------------------------------
# r2 P0-1: custom-LMCache-image mandate.
# After the obsolete local race-guard was removed (recipes 799d634), the ONLY
# retrieve/abort fix (PR #4136) and the incomplete-object fix (PR #5972) live
# inside the custom LMCache build baked into the derivative image
# (build-lmcache.sh / build-image.sh). A stock vLLM image silently reverts to
# upstream LMCache without the fixes -- the engine dies on the first
# retrieve/abort race. Every launcher that starts a model worker or the MP
# server must verify the image identity BEFORE any model load.
# ---------------------------------------------------------------------------
_kvp_require_custom_image() { # _kvp_require_custom_image <image> <kvp-repo-root> [expected-base]
  local img="$1" root="$2" exp_base="${3:-}" rc=0
  [ -n "$img" ] || { echo "ERROR(_kvp_require_custom_image): no IMAGE given" >&2; return 4; }
  [ -n "$root" ] || { echo "ERROR(_kvp_require_custom_image): no repo root given" >&2; return 4; }
  # r4 P0-3: deep verification runs BY DEFAULT (LABELs alone are forgeable).
  # KVP_VERIFY_SHALLOW=1 is the explicit test-stand escape hatch, never set in
  # production (same category as KVP_SKIP_IMAGE_CHECK).
  local _verify_args=()
  [ "${KVP_VERIFY_SHALLOW:-0}" = "1" ] || _verify_args+=(--deep)
  [ -n "$exp_base" ] && _verify_args+=(--expected-base "$exp_base")
  # r22 M1: the BUILD identity of the RUNNING image is exported under its own
  # name (KVP_RUNNING_LMCACHE_BUILD). KVP_LMCACHE_BUILD is the OPERATOR PIN
  # (files or the operator's env) and is never overwritten by the resolver --
  # that pin is what the gate cross-checks against the running image.
  local _out
  _out="$(PYTHONPATH="$root/implementation${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m kvpersist.check_image_identity "$img" "$root/vendor/patches/lmcache" "${_verify_args[@]}")" || rc=$?
  if [ -n "$_out" ]; then
    printf '%s\n' "$_out"
    local _wv
    _wv="$(printf '%s\n' "$_out" | sed -n 's/^KVP_WHEEL_VERSION=//p' | tail -1)"
    if [ -n "$_wv" ]; then
      KVP_RUNNING_LMCACHE_BUILD="$_wv"
      export KVP_RUNNING_LMCACHE_BUILD
    fi
  fi
  return "$rc"
}

# ---------------------------------------------------------------------------
# r20 K1: ONE fail-fast resolution of the RUNTIME image. Two images exist in
# the chain: the BASE pin (stock vLLM, `vendor/pinned-base-image.env`, the
# recipe's `IMAGE=`) and the CUSTOM derivative (`live/image`, built from the
# base by build-image.sh). The launch ALWAYS runs the custom one; a stock
# IMAGE on the input is not a user error -- it is the base pin and must match
# the `org.kvp.lmc-base` label of the image recorded in live/image.
#
#   _kvp_resolve_runtime_image <repo-root> [<image-was-set-in-env:0|1>]
#
# No side effects: live/image is NEVER written here. On success the resolved
# custom image is exported as IMAGE. The optional second argument only
# affects the NOTE wording (start-all.sh remembers whether IMAGE was set in
# the environment BEFORE seeding tp4.env).
#   die (return 1) on an unresolvable / mismatched pair, with the fix text.
# ---------------------------------------------------------------------------
_kvp_resolve_runtime_image() {
  local root="$1" img_from_env="${2:-0}"
  [ -n "$root" ] || { echo "ERROR(_kvp_resolve_runtime_image): no repo root given" >&2; return 4; }
  [ -d "$root" ] || { echo "ERROR(_kvp_resolve_runtime_image): repo root not found: $root" >&2; return 4; }
  # r20 K1: live/ defaults to the GIVEN repo root (the recipe launchers have
  # no $ROOT of their own); KVP_LIVE_DIR overrides (start-all.sh exports the
  # canonical one).
  local _live="${KVP_LIVE_DIR:-$root/live}"
  local _live_image=""
  [ -s "$_live/image" ] && _live_image="$(tr -d '[:space:]' < "$_live/image")"
  local _in="${IMAGE:-}"
  # r20 K1: test-harness opt-out -- the SAME switch that suppressed the old
  # verify-image-custom.sh block in the launchers (round 2). With
  # KVP_SKIP_IMAGE_CHECK=1 only the trivial resolve remains (empty IMAGE ->
  # live/image when present); no refusal, no custom-image check. Production
  # must never set this.
  if [ "${KVP_SKIP_IMAGE_CHECK:-0}" = "1" ]; then
    [ -z "$_in" ] && [ -n "$_live_image" ] && IMAGE="$_live_image"
    export IMAGE
    echo "NOTE: KVP_SKIP_IMAGE_CHECK=1 -- runtime image resolution/check suppressed (test harness)"
    return 0
  fi

  # -- empty input: use live/image, or explain the first-run build
  if [ -z "$_in" ]; then
    if [ -n "$_live_image" ]; then
      IMAGE="$_live_image"
      export IMAGE
      # r21 L1: every successful path exports the BUILD identity of the image
      # that will run (deep check reads the baked identity JSON and prints
      # KVP_WHEEL_VERSION, captured into KVP_RUNNING_LMCACHE_BUILD).
      _kvp_require_custom_image "$IMAGE" "$root" \
        || { echo "ERROR: IMAGE '$IMAGE' recorded in $_live/image is NOT the custom LMCache derivative." >&2
             echo "  Fix: rebuild with bash scripts/build-lmcache.sh && bash scripts/build-image.sh" >&2
             return 1; }
      echo "NOTE: IMAGE not set -- using the custom image recorded in $_live/image: $IMAGE"
      return 0
    fi
    echo "ERROR: no IMAGE set and no $_live/image -- first run:" >&2
    echo "  bash scripts/build-lmcache.sh" >&2
    echo "  bash scripts/build-image.sh" >&2
    return 1
  fi

  # -- input equals live/image: nothing to decide, ordinary deep check
  if [ -n "$_live_image" ] && [ "$_in" = "$_live_image" ]; then
    export IMAGE="$_in"
    _kvp_require_custom_image "$IMAGE" "$root" \
      || { echo "ERROR: IMAGE '$IMAGE' is NOT the custom LMCache derivative." >&2
           echo "  Fix: rebuild with bash scripts/build-lmcache.sh && bash scripts/build-image.sh" >&2
           return 1; }
    return 0
  fi
  # -- explicit sha256:<64hex> override: it WINS (README contract "explicit
  # IMAGE wins"); the deep check below decides whether it is our custom build
  case "$_in" in
    sha256:*)
      if [ -n "$_live_image" ] && [ "$_in" != "$_live_image" ]; then
        echo "NOTE: IMAGE=$_in overrides live/image ($_live_image)"
      fi
      export IMAGE="$_in"
      _kvp_require_custom_image "$IMAGE" "$root" \
        || { echo "ERROR: IMAGE '$IMAGE' is NOT the custom LMCache derivative." >&2
             echo "  Fix: IMAGE=\"\$(cat $_live/image)\" (or unset IMAGE) and rerun." >&2
             return 1; }
      return 0
      ;;
  esac

  # r21 L6: a BARE TAG (no @sha256: digest) is a user format error -- the
  # misleading "base pin mismatch, rebuild" advice must NOT fire for it.
  case "$_in" in
    *@sha256:*) ;;  # properly pinned stock/base -- falls through below
    *)
      echo "ERROR: IMAGE '$_in' has no digest -- a bare tag is NOT accepted." >&2
      echo "  IMAGE must be 'repo:tag@sha256:<64hex>' (base pin) or 'sha256:<id>' (custom)." >&2
      echo "  Take the custom id from:  \$(cat ${_live:-$root/live}/image)" >&2
      echo "  Or simply run: IMAGE=\"\$(cat ${_live:-$root/live}/image)\" bash scripts/start-all.sh" >&2
      return 1
      ;;
  esac

  # -- repo:tag@sha256:... (stock, base pin): must match the custom image's
  # base label; the custom image is the one that runs
  if [ -n "$_live_image" ]; then
    if _kvp_require_custom_image "$_live_image" "$root" "$_in"; then
      local _src="tp4.env or the environment"
      [ "$img_from_env" = "1" ] && _src="the environment"
      echo "NOTE: IMAGE=$_in ($_src) -- base pin; launching custom $_live_image"
      IMAGE="$_live_image"
      export IMAGE
      return 0
    fi
    echo "ERROR: base pin mismatch: IMAGE=$_in does not match the base of the custom image in $_live/image." >&2
    echo "  The custom image was built FROM a different base (see the digest pair above)." >&2
    echo "  Fix: rebuild the custom image from the pinned base (bash scripts/build-lmcache.sh && bash scripts/build-image.sh)," >&2
    echo "  or correct the base pin in tp4.env / the environment." >&2
    return 1
  fi

  # -- stock input and no live/image at all: nothing custom to launch
  echo "ERROR: IMAGE=$_in is a stock/base image and $_live/image does not exist." >&2
  echo "  The launch chain requires the custom LMCache derivative. First run:" >&2
  echo "  bash scripts/build-lmcache.sh" >&2
  echo "  bash scripts/build-image.sh" >&2
  return 1
}