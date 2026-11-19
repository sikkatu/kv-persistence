# SPDX-License-Identifier: Apache-2.0
"""r12 P0: the C6b -> C15 -> provenance chain must deliver VALID JSON.

Round-12 review defect: `export NATIVE_EXTENSIONS_JSON="${NATIVE_EXTENSIONS_JSON:-{}}"`
in _incontainer_build.sh appends a stray `}` to a NON-EMPTY value (bash closes
`${var:-{}}` at the first `}`, the second brace becomes a literal). The junk
flows into identity.env (via repr) and into provenance.json, where
`native_extensions` is no longer valid JSON -- build-image.sh, which reads
provenance, then dies AFTER an already-successful long build. The r11 tests
fed identity.env by hand and never ran the chain end to end.

This test runs the REAL chain with NO manual substitutions:
  1. the REAL `kvpersist.check_wheel_native` on a fake wheel carrying THREE
     native extensions (cuda_ops, lmcache_native, lmcache_redis),
  2. the C6b block EXTRACTED from _incontainer_build.sh by its markers
     (`# ---- C6b` / `# ---- C6c`) -- never a copy in the test,
  3. the C15 block (identity.env writer) the same way,
  4. the default-value lines + provenance heredoc EXTRACTED from
     build-lmcache.sh,
  5. `json.loads(provenance.json)` and the three expected keys.

Mutation: restoring `${...:-{}}` in C6b makes this test RED (JSONDecodeError).
"""
from __future__ import annotations
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
INCONTAINER = ROOT / "scripts" / "_incontainer_build.sh"
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
IMPL = ROOT / "implementation"


def extract_between(text: str, start_snip: str, end_snip: str) -> str:
    """Block from the line containing start_snip (exclusive) up to the line
    containing end_snip (exclusive)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if start_snip in ln)
    end = next(i for i, ln in enumerate(lines) if i > start and end_snip in ln)
    return "\n".join(lines[start + 1:end])


def extract_c15(text: str) -> str:
    """C15 block including its heredoc terminator `PY` (exact line)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if "# ---- C15" in ln)
    end = next(i for i, ln in enumerate(lines)
               if i > start and ln.strip() == "PY")
    return "\n".join(lines[start + 1:end + 1])


def extract_provenance(text: str) -> str:
    """The default-value lines + `cat > "$PROV" <<EOF ... EOF` from
    build-lmcache.sh (the r11 comment marks the start of the defaults)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if "default values assigned separately" in ln)
    end = next(i for i, ln in enumerate(lines) if i > start and ln.strip() == "EOF")
    return "\n".join(lines[start + 1:end + 1])


def test_no_brace_default_literals_left(tmp_path):
    """Acceptance: `grep -rn ':-{' scripts implementation` must be EMPTY.
    The brace-default literal always appends a stray `}` to non-empty values
    and must not exist anywhere in the build scripts or the implementation."""
    import re
    pat = re.compile(r":-\{")
    hits = []
    for base in (ROOT / "scripts", ROOT / "implementation"):
        for f in base.rglob("*"):
            if f.is_file():
                try:
                    txt = f.read_text()
                except (UnicodeDecodeError, OSError):
                    continue
                for i, ln in enumerate(txt.splitlines(), 1):
                    if pat.search(ln):
                        hits.append(f"{f}:{i}: {ln.strip()}")
    assert not hits, "brace-default literals still present:\n" + "\n".join(hits)


def test_c6b_identity_provenance_chain_delivers_valid_json(tmp_path):
    """REAL programs end to end: check_wheel_native -> C6b block (from the
    script) -> C15 block -> identity.env -> provenance heredoc (from
    build-lmcache.sh) -> json.loads. native_extensions must be a JSON object
    with the three real keys -- not `...so\"\"}}` junk."""
    incontainer = INCONTAINER.read_text()
    build_lmc = BUILD_LMC.read_text()
    c6b = extract_between(incontainer, "# ---- C6b", "# ---- C6c")
    c15 = extract_c15(incontainer)
    prov = extract_provenance(build_lmc)

    wheel = tmp_path / "lmcache-0.5.7.dev428+kvp.abc.whl"
    import zipfile
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("lmcache/__init__.py", b"# stub\n")
        z.writestr("lmcache/cuda_ops.cpython-312-x86_64-linux-gnu.so", b"\x00")
        z.writestr("lmcache/lmcache_native.cpython-312-x86_64-linux-gnu.so", b"\x00")
        z.writestr("lmcache/lmcache_redis.cpython-312-x86_64-linux-gnu.so", b"\x00")

    work = tmp_path / "kvp-work"
    work.mkdir()
    patch_dir = tmp_path / "patches"
    patch_dir.mkdir()
    (patch_dir / "4136-mp-retrieve-abort.patch").write_text("a\n")
    (patch_dir / "5972-incomplete-kv-objects.patch").write_text("b\n")
    prov_path = tmp_path / "provenance.json"

    ts = tmp_path / "torchstub" / "torch"
    ts.mkdir(parents=True)
    (ts / "__init__.py").write_text(
        "class _C:\n"
        "    @staticmethod\n    def is_available(): return False\n"
        "    @staticmethod\n    def device_count(): return 0\n"
        "    @staticmethod\n    def get_device_capability(i): return (12, 0)\n"
        "cuda = _C()\n__version__ = '2.4.0'\nclass version:\n    cuda = '13.0'\n")

    driver = tmp_path / "driver.sh"
    driver.write_text("".join([
        "set -Eeuo pipefail\n",
        f'WHEEL_FILE="{wheel}"\n',
        f'KVP_IMPL="{IMPL}"\n',
        f'KVP_WORK="{work}"\n',
        # -- C6b block (verbatim from _incontainer_build.sh) --
        c6b + "\n",
        # -- C15 block (verbatim from _incontainer_build.sh) --
        c15 + "\n",
        # -- host phase: source identity.env strictly, then provenance --
        f'. "{work}/identity.env"\n',
        f'PROV="{prov_path}"\n',
        'LMCACHE_REPO_URL="https://github.com/LMCache/LMCache.git"\n',
        'LMCACHE_SOURCE_COMMIT="03ee5b82418b6bd44e424cdd46932d9346f43eb2"\n',
        'PR6551_MERGE="61faa902a81462177d40d74f8c64bb9df02163c4"\n',
        f'PATCH_4136="{patch_dir}/4136-mp-retrieve-abort.patch"\n',
        f'PATCH_5972="{patch_dir}/5972-incomplete-kv-objects.patch"\n',
        'PATCHED_STATE="feedfacefeedfacefeedfacefeedface"\n',
        'IMAGE="nvcr.io/nvidia/vllm@sha256:' + "a" * 64 + '"\n',
        'NVCC_VERSION_REC="Cuda 13.0"\n',
        'KVP_WHEEL_VERSION="0.5.7.dev428+kvp.feedfacefeed"\n',
        f'WHEEL_NAME="{wheel.name}"\n',
        'WHEEL_SHA="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"\n',
        # -- provenance block (verbatim from build-lmcache.sh) --
        prov + "\n",
    ]))
    env = dict(PYTHONPATH=str(tmp_path / "torchstub"))
    r = subprocess.run(["bash", str(driver)], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, (
        f"the C6b->C15->provenance chain must complete:\nstdout:\n{r.stdout}\n"
        f"stderr:\n{r.stderr}")

    data = json.loads(prov_path.read_text())  # must NOT raise JSONDecodeError
    ext = data["native_extensions"]
    assert isinstance(ext, dict), f"native_extensions must be a JSON object: {ext!r}"
    for key in ("lmcache/cuda_ops", "lmcache/lmcache_native", "lmcache/lmcache_redis"):
        assert key in ext, f"native_extensions must contain {key}: {ext}"
    assert str(ext["lmcache/cuda_ops"]).endswith(".so"), ext