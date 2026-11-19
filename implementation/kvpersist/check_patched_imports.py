#!/usr/bin/env python3
"""Static import gate for a patched LMCache tree (no torch/vllm needed).
usage: check_patched_imports.py TREE FILE [FILE ...]  (FILEs relative to TREE)"""
import ast, pathlib, sys

EXEMPT_MODULES = {"lmcache.lmcache_native", "lmcache.lmcache_redis", "lmcache.lmcache_fs",
                  "lmcache.cuda_ops", "lmcache.c_ops", "lmcache._version"}
EXEMPT_PREFIXES = ("lmcache.lmcache_",)   # generated/native; also any module path containing _proto_gen

def resolve(root, mod):
    p = root.joinpath(*mod.split("."))
    if (p / "__init__.py").exists():
        return p / "__init__.py", True
    if p.with_suffix(".py").exists():
        return p.with_suffix(".py"), False
    if p.is_dir():                      # PEP 420 namespace package
        return p, True
    return None, False

def top_names(path):
    t = ast.parse(path.read_text())
    names, star = set(), False
    for n in ast.walk(t):
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
            for tg in tgts:
                names.update(x.id for x in ast.walk(tg) if isinstance(x, ast.Name))
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                if a.name == "*":
                    star = True
                else:
                    names.add((a.asname or a.name).split(".")[0])
    return names, star

def guarded_nodes(tree):
    """Imports inside try: ... except ImportError/Exception (fallback imports) are skipped."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Try) and any(
                h.type is None or "ImportError" in ast.dump(h.type) or "Exception" in ast.dump(h.type)
                for h in n.handlers):
            for b in n.body:
                out.update(id(x) for x in ast.walk(b))
    return out

def check(root, files):
    root, problems, cache = pathlib.Path(root), [], {}
    for rel in files:
        tree = ast.parse((root / rel).read_text())
        guarded = guarded_nodes(tree)
        for n in ast.walk(tree):
            if not (isinstance(n, ast.ImportFrom) and n.level == 0 and n.module
                    and n.module.split(".")[0] in ("lmcache", "tests")):
                continue
            if (id(n) in guarded or n.module in EXEMPT_MODULES
                    or n.module.startswith(EXEMPT_PREFIXES) or "_proto_gen" in n.module):
                continue
            mf, ispkg = resolve(root, n.module)
            if mf is None:
                problems.append(f"{rel}:{n.lineno}: module not found: {n.module}")
                continue
            if mf not in cache:
                cache[mf] = (set(), False) if mf.is_dir() else top_names(mf)
            names, star = cache[mf]
            for a in n.names:
                if star or a.name == "*" or a.name in names:
                    continue
                if ispkg and resolve(root, f"{n.module}.{a.name}")[0]:
                    continue
                problems.append(f"{rel}:{n.lineno}: '{a.name}' is not defined in {n.module}")
    return problems

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("BUILD REFUSED (stale patch/test import): no files to check "
              "(empty scope) -- refusing to skip the gate", file=sys.stderr)
        sys.exit(1)
    probs = check(sys.argv[1], sys.argv[2:])
    for p in probs:
        print("BUILD REFUSED (stale patch/test import):", p, file=sys.stderr)
    sys.exit(1 if probs else 0)