#!/usr/bin/env python3
"""Debug: run the real build-lmcache.sh with the test fixture, bash -x, save trace."""
import json
import os
import pathlib
import shutil
import subprocess
import zipfile

ROOT = pathlib.Path("/root/kvp-work/kv-persistence")
BUILD_LMC = ROOT / "scripts" / "build-lmcache.sh"
tmp = pathlib.Path("/root/kvp-work/prov-repro")
shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True)

bin_dir = tmp / "bin"
bin_dir.mkdir()
for tool in ("bash", "cat", "grep", "tail", "mktemp", "chmod", "printf",
             "echo", "sed", "awk", "tr", "cut", "find", "head", "wc",
             "mkdir", "rm", "cp", "ls", "date", "basename", "dirname",
             "unzip", "sort", "sha256sum", "id", "python3"):
    real = shutil.which(tool)
    if real:
        (bin_dir / tool).symlink_to(real)

ts = tmp / "torchstub" / "torch"
ts.mkdir(parents=True)
(ts / "__init__.py").write_text(
    "class _C:\n"
    "    @staticmethod\n    def is_available(): return False\n"
    "    @staticmethod\n    def device_count(): return 0\n"
    "    @staticmethod\n    def get_device_capability(i): return (12, 0)\n"
    "cuda = _C()\n__version__ = '2.4.0'\nclass version:\n    cuda = '13.0'\n")

(bin_dir / "docker").write_text(
    "#!/usr/bin/env bash\n"
    "cmd=\"$1\"; shift || true\n"
    "case \"$cmd\" in\n"
    "  run) cat > /dev/null; exit 0 ;;\n"
    "  inspect) echo \"sha256:$(printf 'a%.0s' $(seq 64))\"; exit 0 ;;\n"
    "  info) exit 0 ;;\n"
    "  *) exit 0 ;;\nesac\n")
(bin_dir / "docker").chmod(0o755)

(bin_dir / "git").write_text(
    "#!/usr/bin/env bash\n"
    "case \"$*\" in\n"
    "  *'rev-parse HEAD'*) echo 03ee5b82418b6bd44e424cdd46932d9346f43eb2 ;;\n"
    "  *'merge-base --is-ancestor'*) exit 0 ;;\n"
    "  *'apply --reverse --check'*) exit 1 ;;\n"
    "  *'apply --check'*|*'apply '*) exit 0 ;;\n"
    "  *'diff --cached'*) echo stubdiff ;;\n"
    "  *'ls-files'*) printf 'lmcache/a.yaml\\n' ;;\n"
    "  *) exit 0 ;;\nesac\n")
(bin_dir / "git").chmod(0o755)

build_dir = tmp / "build"
out_dir = tmp / "out"
out_dir.mkdir()
(build_dir / "LMCache" / ".git").mkdir(parents=True)
(build_dir / "LMCache" / "requirements").mkdir()
(build_dir / "LMCache" / "lmcache").mkdir()
(build_dir / "LMCache" / "lmcache" / "a.yaml").write_text("x\n")
(build_dir / "wheels").mkdir()
(build_dir / "identity.env").write_text(
    "TORCH_CUDA_ARCH_LIST_USED='12.0'\n"
    "CUDA_OPS_ARCHS='sm_120'\n"
    'NATIVE_EXTENSIONS_JSON=\'{"lmcache/cuda_ops": "lmcache/cuda_ops.so", '
    '"lmcache/lmcache_native": "lmcache/lmcache_native.so"}\'\n'
    "GPU_REGRESSION_RUN='false'\n")
for whl_dir in (build_dir / "wheels", out_dir / "wheels"):
    whl_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(whl_dir / "lmcache-0.5.7.dev428+kvp.abc.whl", "w") as z:
        z.writestr("lmcache/cuda_ops_stub.so", b"\x00")
        z.writestr("lmcache/lmcache_native_stub.so", b"\x00")
(build_dir / "provenance.json").write_text(json.dumps({"wheel_version": "x"}))

pdir = tmp / "patches"
pdir.mkdir()
(pdir / "4136-mp-retrieve-abort.patch").write_text("a\n")
(pdir / "5972-incomplete-kv-objects.patch").write_text("b\n")

env = dict(os.environ, PATH=str(bin_dir),
           KVP_BUILD_DIR=str(build_dir),
           KVP_PATCH_DIR=str(pdir),
           KVP_OUT_DIR=str(out_dir),
           KVP_IDENTITY_OUT=str(out_dir / "image-identity.env"),
           KVP_LIVE_DIR=str(out_dir / "live"),
           KVP_PIN_FILE=str(ROOT / "vendor" / "pinned-base-image.env"))
for var in ("IMAGE", "KVP_LMC_IMAGE", "KVP_BASE_IMAGE"):
    env.pop(var, None)

r = subprocess.run(["bash", "-x", str(BUILD_LMC)], capture_output=True,
                   text=True, timeout=120, env=env, cwd=str(tmp))
out = pathlib.Path("/root/kvp-work/prov_trace.txt")
out.write_text(f"rc={r.returncode}\n===STDOUT===\n{r.stdout}\n===TRACE(tail 60)===\n"
               + "\n".join(r.stderr.splitlines()[-60:]))
print("rc =", r.returncode)
print("trace saved:", out)