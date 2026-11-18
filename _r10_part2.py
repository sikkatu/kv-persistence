#!/usr/bin/env python3
"""r10 part 2: cuobjdump arch check + native_extensions/CUDA_OPS_ARCHS in
identity.env; _verify_image.py lmcache_native + KVP_NATIVE_IMPORT marker;
build-image.sh strict gate; provenance fields from identity.env."""
import pathlib

# ---------- _incontainer_build.sh: cuobjdump check + identity fields ----------
P = pathlib.Path("scripts/_incontainer_build.sh")
T = P.read_text()

old = '''echo "[in-container] artifacts:"
ls -la $KVP_WHEELS'''
new = '''echo "[in-container] artifacts:"
ls -la $KVP_WHEELS

# r10 P1: the built cuda_ops*.so must carry sm_120 (RTX PRO 6000). cuobjdump
# ships with the CUDA toolkit (same as nvcc). The found arch list goes to
# identity.env -> provenance.json; missing sm_120 is a BUILD REFUSED.
CUDA_OPS_ARCHS="unknown"
if command -v cuobjdump >/dev/null 2>&1; then
  _so="$(ls $KVP_WHEELS/*.whl >/dev/null 2>&1 && unzip -l $KVP_WHEELS/*.whl | awk '/lmcache\\/cuda_ops.*\\.so$/{print $NF; exit}')"
  if [ -n "$_so" ]; then
    mkdir -p /tmp/kvp-arch && cd /tmp/kvp-arch
    unzip -o -q $KVP_WHEELS/*.whl "$_so"
    CUDA_OPS_ARCHS="$(cuobjdump --list-elf "$_so" 2>/dev/null | grep -o 'sm_[0-9]*' | sort -u | tr '\\n' ' ' | sed 's/ $//')"
    cd - >/dev/null
  fi
  echo "[in-container] cuda_ops archs (cuobjdump): $CUDA_OPS_ARCHS"
  case " $CUDA_OPS_ARCHS " in
    *" sm_120 "*) : ;;
    *) echo "BUILD REFUSED: cuda_ops*.so does NOT contain sm_120 (found: '$CUDA_OPS_ARCHS') --" >&2
       echo "  the RTX PRO 6000 kernel would be missing. Check KVP_TORCH_CUDA_ARCH_LIST." >&2
       exit 1 ;;
  esac
else
  echo "[in-container] WARNING: cuobjdump not found -- arch check skipped (see the program audit)" >&2
fi'''
assert old in T
T = T.replace(old, new, 1)

old_id = 'print("TORCH_CUDA_ARCH_LIST_USED=\\"" + __import__("os").environ.get("TORCH_CUDA_ARCH_LIST", "") + "\\"")'
new_id = old_id + '''
print("CUDA_OPS_ARCHS=\\"" + __import__("os").environ.get("CUDA_OPS_ARCHS", "unknown") + "\\"")'''
assert old_id in T
T = T.replace(old_id, new_id, 1)

# NATIVE_EXTENSIONS_JSON must reach identity.env: export before the writer
old_nat = 'echo "$NATIVE_OUT"'
new_nat = 'echo "$NATIVE_OUT"\nexport NATIVE_EXTENSIONS_JSON="$NATIVE_EXTENSIONS_JSON"'
assert old_nat in T
T = T.replace(old_nat, new_nat, 1)
old_id2 = 'print("CUDA_OPS_ARCHS=\\"" + __import__("os").environ.get("CUDA_OPS_ARCHS", "unknown") + "\\"")'
new_id2 = old_id2 + '''
print("native_extensions=" + __import__("os").environ.get("NATIVE_EXTENSIONS_JSON", "{}"))'''
assert old_id2 in T
T = T.replace(old_id2, new_id2, 1)
P.write_text(T)
print("incontainer: cuobjdump + identity fields")

# ---------- _verify_image.py: lmcache_native + marker ----------
P = pathlib.Path("scripts/_verify_image.py")
T = P.read_text()
old = 'import lmcache.cuda_ops  # noqa: F401 -- native binary must import\nprint("KVP_CUDA_OPS_IMPORT=OK")'
new = '''import lmcache.cuda_ops  # noqa: F401 -- native binary must import
print("KVP_CUDA_OPS_IMPORT=OK")
# r10 P0-2: without lmcache_native LMCache drops to CLI-only mode (no compute
# backend). Require it explicitly and emit the strict-gate marker.
import lmcache.lmcache_native  # noqa: F401
print("KVP_NATIVE_IMPORT=OK")'''
assert old in T
T = T.replace(old, new, 1)
P.write_text(T)
print("verify_image: lmcache_native + KVP_NATIVE_IMPORT")

# ---------- build-image.sh: strict marker gate ----------
P = pathlib.Path("scripts/build-image.sh")
T = P.read_text()
old = "for _marker in KVP_CUDA_OPS_IMPORT=OK KVP_PY_IMPORTS=OK KVP_IDENTITY_FILE=OK; do"
new = "for _marker in KVP_CUDA_OPS_IMPORT=OK KVP_NATIVE_IMPORT=OK KVP_PY_IMPORTS=OK KVP_IDENTITY_FILE=OK; do"
assert old in T
T = T.replace(old, new, 1)
P.write_text(T)
print("build-image: KVP_NATIVE_IMPORT in the gate")

# ---------- build-lmcache.sh: provenance from identity.env ----------
P = pathlib.Path("scripts/build-lmcache.sh")
T = P.read_text()
old = '  "torch_cuda_arch_list": "${TORCH_CUDA_ARCH_LIST:-12.0}",'
new = '''  "torch_cuda_arch_list": "${TORCH_CUDA_ARCH_LIST_USED:-${KVP_TORCH_CUDA_ARCH_LIST:-12.0}}",
  "cuda_ops_archs": "${CUDA_OPS_ARCHS:-unknown}",
  "native_extensions": ${NATIVE_EXTENSIONS_JSON:-{}},'''
assert old in T
T = T.replace(old, new, 1)
P.write_text(T)
print("build-lmcache: provenance fields")