# observability/ — LMCache monitoring stack

Full monitoring for the kv-persistence cache service, copied from the official
LMCache example (`LMCache/LMCache` `examples/observability`, Apache-2.0) and
adapted so that **one command** brings everything up:

```bash
bash scripts/start-observability.sh
# or, equivalently:
cd observability && docker compose up -d
```

## What runs

| Service | Port | Purpose |
|---|---|---|
| OTel Collector | 4320 (OTLP gRPC), 8889 (scrape) | receives telemetry from the lmcache server, fans out to Tempo + Prometheus |
| Prometheus | 9091 | metrics storage (7 days retention) |
| Tempo | 3200 | trace storage + TraceQL metrics |
| Grafana | **3000** | dashboards UI (anonymous Admin, no login form) |

**All ports are bound to `127.0.0.1` only** (the `docs/security.md` loopback
contract). Grafana runs with anonymous Admin access and the login form
disabled — exposing it on `0.0.0.0` would hand out admin on the whole network.
To reach the stack from another host, put an authenticated reverse proxy in
front of it; do NOT widen the port bindings.

**Images are pinned** to exact versions (`otel/opentelemetry-collector-contrib:0.164.0`,
`grafana/tempo:2.7.2`, `prom/prometheus:v3.15.1`, `grafana/grafana:13.2.5`), matching
the project's "no `:latest`" rule. Upgrading is a deliberate action: edit the
tag in `docker-compose.yml`, re-run `docker compose up -d`, re-check the
dashboards.

## Grafana

Open <http://localhost:3000> → **Dashboards**. Two dashboards are
pre-provisioned from `grafana/provisioning/dashboards/`:

* **LMCache** — cache hit rate, StorageManager read/write rates, L1/L2 usage,
  evictions and the live trace panel.
* **LMCache Coordinator** — MP coordinator health (only populated when a
  coordinator runs).

Datasources (Prometheus, Tempo) are provisioned automatically from
`grafana/provisioning/datasources/datasources.yml`.

## Connecting the cache service

The stack is passive until the lmcache server pushes to the collector. Enable
it with either switch (off by default — the cache works fine without it):

```bash
USE_OBSERVABILITY=1 bash scripts/start-cache.sh
# or explicitly:
KVP_OTLP_ENDPOINT=http://localhost:4320 bash scripts/start-cache.sh
```

`--enable-tracing` is added automatically when OTLP is enabled, so request
waterfalls appear in Tempo (Explore → datasource Tempo).

## Metrics worth watching

| Panel / metric family | Meaning |
|---|---|
| cache hit rate | hit / miss ratio per request — the primary health signal |
| L1 / L2 usage | tier occupancy vs capacity (L1 = host RAM, L2 = NVMe) |
| store / retrieve throughput | bytes/s written to and read from NVMe |
| evictions | LRU evictions per tier; a rising rate means the budget is too small |
| restore (prefetch) time | how fast a restarted server refills L1 from NVMe |

## Files

| File | Content |
|---|---|
| `docker-compose.yml` | 4-service stack (collector, tempo, prometheus, grafana) |
| `otel-collector.yml` | OTLP receiver → Tempo + Prometheus fan-out |
| `prometheus.yml` | scrapes the collector's Prometheus endpoint |
| `tempo.yml` | local trace storage + local-blocks TraceQL metrics |
| `grafana/provisioning/` | auto-provisioned datasources + LMCache dashboards |

## Stopping

```bash
bash scripts/start-observability.sh --down
```

Data volumes (`prometheus_data`, `tempo_data`, `grafana_data`) are kept; remove
them manually with `docker volume rm` if you want a clean slate.

## Attribution

The stack configuration and the dashboards originate from the official LMCache
repository (`LMCache/LMCache`, Apache-2.0, `examples/observability`). They are
vendored here so `docker compose up -d` works offline from this repository.