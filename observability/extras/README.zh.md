# KV-Persistence 可观测性扩展（Extras）

本目录存放 kv-persistence 监控栈的**扩展层**：基于 vLLM 引擎实时指标的推理
仪表盘，以及（可选的）GPU 遥测。所有内容来自 2026 年 11 月的 87–88 轮评审，
是叠加在原有 LMCache 仪表盘之上的增强；整个目录删除后基础栈仍能正常运行。

```
extras/
  README.md（本文件；旁置 README.ru.md / README.zh.md）
  scrape.d/vllm.yml gpu.yml       # Prometheus file_sd 抓取任务
  rules.d/vllm.yml gpu.yml        # recording rules -> 稳定的 kvp:* 名称
  expected-metrics.txt            # vllm:* 名称指纹 @ v0.32.1rc1.dev197
  targets/                        # file_sd 目标，按机器生成（gitignored）
  relay/nginx.conf.template       # vLLM 指标中继（引擎 loopback -> bridge 网络）
  gpu/dcgm-counters.csv           # DCGM 默认计数器（仅核心字段）
  gpu/dcgm-counters.prof.csv      # DCGM 性能剖析字段（可选，需 SYS_ADMIN）
  grafana/dashboards/kvp-inference.json   # 仪表盘（uid: kvp-inference）
  check.sh                        # 自检脚本：--static / --live
```

## 挂载关系

唯一 override 文件（`../docker-compose.override.yml`）在四个基础容器旁添加
三个带 **compose profile** 的服务。不向宿主机发布任何端口：Prometheus 通过
compose 网络访问 exporter，中继通过宿主机 loopback 访问引擎。

| 服务 | profile | 镜像（固定标签） | 作用 |
|---|---|---|---|
| `vllm-relay` | `extras` | `nginx:1.29.4-alpine` | 将引擎（127.0.0.1）的 GET /metrics 代理到 Docker bridge 网关地址；Authorization 头被剥离；非 GET /metrics 的请求返回 404/405。 |
| `dcgm-exporter` | `gpu` | `nvcr.io/nvidia/k8s/dcgm-exporter:4.5.2-4.8.1-ubuntu22.04` | NVIDIA DCGM GPU 遥测。计数器来自 `gpu/dcgm-counters.csv`（挂载到 `/etc/dcgm-exporter/default-counters.csv`）。标签已于 2026-11-13 通过 `https://nvcr.io/v2/nvidia/k8s/dcgm-exporter/tags/list` API 确认。 |
| `nvidia-smi-exporter` | `gpu-smi` | `utkuozdemir/nvidia_gpu_exporter:1.3.0` | 通过 nvidia-smi 采集的备用 exporter（`KVP_OBS_GPU_EXPORTER=smi`），无 PCIe 流量指标。 |

**镜像大小未凭空编造**（规则 11.3）——首次拉取后运行 `docker image ls`，将实际
大小填入此处。

## 开关

| 变量 | 默认 | 效果 |
|---|---|---|
| `KVP_OBS_EXTRAS` | `1` | `0` = 不部署任何 extras 服务或仪表盘（基础栈不变） |
| `KVP_OBS_GPU` | `auto` | `off` = 从不启动 GPU exporter；`on` = 强制；`auto` = 检测到 nvidia-smi + NVIDIA container runtime 时启动 |
| `KVP_OBS_GPU_EXPORTER` | `dcgm` | `smi` = 切换到 nvidia_gpu_exporter（gpu-smi profile） |
| `KVP_OBS_GPU_PROF` | `0` | `1` = 启用 DCGM 性能剖析字段（SM active / DRAM active / PCIe RX-TX）。仅限 gpu profile；添加 cap_add: SYS_ADMIN |
| `KVP_OBS_RELAY_PORT` | `30008` | 中继监听端口（仅绑定 bridge 网关地址，绝不绑定 0.0.0.0） |

## 首次启动提示 —— 基础容器一次性重启

Extras 会向 `obs_prometheus` 和 `obs_grafana` **挂载新卷**。首次启用 extras
运行 `start-all.sh` 时，Docker Compose 会**重新创建**这两个容器。数据卷
（`prometheus_data`、`grafana_data`）保留——历史和仪表盘不受影响；容器本身
重启一次。之后的运行均为幂等，不再重建。在长驻容器上添加新配置不可避免。

## 仪表盘：`kv-persistence - Inference (vLLM + GPU)`

自动配置到 Grafana 文件夹 **kv-persistence**（uid `kvp-inference`），路径：
Dashboards → kv-persistence。自上而下回答：

* 当前多少聊天在计算 vs 在排队（Running / Waiting）；
* 单聊天速度（per-stream tok/s）——核心数字；
* KV 池填充（KV usage %）——80% 黄色，90% 红色，之后出现抢占；
* GPU 行：诚实的工作量信号 = **功耗**；NVML「utilization」在内核常驻时
  就会读到 100%，那不是负载（面板标题明确写了这一点）。

仪表盘**只**读取 `kvp:*` recording rules（`rules.d/`），表达式中不出现原始
`vllm:*`/`DCGM_*`。当 vLLM 或 DCGM 在新版本中重命名指标时，只改
`rules.d/*.yml` 中的一行，32 块面板照常工作。

### 空面板 {#empty-panels} —— 原因与对策

| 面板 | 可能原因 | 如何验证 |
|---|---|---|
| Running / Waiting / vLLM 行 | 引擎未启动（relay 显示 `up=0`）或端口 != `PORT` | `bash extras/check.sh --live`，job vllm；引擎日志 `Starting vLLM server...` |
| KV usage % | 尚无请求（0 是正常的） | 发一个请求，面板应有变化 |
| 整个 GPU 行 | `KVP_OBS_GPU=off`，auto 未检测到 NVIDIA runtime，或 exporter 崩溃 | `KVP_OBS_GPU=on bash scripts/start-observability.sh`；`docker compose logs` |
| GPU 数值全为零 / 不正确 | DCGM 在该 GPU/驱动上未暴露请求的字段；规则中 or-回退静默跳过 | `check.sh --live`，WARN「metrics DISAPPEARED/NEW」列出漂移 |
| SM active / DRAM active / PCIe | `KVP_OBS_GPU_PROF=0`（默认） | 设置 `KVP_OBS_GPU_PROF=1` |
| 时钟显示 kHz | 永远不会——时钟已是 Hz（`kvp:gpu_sm_clock_hertz`，r88 M3 修复） | 见 check.sh --static |
| per-stream tok/s 为 NaN | 零流量时正常（无令牌间隔） | 发一个请求 |

## 权限与性能剖析（`KVP_OBS_GPU_PROF=1`）

DCGM 性能剖析字段通过 CUPTI 采集，通常需要容器 `SYS_ADMIN` 权限：
`KVP_OBS_GPU_PROF=1` 将计数器切换到 `dcgm-counters.live.csv`
（`dcgm-counters.prof.csv` 的副本，已加入 .gitignore）并在 override 中
启用 cap_add。NVIDIA 文档标注的两个限制：性能剖析与 Nsight/CUPTI 工具
**冲突**（不要在同一 GPU 上同时使用）；虚拟化节点上剖析接口可能被禁用
——面板显示 `no data`，不会报错。

## 如何更新

1. 修改 `../docker-compose.override.yml` 中固定标签；
2. `bash scripts/start-all.sh`（重复运行幂等）；
3. `bash extras/check.sh --live`——解析 WARN/FAIL。

## 如何编辑

**配置的仪表盘和规则在每次启动时从文件重新加载——Grafana UI 中的修改会被
覆盖。** 修改面板或添加规则：编辑仓库中的
`grafana/dashboards/kvp-inference.json` / `rules.d/*.yml`，重启后再运行
`check.sh --static` 验证。不要将 Grafana 导出的 JSON 直接放回目录——
必须先清除 `id`/`version`/`meta`（check.sh 强制 `id: null`）。

## 自检

```
bash extras/check.sh --static   # 无需 docker；0 = 干净，1 = FAIL，2 = WARN
bash extras/check.sh --live     # + 目标健康、中继行为、指标漂移、Grafana
bash extras/check.sh --json     # 机器可读报告
```

脚本只读操作——绝不改变系统状态。