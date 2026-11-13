# KV-Persistence Observability Extras

This directory holds the **extras layer** of the kv-persistence monitoring
stack: an inference dashboard over the live vLLM engine metrics and (optional)
per-GPU telemetry. Everything came in review round 87-88 (Nov 2026) as an
overlay on top of the original LMCache dashboards; the base stack keeps
working if this whole directory is deleted.

```
extras/
  README.md (this file; README.ru.md / README.zh.md alongside)
  scrape.d/vllm.yml gpu.yml       # Prometheus file_sd scrape jobs
  rules.d/vllm.yml gpu.yml        # recording rules -> stable kvp:* names
  expected-metrics.txt            # fingerprint of vllm:* names @ v0.32.1rc1.dev197
  targets/                        # file_sd targets, GENERATED per machine (gitignored)
  relay/nginx.conf.template       # vLLM metrics relay (loopback engine -> bridge network Prometheus)
  gpu/dcgm-counters.csv           # DEFAULT DCGM counter set (core fields only)
  gpu/dcgm-counters.prof.csv      # PROFILING DCGM fields (opt-in, needs SYS_ADMIN)
  grafana/dashboards/kvp-inference.json   # the dashboard (uid: kvp-inference)
  check.sh                        # self-check: --static (no docker) / --live
```

## What is mounted where

A single override file (`../docker-compose.override.yml`) adds three
**profile-gated** services alongside the four base containers. Nothing
published on the host; Prometheus reaches the exporters over the compose
network, and the relay reaches the engine over host loopback.

| service | profile | image (pinned) | what it does |
|---|---|---|---|
| `vllm-relay` | `extras` | `nginx:1.29.4-alpine` | proxies GET /metrics from the engine (127.0.0.1) on the Docker bridge gateway address, so bridge-network Prometheus can scrape a loopback-only engine. Authorization header is stripped; anything but GET /metrics is 404/405. |
| `dcgm-exporter` | `gpu` | `nvcr.io/nvidia/k8s/dcgm-exporter:4.5.2-4.8.1-ubuntu22.04` | NVIDIA DCGM GPU telemetry. Counter list comes from `gpu/dcgm-counters.csv` mounted at `/etc/dcgm-exporter/default-counters.csv` (and pointed at with `--collectors=`). Tag confirmed live against `https://nvcr.io/v2/nvidia/k8s/dcgm-exporter/tags/list` on 2026-11-13 -- run `docker manifest inspect` on your own host to double-check before profit. |
| `nvidia-smi-exporter` | `gpu-smi` | `utkuozdemir/nvidia_gpu_exporter:1.3.0` | fallback GPU telemetry reusing `nvidia-smi` calls (`KVP_OBS_GPU_EXPORTER=smi`). No PCIe traffic metrics. |

**Image sizes were NOT invented** (rule 11.3: no made-up numbers) -- run
`docker image ls` after the first pull and paste the real sizes here:

```
REPOSITORY                                       TAG                     SIZE
nvcr.io/nvidia/k8s/dcgm-exporter                 4.5.2-4.8.1-ubuntu22.04 (fill in)
utkuozdemir/nvidia_gpu_exporter                  1.3.0                   (fill in)
nginx                                            1.29.4-alpine           (fill in)
```

## How to turn pieces on/off

| env var | default | effect |
|---|---|---|
| `KVP_OBS_EXTRAS` | `1` | `0` = do not deploy ANY extras service or dashboard (base stack untouched) |
| `KVP_OBS_GPU` | `auto` | `off` = never start a GPU exporter; `on` = force; `auto` = start if `nvidia-smi` + NVIDIA container runtime are detected |
| `KVP_OBS_GPU_EXPORTER` | `dcgm` | `smi` = swap to the `nvidia_gpu_exporter` fallback (`gpu-smi` profile) |
| `KVP_OBS_GPU_PROF` | `0` | `1` = enable DCGM PROFIling fields (SM active / DRAM active / PCIe RX-TX). Only under `gpu` (dcgm) profile. Adds `cap_add: SYS_ADMIN` to the exporter -- read `# Caps and profiling` below first. |
| `KVP_OBS_RELAY_PORT` | `30008` | relay listen port (bound to the bridge gateway address only, never 0.0.0.0) |

## First start caveat -- one-time restart of base containers

Adding these extras mounts **new volumes into `obs_prometheus` and
`obs_grafana`**. Docker Compose therefore RECREATES those two containers the
FIRST time `start-all.sh` runs with extras enabled. Their DATA volumes
(`prometheus_data`, `grafana_data`) are preserved -- history and dashboards
survive; the containers themselves restart once. Subsequent runs are no-ops.
This is unavoidable when mounting new config into long-lived containers.

## Dashboard: `kv-persistence - Inference (vLLM + GPU)`

Provisioned automatically into the Grafana folder **kv-persistence** (uid:
`kvp-inference`; look for `Dashboards -> kv-persistence` after the stack is
up). It answers, top to bottom:

* how many chats compute vs queue right now (`Running` / `Waiting` panes),
* per-chat speed (`per-stream tok/s`, the headline number),
* KV-pool fill `KV usage %` -- +80% yellow, +90% red, preemptions arrive,
* GPU row: honest workload = **power draw**; NVML "utilization" reads 100%
  whenever a kernel is resident, that is NOT load (panel title says so).

The dashboard reads ONLY `kvp:*` recording rules (`rules.d/`), never raw
`vllm:*`/`DCGM_*` -- so when vLLM or DCGM rename a metric upstream, one line
in `rules.d/*.yml` is fixed and all 32 panels keep working.

### {#empty-panels} Empty panels -- what causes what

| panel | probable cause | how to verify |
|---|---|---|
| `Running` / `Waiting` / any vLLM row | engine not up yet (relay shows `up=0`), or engine port != `PORT` | `bash extras/check.sh --live` -- job `vllm`; engine log line `Starting vLLM server on http://...` |
| `KV usage %` | engine under load but NO requests yet (ratio 0 = normal) | fire one request; panel must move |
| entire GPU row | `KVP_OBS_GPU=off`, auto-detect found no NVIDIA runtime, or the exporter crashed | `KVP_OBS_GPU=on bash scripts/start-observability.sh`; `docker compose logs dcgm-exporter` |
| GPU row numeric values all zero / wrong | DCGM does not expose the requested fields on this GPU/driver; rules have `or`-fallbacks so a missing name silently falls through | `bash extras/check.sh --live` -- metric-drift WARN lists disappeared/renamed series |
| "SM active / DRAM active / PCIe" (profile row) | `KVP_OBS_GPU_PROF=0` (default) -- profiling counters are OFF | set `KVP_OBS_GPU_PROF=1` (see Caps below) |
| wrong units / "2.4 kHz" on clocks | NEVER -- clocks are already in Hz via `kvp:gpu_sm_clock_hertz` (fixed in r88 M3) | see json check in `check.sh --static` |

## Caps and profiling (`KVP_OBS_GPU_PROF=1`)

NVIDIA DCGM profiling fields (SM-active, DRAM-active, PCIe bandwidth) are
collected through CUPTI and typically require `SYS_ADMIN` in the container.
`KVP_OBS_GPU_PROF=1` therefore adds `cap_add: SYS_ADMIN` to the dcgm
exporter and swaps the mounted counters file to `dcgm-counters.live.csv`
(copy of `dcgm-counters.prof.csv`; gitignored). Two documented NVIDIA caveats:

* profiling counters CONFLICT with Nsight / CUPTI-based tools -- do not run
  both at once on the same GPU;
* on workstations / virtualized nodes the profiling surface may be disabled
  entirely -- the panels then simply show `no data`, never an error.

## How to update

1. change any pinned tag in `../docker-compose.override.yml`,
2. `bash scripts/start-all.sh` (extras re-render is idempotent),
3. `bash extras/check.sh --live` -- parse WARN/FAIL lines.

## How to edit

**Provisioned dashboards and rules are reloaded from files on every start --
UI edits in Grafana will be OVERWRITTEN.** To change a panel or add a rule:
edit `grafana/dashboards/kvp-inference.json` / `rules.d/*.yml` in the repo,
restart (`bash scripts/start-observability.sh`), then re-run `check.sh --static`
to validate. Never save exports straight from Grafana back into this tree
without clearing `id`/`version`/`meta` first (check.sh enforces `id: null`).

## Self-check

```
bash extras/check.sh --static    # no docker needed; exit 0 = clean, 1 = FAIL, 2 = WARN
bash extras/check.sh --live      # adds Prometheus target health, relay behaviour,
                                 # metric drift, Grafana loading
bash extras/check.sh --json      # machine-readable variant of the above
```

Everything it checks is read-only; the script NEVER changes system state.
