# observability/ —— LMCache 监控栈

kv-persistence 持久缓存服务的完整监控，取自 LMCache 官方示例
（`LMCache/LMCache` `examples/observability`，Apache-2.0）并做了适配，
使得**一条命令**即可启动全部：

```bash
bash scripts/start-observability.sh
# 或等价地：
cd observability && docker compose up -d
```

## 启动内容

| 服务 | 端口 | 用途 |
|---|---|---|
| OTel Collector | 4320（OTLP gRPC）、8889（抓取） | 接收 lmcache 服务的遥测，分发到 Tempo + Prometheus |
| Prometheus | 9091 | 指标存储（保留 7 天） |
| Tempo | 3200 | 跟踪存储 + TraceQL 指标 |
| Grafana | **3000** | 仪表盘界面（匿名 Admin，无登录表单） |

## Grafana

打开 <http://localhost:3000> → **Dashboards**。两个仪表盘已从
`grafana/provisioning/dashboards/` 预置：

* **LMCache** —— 缓存命中率、StorageManager 读写速率、L1/L2 占用、
  淘汰以及实时跟踪面板。
* **LMCache Coordinator** —— MP 协调器健康状况（仅在协调器运行时填充）。

数据源（Prometheus、Tempo）由 `grafana/provisioning/datasources/datasources.yml`
自动配置。

## 连接缓存服务

在 lmcache 服务开始向收集器推送遥测之前，该栈是被动的。用任一开关启用
（默认关闭 —— 缓存不依赖它也能完整工作）：

```bash
USE_OBSERVABILITY=1 bash scripts/start-cache.sh
# 或显式指定：
KVP_OTLP_ENDPOINT=http://localhost:4320 bash scripts/start-cache.sh
```

启用 OTLP 时会自动附加 `--enable-tracing`，因此请求瀑布图会出现在 Tempo
（Explore → 数据源 Tempo）。

## 值得关注的指标

| 面板 / 指标族 | 含义 |
|---|---|
| cache hit rate | 每个请求的命中/未命中比 —— 首要健康指标 |
| L1 / L2 usage | 各层占用与容量（L1 = 主机内存，L2 = NVMe） |
| store / retrieve throughput | 写入/读取 NVMe 的每秒字节数 |
| evictions | 各层 LRU 淘汰；速率上升意味着预算过小 |
| restore (prefetch) time | 重启后的服务多快从 NVMe 回填 L1 |

## 文件

| 文件 | 内容 |
|---|---|
| `docker-compose.yml` | 4 服务栈（收集器、tempo、prometheus、grafana） |
| `otel-collector.yml` | OTLP 接收器 → Tempo + Prometheus 分发 |
| `prometheus.yml` | 抓取收集器的 Prometheus 端点 |
| `tempo.yml` | 本地跟踪存储 + local-blocks TraceQL 指标 |
| `grafana/provisioning/` | 自动配置的数据源 + LMCache 仪表盘 |

## 停止

```bash
bash scripts/start-observability.sh --down
```

数据卷（`prometheus_data`、`tempo_data`、`grafana_data`）保留；如需干净
环境请用 `docker volume rm` 手动删除。

## 致谢

栈配置与仪表盘来自 LMCache 官方仓库
（`LMCache/LMCache`，Apache-2.0，`examples/observability`）。它们被内联到
本仓库中，使 `docker compose up -d` 可离线使用。