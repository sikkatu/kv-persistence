#!/usr/bin/env bash
# preflight.sh — verify host, storage and identity before starting the cache.
# Read-only: never modifies the production recipe, model files or cache data.
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=/dev/null
. "$ROOT/configs/generic/cache.env"
[ -f "$ROOT/configs/generic/cache.env.local" ] && . "$ROOT/configs/generic/cache.env.local"
# shellcheck source=/dev/null
. "$ROOT/configs/mimo-v2.6-pro/identity.env"
FAIL=0
note(){ printf '  [ok] %s\n' "$*"; }
warn(){ printf '  [!!] %s\n' "$*" >&2; FAIL=1; }

echo "== kv-persistence preflight =="
echo "-- storage budget --"
BUDGET_GB="${KVP_CACHE_BUDGET_GB:?}"
[ "${BUDGET_GB%.*}" -ge 1 ] 2>/dev/null || die "KVP_CACHE_BUDGET_GB must be >= 1, got '$BUDGET_GB'"
if [ "${BUDGET_GB%.*}" -gt 1024 ]; then
  warn "KVP_CACHE_BUDGET_GB=$BUDGET_GB exceeds the 1 TB NVMe ceiling"
else
  note "budget ${BUDGET_GB} GB (physical ceiling 1 TB)"
fi
ROOT_PATH="${KVP_CACHE_ROOT:?}"
mkdir -p "$ROOT_PATH" || die "cannot create cache root $ROOT_PATH"
chmod 700 "$ROOT_PATH" || warn "cannot chmod 700 $ROOT_PATH"
case "$ROOT_PATH" in
  /var/lib/kv-persistence/*|/mnt/*|/data/*|/nvme*|"${HOME}"/*) note "cache root: $ROOT_PATH" ;;
  *) warn "cache root $ROOT_PATH is unusual — confirm it is on local NVMe" ;;
esac
if command -v df >/dev/null; then
  FS_TYPE="$(df -T "$ROOT_PATH" 2>/dev/null | awk 'NR==2{print $2}')"
  case "$FS_TYPE" in
    nfs*|cifs|smb*|fuse.sshfs) warn "$ROOT_PATH is on network storage ($FS_TYPE) — persistence may break" ;;
    *) note "filesystem: ${FS_TYPE:-unknown}" ;;
  esac
  FREE_GB="$(df -BG --output=avail "$ROOT_PATH" 2>/dev/null | tail -1 | tr -dc 0-9)"
  if [ -n "$FREE_GB" ] && [ "$FREE_GB" -lt "$BUDGET_GB" ]; then
    warn "only ${FREE_GB} GB free on $(df -P "$ROOT_PATH" | awk 'NR==2{print $6}') < budget ${BUDGET_GB} GB"
  else
    note "free space on cache filesystem: ${FREE_GB:-?} GB"
  fi
fi

echo "-- identity (compatibility namespace) --"
hex40(){ printf '%s' "$1" | grep -Eq '^[0-9a-f]{40}$'; }
hex64(){ printf '%s' "$1" | grep -Eq '^[0-9a-f]{64}$'; }
hex40 "${KVP_MODEL_REVISION:-}" || warn "KVP_MODEL_REVISION must be a full 40-hex SHA (got '${KVP_MODEL_REVISION:-<empty>}')"
hex40 "${KVP_TOKENIZER_REVISION:-}" || warn "KVP_TOKENIZER_REVISION must be a full 40-hex SHA"
hex64 "${KVP_CHAT_TEMPLATE_DIGEST:-}" || warn "KVP_CHAT_TEMPLATE_DIGEST must be 64 hex (run with KVP_MODEL_DIR set to compute it)"
[ -n "${KVP_KV_CACHE_DTYPE:-}" ] && [ "$KVP_KV_CACHE_DTYPE" != auto ] \
  && note "kv dtype: $KVP_KV_CACHE_DTYPE" || warn "KVP_KV_CACHE_DTYPE must be the RESOLVED dtype (fp16/bf16/fp8...), not 'auto'"
[ "${KVP_TP_SIZE:-0}" = 4 ] && note "tp_size=4" || warn "KVP_TP_SIZE=${KVP_TP_SIZE:-?} (expected 4 for this deployment)"

echo "-- optional: compute chat-template digest from model dir --"
if [ -n "${KVP_MODEL_DIR:-}" ] && [ -d "${KVP_MODEL_DIR:-}" ]; then
  TPL=""
  for f in "$KVP_MODEL_DIR/chat_template.jinja" "$KVP_MODEL_DIR/tokenizer_config.json"; do
    [ -f "$f" ] && TPL="$f" && break
  done
  if [ -n "$TPL" ]; then
    DIG="$(sha256sum "$TPL" | cut -d' ' -f1)"
    echo "  chat template source: $TPL"
    echo "  KVP_CHAT_TEMPLATE_DIGEST=$DIG   <- copy into configs/mimo-v2.6-pro/identity.env"
    note "chat-template digest computed"
  else
    warn "no chat_template.jinja / tokenizer_config.json in KVP_MODEL_DIR"
  fi
fi

echo "-- runtime tools --"
command -v python3 >/dev/null && note "python3 $(python3 --version 2>&1 | cut -d' ' -f2)" || warn "python3 missing"
command -v lmcache >/dev/null && note "lmcache CLI present ($(lmcache --help >/dev/null 2>&1 && echo ok || echo broken))" \
  || warn "lmcache CLI not on PATH (install per docs/deployment.md)"
python3 - <<'PY' 2>/dev/null && note "kvpersist package importable" || warn "kvpersist not importable (PYTHONPATH=implementation)"
import sys; sys.path.insert(0, "implementation")
import kvpersist
PY

echo
if [ "$FAIL" = 0 ]; then
  echo "preflight PASSED"
  exit 0
else
  echo "preflight found problems (marked [!!]) — fix them before relying on persistence" >&2
  exit 1
fi