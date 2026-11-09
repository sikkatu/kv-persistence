#!/usr/bin/env bash
# ipc-test.sh — CAN THE CACHE SERVER OPEN GPU MEMORY CREATED IN THE CONTAINER?
#
# WHY (review round 45): the install run failed at register_kv_caches with
#   torch.AcceleratorError: CUDA error: mapping of buffer object failed
#   Failed to open/create interprocess memory file
#   Returning 205 (CUDA_ERROR_MAP_FAILED) from cuIpcOpenEventHandle
# The cache server (host) could not map the CUDA IPC buffer that the vLLM
# container created. This test reproduces EXACTLY that step in ~1 minute,
# WITHOUT loading the model: a producer in the container creates a GPU
# tensor and hands over its IPC handle; the consumer opens it.
#
# Usage:
#   bash scripts/ipc-test.sh            (IMAGE auto-resolved from the recipe)
#   IMAGE=<pinned @sha256:...> bash scripts/ipc-test.sh   (explicit override)
#   CONSUMER=host-root  bash scripts/ipc-test.sh   (sudo consumer)
#   CONSUMER=container  bash scripts/ipc-test.sh   (2nd container)
# Env: HOST_PY (default ~/.venvs/lmcache/bin/python), GPU_IDX (default 0),
#     PRODUCER_USER -- run the producer container with this UID:GID
#     (e.g. PRODUCER_USER=$(id -u):$(id -g) to test the SAME-UID path:
#     PASS means the transfer works WITHOUT root).
#
# Result:
#   PASS  = GPU memory crosses container -> consumer. If the server still
#           fails, the cause is NOT the IPC boundary.
#   FAIL / TIMEOUT = this boundary is broken; see the verdict hints.
#
# Nothing is written outside /tmp/kvp-ipc-test.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# IMAGE is OPTIONAL: without it, resolve the recipe the same way the
# launcher does and extract the pinned digest from tp4.env.example
# (print-recipe-image.sh). The command is then simply:
#   bash scripts/ipc-test.sh
if [ -z "${IMAGE:-}" ]; then
  if [ -n "${RECIPE_DIR:-}" ]; then RECIPE="$RECIPE_DIR"
  elif [ -d "$HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit" ]; then
    RECIPE="$HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit"
  else
    RECIPE="$HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit"
  fi
  if [ -f "$HERE/print-recipe-image.sh" ]; then
    IMAGE="$(bash "$HERE/print-recipe-image.sh" "$RECIPE" 2>/dev/null)" || IMAGE=""
  fi
  if [ -z "$IMAGE" ]; then
    echo "ERROR: IMAGE not set and could not be resolved from the recipe." >&2
    echo "  Looked in: $RECIPE (tp4.env.example)" >&2
    echo "  Fix one of:" >&2
    echo "    - run from the recipes checkout, or set RECIPE_DIR=<recipe dir>" >&2
    echo "    - or set IMAGE explicitly: IMAGE=<pinned @sha256:...> bash scripts/ipc-test.sh" >&2
    exit 2
  fi
  echo "image resolved from $RECIPE: $IMAGE"
fi
HOST_PY="${HOST_PY:-$HOME/.venvs/lmcache/bin/python}"
GPU_IDX="${GPU_IDX:-0}"
CONSUMER="${CONSUMER:-host}"
D=/tmp/kvp-ipc-test

die() { echo "ERROR: $*" >&2; exit 2; }

[ -x "$HOST_PY" ] || die "$HOST_PY not found -- set HOST_PY=... (the lmcache venv python)"
command -v docker >/dev/null 2>&1 || die "docker not found"

rm -rf "$D"; mkdir -p "$D"; chmod 777 "$D"

cat > "$D/producer.py" <<'PY'
import os, pickle, sys, time
import torch
from torch.multiprocessing.reductions import reduce_tensor
idx = int(os.environ.get("GPU_IDX", "0"))
t = torch.arange(1024, dtype=torch.float32, device=f"cuda:{idx}")
fn, args = reduce_tensor(t)
tmp = "/ipc/handle.pkl.tmp"
with open(tmp, "wb") as f:
    pickle.dump((fn, args), f)
os.rename(tmp, "/ipc/handle.pkl")
print(f"producer: torch {torch.__version__}, python {sys.version.split()[0]}, IPC handle written", flush=True)
for _ in range(90):
    if os.path.exists("/ipc/done"):
        break
    time.sleep(1)
PY

cat > "$D/consumer.py" <<'PY'
import pickle, sys
import torch
fn, args = pickle.load(open("/tmp/kvp-ipc-test/handle.pkl", "rb"))
t = fn(*args)
got = float(t.sum().item())
exp = float(sum(range(1024)))
print(f"consumer: torch {torch.__version__}, python {sys.version.split()[0]}, sum={got} expected={exp}")
sys.exit(0 if got == exp else 1)
PY

python3 -m py_compile "$D/producer.py" "$D/consumer.py" || die "generated test scripts do not compile"

echo "-- producer in container (GPU $GPU_IDX, --ipc host, same image as the engine)"
PRODUCER_USER="${PRODUCER_USER:-}"
if [ -n "$PRODUCER_USER" ]; then
  case "$PRODUCER_USER" in
    0:0|0) echo "   producer UID:GID = $PRODUCER_USER (root producer)" ;;
    *)     echo "   producer UID:GID = $PRODUCER_USER (same-UID test -- no root)" ;;
  esac
fi
docker rm -f kvp-ipc-test >/dev/null 2>&1 || true
docker run -d --rm --name kvp-ipc-test --gpus all --ipc host -e GPU_IDX="$GPU_IDX" \
  ${PRODUCER_USER:+--user "$PRODUCER_USER"} \
  -v "$D:/ipc" --entrypoint python3 "$IMAGE" /ipc/producer.py >/dev/null \
  || die "docker run failed for $IMAGE"

echo -n "waiting for the IPC handle"
for i in $(seq 1 90); do
  [ -f "$D/handle.pkl" ] && break
  if [ $((i % 10)) -eq 0 ]; then printf ' %ds' "$i"; fi
  sleep 1
done
echo ""
if [ ! -f "$D/handle.pkl" ]; then
  echo "FAIL: producer did not write the IPC handle within 90s. Container log:" >&2
  docker logs kvp-ipc-test 2>&1 | tail -15 >&2 || true
  touch "$D/done"; docker rm -f kvp-ipc-test >/dev/null 2>&1 || true
  exit 1
fi
docker logs kvp-ipc-test 2>&1 | grep 'producer:' || true

rc=0
case "$CONSUMER" in
  host)
    echo "-- consumer on the host as $(id -un), $HOST_PY (40s limit)"
    CUDA_VISIBLE_DEVICES="$GPU_IDX" timeout 40 "$HOST_PY" "$D/consumer.py"; rc=$? ;;
  host-root)
    echo "-- consumer on the host as root (sudo), $HOST_PY (40s limit)"
    sudo env CUDA_VISIBLE_DEVICES="$GPU_IDX" timeout 40 "$HOST_PY" "$D/consumer.py"; rc=$? ;;
  container)
    echo "-- consumer in a second container of the same image (root, --ipc host, 60s limit)"
    timeout 60 docker run --rm --gpus all --ipc host -v "$D:/tmp/kvp-ipc-test" \
      --entrypoint python3 "$IMAGE" /tmp/kvp-ipc-test/consumer.py; rc=$? ;;
  *)
    die "CONSUMER must be host | host-root | container (got: $CONSUMER)" ;;
esac

touch "$D/done"; sleep 1; docker rm -f kvp-ipc-test >/dev/null 2>&1 || true

case $rc in
  0)
    echo "PASS: the consumer opened GPU memory created in the container."
    echo "      The CUDA IPC boundary works for CONSUMER=$CONSUMER -- if the cache"
    echo "      server still fails, the cause is elsewhere (check the server log)."
    if [ -n "$PRODUCER_USER" ]; then
      case "$PRODUCER_USER" in
        0:0|0) echo "      ROOT-PRODUCER result: PASS -- the transfer works with a root producer." ;;
        *)     echo "      SAME-UID result: PASS -- the transfer works WITHOUT root." ;;
      esac
      echo "      This path IS wired: KVP_SAME_UID=1 bash scripts/run-vllm-with-persist.sh"
      echo "      (the launcher runs the engine as \$(id -u):\$(id -g) with caches under /tmp)."
    fi ;;
  124)
    echo "TIMEOUT: the consumer hung while opening GPU memory from the container."
    echo "      This REPRODUCES the register_kv_caches failure."
    echo "      Fix options (review round 45):"
    echo "        - run the cache server in the container too:"
    echo "          KVP_LAUNCH_MODE=docker USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh"
    echo "        - or run the cache server as root (same user as the engine container)"
    echo "      Then rerun: CONSUMER=host-root bash scripts/ipc-test.sh" ;;
  1)
    echo "FAIL: the consumer opened the memory but the data does not match."
    echo "      The IPC path transfers data incorrectly -- check GPU drivers / ECC." ;;
  *)
    echo "FAIL (exit $rc): the consumer could not open GPU memory created in the"
    echo "      container. This REPRODUCES the register_kv_caches failure"
    echo "      (CUDA_ERROR_MAP_FAILED). Fix options:"
    echo "        - run the cache server as root (proven by CONSUMER=host-root):"
    echo "          sudo USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh"
    echo "        - or run the cache server in the container too:"
    echo "          KVP_LAUNCH_MODE=docker USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh"
    if [ -n "$PRODUCER_USER" ]; then
      echo "      SAME-UID result: FAIL -- even matching UIDs is not enough here."
    else
      echo "      To test the same-UID path: PRODUCER_USER=\$(id -u):\$(id -g) bash scripts/ipc-test.sh"
    fi
    echo "      Last consumer output is above." ;;
esac
exit $rc