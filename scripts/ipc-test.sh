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
#     CONSUMER_USER -- run the container consumer with this UID:GID (the
#     EXPECTED server UID). Defaults to PRODUCER_USER when set: the cache
#     server and the engine must share ONE UID (round 27 review).
#     IPC_TEST_DIR (default /tmp/kvp-ipc-test) and IPC_CONTAINER (default
#     kvp-ipc-test) -- test isolation, never touch real paths.
#
# Result:
#   PASS  = GPU memory crosses container -> consumer. If the server still
#           fails, the cause is NOT the IPC boundary.
#   FAIL / TIMEOUT = this boundary is broken; see the verdict hints.
#   Round 27 (K4): the verdict always reports both PIDs (producer/consumer);
#   equal PIDs mean the test topology is invalid ("invalid by construction")
#   and no boundary verdict is given.
#
# Nothing is written outside /tmp/kvp-ipc-test (or IPC_TEST_DIR).
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
D="${IPC_TEST_DIR:-/tmp/kvp-ipc-test}"
CNAME="${IPC_CONTAINER:-kvp-ipc-test}"

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
print(f"producer: torch {torch.__version__}, python {sys.version.split()[0]}, pid={os.getpid()}, IPC handle written", flush=True)
for _ in range(90):
    if os.path.exists("/ipc/done"):
        break
    time.sleep(1)
PY

cat > "$D/consumer.py" <<'PY'
import os, pickle, sys, traceback
# r28 L1: the pid line MUST print before any import that can fail and before
# fn(*args): on a real CUDA refusal Python exited with rc=1 and NO pid, the
# script showed "PIDs: producer=1 consumer=?" and blamed ECC instead of UIDs.
print(f"consumer: pid={os.getpid()}", flush=True)
try:
    import torch
    fn, args = pickle.load(open("/tmp/kvp-ipc-test/handle.pkl", "rb"))
    t = fn(*args)
    got = float(t.sum().item())
    exp = float(sum(range(1024)))
except BaseException as e:  # noqa: BLE001 - report ANY failure honestly
    traceback.print_exc()
    print(f"consumer: OPEN FAILED {type(e).__name__}: {e}", flush=True)
    sys.exit(4)
print(f"consumer: torch {torch.__version__}, python {sys.version.split()[0]}, pid={os.getpid()}, sum={got} expected={exp}")
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
# Round 27 (K4): CONSUMER_USER = the EXPECTED server UID (defaults to
# PRODUCER_USER): the deployed configuration has server and engine on ONE UID.
CONSUMER_USER="${CONSUMER_USER:-$PRODUCER_USER}"

# Round 27 (K5): cleanup must run on EVERY exit (die, refusal, failed
# consumer) -- under set -e a failing consumer used to abort the script
# without a verdict and without cleanup.
cleanup() {
  [ -f "$D/done" ] || touch "$D/done" 2>/dev/null || true
  sleep 1 2>/dev/null || true
  docker rm -f "$CNAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker rm -f "$CNAME" >/dev/null 2>&1 || true
# Round 27 (K4): the producer is started through `sh -c '...; true'` so its
# PID is NOT 1 inside the container -- both sides at PID 1 made the driver
# treat the handle as locally created ("invalid device context", rc 201).
# No --pid host: hardware proved separate PID namespaces pass.
docker run -d --rm --name "$CNAME" --gpus all --ipc host -e GPU_IDX="$GPU_IDX" \
  ${PRODUCER_USER:+--user "$PRODUCER_USER"} \
  -v "$D:/ipc" --entrypoint sh "$IMAGE" -c 'python3 /ipc/producer.py; true' >/dev/null \
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
  docker logs "$CNAME" 2>&1 | tail -15 >&2 || true
  exit 1
fi
PROD_LINE="$(docker logs "$CNAME" 2>&1 | grep 'producer:' || true)"
echo "$PROD_LINE"
PROD_PID="$(printf '%s' "$PROD_LINE" | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | tail -1)"

rc=0
case "$CONSUMER" in
  host)
    echo "-- consumer on the host as $(id -un), $HOST_PY (40s limit)"
    CONS_OUT="$(CUDA_VISIBLE_DEVICES="$GPU_IDX" timeout 40 "$HOST_PY" "$D/consumer.py" 2>&1)" || rc=$? ;;
  host-root)
    echo "-- consumer on the host as root (sudo), $HOST_PY (40s limit)"
    CONS_OUT="$(sudo env CUDA_VISIBLE_DEVICES="$GPU_IDX" timeout 40 "$HOST_PY" "$D/consumer.py" 2>&1)" || rc=$? ;;
  container)
    echo "-- consumer in a second container of the same image${CONSUMER_USER:+ (user $CONSUMER_USER)}, --ipc host, 60s limit"
    CONS_OUT="$(timeout 60 docker run --rm --gpus all --ipc host ${CONSUMER_USER:+--user "$CONSUMER_USER"} \
      -v "$D:/tmp/kvp-ipc-test" \
      --entrypoint python3 "$IMAGE" /tmp/kvp-ipc-test/consumer.py 2>&1)" || rc=$? ;;
  *)
    die "CONSUMER must be host | host-root | container (got: $CONSUMER)" ;;
esac
printf '%s\n' "$CONS_OUT"
CONS_PID="$(printf '%s' "$CONS_OUT" | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | tail -1)"
echo "PIDs: producer=${PROD_PID:-?} consumer=${CONS_PID:-?}"

# Round 27 (K4): equal PIDs = the test topology is invalid (both sides seen as
# one process, e.g. PID 1 in both containers) -- that is NOT a boundary verdict.
if [ -n "${PROD_PID:-}" ] && [ "$PROD_PID" = "${CONS_PID:-}" ]; then
  echo "VERDICT: TEST INVALID BY CONSTRUCTION -- producer and consumer have the same PID ($PROD_PID)."
  echo "      The driver then treats the handle as locally created and refuses it (rc 201)."
  echo "      This says NOTHING about the CUDA IPC boundary. Rerun with a setup where"
  echo "      the two sides have different PIDs (producer via 'sh -c ...; true')."
  exit 3
fi

uid_hint(){ # r28 L1: ONE honest fix text for a real OPEN failure (any rc except
  # 0/1-with-sum/124): the usual cause is DIFFERENT UIDs on the two sides.
  echo "FAIL (exit $rc): the consumer could not open GPU memory created in the"
  echo "      container. This REPRODUCES the register_kv_caches failure"
  echo "      (CUDA_ERROR_MAP_FAILED). The usual cause is DIFFERENT UIDs on the two"
  echo "      sides of the boundary: CUDA IPC only opens when the cache server and"
  echo "      the engine share ONE UID. Check:"
  echo "        docker inspect -f '{{.Config.User}}' lmcache-kvp-server"
  echo "      and the engine UID. Restart the server so both sides match:"
  echo "        bash scripts/stop-cache.sh && bash scripts/start-cache.sh"
  echo "      (the default deployment is docker mode: KVP_LAUNCH_MODE=docker)"
  if [ -n "$PRODUCER_USER" ]; then
    echo "      SAME-UID result: FAIL -- even matching UIDs is not enough here."
  else
    echo "      To test the same-UID path: PRODUCER_USER=\$(id -u):\$(id -g) bash scripts/ipc-test.sh"
  fi
  echo "      Last consumer output is above."
}
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
      echo "      This path IS wired: the engine and the cache server must share ONE UID"
      echo "      (KVP_SAME_UID=1 default; both sides run as \$(id -u):\$(id -g))."
    fi ;;
  124)
    echo "TIMEOUT: the consumer hung while opening GPU memory from the container."
    echo "      This REPRODUCES the register_kv_caches failure."
    echo "      The cache server and the engine must run under ONE UID -- check:"
    echo "        docker inspect -f '{{.Config.User}}' lmcache-kvp-server"
    echo "      It must equal the engine UID. To restart the server correctly:"
    echo "        bash scripts/stop-cache.sh && bash scripts/start-cache.sh" ;;
  1)
    # r28 L1: "data does not match" ONLY when the memory actually opened (the
    # consumer printed sum=). rc=1 without sum= is an OPEN failure whose
    # exception escaped (rc=1 instead of 4) -- the UID hint is the honest one.
    case "$CONS_OUT" in
      *sum=*)
        echo "FAIL: the consumer opened the memory but the data does not match."
        echo "      The IPC path transfers data incorrectly -- check GPU drivers / ECC." ;;
      *)
        uid_hint ;;
    esac ;;
  4|*)
    # r28 L1: rc=4 = "OPEN FAILED <type>: <text>" from consumer.py (the ONLY
    # honest refusal code now); any other odd rc also means the boundary did
    # not open -- never blame ECC/data for it.
    uid_hint ;;
esac
exit $rc