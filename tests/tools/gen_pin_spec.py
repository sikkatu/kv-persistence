#!/usr/bin/env python3
"""Generate tests/unit/pin_server_spec.json from the PIN source (r22 B1-B6,
r23 D6.3 -- the generator is committed so the fixture can be re-created).

Usage (from the repo root, with the pin source checked out at /tmp/pin_extract):

    git -C <lmcache-repo> archive 07f1c6bb642f4772606a36eab730ae009ddce08f \
        lmcache/v1/multiprocess/config.py lmcache/v1/distributed/config.py \
        lmcache/v1/distributed/l2_adapters/config.py \
        lmcache/v1/mp_observability/config.py | tar -x -C /tmp/pin_extract
    python3 tests/tools/gen_pin_spec.py /tmp/pin_extract

The spec is NEVER hand-typed: it is extracted with an AST walk of the
add_argument() calls (flag, type, choices, default) from the pin source
(07f1c6bb), and the tests validate the server argv against it.
"""
from __future__ import annotations
import ast
import json
import pathlib
import sys

DEFAULT_PIN_SRC = "/tmp/pin_extract"
DEFAULT_OUT = pathlib.Path(__file__).resolve().parents[1] / "unit" / "pin_server_spec.json"

# the files the review names (lmcache/v1/{multiprocess,distributed,
# distributed/l2_adapters,mp_observability}/config.py)
FILES = {
    "lmcache/v1/multiprocess/config.py": "mp",
    "lmcache/v1/distributed/config.py": "dist",
    "lmcache/v1/distributed/l2_adapters/config.py": "l2",
    "lmcache/v1/mp_observability/config.py": "obs",
}


def extract_flags(path: pathlib.Path):
    """Walk the AST for add_argument(...) calls and record flag, type, choices."""
    tree = ast.parse(path.read_text())
    out = []

    class V(ast.NodeVisitor):
        def visit_Call(self, node):
            fn = node.func
            name = ""
            if isinstance(fn, ast.Attribute):
                name = fn.attr
            elif isinstance(fn, ast.Name):
                name = fn.id
            if name == "add_argument":
                flags = [a.value for a in node.args
                         if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                kw = {}
                for k in node.keywords:
                    if k.arg == "type":
                        if isinstance(k.value, ast.Name):
                            kw["type"] = k.value.id
                        elif isinstance(k.value, ast.Attribute):
                            kw["type"] = k.value.attr
                    elif k.arg == "choices":
                        if isinstance(k.value, (ast.List, ast.Tuple)):
                            kw["choices"] = [e.value for e in k.value.elts
                                             if isinstance(e, ast.Constant)]
                    elif k.arg == "default":
                        if isinstance(k.value, ast.Constant):
                            kw["default"] = k.value.value
                if flags:
                    out.append({"flags": flags, **kw})
            self.generic_visit(node)

    V().visit(tree)
    return out


def main() -> int:
    pin_src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PIN_SRC)
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    spec = {}
    for rel, key in FILES.items():
        p = pin_src / rel
        if not p.is_file():
            print(f"MISSING: {p}", file=sys.stderr)
            return 1
        spec[key] = {"file": rel, "flags": extract_flags(p)}
    out.write_text(json.dumps(spec, indent=2, sort_keys=True))
    print(f"wrote {out} ({sum(len(v['flags']) for v in spec.values())} flags)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())