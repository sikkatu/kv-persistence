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
. "$HERE/env.sh"
_kvp_load_env "$ROOT"
FAIL=0
note(){ printf '  [ok] %s\n' "$*"; }
warn(){ printf '  [!!] %s\n' "$*" >&2; FAIL=1; }

echo "== kv-persistence preflight =="
echo "-- storage budget --"
BUDGET_GB="${KVP_CACHE_BUDGET_GB:?}"
[ "${BUDGET_GB%.*}" -ge 1 ] 2>/dev/null || die "KVP_CACHE_BUDGET_GB must be >= 1, got '$BUDGET_GB'"
# No hard-coded ceiling check here: the real bound is the FREE SPACE on the
# pinned NVMe device, verified below via KVP_CACHE_DEVICE. A fixed numeric cap
# would contradict a legitimate large-budget deployment (the default is 1.5 TB).
note "budget ${BUDGET_GB} GB"
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
# The budget is 1.5 TB: the cache MUST sit on the intended cache disk on the shared VM volume, not merely on
# a filesystem with enough free space. KVP_CACHE_DEVICE pins the expected
# block device (e.g. /dev/nvme0n1, /dev/sda, /dev/vda, or a partition of one).
# findmnt resolves the mount of KVP_CACHE_ROOT; lsblk maps it to the parent disk.
TARGET_DEV="${KVP_CACHE_DEVICE:-}"
if command -v findmnt >/dev/null && command -v lsblk >/dev/null; then
  MOUNT_SRC="$(findmnt -nro SOURCE --target "$ROOT_PATH" 2>/dev/null || true)"
  PARENT_DISK="$(lsblk -npo NAME,PKNAME 2>/dev/null | awk -v m="${MOUNT_SRC#/dev/}" '$1=="/dev/"m || $1==m {print ($2==""?$1:"/dev/"$2)}' | head -1 || true)"
  FS_TYPE="$(df -T "$ROOT_PATH" 2>/dev/null | awk 'NR==2{print $2}')"
  note "mount point of $ROOT_PATH: ${MOUNT_SRC:-unknown} (fs: ${FS_TYPE:-?}, disk: ${PARENT_DISK:-unknown})"
  case ",$FS_TYPE," in
    *,tmpfs,*|*,ramfs,*)
      die "EPHEMERAL FS DETECTED: $ROOT_PATH is on $FS_TYPE. tmpfs/ramfs contents are
LOST on reboot -- the cache must survive a reboot to satisfy the persistence
requirement. Move KVP_CACHE_ROOT to a real disk-backed filesystem." ;;
    *,nfs*,*|*,cifs,*|*,smb*,*|*,fuse*,*)
      # Review 2C: NETWORK FS BREAKS atomic-rename semantics and small-file
      # performance -- HARD STOP, not a warning.
      die "NETWORK FS DETECTED: $ROOT_PATH is on $FS_TYPE. Atomic rename and
persistence guarantees require a POSIX local filesystem (ext4/xfs/btrfs).
Move KVP_CACHE_ROOT to local storage or recovery cannot be trusted." ;;
  esac
  if [ -n "$TARGET_DEV" ]; then
    case " $PARENT_DISK " in
      *" $TARGET_DEV "*|*" ${TARGET_DEV%/}"[0-9]*" "*) note "cache root is on the expected NVMe device ($TARGET_DEV)" ;;
      *) warn "cache root device ($PARENT_DISK) does not match KVP_CACHE_DEVICE=$TARGET_DEV -- wrong disk for a ${BUDGET_GB} GB budget" ;;
    esac
  else
    # Unset KVP_CACHE_DEVICE is an unproven claim, not a config error: preflight
    # itself does NOT fail on it (a Quick Start run without it must reach
    # PASSED). It DOES mean test-recovery.sh will reach INCONCLUSIVE instead of
    # PASS until the variable is set -- see docs/deployment.md step 2.
    note "KVP_CACHE_DEVICE is unset: cache-device proof unavailable here."
    echo "      The cache may already sit on the right NVMe -- we just cannot prove it."
    echo "      To enable the full verification (and PASS in test-recovery.sh), set"
    echo "      KVP_CACHE_DEVICE=/dev/<blockdev>  (any block device: nvme/sd/vd) in cache.env.local after checking with lsblk."
    echo "      On a ${BUDGET_GB} GB budget an unnoticed wrong disk would silently consume"
    echo "      the system drive, so this is strongly recommended before production use."
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

echo "-- runtime prerequisites (review 4: before the cache can be trusted) --"
# Python 3.12 is the documented interpreter (Debian/Ubuntu (tested on Debian 12) ships 3.11 -- see
# docs/deployment.md); require it by version, not just "python3 exists".
if command -v python3.12 >/dev/null 2>&1; then
  note "python3.12 $(python3.12 --version 2>&1 | cut -d' ' -f2)"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)'; then
  note "python3 $(python3 --version 2>&1 | cut -d' ' -f2) (>= 3.12)"
else
  warn "Python 3.12 not found -- Debian/Ubuntu (tested on Debian 12) system python3 is 3.11; install 3.12 first (docs/deployment.md step 0)"
fi
if command -v python3 >/dev/null 2>&1 && python3 -c 'import venv' 2>/dev/null; then
  note "python3 venv module available"
else
  warn "python3 venv module missing -- install python3-venv (Debian: separate package)"
fi
# Free space >= budget on the cache root filesystem.
if [ -n "${BUDGET_GB:-}" ] && [ -n "${FREE_GB:-}" ]; then
  if [ "$FREE_GB" -lt "$BUDGET_GB" ]; then
    warn "free space ${FREE_GB} GB < budget ${BUDGET_GB} GB on $(df -P "$ROOT_PATH" | awk 'NR==2{print $6}') -- raises the risk of refusing stores mid-flight"
  fi
fi
# Cache root perms must be 0700.
PERMS="$(stat -c '%a' "$ROOT_PATH" 2>/dev/null || echo '?')"
case "$PERMS" in
  700|0700) note "cache root permissions $PERMS" ;;
  *) warn "cache root permissions are $PERMS (expected 700) -- other users may read prompt-derived KV data" ;;
esac
# Listener check: lmcache request port and HTTP port must bind loopback only.
if command -v ss >/dev/null 2>&1; then
  for P in "${LMCACHE_PORT:-5555}" "${LMCACHE_HTTP_PORT:-8080}"; do
    LISTEN="$(ss -ltnH 2>/dev/null | awk -v p=":$P" '$4 ~ p"$"' | head -1)"
    if [ -n "$LISTEN" ] && ! printf '%s' "$LISTEN" | grep -q '127.0.0.1\|::1\|localhost'; then
      warn "port $P appears bound to a non-loopback address ($LISTEN) -- cache management must stay local-only"
    fi
  done
else
  note "ss unavailable -- listener loopback check skipped (cannot verify cache service binds)"
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