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


def _real_cache_files(root: str) -> list:
    """Actual L2 cache data files the LMCache adapter writes.

    The nixl_store_dynamic adapter writes FLAT files
    ``<safe_model_name>_<kv_rank>_<group>_<chunkhash>.bin`` directly into the
    cache root -- NOT into per-namespace subdirectories. The CLI must account
    for these files, otherwise used_bytes is 0 and purge/emergency-evict
    delete nothing (review: "kvpersist.cli does not see the real cache").
    """
    out = []
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if os.path.isfile(p) and name.endswith(".bin"):
            try:
                out.append(p)
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
    for p in real_files:
        name = os.path.basename(p)
        ns = name.split("_", 1)[0] or "(unnamed)"
        try:
            sizes[ns] = sizes.get(ns, 0) + os.path.getsize(p)
        except OSError:
            pass
        file_count += 1
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
        "note": "L2 adapter writes flat *.bin files; namespaces are "
                "model_name prefixes of those files",
    })
    print(json.dumps(rep, indent=2))
    return 0


def cmd_purge(args) -> int:
    root = _cache_root()
    if not args.yes:
        print("refusing to delete anything without --yes", file=sys.stderr)
        return 2
    freed = 0
    targets = []
    if args.all:
        # Real L2 layout: flat *.bin files in the cache root.
        for p in _real_cache_files(root):
            try:
                freed += os.path.getsize(p)
                os.remove(p)
                targets.append(p)
            except OSError as exc:
                print(f"warning: could not delete {p}: {exc}", file=sys.stderr)
        # Legacy per-namespace dirs.
        for n in sorted(os.listdir(root)):
            ns_dir = os.path.join(root, n)
            if n.startswith(_NS_PREFIX) and os.path.isdir(ns_dir):
                freed += _dir_size(ns_dir)
                shutil.rmtree(ns_dir, ignore_errors=True)
                targets.append(ns_dir)
    elif args.namespace:
        # Match the namespace against the sanitized model_name file prefix and
        # against legacy kvp-* directories. The prefix of a flat file is the
        # part before the first '_' (safe_model_name), e.g.
        # "mimo-pro--a84fadf1b808e8cfa5ef24f7a2924fa9_...bin".
        ns = args.namespace
        for p in _real_cache_files(root):
            prefix = os.path.basename(p).split("_", 1)[0]
            if prefix == ns or ns in prefix:
                try:
                    freed += os.path.getsize(p)
                    os.remove(p)
                    targets.append(p)
                except OSError as exc:
                    print(f"warning: could not delete {p}: {exc}", file=sys.stderr)
        if ns.startswith(_NS_PREFIX):
            p = _safe_namespace_dir(root, ns)
            if os.path.isdir(p):
                freed += _dir_size(p)
                shutil.rmtree(p, ignore_errors=True)
                targets.append(p)
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
    p.add_argument("--namespace")
    p.add_argument("--all", action="store_true")
    p.add_argument("--yes", action="store_true", help="required to actually delete")
    q = sub.add_parser("quota", help="show or set per-namespace quota")
    q.add_argument("--set-namespace")
    q.add_argument("--gb", type=float)
    mn = sub.add_parser("model-name",
                        help="print the LMCache model_name enforcing the identity namespace")
    mn.add_argument("--discriminator", default="",
                    help="optional per-tenant discriminator (no '/' or '@')")
    args = ap.parse_args(argv)
    try:
        return {"status": cmd_status, "purge": cmd_purge, "quota": cmd_quota,
                "free": cmd_free, "emergency-evict": cmd_emergency_evict,
                "model-name": cmd_model_name}[args.cmd](args)
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
    """Review 2B: forced LRU-ish eviction when PHYSICAL free space drops below
    the emergency threshold, regardless of the logical budget. Deletes the
    oldest namespace directories inside KVP_CACHE_ROOT until physical free
    space recovers above the threshold (or nothing left to delete).
    Requires --yes: this is DESTRUCTIVE and wipes entire namespaces (coarser
    than per-entry eviction)."""
    import shutil, os
    if not args.yes:
        print("REFUSED: emergency-evict deletes whole namespaces (destructive)."
              "\nRe-run with --yes after confirming you want to drop the oldest cache"
              "\nnamespaces, or use 'purge --namespace <ns> --yes' for targeted cleanup.",
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
    for p in _real_cache_files(root):
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
    cfg_dir = os.path.join(root, "configs", model_cfg) if model_cfg else None
    paths = []
    if cfg_dir:
        paths.append(os.path.join(cfg_dir, "identity.env"))
        paths.append(os.path.join(cfg_dir, "identity.env.local"))
    else:
        # layer every model config dir we can find; the last one wins per key
        import glob
        for d in sorted(glob.glob(os.path.join(root, "configs", "*"))):
            paths.append(os.path.join(d, "identity.env"))
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