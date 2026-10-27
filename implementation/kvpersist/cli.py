"""Admin CLI for the persistent KV cache — status, budget, namespace and purge.

Reads the runtime configuration (env + configs/), scans only the cache root
namespace directories, and reports usage. Deletes happen ONLY inside the cache
root and only on explicit user action (purge), never automatically.

Commands:
    status   JSON report: usage vs budget, per-namespace bytes, disk free
    purge    [--namespace NS | --all] [--yes] explicit manual cleanup
    quota    show/set per-namespace quota (set writes quotas.json in cache root)

Exit codes: 0 ok, 1 error, 2 refused (missing --yes / path safety).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from .budget import GB, BudgetLedger, BudgetError

# Safety rail: purge only ever touches a directory that lives strictly inside
# the configured cache root and matches our namespace prefix.
_NS_PREFIX = "kvp-"


def _cache_root() -> str:
    root = os.environ.get("KVP_CACHE_ROOT", "").strip()
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


def _budget_bytes() -> int:
    gb = float(os.environ.get("KVP_CACHE_BUDGET_GB", "750"))
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
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            ns_dir = os.path.join(root, name)
            if os.path.isdir(ns_dir) and name.startswith(_NS_PREFIX):
                sizes[name] = _dir_size(ns_dir)
                for _dp, _dn, fn in os.walk(ns_dir):
                    file_count += len(fn)
    ledger.sync_from_sizes(sizes)
    rep = ledger.report()
    rep.update({
        "cache_root": root,
        "cache_files": file_count,
        "disk_free_bytes": shutil.disk_usage(root).free if os.path.isdir(root) else None,
        "namespaces": sorted(sizes),
    })
    print(json.dumps(rep, indent=2))
    return 0


def cmd_purge(args) -> int:
    root = _cache_root()
    if not args.yes:
        print("refusing to delete anything without --yes", file=sys.stderr)
        return 2
    if args.all:
        targets = [
            os.path.join(root, n) for n in sorted(os.listdir(root))
            if n.startswith(_NS_PREFIX) and os.path.isdir(os.path.join(root, n))
        ] if os.path.isdir(root) else []
    elif args.namespace:
        targets = [_safe_namespace_dir(root, args.namespace)]
    else:
        print("specify --namespace NS or --all", file=sys.stderr)
        return 2
    freed = 0
    for path in targets:
        freed += _dir_size(path)
        shutil.rmtree(path)
        print(f"purged {path}")
    print(json.dumps({"purged_dirs": len(targets), "freed_bytes_estimated": freed}, indent=2))
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
    sub.add_parser("emergency-evict", help="forced LRU eviction when physical free space < 5 GB")
    p = sub.add_parser("purge", help="manual cleanup (explicit, never automatic)")
    p.add_argument("--namespace")
    p.add_argument("--all", action="store_true")
    p.add_argument("--yes", action="store_true", help="required to actually delete")
    q = sub.add_parser("quota", help="show or set per-namespace quota")
    q.add_argument("--set-namespace")
    q.add_argument("--gb", type=float)
    args = ap.parse_args(argv)
    try:
        return {"status": cmd_status, "purge": cmd_purge, "quota": cmd_quota, "free": cmd_free, "emergency-evict": cmd_emergency_evict}[args.cmd](args)
    except BudgetError as exc:
        print(f"budget error: {exc}", file=sys.stderr)
        return 1


def cmd_free(args):
    """Report PHYSICAL free space on the cache filesystem (review 2B)."""
    import shutil, os
    root = os.environ.get("KVP_CACHE_ROOT", "")
    if not root or not os.path.isdir(root):
        print("KVP_CACHE_ROOT not set or missing", file=sys.stderr)
        return 1
    du = shutil.disk_usage(root)
    free_gb = du.free / (1 << 30)
    total_gb = du.total / (1 << 30)
    budget_gb = float(os.environ.get("KVP_CACHE_BUDGET_GB", "0"))
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
    """Review 2B: forced LRU-ish eviction when PHYSICAL free space drops below
    the emergency threshold, regardless of the logical budget. Deletes the
    oldest namespace directories inside KVP_CACHE_ROOT until physical free
    space recovers above the threshold (or nothing left to delete)."""
    import shutil, os
    root = os.environ.get("KVP_CACHE_ROOT", "")
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
    # Sort namespace dirs by mtime: oldest first (closest to LRU semantics)
    ns_dirs = sorted(
        [os.path.join(root, n) for n in os.listdir(root)
         if n.startswith("kvp-") and os.path.isdir(os.path.join(root, n))],
        key=lambda p: os.path.getmtime(p),
    )
    freed = 0
    deleted = []
    for ns in ns_dirs:
        du = shutil.disk_usage(root)
        if du.free / (1 << 30) >= threshold_gb:
            break
        sz = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, fnames in os.walk(ns)
            for f in fnames
        )
        try:
            shutil.rmtree(ns)
            freed += sz
            deleted.append(os.path.basename(ns))
        except OSError as exc:
            print(f"warning: could not delete {ns}: {exc}", file=sys.stderr)
    du2 = shutil.disk_usage(root)
    print(json.dumps({
        "action": "emergency_evict",
        "deleted_namespaces": deleted,
        "freed_bytes_estimated": freed,
        "physical_free_gb_after": round(du2.free / (1 << 30), 2),
        "threshold_gb": threshold_gb,
    }, indent=2))
    return 0 if du2.free / (1 << 30) >= threshold_gb else 1


if __name__ == "__main__":
    raise SystemExit(main())