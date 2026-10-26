#!/usr/bin/env bash
# preflight.sh — verify host, storage and identity before starting the cache.
# Read-only: never modifies the production recipe, model files or cache data.
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# shellcheck source=/dev/null
# Env contract (same as the recipe launchers): values already exported WIN over
# the files -- plain '.' sourcing would clobber explicit env like KVP_CACHE_ROOT.
_kvp_seed(){ # _kvp_seed <env file>: export only names NOT already set
  [ -f "$1" ] || return 0
  local line name val
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    name="${line%%=*}"
    [ "$name" = "$line" ] && continue
    name="$(printf '%s' "$name" | tr -d ' ')"
    [ -z "$name" ] && continue
    if [ -z "${!name:-}" ]; then
      val="${line#*=}"
      val="${val%% #*}"
      val="${val%\"${val##*[![:space:]]}\"}"
      val="${val//\"/}"
      case "$val" in *'$HOME'*|*'${HOME}'*) val="${val//\$HOME/$HOME}"; val="${val//\$\{HOME\}/$HOME}" ;; esac
      export "$name=$val"
    fi
  done < "$1"
}
_kvp_seed "$ROOT/configs/generic/cache.env"
_kvp_seed "$ROOT/configs/generic/cache.env.local"
_kvp_seed "$ROOT/configs/generic/lmcache-server.env"
_kvp_seed "$ROOT/configs/mimo-v2.6-pro/identity.env"
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
if ! mkdir -p "$ROOT_PATH" 2>/dev/null; then
  die "cannot create cache root $ROOT_PATH -- permission denied (not root?).
/var/lib/... belongs to root. Either run the setup once with:
  sudo install -d -m 700 -o $(id -un) -g $(id -gn) $ROOT_PATH
or point KVP_CACHE_ROOT at a directory YOU own on the target NVMe
(edit configs/generic/cache.env.local). See docs/deployment.md."
fi
chmod 700 "$ROOT_PATH" 2>/dev/null || warn "cannot chmod 700 $ROOT_PATH (storage may be readable by others)"
case "$ROOT_PATH" in
  /var/lib/kv-persistence/*|/mnt/*|/data/*|/nvme*|"${HOME}"/*) note "cache root: $ROOT_PATH" ;;
  *) warn "cache root $ROOT_PATH is unusual — confirm it is on local NVMe" ;;
esac
echo "-- storage location (device-level check) --"
# The budget is 750 GB: the cache MUST sit on the intended NVMe, not merely on
# a filesystem with enough free space. KVP_CACHE_DEVICE pins the expected
# block device (e.g. /dev/nvme1n1 or its partition). findmnt resolves the mount
# of KVP_CACHE_ROOT; lsblk maps it to the parent disk.
TARGET_DEV="${KVP_CACHE_DEVICE:-}"
if command -v findmnt >/dev/null && command -v lsblk >/dev/null; then
  MOUNT_SRC="$(findmnt -nro SOURCE --target "$ROOT_PATH" 2>/dev/null || true)"
  PARENT_DISK="$(lsblk -npo NAME,PKNAME 2>/dev/null | awk -v m="${MOUNT_SRC#/dev/}" '$1=="/dev/"m || $1==m {print ($2==""?$1:"/dev/"$2)}' | head -1 || true)"
  FS_TYPE="$(df -T "$ROOT_PATH" 2>/dev/null | awk 'NR==2{print $2}')"
  note "mount point of $ROOT_PATH: ${MOUNT_SRC:-unknown} (fs: ${FS_TYPE:-?}, disk: ${PARENT_DISK:-unknown})"
  case ",$FS_TYPE," in
    *,nfs*,*|*,cifs,*|*,smb*,*|*,fuse.sshfs,*)
      warn "$ROOT_PATH is on network storage ($FS_TYPE) -- atomic writes and persistence are NOT reliable there" ;;
  esac
  if [ -n "$TARGET_DEV" ]; then
    case " $PARENT_DISK " in
      *" $TARGET_DEV "*|*" ${TARGET_DEV%/}"[0-9]*" "*) note "cache root is on the expected NVMe device ($TARGET_DEV)" ;;
      *) warn "cache root device ($PARENT_DISK) does not match KVP_CACHE_DEVICE=$TARGET_DEV -- wrong disk for a ${BUDGET_GB} GB budget" ;;
    esac
  else
    warn "KVP_CACHE_DEVICE is unset: cannot prove $ROOT_PATH is on the intended NVMe.
Set KVP_CACHE_DEVICE=/dev/nvmeXnX in cache.env.local after checking with lsblk.
On a ${BUDGET_GB} GB budget a wrong disk would silently consume the system drive."
  fi
else
  warn "findmnt/lsblk missing -- device-level check skipped (install util-linux to enforce it)"
fi
  FREE_GB="$(df -BG --output=avail "$ROOT_PATH" 2>/dev/null | tail -1 | tr -dc 0-9)"
  if [ -n "$FREE_GB" ] && [ "$FREE_GB" -lt "$BUDGET_GB" ]; then
    warn "only ${FREE_GB} GB free on $(df -P "$ROOT_PATH" | awk 'NR==2{print $6}') < budget ${BUDGET_GB} GB"
  else
    note "free space on cache filesystem: ${FREE_GB:-?} GB"
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

echo "-- chat-template digest (single algorithm: kvpersist.chat_template) --"
if [ -n "${KVP_MODEL_DIR:-}" ] && [ -d "${KVP_MODEL_DIR:-}" ]; then
export PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}"
  DLINE="$(python3 - "$KVP_MODEL_DIR" <<'PY' 2>&1 || true
import sys
from kvpersist.chat_template import chat_template_digest
d, s = chat_template_digest(sys.argv[1])
print(d + "\t" + s)
PY
)"
  case "$DLINE" in
    *$'\t'*)
      DIG="${DLINE%%$'\t'*}"; SRC="${DLINE#*$'\t'}"
      echo "  source: $SRC"
      if [ "$DIG" = "${KVP_CHAT_TEMPLATE_DIGEST:-}" ]; then
        note "configured digest matches the model dir (identity consistent)"
      else
        warn "configured KVP_CHAT_TEMPLATE_DIGEST ($KVP_CHAT_TEMPLATE_DIGEST) != computed ($DIG).
Run: bash scripts/setup-identity.sh \"$KVP_MODEL_DIR\"   (fills and verifies all identity fields)"
      fi ;;
    *) warn "template digest failed: $DLINE" ;;
  esac
fi

echo "-- runtime tools --"
command -v python3 >/dev/null && note "python3 $(python3 --version 2>&1 | cut -d' ' -f2)" || warn "python3 missing"
command -v lmcache >/dev/null && note "lmcache CLI present ($(lmcache --help >/dev/null 2>&1 && echo ok || echo broken))" \
  || warn "lmcache CLI not on PATH (install per docs/deployment.md)"
PYTHONPATH="$ROOT/implementation${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'import kvpersist' 2>/dev/null \
  && note "kvpersist package importable" \
  || warn "kvpersist not importable from $ROOT/implementation"

echo
if [ "$FAIL" = 0 ]; then
  echo "preflight PASSED"
  exit 0
else
  echo "preflight found problems (marked [!!]) — fix them before relying on persistence" >&2
  exit 1
fi