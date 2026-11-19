# SPDX-License-Identifier: Apache-2.0
"""Shared gate logic for the in-container build and the fast rerun script.

r16 P3: the CPU-gate rerun script (`scripts/rerun-lmcache-gate.sh`) must run
the SAME C8/C12 gates as `_incontainer_build.sh` -- not a copy of their
logic. Both callers invoke this module as `python3 -m kvpersist.gate_common
<command>` with PYTHONPATH pointing at implementation/.

Commands:
  junit-counts XML          print "tests failures errors skipped passed"
                            (summed over all <testsuite>; missing/empty/
                            broken XML -> "__ERROR__<reason>")
  check-imports             the C8 import-check: import lmcache + the native
                            modules, assert_installed() against the installed
                            distribution; exit 1 with the refusal on stderr.
  prod-hash SRC             r17 B3: hash of the PRODUCTION part of the patched
                            tree in SRC (everything except tests/ and docs/),
                            using the index content of the staged tree (git
                            show :file). Prints the hash on stdout; used by
                            build-lmcache.sh (records rerun-prod-hash.txt
                            BEFORE docker run) and rerun-lmcache-gate.sh
                            (compares) -- one algorithm, never a bash copy.
"""
from __future__ import annotations
import sys


def prod_hash(src: str) -> str:
    """Hash of the PRODUCTION part of the patched tree in `src` (everything
    except tests/ and docs/). The tree is expected to be staged (`git add
    -A`); the hash covers the sorted per-file SHA-256 of the index content.
    r17 B3: shared by build-lmcache.sh and rerun-lmcache-gate.sh so the
    recorded and the compared hash cannot drift apart.
    r18 G3.2: filenames are read with -z / NUL separation and
    core.quotepath=off -- a filename with a space (or non-ASCII, which git
    quotes) must not split into bogus paths and produce a wrong hash."""
    import hashlib
    import subprocess

    def _git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", src, "-c", "core.quotepath=off", *args],
            capture_output=True, check=True).stdout

    _git("add", "-A")
    files = _git("diff", "--cached", "--name-only", "-z").decode().split("\0")
    prod = sorted(f for f in files
                  if f and not f.startswith("tests/") and not f.startswith("docs/"))
    h = hashlib.sha256()
    for f in prod:
        content = _git("show", f":{f}")
        h.update(f.encode() + b" " + hashlib.sha256(content).hexdigest().encode())
    return h.hexdigest()


def junit_counts(xml_path: str) -> str:
    """Return 'tests failures errors skipped passed' for the junit report,
    or '__ERROR__<reason>' when the report is missing/empty/unparsable."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(xml_path).getroot()
    except Exception as e:
        return f"__ERROR__{e}"
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return "__ERROR__no testsuite elements"

    def _sum(attr):
        return sum(int(s.get(attr, 0) or 0) for s in suites)

    tests, failures, errors, skipped = (
        _sum("tests"), _sum("failures"), _sum("errors"), _sum("skipped"))
    passed = tests - failures - errors - skipped
    return f"{tests} {failures} {errors} {skipped} {passed}"


def check_imports() -> int:
    """The C8 import-check: the modules must come from the INSTALLED lmcache
    distribution (assert_installed, shared with the pytest plugin)."""
    import os
    import sys
    import lmcache
    import lmcache.cuda_ops  # noqa: F401
    import lmcache.lmcache_native  # noqa: F401
    from lmcache.integration.vllm import lmcache_mp_connector  # noqa: F401
    from lmcache.v1.multiprocess.modules import lmcache_driven_transfer  # noqa: F401
    from kvp_assert_installed import assert_installed

    mods = (lmcache,
            sys.modules["lmcache.cuda_ops"],
            sys.modules["lmcache.lmcache_native"])
    try:
        assert_installed(mods, source_roots=[os.environ.get("KVP_LMC_SRC", ".")])
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    for mod in mods:
        print(f"{mod.__name__}.__file__ =", mod.__file__)
    print("[in-container] import-check OK (all native modules under the "
          "installed distribution location)")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: gate_common.py {junit-counts XML | check-imports | prod-hash SRC}",
              file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "junit-counts":
        if len(argv) != 2:
            print("usage: gate_common.py junit-counts XML", file=sys.stderr)
            return 2
        print(junit_counts(argv[1]))
        return 0
    if cmd == "check-imports":
        return check_imports()
    if cmd == "prod-hash":
        if len(argv) != 2:
            print("usage: gate_common.py prod-hash SRC", file=sys.stderr)
            return 2
        print(prod_hash(argv[1]))
        return 0
    print(f"gate_common.py: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))