> 🌐 Language versions: **English** | [Русский](deployment.ru.md) | [中文](deployment.zh.md)

# Deployment

Linear path — no steps to add by hand. Commands are meant to be copied verbatim
onto a GPU host. The host runs the production recipe from
`sikkatu/recipes`; this project does not modify it.

## 0. Preconditions (host)

* Linux host (any distribution) with NVIDIA GPUs, Docker, `nvidia-smi`, the pinned vLLM image
  (`IMAGE` with `@sha256:` digest) and the model directory `MODEL_DIR`.
* Local NVMe with at least `KVP_CACHE_BUDGET_GB` (default 1.5 TB) free on the
  pinned device — preflight checks this against `KVP_CACHE_DEVICE`.
* Production recipe cloned read-only (e.g. `~/sikkatu-recipes`).
* Python 3.12 on the host (for `kvpersist` admin + scripts and the `lmcache`
  CLI). On Debian/Ubuntu (tested on Debian 12) the system Python is 3.11: install a 3.12 interpreter and
  `python3-venv` separately (e.g. from bookworm-backports or pyenv) and use
  `python3.12 -m venv` in step 3. The venv must be created by the SAME 3.12
  interpreter that will run `lmcache`.

## 1. Get kv-persistence

```bash
git clone https://github.com/sikkatu/kv-persistence ~/kv-persistence
cd ~/kv-persistence
export REPO="$PWD"
```

`REPO` is used by every later command here. Exported variables (`REPO`,
`IMAGE`, `MODEL_DIR`) live only in their terminal shell: in a NEW terminal
repeat these exports before continuing (shell behaviour, not a bug).

## 2. Configure

Site-local overrides all live in `*.local` files, which win over the shipped
defaults and are gitignored (`setup-identity.sh` writes `identity.env.local`,
never the tracked `identity.env`):


```bash
cp configs/generic/cache.env configs/generic/cache.env.local
$EDITOR configs/generic/cache.env.local        # KVP_CACHE_ROOT must be on NVMe
# identity.env.local is created automatically by setup-identity.sh below;
# edit identity.env ONLY for fields you deliberately pin yourself (e.g. HF SHA).
```

**Identity values are filled and verified automatically** — do not assemble them
by hand:

```bash
bash "$REPO/scripts/setup-identity.sh" "$HOME/models/<your-model-dir>"
```

One command covers ALL mandatory fields (`KVP_MODEL_REVISION`,
`KVP_TOKENIZER_REVISION`, `KVP_CHAT_TEMPLATE_DIGEST`), writes them into
`configs/mimo-v2.6-pro/identity.env.local` (site override, gitignored — the
tracked `identity.env` is NEVER modified, keeping `git pull` conflict-free)
and verifies the result (the cache
namespace must compute). Tokenizer revision is derived from the tokenizer files
themselves when no separate commit SHA is known — and the script refuses to
guess anything uncertain. Setting `KVP_MODEL_REVISION` in the environment first
overrides the file-derived fingerprint (use the HF commit SHA if you track
remote provenance).

To obtain the HF commit SHA for `KVP_MODEL_REVISION` override (downloads
NOTHING):

```bash
curl -fsSL https://huggingface.co/api/models/<your-org>/<your-model> \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])'
```

## 3. Install LMCache CLI

```bash
python3 -m venv ~/.venvs/lmcache
~/.venvs/lmcache/bin/pip install --upgrade pip
~/.venvs/lmcache/bin/pip install -r "$REPO/requirements-lmcache.txt"
export PATH="$HOME/.venvs/lmcache/bin:$PATH"
```

The LMCache version is **pinned** in `requirements-lmcache.txt` (`lmcache==X.Y.Z`)
together with the documented vLLM image combination — an unpinned install can
resolve to a release that changed the MP-connector protocol or the L2 adapter
behaviour. Do not upgrade lmcache without replaying `scripts/test-recovery.sh`.
**Note**: `openai` is pinned there too -- the lmcache 0.5.6 CLI imports it at
startup (`ModuleNotFoundError: No module named 'openai'` otherwise), even for
`lmcache server`. Install the requirements file into the SAME venv that
provides the `lmcache` command.
> LMCache must be the release channel matched to the pinned vLLM/PyTorch
> runtime of the production image — see docs/compatibility.md. If plain pip
> wheels mismatch the image ABI, run the `lmcache server` from the same image
> (`docker run --rm --entrypoint lmcache "$IMAGE" server …`) and adapt
> `scripts/start-cache.sh`; the arguments stay identical.

**Note for the pinned 0.5.6 CLI**: `lmcache server --l2-adapter` takes the
adapter spec as a JSON **string**, not a path to a JSON file. `start-cache.sh`
handles this automatically (it inlines the rendered spec), but if you start the
server by hand, pass `--l2-adapter "$(cat l2-adapter.rendered.json)"` rather
than `--l2-adapter l2-adapter.rendered.json`.

## 4. Preflight

```bash
bash scripts/preflight.sh
```

Expected output ends with `preflight PASSED`. Any `[!!]` must be fixed before
going on. If `KVP_MODEL_DIR` is set, the script prints
`KVP_CHAT_TEMPLATE_DIGEST=…` — copy that value into `identity.env.local`
(site override; the tracked `identity.env` is not edited).

## 5. Start the persistent cache service
```bash
bash scripts/start-cache.sh
```
This creates `KVP_CACHE_ROOT` (chmod 700), renders the L2 adapter JSON, and
starts `lmcache server` detached (`lmcache-server.pid`, log
`KVP_CACHE_ROOT/lmcache-server.log`). Existing cache data is preserved — there
is no automatic wipe on start.

### Lossless L2 codec (optional)

By default the L2 tier stores **byte-exact raw** chunks (`LMCACHE_L2_SERDE=none`)
— identical to the uncompressed upstream path, no transform at all. To store
them with lossless compression instead (modest, roughly 1.1–1.4x on real KV
payloads), pick a codec:

```bash
USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh     # zstd
USE_LOSSLESS_SERDE=lz4 bash scripts/start-cache.sh   # lz4
# Pin the SAME codec as KVP_L2_SERDE in identity.env.local -- the identity
# gate compares it against the server's rendered L2 adapter.
```

or permanently in `configs/generic/lmcache-server.env(.local)`:

```
### The cache server MUST run as root (round 45)

The CUDA IPC boundary test (`bash scripts/ipc-test.sh`) proved on the
target host: the engine container (root) creates GPU memory that only
ROOT on the host can open. A non-root cache server fails at
`register_kv_caches` with `CUDA_ERROR_MAP_FAILED`. Start BOTH services
with sudo:

```bash
sudo USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh
sudo USE_LOSSLESS_SERDE=1 bash scripts/run-vllm-with-persist.sh
```

`start-cache.sh` resolves the lmcache venv python under sudo
automatically (`SUDO_UID`/`SUDO_USER` -- no manual export needed).
Cache files will be root-owned: run `budget-report.sh` / `purge` with
sudo as well.bash
LMCACHE_L2_SERDE=zstd     # or lz4, or none
```

There is **no lossy quantization** in this project (fp8 / turboquant / lbq are
refused loudly at startup). The codec is part of the cache-identity namespace:
switching codecs turns previously stored chunks into a safe cache miss (never a
wrong read), so use the same value for every launcher.

`KVP_L2_COMPRESS_LEVEL` (zstd only, default 3, range 1–19) trades ratio for
store speed.

## 6. Start vLLM with the persistent KV connector

```bash
export IMAGE='…@sha256:<64-hex-digest>'   # same digest as production
export MODEL_DIR="$HOME/models/<your-model-dir>"
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

### Prometheus + Grafana (optional)

The full observability stack (OTel Collector + Tempo + Prometheus + Grafana)
ships in `observability/` with the official LMCache dashboards pre-provisioned:

```bash
bash scripts/start-observability.sh
# Grafana -> http://localhost:3000 (anonymous Admin, no login)
```

Then point the cache service at the collector so metrics flow
(`KVP_OTLP_ENDPOINT=http://localhost:4320`, or the `USE_OBSERVABILITY=1`
shorthand). Telemetry is off by default: the cache works fine without the
stack. Details and the panel list are in `observability/README.md`.
## Running the cache service under systemd (optional)

Instead of `scripts/start-cache.sh` (which detaches via `setsid nohup`), the
cache service can run as a systemd unit — survives reboot, restarts on
failure, logs in journald. The unit calls a small wrapper that inlines the L2
adapter JSON (the pinned LMCache 0.5.6 CLI takes `--l2-adapter` as a JSON
STRING, not a file path).

```bash
sudo install -m 0755 systemd/kvp-lmcache-server /usr/local/sbin/kvp-lmcache-server
sudo install -m 0644 systemd/lmcache-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lmcache-server
journalctl -u lmcache-server -f
```

* The wrapper renders the adapter spec from
  `configs/${KVP_MODEL_CONFIG:-mimo-v2.6-pro}/l2-adapter.json.in` when no
  rendered file exists yet (clean host), so the unit starts before
  `start-cache.sh` has ever run. Set `KVP_REPO_ROOT` in
  `/etc/kv-persistence.env` to the repo checkout path (README clones to
  `~/kv-persistence`; the unit defaults to `/home/kvp/kv-persistence`).
* The wrapper reads `cache.env` / `cache.env.local` for `KVP_CACHE_ROOT` and
  `KVP_CACHE_BUDGET_GB` with the same precedence as `scripts/env.sh` (process
  env wins over files), so a custom cache root pinned in `cache.env.local` is
  honoured -- no silent 1.5 TB on the system disk.
* The unit runs as user `kvp` (create it or edit `User=`/`Group=`); for
  per-instance users use the template unit `lmcache-server@.service`
  (`systemctl enable --now lmcache-server@<user>` — `User=%i` is only valid in
  a template unit).
* **Cache root ownership**: the service user must be able to write the cache
  root. For the DEFAULT `/var/lib/kv-persistence` the unit's
  `StateDirectory=kv-persistence` creates it with the right ownership. For a
  custom `KVP_CACHE_ROOT` run once:
  ```bash
  sudo install -d -o kvp -g kvp -m 0700 /mnt/nvme/kvp   # example path
  ```
  The wrapper also checks writability at startup and prints this exact command
  if the directory is not writable.
* Do NOT run both the unit and `start-cache.sh` at the same time: they would
  fight over the same cache root and ports.

## Stopping (engine vs cache service)
The engine and the cache service are SEPARATE processes; stop each one
explicitly:

* **vLLM engine** (the container started by `run-vllm-with-persist.sh`):
  `bash scripts/stop-engine.sh` (or `docker rm -f mimo-pro-tp4-kvp` if the
  script is unavailable). Ctrl-C during the launcher only stops the
  LAUNCHER -- the container keeps running; run `stop-engine.sh` after it.
* **Cache service** (lmcache server): `bash scripts/stop-cache.sh`
  (graceful SIGTERM, data files are KEPT).

Stopping either one never deletes persistent cache data on disk.

## Rollback (one command)

```bash
bash $HOME/sikkatu-recipes/recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit/scripts/run-tp4-uva.sh
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