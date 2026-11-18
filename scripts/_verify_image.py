#!/usr/bin/env python3
"""In-image verification of the custom LMCache derivative (r6-7 A-2).

Runs on ALL visible GPUs (--gpus all): real device count, real compute
capability, one native kernel per GPU, the baked identity file and the
installed lmcache metadata. Prints KVP_* markers to stdout; build-image.sh
greps them STRICTLY before recording anything (empty verify == refusal).
"""
import importlib.metadata as md
import torch
import sys
print("KVP_LMC_VERSION=" + md.version("lmcache"))
print("KVP_TORCH_VERSION=" + torch.__version__)
print("KVP_TORCH_CUDA=" + str(torch.version.cuda))
print("KVP_PYTHON_VERSION=" + sys.version.split()[0])
print("KVP_CUDA_AVAILABLE=" + str(torch.cuda.is_available()))
print("KVP_GPU_COUNT=" + str(torch.cuda.device_count()))
import lmcache.integration.vllm.lmcache_mp_connector as c
import lmcache.v1.multiprocess.modules.lmcache_driven_transfer as t
print("KVP_PY_IMPORTS=OK")
print("KVP_CONN_MODULE=" + str(c.__file__))
print("KVP_TRANSFER_MODULE=" + str(t.__file__))
# honest native-extension check: import the compiled module and run ONE
# native kernel per GPU (a PATH-only grep proves nothing).
import lmcache.cuda_ops  # noqa: F401
print("KVP_CUDA_OPS_IMPORT=OK")
_smoke_ok = []
for _i in range(torch.cuda.device_count()):
    with torch.cuda.device(_i):
        _bus = lmcache.cuda_ops.get_gpu_pci_bus_id(_i)
        _cc = torch.cuda.get_device_capability(_i)
        print(f"KVP_GPU_SMOKE[{_i}] dev={_bus} cc={_cc[0]}.{_cc[1]} OK")
        _smoke_ok.append(_i)
print("KVP_GPU_SMOKE_OK_COUNT=" + str(len(_smoke_ok)))
if len(_smoke_ok) != torch.cuda.device_count() or not _smoke_ok:
    raise SystemExit("native kernel smoke did not run on every visible GPU")
_cc0 = torch.cuda.get_device_capability(0)
print(f"KVP_GPU_CC={_cc0[0]}.{_cc0[1]}")
import json
import pathlib
ident = json.loads(pathlib.Path("/etc/kvp-lmcache-identity.json").read_text())
print("KVP_IDENTITY_FILE=OK")
print("KVP_IDENTITY_BASE=" + ident["lmcache_source_commit"])
print("KVP_IDENTITY_P4136=" + ident["pr4136_patch_sha256"])
print("KVP_IDENTITY_P5972=" + ident["pr5972_patch_sha256"])
