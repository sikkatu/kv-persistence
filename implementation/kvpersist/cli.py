"""Admin CLI for the persistent KV cache — status, budget, namespace and purge.
Reads the runtime configuration (env + configs/), scans the cache root and
reports usage. The cache layout is FLAT: the L2 adapter writes `*.bin` files
directly under `KVP_CACHE_ROOT`, and the identity namespace is the file-name
prefix (the sanitized model name); legacy `kvp-*` namespace directories are
also counted if present. Deletes happen ONLY inside the cache root and only on
explicit user action (purge / emergency-evict), never automatically.
Commands:
    status   JSON report: usage vs budget, per-namespace bytes, disk free
    purge    [--namespace NAME | --all | --tmp] [--yes] explicit manual cleanup
    quota    show/set per-namespace quota (set writes quotas.json in cache root)
    emergency-evict  oldest cache files when physical free space < 5 GB
Exit codes: 0 ok, 1 error, 2 refused (missing --yes / path safety).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

from .budget import GB, BudgetLedger, BudgetError

# Safety rail: purge only ever touches a directory that lives strictly inside
# the configured cache root and matches our namespace prefix.
_NS_PREFIX = "kvp-"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def _config_value(name: str) -> str:
    """Effective config value: process env wins, then the repo env files.

    Review round 13: the CLI read ONLY os.environ, so `emergency-evict` in a
    cron job (no exported env) failed with "KVP_CACHE_ROOT is not set" even
    though docs advise running it from cron. Fall back to the same config
    loader the scripts use (env wins over files -- the documented contract).
    """
    v = os.environ.get(name, "").strip()
    if v:
        return v
    try:
        from .config import load_config
        return (load_config(_repo_root()) or {}).get(name, "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _cache_root() -> str:
    root = _config_value("KVP_CACHE_ROOT")
    if not root:
        print("KVP_CACHE_ROOT is not set (see configs/generic/cache.env)", file=sys.stderr)
        raise SystemExit(1)
    return os.path.realpath(root)


def _safe_namespace_dir(root: str, ns: str) -> str:
    if not ns.startswith(_NS_PREFIX) or "/" in ns or ".." in ns or os.sep in ns:
        print(f"refusing: {ns!r} is not a cache namespace", file=sys.stderr)
        raise SystemExit(2)
    path = os.path.realpath(os.path.join(root, ns))
    if not path.startswith(root + os.sep):
        print(f"refusing: {path} escapes cache root {root}", file=sys.stderr)
        raise SystemExit(2)
    return path


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _tmp_files(root: str) -> list:
    """Crash leftovers: ``<final>.tmp.<uuid>`` files the adapter never renamed.

    Upstream nixl_store_dynamic writes to ``<file_path>.tmp.<uuid>`` and
    atomically renames on success; only a CLEAN shutdown sweeps the leftovers
    (``cleanup_temp_files``). After a crash they stay forever, invisible to the
    cache index and to the naive ``*.bin`` scan -- so ``status``/``purge``/
    ``emergency-evict`` would silently under-report and under-free. Review fix:
    surface them everywhere, and let purge/delete remove them on request.
    """
    out = []
    if not os.path.isdir(root):
        return out
    pat = re.compile(r"^(?P<final>.+\.bin)\.tmp\.[0-9a-f]{32}$")
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if not (os.path.isfile(p) and ".tmp." in name):
            continue
        m = pat.match(name)
        try:
            out.append((p, m.group("final") if m else name))
        except OSError:
            pass
    return out


# A ``*.tmp.<uuid>`` file is safe to delete only when no write is in flight.
# TWO guards, strongest first:
#
# 1. ACTIVE-WRITE REGISTRY (authoritative): ``kvpersist.tmp_guard`` keeps a
#    marker per in-flight store (installed by kvpersist.launch around the
#    adapter's tmp-write + atomic-rename). A tmp file whose final name is in
#    the registry is NEVER deleted -- for however long the write runs.
# 2. mtime STALE WINDOW (fallback for unpatched writers / crashed stores):
#    tmp files modified within _TMP_ACTIVE_WINDOW_SEC are skipped. This is a
#    HEURISTIC -- a write longer than the window can still be hit IF the
#    writer does not use the registry. The registry removes that case; the
#    window remains as a second net.
#
# The race between "check" and "delete" is closed by the registry for live
# writes (the marker exists before the tmp file is created and is removed only
# after the rename). For dead writers the window is the only filter.
_TMP_ACTIVE_WINDOW_SEC = 15 * 60


def _tmp_files_safe_to_delete(root: str) -> list:
    """Crash leftovers that are NOT an in-flight write.

    Excludes any tmp file whose final name is registered by
    ``kvpersist.tmp_guard`` (live write) or modified within the stale window
    (unpatched writer / recent crash). The registry is authoritative; the
    window is the fallback.
    """
    import time as _time

    try:
        from . import tmp_guard

        active = tmp_guard.active_final_names(root)
    except Exception:  # noqa: BLE001 - never break the CLI over the guard
        active = set()

    safe, now = [], _time.time()
    for p, final in _tmp_files(root):
        if os.path.basename(final) in active:
            continue  # live write -- never delete
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            continue
        if age < _TMP_ACTIVE_WINDOW_SEC:
            continue  # recent -- heuristic net for unpatched writers
        safe.append((p, final))
    return safe


def _real_cache_files(root: str) -> list:
    """Actual L2 cache data files the LMCache adapter writes.

    The nixl_store_dynamic adapter writes FLAT files
    ``<safe_model_name>_<kv_rank:08x>_<group:x>_<chunkhash>.bin`` directly into
    the cache root -- NOT into per-namespace subdirectories. The CLI must account
    for these files, otherwise used_bytes is 0 and purge/emergency-evict
    delete nothing (review: "kvpersist.cli does not see the real cache").

    Review round 13: the model name may itself contain ``_`` (e.g. ``my_model``),
    so the namespace must be parsed with a RIGHT-anchored regex, not by
    splitting at the first ``_`` (which truncated ``my_model`` to ``my``).
    Returns a list of ``(path, namespace)`` tuples.
    """
    out = []
    if not os.path.isdir(root):
        return out
    # rank is exactly 8 hex chars, group is hex, chunk hash is 64 hex chars.
    # Review round 15: the kv-persistence filename patch appends the cache_salt
    # (`_<hash>_<salt>.bin`), so an optional trailing salt field is allowed.
    pat = re.compile(
        r"^(?P<model>.+)_(?P<rank>[0-9a-f]{8})_(?P<group>[0-9a-f]+)"
        r"_(?P<hash>[0-9a-f]{64})(?:_(?P<salt>[^/\\@]+))?\.bin$"
    )
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if not (os.path.isfile(p) and name.endswith(".bin")):
            continue
        m = pat.match(name)
        ns = m.group("model") if m else name[:-4]
        try:
            out.append((p, ns))
        except OSError:
            pass
    return out


def _budget_bytes() -> int:
    # Review round 13: default was 750 GB here but 1500 GB everywhere else
    # (cache.env, docs). The default must match the documented budget.
    gb = float(_config_value("KVP_CACHE_BUDGET_GB") or "1500")
    return int(gb * GB)


def cmd_status(_args) -> int:
    root = _cache_root()
    budgets_json = os.path.join(root, "quotas.json")
    quotas = {}
    if os.path.isfile(budgets_json):
        with open(budgets_json, "r", encoding="utf-8") as fh:
            quotas = {k: int(v) for k, v in json.load(fh).items()}
    ledger = BudgetLedger(_budget_bytes(), quotas=quotas)
    sizes = {}
    file_count = 0
    # Real L2 layout (review round 13): the nixl_store_dynamic adapter writes
    # FLAT *.bin files into the cache root. The file-name prefix is the
    # sanitized model_name (the LMCache identity namespace), so group by that
    # prefix to report per-namespace usage that matches reality.
    real_files = _real_cache_files(root)
    for p, ns in real_files:
        ns = ns or "(unnamed)"
        try:
            sizes[ns] = sizes.get(ns, 0) + os.path.getsize(p)
        except OSError:
            pass
        file_count += 1
    # Crash leftovers (*.bin.tmp.<uuid>) are counted SEPARATELY: they are not
    # reachable by the cache index and never returned as KV, but they do
    # consume disk. Surfacing them here is what lets an operator see (and
    # purge) space that upstream only sweeps on a clean shutdown.
    tmp_files = _tmp_files(root)
    tmp_bytes = 0
    for p, _final in tmp_files:
        try:
            tmp_bytes += os.path.getsize(p)
        except OSError:
            pass
    # Legacy per-namespace directories (older layouts / admin bookkeeping).
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            ns_dir = os.path.join(root, name)
            if os.path.isdir(ns_dir) and name.startswith(_NS_PREFIX):
                sizes[name] = sizes.get(name, 0) + _dir_size(ns_dir)
                for _dp, _dn, fn in os.walk(ns_dir):
                    file_count += len(fn)
    ledger.sync_from_sizes(sizes)
    rep = ledger.report()
    rep.update({
        "cache_root": root,
        "cache_files": file_count,
        "disk_free_bytes": shutil.disk_usage(root).free if os.path.isdir(root) else None,
        "namespaces": sorted(sizes),
        "tmp_files": len(tmp_files),
        "tmp_files_bytes": tmp_bytes,
        "note": "L2 adapter writes flat *.bin files; namespaces are "
                "model_name prefixes of those files. tmp_files are crash "
                "leftovers (*.bin.tmp.<uuid>) -- invisible to the cache "
                "index, never returned as KV, but they do consume disk; "
                "remove with `purge --all --yes` or `purge --tmp --yes`. "
                "Files modified within the active-write window are kept "
                "(they may be an in-flight store).",
    })
    print(json.dumps(rep, indent=2))
    return 0


def cmd_purge(args) -> int:
    root = _cache_root()
    if not args.yes:
        print("refusing to delete anything without --yes", file=sys.stderr)
        return 2
    # Accept the namespace from either form: --namespace=NAME (or
    # --namespace NAME when NAME has no leading dash) OR the positional
    # argument. Real namespaces look like `--models--MyModel--<hash>` and
    # argparse rejects `--namespace --models--...` as an unknown option, so
    # the positional form (and the `=` form) is what the docs recommend.
    ns = getattr(args, "namespace", None) or getattr(args, "namespace_pos", None)
    freed = 0
    targets = []
    if args.all:
        # Real L2 layout: flat *.bin files in the cache root.
        for p, _ns in _real_cache_files(root):
            try:
                freed += os.path.getsize(p)
                os.remove(p)
                targets.append(p)
            except OSError as exc:
                print(f"warning: could not delete {p}: {exc}", file=sys.stderr)
        # Crash leftovers: sweep them too on --all (upstream only does this on
        # a clean shutdown, so after a crash they linger and eat disk).
        # Active writes (young *.tmp.*) are skipped -- deleting a file the
        # adapter is still writing into would corrupt a store in progress.
        for p, _final in _tmp_files_safe_to_delete(root):
            try:
                freed += os.path.getsize(p)
                os.remove(p)
                targets.append(p)
            except OSError as exc:
                print(f"warning: could not delete {p}: {exc}", file=sys.stderr)
        try:
            from . import tmp_guard

            swept = tmp_guard.sweep_stale_markers(root)
            if swept:
                print(
                    f"note: swept {swept} stale active-write marker(s) "
                    f"(crash leftovers)",
                    file=sys.stderr,
                )
        except Exception:  # noqa: BLE001 - never break purge over the guard
            pass
        skipped_active = len(_tmp_files(root)) - len(_tmp_files_safe_to_delete(root))
        if skipped_active:
            print(
                f"note: kept {skipped_active} *.tmp.* file(s) -- in-flight "
                f"(active-write registry) or modified within "
                f"{_TMP_ACTIVE_WINDOW_SEC}s; rerun later to sweep them",
                file=sys.stderr,
            )
        # Legacy per-namespace dirs.
        for n in sorted(os.listdir(root)):
            ns_dir = os.path.join(root, n)
            if n.startswith(_NS_PREFIX) and os.path.isdir(ns_dir):
                freed += _dir_size(ns_dir)
                shutil.rmtree(ns_dir, ignore_errors=True)
                targets.append(ns_dir)
    elif getattr(args, "tmp", False):
        # --tmp: remove ONLY the crash leftovers, keep every cache file.
        # Active writes (young *.tmp.*) are skipped (review fix).
        for p, _final in _tmp_files_safe_to_delete(root):
            try:
                freed += os.path.getsize(p)
                os.remove(p)
                targets.append(p)
            except OSError as exc:
                print(f"warning: could not delete {p}: {exc}", file=sys.stderr)
        skipped_active = len(_tmp_files(root)) - len(_tmp_files_safe_to_delete(root))
        if skipped_active:
            print(
                f"note: kept {skipped_active} *.tmp.* file(s) -- in-flight "
                f"(active-write registry) or modified within "
                f"{_TMP_ACTIVE_WINDOW_SEC}s; rerun later to sweep them",
                file=sys.stderr,
            )
    elif ns:
        # Review round 13: EXACT namespace match only. The old substring rule
        # (`ns in prefix`) deleted `MiMo` AND `MiMo-V2.6-Pro` -- and `--namespace -`
        # matched every file. The namespace is the sanitized model_name (the
        # part before the trailing _<rank8>_<group>_<hash64>.bin), so equality
        # on it is unambiguous. Nothing found -> non-zero exit and a clear
        # message, never a silent "purged 0".
        found = False
        for p, fns in _real_cache_files(root):
            if fns != ns:
                continue
            try:
                freed += os.path.getsize(p)
                os.remove(p)
                targets.append(p)
                found = True
            except OSError as exc:
                print(f"warning: could not delete {p}: {exc}", file=sys.stderr)
        if ns.startswith(_NS_PREFIX):
            p = _safe_namespace_dir(root, ns)
            if os.path.isdir(p):
                freed += _dir_size(p)
                shutil.rmtree(p, ignore_errors=True)
                targets.append(p)
                found = True
        if not found:
            print(f"purge: no cache items match namespace {ns!r} "
                  "(exact match required -- see `status` for the namespace "
                  "names in use)", file=sys.stderr)
            return 1
    else:
        print("specify --namespace NS or --all", file=sys.stderr)
        return 2
    for path in targets:
        print(f"purged {path}")
    print(json.dumps({"purged_items": len(targets), "freed_bytes_estimated": freed}, indent=2))
    return 0


def cmd_quota(args) -> int:
    root = _cache_root()
    path = os.path.join(root, "quotas.json")
    if args.set_namespace and args.gb is not None:
        quotas = {}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                quotas = json.load(fh)
        quotas[args.set_namespace] = int(args.gb * GB)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(quotas, fh, indent=2, sort_keys=True)
        print(json.dumps({"updated": args.set_namespace, "quota_gb": args.gb}, indent=2))
        return 0
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            print(json.dumps(json.load(fh), indent=2, sort_keys=True))
    else:
        print("{}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kvp-admin", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="usage vs budget report")
    sub.add_parser("free", help="physical free space on cache filesystem")
    e = sub.add_parser("emergency-evict", help="forced LRU eviction when physical free space < 5 GB (DESTRUCTIVE, needs --yes)")
    e.add_argument("--yes", action="store_true", help="required to actually delete")
    p = sub.add_parser("purge", help="manual cleanup (explicit, never automatic)")
    p.add_argument("--namespace",
                   help="namespace to purge (EXACT match). Namespaces may start "
                        "with '-' (e.g. --models--MyModel--<hash>), so pass the "
                        "value as --namespace=NAME or as a positional argument.")
    p.add_argument("namespace_pos", nargs="?", default=None,
                   help="same as --namespace, positional form (safe for names "
                        "starting with '-')")
    p.add_argument("--all", action="store_true")
    p.add_argument("--tmp", action="store_true",
                   help="remove ONLY crash leftovers (*.bin.tmp.<uuid>), keep "
                        "every cache file. Useful after a hard kill: upstream "
                        "sweeps those files only on a clean shutdown. Files "
                        "modified within the active-write window (15 min) are "
                        "kept as a HEURISTIC guard (mtime-based) against "
                        "deleting an in-flight write.")
    p.add_argument("--yes", action="store_true", help="required to actually delete")
    q = sub.add_parser("quota", help="show or set per-namespace quota")
    q.add_argument("--set-namespace")
    q.add_argument("--gb", type=float)
    ei = sub.add_parser("effective-identity",
                        help="print the effective engine identity (KVP_* lines) from "
                             "the environment; shared resolver for the launchers "
                             "and the identity gate")
    ei.add_argument("--model-dir", default="",
                    help="local model dir (resolves KV_CACHE_DTYPE=auto via config.json)")
    mn = sub.add_parser("model-name",
                        help="print the LMCache model_name enforcing the identity namespace")
    mn.add_argument("--discriminator", default="",
                    help="optional per-tenant discriminator (no '/' or '@')")
    args = ap.parse_args(argv)
    try:
        return {"status": cmd_status, "purge": cmd_purge, "quota": cmd_quota,
                "free": cmd_free, "emergency-evict": cmd_emergency_evict,
                "model-name": cmd_model_name,
                "effective-identity": cmd_effective_identity}[args.cmd](args)
    except BudgetError as exc:
        print(f"budget error: {exc}", file=sys.stderr)
        return 1


def cmd_free(args):
    """Report PHYSICAL free space on the cache filesystem (review 2B)."""
    import shutil, os
    root = _config_value("KVP_CACHE_ROOT")
    if not root or not os.path.isdir(root):
        print("KVP_CACHE_ROOT not set or missing", file=sys.stderr)
        return 1
    du = shutil.disk_usage(root)
    free_gb = du.free / (1 << 30)
    total_gb = du.total / (1 << 30)
    budget_gb = float(_config_value("KVP_CACHE_BUDGET_GB") or "0")
    print(json.dumps({
        "cache_root": root,
        "physical_free_gb": round(free_gb, 2),
        "physical_total_gb": round(total_gb, 2),
        "budget_gb": budget_gb,
        "emergency_threshold_gb": 5.0,
        "needs_emergency_eviction": free_gb < 5.0,
    }, indent=2))
    return 0


def cmd_emergency_evict(args):
    """Review 2B: forced eviction when PHYSICAL free space drops below
    the emergency threshold, regardless of the logical budget. Deletes the
    OLDEST cache items inside KVP_CACHE_ROOT -- flat `*.bin` cache files first
    (the current layout), then legacy `kvp-*` namespace directories -- until
    physical free space recovers above the threshold (or nothing left to
    delete). Crash leftovers (`*.tmp.*`) and in-flight writes are skipped by
    the same guard as `purge --tmp`.

    Ordering note (review fix): items are ordered by FILE MTIME (oldest
    first) -- that is FIFO, NOT true LRU. Real LRU needs last-ACCESS time,
    which is not tracked on disk; mtime is the creation time of the file and
    never changes on read. So "evict the oldest" here means "evict what was
    stored longest ago", not "evict what was used longest ago". Callers must
    not assume this matches the value-based retention policy either --
    retention.py (see docs/eviction-and-deduplication.md) is a separate
    planner, not wired into this path.
    Requires --yes: this is DESTRUCTIVE and wipes cache data (coarser than
    per-entry eviction)."""
    import shutil, os
    if not args.yes:
        print("REFUSED: emergency-evict deletes cache data (destructive)."
              "\nRe-run with --yes after confirming you want to drop the oldest cache"
              "\nentries, or use 'purge --namespace=NAME --yes' for targeted cleanup.",
              file=sys.stderr)
        return 2
    root = _config_value("KVP_CACHE_ROOT")
    if not root or not os.path.isdir(root):
        print("KVP_CACHE_ROOT not set or missing", file=sys.stderr)
        return 1
    threshold_gb = 5.0
    du = shutil.disk_usage(root)
    free_gb = du.free / (1 << 30)
    if free_gb >= threshold_gb:
        print(json.dumps({"action": "none", "physical_free_gb": round(free_gb, 2),
                          "threshold_gb": threshold_gb}))
        return 0
    # Sort cache items by mtime: oldest first (closest to LRU semantics).
    # Real L2 layout: flat *.bin files. Legacy: kvp-* namespace directories.
    items = []
    for p, _ns in _real_cache_files(root):
        try:
            items.append((os.path.getmtime(p), "file", p,
                          os.path.getsize(p)))
        except OSError:
            pass
    for n in sorted(os.listdir(root)):
        ns_dir = os.path.join(root, n)
        if n.startswith(_NS_PREFIX) and os.path.isdir(ns_dir):
            try:
                items.append((os.path.getmtime(ns_dir), "dir", ns_dir,
                              _dir_size(ns_dir)))
            except OSError:
                pass
    items.sort()
    freed = 0
    deleted = []
    for _mtime, kind, path, sz in items:
        du = shutil.disk_usage(root)
        if du.free / (1 << 30) >= threshold_gb:
            break
        try:
            if kind == "dir":
                shutil.rmtree(path)
            else:
                os.remove(path)
            freed += sz
            deleted.append(os.path.basename(path))
        except OSError as exc:
            print(f"warning: could not delete {path}: {exc}", file=sys.stderr)
    du2 = shutil.disk_usage(root)
    print(json.dumps({
        "action": "emergency_evict",
        "deleted_namespaces": deleted,
        "freed_bytes_estimated": freed,
        "physical_free_gb_after": round(du2.free / (1 << 30), 2),
        "threshold_gb": threshold_gb,
    }, indent=2))
    return 0 if du2.free / (1 << 30) >= threshold_gb else 1


def resolve_effective_identity(model_dir: str = "") -> dict:
    """Resolve the EFFECTIVE engine identity from the environment (review 23).

    Single source of truth for every consumer:
      * the recipe launchers (namespace via `model-name`, and the env file they
        persist for the manual verify-engine-identity.sh run),
      * run-vllm-with-persist.sh,
      * the identity gate itself (via the exported KVP_* names).

    Reads the operators' usual knobs (KV_CACHE_DTYPE / MAX_MODEL_LEN /
    KV_PERSIST_BLOCK_SIZE / TP_SIZE / SERVED_MODEL_NAME) and the already-prefixed
    KVP_* forms (KVP_* wins, so a consumer can pre-pin a value). `auto` for the
    KV dtype is resolved to the MODEL's dtype from <model_dir>/config.json
    (torch_dtype, then dtype; moe_router_dtype is deliberately NOT a fallback --
    that is the ROUTER dtype, not the KV dtype).
    """
    def pick(*names: str, default: str = "") -> str:
        for n in names:
            v = os.environ.get(n, "").strip()
            if v:
                return v
        return default
    kv_dtype = pick("KVP_KV_CACHE_DTYPE", "KV_CACHE_DTYPE", default="auto").lower()
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
    return {
        "KVP_SERVED_MODEL_NAME": pick("KVP_SERVED_MODEL_NAME", "SERVED_MODEL_NAME",
                                      default="mimo-pro"),
        "KVP_KV_CACHE_DTYPE": kv_dtype,
        "KVP_MAX_MODEL_LEN": pick("KVP_MAX_MODEL_LEN", "MAX_MODEL_LEN",
                                  default="1048576"),
        "KVP_BLOCK_SIZE": pick("KVP_BLOCK_SIZE", "KV_PERSIST_BLOCK_SIZE", "BLOCK_SIZE",
                               default="16"),
        "KVP_TP_SIZE": pick("KVP_TP_SIZE", "TP_SIZE", default="4"),
    }


def cmd_effective_identity(args) -> int:
    """Print the effective engine identity as `KVP_X=value` lines (shell-safe:
    values contain no spaces or quotes). Consumers `source` this output or hand
    it to `python3 -m kvpersist.cli model-name` via the environment so namespace
    derivation and the identity gate always see the SAME values."""
    eff = resolve_effective_identity(args.model_dir)
    for k, v in eff.items():
        print(f"{k}={v}")
    return 0


def cmd_model_name(args) -> int:
    """Print the LMCache model_name that enforces the identity namespace.

    Used by scripts/run-vllm-with-persist.sh to mount the model under a
    namespace-coded path so that `--model <path>` (which vLLM reports as
    `model_config.model`, the value LMCache derives ObjectKey and the L2 file
    name from) is UNIQUE per identity. This is what makes the identity
    namespace effective at the LMCache STORAGE level in the real vLLM path --
    without it every deployment would register as the same model name and
    share (or collide on) the same L2 objects.
    """
    root = os.environ.get("KVP_REPO_ROOT", "")
    if not root:
        here = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        root = here
    model_cfg = os.environ.get("KVP_MODEL_CONFIG", "").strip()
    paths = []
    # Layer ALL config sources in the same order the bash loader seeds them
    # (later file wins per key). Review round 13: LMCACHE_L2_SERDE lives in
    # configs/generic/lmcache-server.env, not in identity.env -- reading only
    # identity.env made l2_serde invisible to the namespace (none/fp8 gave the
    # SAME name). The generic files are layered BELOW the model identity files.
    import glob
    gen = os.path.join(root, "configs", "generic")
    paths += [os.path.join(gen, "cache.env"),
              os.path.join(gen, "lmcache-server.env")]
    if model_cfg:
        dirs = [os.path.join(root, "configs", model_cfg)]
    else:
        # every model config dir we can find; the last one wins per key
        dirs = sorted(d for d in glob.glob(os.path.join(root, "configs", "*"))
                      if os.path.basename(d) != "generic")
    for d in dirs:
        paths.append(os.path.join(d, "identity.env"))
    paths += [os.path.join(gen, "cache.env.local"),
              os.path.join(gen, "lmcache-server.env.local")]
    for d in dirs:
        paths.append(os.path.join(d, "identity.env.local"))
    try:
        from .identity import identity_from_env_files
        ident = identity_from_env_files(*paths)
        base = os.environ.get("KVP_SERVED_MODEL_NAME", "").strip()
        disc = args.discriminator or ""
        print(ident.lmcache_model_name(base, disc))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"cannot derive LMCache model_name: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())