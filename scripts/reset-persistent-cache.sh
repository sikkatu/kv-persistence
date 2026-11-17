#!/usr/bin/env bash
# reset-persistent-cache.sh -- EXPLICIT, ONE-TIME destructive reset of the
# persistent LMCache KV-cache payload (spec section 7, r2 P0-3 revision).
#
# NEVER called by start/stop/restart. A normal lifecycle must never clear
# cache data. Run it only as a deliberate migration step.
#
# r2 P0-3 fixes over the first version:
#   * diagnostic artifacts (lmcache-server.log / .pid / l2-adapter.rendered.json
#     / crash-*) are PRESERVED in place (whitelist) and the log is additionally
#     archived under live/ -- the old find -mindepth rm wiped the previous
#     deployment's diagnostic log (the spec forbids deleting it);
#   * everything runs on the RESOLVED path (readlink -f): a symlinked cache
#     root is fully cleared, and a post-clear check FAILS on leftovers instead
#     of reporting a fake "cleared";
#   * dedicated-cache confirmation: the target must match a configured root
#     (KVP_CACHE_ROOT / LMCACHE_L2_PATH / LMCACHE_ROOT / live/cache_root /
#     live/l2_path) or carry the marker file .kvp-cache-root-marker; repos
#     (.git) and model dirs are refused;
#   * filesystem/mount recorded via findmnt, network filesystems refused;
#   * a host MP server (pid file / pgrep kvpersist|lmcache) is detected and
#     blocks the reset in addition to docker containers;
#   * the report records config source, filesystem, preserved files, bytes,
#     the next namespace identity and the custom LMCache build identity.
#
# Safety contract (refuses to run unless ALL hold):
#   * explicit confirmation: KVP_RESET_CONFIRM=FULL-RESET
#   * services stopped first (or KVP_RESET_FORCE_STOP=1 to force stop-all)
#   * every target resolves inside a confirmed dedicated cache-data directory
#   * refuses "/", home dirs, empty strings, model dirs, repos, build
#     artifacts, network filesystems and symlinks escaping the cache root
#
# Usage:
#   KVP_RESET_CONFIRM=FULL-RESET bash scripts/reset-persistent-cache.sh
#
# Report: live/cache-reset-report-<UTC>.txt (spec 7.4).

set -Eeuo pipefail
die() { echo "ERROR(reset-cache): $*" >&2; exit 1; }
note() { echo "[reset-cache] $*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# --------------------------------------------------------------------------
# 0. Explicit one-time confirmation -- never guess, never run from lifecycle.
# --------------------------------------------------------------------------
[ "${KVP_RESET_CONFIRM:-}" != "FULL-RESET" ] && [ -n "${KVP_RESET_CONFIRM:-}" ] && \
  die "KVP_RESET_CONFIRM must be exactly 'FULL-RESET' (got '$KVP_RESET_CONFIRM')"
[ "${KVP_RESET_CONFIRM:-}" = "FULL-RESET" ] || \
  die "refusing to destroy persistent cache without KVP_RESET_CONFIRM=FULL-RESET (one-time migration only; NEVER run from start/stop/restart)"

. "$HERE/env.sh" 2>/dev/null || true
if command -v _kvp_load_env >/dev/null 2>&1 || typeset -f _kvp_load_env >/dev/null 2>&1; then
  _kvp_load_env "$ROOT" 2>/dev/null || true
fi

# --------------------------------------------------------------------------
# 1. Discover the configured cache root(s) + their config source (7.1).
# --------------------------------------------------------------------------
CANDIDATES=()
declare -A SRC_OF=()
_add_candidate() { # _add_candidate <path> <source>
  [ -n "$1" ] || return 0
  CANDIDATES+=("$1")
  SRC_OF["$1"]="$2"
}
_add_candidate "${KVP_CACHE_ROOT:-}" "KVP_CACHE_ROOT (cache.env / env)"
_add_candidate "${LMCACHE_L2_PATH:-}" "LMCACHE_L2_PATH (lmcache-server.env)"
_add_candidate "${LMCACHE_ROOT:-}" "LMCACHE_ROOT (env)"
# live/markers and kv-persist launcher record the effective cache dir (r3 P1-2
# point 4: honors KVP_LIVE_DIR so test runs stage records in tmp, never the
# repo's live/).
_LIVE_RECORDS="${KVP_LIVE_DIR:-$ROOT/live}"
for f in "$_LIVE_RECORDS/cache_root" "$_LIVE_RECORDS/l2_path"; do
  [ -f "$f" ] && _add_candidate "$(head -n1 "$f" | tr -d '[:space:]')" "record $(basename "$f")"
done
TARGETS=()
for c in "${CANDIDATES[@]:-}"; do
  [ -n "$c" ] && TARGETS+=("$c")
done
[ "${#TARGETS[@]}" -gt 0 ] || die "no cache root discovered -- set KVP_CACHE_ROOT explicitly (refuse to guess)"

# --------------------------------------------------------------------------
# 2. Safety gate per target (7.2): dedicated cache dir only.
# --------------------------------------------------------------------------
cached_root_of() { # cached_root_of <candidate> -> echo the CONFIRMED raw root or ""
  case "$1" in
    "${KVP_CACHE_ROOT:-__none__}"|\
    "${LMCACHE_L2_PATH:-__none__}"|\
    "${LMCACHE_ROOT:-__none__}") return 0 ;;
  esac
  [ -f "$1/.kvp-cache-root-marker" ] && return 0
  return 1
}

# r4 P0-2: known LMCache/kvpersist entry set -- every top-level name the code
# in THIS repo actually writes into the cache root:
#   * lmcache-server.log / lmcache-server.pid   (scripts/start-cache.sh, stop-cache.sh)
#   * l2-adapter.rendered.json                  (scripts/start-cache.sh)
#   * engine-identity.env                       (scripts/verify-engine-identity.sh)
#   * crash-*                                   (scripts/collect-crash-evidence.sh,
#                                                kept as diagnostics)
#   * .kvp-cache-root-marker / .kvp-active-writes (start-cache.sh / tmp_guard.py)
#   * *.bin / *.bin.tmp.<uuid>                  (nixl_store_dynamic payload,
#                                                implementation/kvpersist/cli.py)
#   * objects/ and kvp-* dirs                   (LMCache object stores + legacy
#                                                legacy kvp-* namespace dirs)
# Anything else top-level means this is NOT a pure cache dir: refuse and list.
_kvp_known_cache_entry() { # _kvp_known_cache_entry <top-level name> -> 0 known / 1 unknown
  case "$1" in
    .kvp-cache-root-marker|\.kvp-cache-root-marker|\
    .kvp-active-writes|\.kvp-active-writes|\
    lmcache-server.log|lmcache-server.pid|l2-adapter.rendered.json|engine-identity.env|\
    index.db|crash-*|objects|kvp-*|*.bin|*.bin.tmp.*)
      return 0 ;;
  esac
  return 1
}
_kvp_unknown_cache_entries() { # _kvp_unknown_cache_entries <dir> -> echoes unknown names, rc 0 when at least one unknown
  local d="$1" entry base unknown=1
  for entry in "$d"/* "$d"/.[!.]* "$d"/..?*; do
    [ -e "$entry" ] || continue
    base="$(basename "$entry")"
    case "$base" in
      .|..) continue ;;
    esac
    if ! _kvp_known_cache_entry "$base"; then
      printf '%s\n' "$base"
      unknown=0
    fi
  done
  return $unknown
}

# r4 P0-2: refuse any target that is a home dir, an ancestor of this repo (or
# of a models/recipes checkout), or carries .git at nesting depth <= 2.
# Print the reason to stderr (the caller turns safe_target's rc into a generic
# refusal, so the DETAIL must be visible right here).
_kvp_home_of_any_user() { # _kvp_home_of_any_user <real path> -> 0 when it IS a home dir
  local real="$1" h
  # current user's home + root + every dir matched by /home/<name> /root
  for h in "${HOME:-}" /root; do
    if [ -n "$h" ] && [ "$real" = "$(readlink -f "$h" 2>/dev/null || echo "$h")" ]; then
      echo "  reason: '$real' is a user home directory (\$HOME)" >&2
      return 0
    fi
  done
  case "$real" in
    /home/*|/root)
      # /home/<name> exactly one level below /home = the user's home
      if [ "$real" = "/home/$(basename "$real")" ] || [ "$real" = "/root" ]; then
        echo "  reason: '$real' is a user home directory (/home/<user> or /root)" >&2
        return 0
      fi
      ;;
  esac
  return 1
}
_kvp_has_git_within_two_levels() { # _kvp_has_git_within_two_levels <dir> -> 0 when a .git exists at depth <= 2
  local d="$1"
  [ -e "$d/.git" ] && return 0
  local sub g
  for sub in "$d"/* "$d"/.[!.]*; do
    [ -d "$sub" ] || continue
    g="$(basename "$sub")"
    [ "$g" = ".kvp-active-writes" ] && continue
    [ -e "$sub/.git" ] && return 0
    local sub2
    for sub2 in "$sub"/* "$sub"/.[!.]*; do
      [ -e "$sub2/.git" ] && return 0
    done
  done
  return 1
}
_kvp_is_ancestor_of_checkout() { # _kvp_is_ancestor_of_checkout <real path> -> 0 when an ancestor of THIS repo checkout
  local real="$1" parent
  # The reset script itself lives inside the repo checkout, so walk up from
  # HERE: if $real is an ancestor of HERE, it is an ancestor of the checkout.
  parent="$HERE"
  while [ "$parent" != "/" ] && [ -n "$parent" ]; do
    if [ "$real" = "$(readlink -f "$parent" 2>/dev/null || echo "$parent")" ]; then
      echo "  reason: '$real' is an ancestor of the kv-persistence checkout -- never wipeable" >&2
      return 0
    fi
    parent="$(dirname "$parent")"
  done
  return 1
}
safe_target() { # safe_target <path> -> 0 accepted / 1 refused
  local p="$1" real root rroot probe abs
  [ -n "$p" ] || return 1
  real="$(readlink -f "$p" 2>/dev/null)" || return 1
  [ -n "$real" ] || return 1

  # r3 P1-2 point 1: judge the EXPLICITLY-CONFIGURED cache root BEFORE blanket
  # subtree refusals. A real NVMe commonly mounts as /mnt/<device>/<dir> and a
  # user's cache may live under $HOME/<dir> -- as long as the path IS the
  # operator-pinned KVP_CACHE_ROOT (or a marked cache root's subdir), it is a
  # CONFIRMED cache root by definition and must NOT be lumped in with bare
  # /mnt or bare /home (which stay refused below).
  root=""
  marked="0"
  for _cfg_root in "${KVP_CACHE_ROOT:-}" "${LMCACHE_L2_PATH:-}" "${LMCACHE_ROOT:-}"; do
    [ -n "$_cfg_root" ] || continue
    local _cfg_resolved
    _cfg_resolved="$(readlink -f "$_cfg_root" 2>/dev/null || echo "$_cfg_root")"
    # r3 P1-2 point 2: strict '/' boundary so root .../l2 does NOT also swallow
    # sibling .../l2-old.
    if [ "$real" = "$_cfg_resolved" ] || [ "$real" = "$_cfg_root" ]; then
      root="$_cfg_root"; break
    fi
    case "$real" in
      "$_cfg_resolved"/*|"$_cfg_root"/*) root="$_cfg_root"; break ;;
    esac
  done
  if [ -z "$root" ] && [ -e "$real/.kvp-cache-root-marker" ]; then root="$real"; fi
  if [ -z "$root" ]; then
    local up
    up="$(dirname "$real")"
    [ -e "$up/.kvp-cache-root-marker" ] && root="$up"
  fi
  # r4 P0-2: track whether a cache-root MARKER stamp exists anywhere relevant
  # (the target itself, the confirmed root, or one level up) -- a marker alone
  # is sufficient proof for the known-entry check below.
  marked="0"
  { [ -e "$real/.kvp-cache-root-marker" ] && marked=1; } || true
  if [ "$marked" = "0" ] && [ -n "$root" ] && [ -e "$root/.kvp-cache-root-marker" ]; then
    marked=1
  fi
  if [ "$marked" = "0" ] && [ -e "$(dirname "$real")/.kvp-cache-root-marker" ]; then
    marked=1
  fi

  # Rule out genuinely dangerous exact paths outright -- these stay refused even
  # when explicitly configured as the cache root (bare /mnt, bare /home, ...).
  # KVP_TEST_DANGER_ROOTS extra colon-separated list (simulation aid; tests stage
  # .mount-like tmp dirs and assert "bare root" refusals against them).
  case "$real" in
    /|/home|/root|/mnt|/usr|/etc|/opt|/tmp|/proc|/sys|/dev|/var) return 1 ;;
  esac
  if [ -n "${KVP_TEST_DANGER_ROOTS:-}" ]; then
    IFS=':' read -r -a _danger_arr <<< "$KVP_TEST_DANGER_ROOTS"
    for _d in "${_danger_arr[@]}"; do
      [ -n "$_d" ] || continue
      local _d_resolved
      _d_resolved="$(readlink -f "$_d" 2>/dev/null || echo "$_d")"
      if [ "$real" = "$_d_resolved" ] || [ "$real" = "$_d" ]; then
        return 1   # treat exactly this dangerous env-listed root as refused
      fi
    done
  fi
  # Repos (source trees) and model dirs are NEVER cache artifacts.
  [ -e "$real/.git" ] && return 1
  case "$real" in */models|*/models/*|*/models/*/*) return 1 ;; esac

  # r4 P0-2: ALWAYS refuse homes, ancestors of this checkout and dirs holding
  # a .git at nesting depth <= 2 -- even when the path was explicitly
  # configured as KVP_CACHE_ROOT (the r3 change dropped this protection and a
  # `KVP_CACHE_ROOT=$HOME` typo wiped a real home directory).
  _kvp_home_of_any_user "$real" && return 1
  _kvp_is_ancestor_of_checkout "$real" && return 1
  if _kvp_has_git_within_two_levels "$real"; then
    echo "  reason: '.git' found within nesting depth 2 of '$real' -- repository data must never be wiped" >&2
    return 1
  fi

  [ -n "$root" ] || return 1

  # r4 P0-2: an explicit root must PROVE it is a cache directory: either the
  # .kvp-cache-root-marker stamp (created by start-cache.sh at cache
  # initialization) or -- when unmarked -- a top-level listing made up
  # EXCLUSIVELY of the known LMCache/kvpersist entry set. A single unknown
  # entry means this is not a pure cache dir; refuse and list the entries so
  # the operator can inspect and mark the dir manually afterwards.
  if [ "$marked" = "1" ]; then
    :  # marker confirmed this dir behind
  else
    local _uk _uk_all=""
    _uk="$(_kvp_unknown_cache_entries "$real" || true)"
    if [ -n "$_uk" ]; then
      _uk_all="$(printf '%s' "$_uk" | tr '\n' ' ')"
      echo "  reason: '$real' is not a confirmed cache dir -- unknown top-level entries: $_uk_all" >&2
      echo "  inspect them; if this really is a dedicated cache dir, create the marker manually after inspection:" >&2
      echo "    touch '$real/.kvp-cache-root-marker'" >&2
      return 1
    fi
  fi

  # symlink escape: every broken-out prefix must stay inside root's resolved
  # path, again with a strict '/' boundary (r3 P1-2 point 2).
  rroot="$(readlink -f "$root")"
  probe="$p"
  while [ -n "$probe" ] && [ "$probe" != "/" ]; do
    if [ -L "$probe" ]; then
      abs="$(readlink -f "$probe")" || return 1
      if [ "$abs" != "$rroot" ] && [ "$abs" != "$root" ]; then
        case "$abs" in
          "$rroot"/*|"$root"/*) : ;;
          *) return 1 ;;
        esac
      fi
    fi
    probe="$(dirname "$probe")"
  done
  return 0
}

# --------------------------------------------------------------------------
# 3. Mount/filesystem check + refusal of network FS (7.2).
# --------------------------------------------------------------------------
mount_info_of() { findmnt -no SOURCE,FSTYPE --target "$1" 2>/dev/null | head -1 || echo "unknown unknown"; }
is_network_fs() { case "$2" in nfs|nfs4|cifs|smb|smb3|sshfs|9p|afs|glusterfs) return 0 ;; *) return 1 ;; esac; }

FINAL=()
declare -A RESOLVED_OF=() FS_OF=() MT_OF=() SRCCFG_OF=()
for t in "${TARGETS[@]}"; do
  if safe_target "$t"; then
    real="$(readlink -f "$t")"
    mi="$(mount_info_of "$real")"
    msrc="$(echo "$mi" | awk '{print $1}')"
    mfs="$(echo "$mi" | awk '{print $2}')"
    is_network_fs "$msrc" "$mfs" && die "refusing to clear a NETWORK filesystem ($mfs on $msrc) at $t -- cache payload must be on local NVMe"
    FINAL+=("$real")
    RESOLVED_OF["$real"]="$t"
    FS_OF["$real"]="$mfs"
    MT_OF["$real"]="$msrc"
    SRCCFG_OF["$real"]="${SRC_OF[$t]:-unknown}"
    note "approved cache target: $t -> $real ($mfs on $msrc, source: ${SRC_OF[$t]:-unknown})"
  else
    die "unsafe cache target refused: '$t' (must be a confirmed dedicated cache-data dir: KVP_CACHE_ROOT/record/marker; not a repo, model dir, '/' or a home)"
  fi
done
[ "${#FINAL[@]}" -gt 0 ] || die "no safe cache target to clear"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
# r3 P1-2 point 4: LIVE is overridable (KVP_LIVE_DIR) so test runs stage
# reports/cache-archives in tmp instead of littering the repo's live/.
LIVE="${KVP_LIVE_DIR:-$ROOT/live}"
mkdir -p "$LIVE"
REPORT="$LIVE/cache-reset-report-$STAMP.txt"

# --------------------------------------------------------------------------
# 4. Stop services first (docker containers AND host MP server).
# --------------------------------------------------------------------------
HOST_PIDS=""
# pid files inside the cache roots are only meaningful when the process is
# REALLY ALIVE and is really an lmcache/kvpersist server (r2 P0-3: a stale
# pid file must not block the migration, a live host MP server must).
_host_pid_live() { # _host_pid_live <pid> -> 0 when a matching server is alive
  local pid="$1" cmd
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || return 1
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  case "$cmd" in
    *lmcache*|*kvpersist*) return 0 ;;
    *) return 1 ;;
  esac
}
for t in "${FINAL[@]}"; do
  for pf in "$t/lmcache-server.pid" "$t/lmcache-server/lmcache-server.pid"; do
    [ -f "$pf" ] || continue
    _pid="$(tr -d '[:space:]' < "$pf" 2>/dev/null || true)"
    if _host_pid_live "$_pid"; then
      HOST_PIDS="$HOST_PIDS $_pid"
    else
      note "stale pid file ignored (process $_pid is gone): $pf"
    fi
  done
done
# independent detection: any live process running the LMCache MP server
for _pid in $(pgrep -f 'kvpersist\.launch|lmcache[[:space:]]+server' 2>/dev/null || true); do
  case " $HOST_PIDS " in *" $_pid "*) continue ;; esac
  _host_pid_live "$_pid" && HOST_PIDS="$HOST_PIDS $_pid"
done
HOST_PIDS="$(echo "$HOST_PIDS" | tr -s ' ' | tr -d ' ')"
RUNNING=""
command -v docker >/dev/null 2>&1 && \
  RUNNING="$(docker ps --format '{{.Names}}' | grep -E 'lmcache|vllm|mimo-pro|tp4' | tr '\n' ' ' || true)"

if [ -n "$RUNNING" ] || [ -n "$HOST_PIDS" ]; then
  if [ "${KVP_RESET_FORCE_STOP:-0}" != "1" ]; then
    echo "these services are still running:" >&2
    [ -n "$RUNNING" ] && echo "  containers: $RUNNING" >&2
    [ -n "$HOST_PIDS" ] && echo "  host LMCache/server pids: $HOST_PIDS" >&2
    die "stop everything first (bash scripts/stop-all.sh; bash scripts/stop-cache.sh) or re-run with KVP_RESET_FORCE_STOP=1"
  fi
  [ -x "$HERE/stop-all.sh" ] && { note "KVP_RESET_FORCE_STOP=1 -- running stop-all.sh"; bash "$HERE/stop-all.sh" || true; }
  [ -x "$HERE/stop-cache.sh" ] && bash "$HERE/stop-cache.sh" || true
  sleep 3
  RUNNING2=""
  command -v docker >/dev/null 2>&1 && \
    RUNNING2="$(docker ps --format '{{.Names}}' | grep -E 'lmcache|vllm|mimo-pro|tp4' || true)"
  HOST_PIDS2="$(pgrep -f 'kvpersist.launch|lmcache server' 2>/dev/null | tr '\n' ' ' || true)"
  [ -n "$RUNNING2" ] || [ -n "$HOST_PIDS2" ] && \
    die "services still running after forced stop (containers: '$RUNNING2'; hosts pids: '$HOST_PIDS2') -- aborting"
fi
note "all cache services confirmed stopped (docker+host)"
for t in "${FINAL[@]}"; do
  # r2 P0-3 fix: fuser is optional tooling -- its ABSENCE must not trip this
  # check. Only when fuser exists and somebody holds the dir do we refuse.
  if command -v fuser >/dev/null 2>&1; then
    if fuser -s "$t" 2>/dev/null; then
      die "a process still uses $t -- stop it and retry"
    fi
  fi
done

# --------------------------------------------------------------------------
# 5. Archive diagnostic log (spec 7: "logs required for diagnosing the
#    previous deployment" must NEVER be deleted) and whitelist it below.
# --------------------------------------------------------------------------
PRESERVED_ENTRIES=()
PRESERVED_DETAIL=()
for t in "${FINAL[@]}"; do
  for diag in lmcache-server.log lmcache-server.pid l2-adapter.rendered.json; do
    if [ -e "$t/$diag" ]; then
      PRESERVED_ENTRIES+=("$t/$diag")
      if [ "$diag" = "lmcache-server.log" ]; then
        cp -p "$t/$diag" "$LIVE/lmcache-server-pre-reset-$STAMP.log" 2>/dev/null || true
        PRESERVED_DETAIL+=("$t/$diag -> archived to live/lmcache-server-pre-reset-$STAMP.log (original kept in place)")
      else
        PRESERVED_DETAIL+=("$t/$diag (kept in place)")
      fi
    fi
  done
  if compgen -G "$t/crash-*" > /dev/null 2>&1; then
    for c in "$t"/crash-*; do
      PRESERVED_ENTRIES+=("$c")
      PRESERVED_DETAIL+=("$c (crash evidence, kept in place)")
    done
  fi
done
note "preserved diagnostic artifacts: ${#PRESERVED_ENTRIES[@]} entries"

# --------------------------------------------------------------------------
# 6. Clear the cache payload on the RESOLVED path, whitelist diagnostics,
#    then VERIFY the directory actually is empty of payload (r2 P0-3: a
#    symlinked root previously reported a fake "cleared" because find on the
#    symlink itself matched nothing).
# --------------------------------------------------------------------------
CLEARED_REPORT=()
LEFTOVER=0
for t in "${FINAL[@]}"; do
  before="$(du -sb "$t" 2>/dev/null | cut -f1 || echo 0)"
  kept=0 removed=0
  # delete everything except the whitelisted diagnostic artifacts
  if [ -d "$t" ]; then
    for entry in "$t"/* "$t"/.[!.]* "$t"/..?*; do
      [ -e "$entry" ] || continue
      base="$(basename "$entry")"
      case "$base" in
        lmcache-server.log|lmcache-server.pid|l2-adapter.rendered.json|crash-*|\.kvp-cache-root-marker)
          kept=$((kept+1)); continue ;;
      esac
      rm -rf -- "$entry" && removed=$((removed+1))
    done
  fi
  after="$(du -sb "$t" 2>/dev/null | cut -f1 || echo 0)"
  CLEARED_REPORT+=("path: $t (source: ${SRCCFG_OF[$t]:-unknown}, fs: ${FS_OF[$t]:-unknown}, mount: ${MT_OF[$t]:-unknown})"
                   "  configured_as: ${RESOLVED_OF[$t]:-unknown}"
                   "  bytes_before: $before"
                   "  bytes_after: $after"
                   "  removed_entries: $removed"
                   "  kept_diagnostic_entries: $kept")
  # post-clear verification: only whitelisted diagnostics may remain
  non_diag=0
  for entry in "$t"/* "$t"/.[!.]* "$t"/..?*; do
    [ -e "$entry" ] || continue
    base="$(basename "$entry")"
    case "$base" in
      lmcache-server.log|lmcache-server.pid|l2-adapter.rendered.json|crash-*|\.kvp-cache-root-marker) continue ;;
    esac
    non_diag=$((non_diag+1))
  done
  if [ "$non_diag" != "0" ]; then
    LEFTOVER=1
    note "LEFTOVER: $non_diag unexpected entries remain in $t -- DO NOT trust this reset" >&2
  fi
done

# --------------------------------------------------------------------------
# 7. Reset report (spec 7.4).
# --------------------------------------------------------------------------
NEW_NAMESPACE="mimo-pro (empty cache; namespace derived from model identity at next start)"
CUSTOM_BUILD_ID="$(python3 - <<PYEOF 2>/dev/null || echo "unavailable (custom image not deployed yet)"
import json, pathlib
p = pathlib.Path("$ROOT/live/image-identity.env")
if not p.exists():
    raise SystemExit("unavailable (build-image.sh not run yet)")
print(p.read_text().replace(chr(10), "; "))
PYEOF
)"
{
  echo "=== LMCache persistent-cache reset report ==="
  echo "time_utc: $STAMP"
  echo "confirmed_by: KVP_RESET_CONFIRM=FULL-RESET"
  echo "force_stop: ${KVP_RESET_FORCE_STOP:-0}"
  echo "host_pids_stopped: ${HOST_PIDS:-none}"
  echo "containers_stopped: ${RUNNING:-none}"
  echo
  echo "--- cleared paths ---"
  printf '%s\n' "${CLEARED_REPORT[@]}"
  echo
  echo "--- preserved (spec 7: never delete) ---"
  if [ "${#PRESERVED_DETAIL[@]}" -gt 0 ]; then
    printf '%s\n' "${PRESERVED_DETAIL[@]}"
  else
    echo "(no diagnostic artifacts were present in any cache target)"
  fi
  echo "  model weights, model config, application data, recipes, source code,"
  echo "  LMCache build artifacts, unrelated NVMe files, the NVMe mount itself"
  echo "  -- all preserved (only within the confirmed cache root)."
  echo
  echo "--- post-clear verification ---"
  echo "leftover_non_diagnostic_entries: $LEFTOVER (0 = clean)"
  echo
  echo "--- next cache identity (spec 7.4) ---"
  echo "new_namespace: $NEW_NAMESPACE"
  echo "custom_lmcache_build: $CUSTOM_BUILD_ID"
  echo
  echo "--- filesystem (spec 7.2) ---"
  for t in "${FINAL[@]}"; do
    echo "$t: source=${MT_OF[$t]:-unknown} fs=${FS_OF[$t]:-unknown}"
  done
} > "$REPORT"

note "report: $REPORT"
[ "$LEFTOVER" = "0" ] || die "cache payload NOT fully cleared (leftovers remain) -- DO NOT continue; inspect $REPORT"
note "DONE -- next start initializes an EMPTY persistent cache (no old entries reachable)"
