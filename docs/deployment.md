> Multilingual docs: [English](https://github.com/sikkatu/kv-persistence/wiki/Quick-Start) | [Русский](https://github.com/sikkatu/kv-persistence/wiki/Quick-Start.ru) | [中文](https://github.com/sikkatu/kv-persistence/wiki/Quick-Start.zh)

# Deployment

Linear path — no steps to add by hand. Commands are meant to be copied verbatim
onto the 4-GPU host (Debian). The host runs the production recipe from
`sikkatu/recipes`; this project does not modify it.

## 0. Preconditions (host)

* Debian host with 4× RTX PRO 6000, Docker, `nvidia-smi`, the pinned vLLM image
  (`IMAGE` with `@sha256:` digest) and the model directory `MODEL_DIR`.
* Local NVMe with at least `KVP_CACHE_BUDGET_GB` (default 750) free, physical
  ceiling 1 TB.
* Production recipe cloned read-only (e.g. `~/recipes`).
* Python 3.12 on the host (for `kvpersist` admin + scripts).

## 1. Get kv-persistence

```bash
git clone https://github.com/sikkatu/kv-persistence ~/kv-persistence
cd ~/kv-persistence
```

## 2. Configure

```bash
cp configs/generic/cache.env configs/generic/cache.env.local
$EDITOR configs/generic/cache.env.local        # KVP_CACHE_ROOT must be on NVMe
$EDITOR configs/mimo-v2.6-pro/identity.env     # fill KVP_MODEL_REVISION (40-hex)
```

To obtain the model revision (this downloads NOTHING):

```bash
curl -fsSL https://huggingface.co/api/models/XiaomiMiMo/MiMo-V2.6-Pro-MOPD \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])'
```

## 3. Install LMCache CLI

```bash
python3 -m venv ~/.venvs/lmcache
~/.venvs/lmcache/bin/pip install --upgrade pip
~/.venvs/lmcache/bin/pip install lmcache
export PATH="$HOME/.venvs/lmcache/bin:$PATH"
```

> LMCache must be the release channel matched to the pinned vLLM/PyTorch
> runtime of the production image — see docs/compatibility.md. If plain pip
> wheels mismatch the image ABI, run the `lmcache server` from the same image
> (`docker run --rm --entrypoint lmcache "$IMAGE" server …`) and adapt
> `scripts/start-cache.sh`; the arguments stay identical.

## 4. Preflight

```bash
bash scripts/preflight.sh
```

Expected output ends with `preflight PASSED`. Any `[!!]` must be fixed before
going on. If `KVP_MODEL_DIR` is set, the script prints
`KVP_CHAT_TEMPLATE_DIGEST=…` — copy that value into `identity.env`.

## 5. Start the persistent cache service

```bash
bash scripts/start-cache.sh
```

This creates `KVP_CACHE_ROOT` (chmod 700), renders the L2 adapter JSON, and
starts `lmcache server` detached (`lmcache-server.pid`, log
`KVP_CACHE_ROOT/lmcache-server.log`). Existing cache data is preserved — there
is no automatic wipe on start.

## 6. Start vLLM with the persistent KV connector

```bash
export IMAGE='…@sha256:<64-hex-digest>'   # same digest as production
export MODEL_DIR="$HOME/models/MiMo-V2.6-Pro-MOPD"
bash scripts/run-vllm-with-persist.sh
```

The script mirrors the production launcher arguments and adds
`--kv-transfer-config` pointing vLLM at the running lmcache server (MP mode,
`LMCacheMPConnector` via `kv_connector_module_path`). Recipe patches are
verified against `MANIFEST.sha256` and mounted **read-only**.

## 7. Verify

```bash
bash scripts/test-recovery.sh
```

This runs the acceptance scenarios (warm → restart vLLM → re-send; restart both
services → re-send) and reports `cached_tokens` and lmcache `Retrieved` lines
per phase under `benchmarks/results/`. PASS requires disk-backed recovery
evidence, not speed alone (see docs/benchmarks.md).

## Monitoring

```bash
bash scripts/budget-report.sh status      # usage vs budget, namespaces, disk free
bash scripts/budget-report.sh quota       # per-namespace quotas
bash scripts/budget-report.sh purge --help
```

## Rollback (one command)

```bash
bash $HOME/recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/scripts/run-tp4-uva.sh
```

The production recipe is unchanged; its launcher runs exactly as before.
Persistent KV data on NVMe is NOT deleted and is simply ignored. To also stop
the cache service (keeping data): `bash scripts/stop-cache.sh`. To erase cached
data deliberately: `bash scripts/budget-report.sh purge --all --yes`
(manual, explicit — never done automatically).

## Changing the cache limit

Edit `KVP_CACHE_BUDGET_GB` in `cache.env.local` and restart the cache service
(`bash scripts/stop-cache.sh && bash scripts/start-cache.sh`). Data is kept;
only the effective budget (and `max_capacity_gb` in the rendered adapter spec)
changes. Raising it requires free disk; lowering it below current usage triggers
eviction on the next maintenance cycle (soft-watermark), the hard bound stops
new stores immediately.

## Host restart / new terminal

`export` variables live only in their terminal. In a new terminal, re-export
`IMAGE` and `MODEL_DIR` (and optionally `PATH` to the lmcache venv) before
running scripts. After any host reboot check `free -h` and that the NVMe is
mounted before starting the cache service.