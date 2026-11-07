> [English](README.md) | [Русский](README.ru.md) | **中文**

# kv-persistence —— 面向 vLLM 的通用持久化缓存

<p align="center">
  <img src="docs/assets/og-template.jpg" alt="kv-persistence —— 面向 vLLM 的持久化 NVMe KV 缓存" width="820">
</p>

面向 vLLM 的磁盘持久化 KV 缓存层：重启后可复用的提示词 KV 块从 NVMe 恢复，
而无需重新计算。设计目标 —— 聊天应用 中的长对话（10 万–100 万 token）几秒即可
重新打开，而不是几十分钟 —— 在目标硬件上用 `scripts/benchmark.sh` 实测；
验收运行完成后结果发布于 [docs/benchmarks.md](docs/benchmarks.md)
（见[状态](#状态)）。

**📖 文档（EN / RU / ZH）位于 Wiki：**
[首页](https://github.com/sikkatu/kv-persistence/wiki) ·
[快速开始](https://github.com/sikkatu/kv-persistence/wiki/Quick-Start.zh) ·
[架构](https://github.com/sikkatu/kv-persistence/wiki/Architecture.zh) ·
[配置](https://github.com/sikkatu/kv-persistence/wiki/Configuration.zh) ·
[持久化与恢复](https://github.com/sikkatu/kv-persistence/wiki/Persistence-and-Recovery.zh) ·
[存储与淘汰](https://github.com/sikkatu/kv-persistence/wiki/Storage-and-Eviction.zh) ·
[配方集成](https://github.com/sikkatu/kv-persistence/wiki/Recipes-Integration.zh) ·
[安全](https://github.com/sikkatu/kv-persistence/wiki/Security.zh) ·
[故障排查](https://github.com/sikkatu/kv-persistence/wiki/Troubleshooting.zh) ·
[FAQ](https://github.com/sikkatu/kv-persistence/wiki/FAQ.zh)

设计用于**任何 vLLM 服务模型与任何 NVIDIA GPU 主机**的复用——模型无关的
核心将策略层与模型集成分离。经硬件确认的仅有参考配置：
4× NVIDIA RTX PRO 6000 上的 MiMo-V2.6-Pro（经 `sikkatu/recipes` 配方）。
其他模型或主机在通过验收测试（`scripts/test-recovery.sh`）之前均属
**实验性兼容**；架构差异较大的模型预期需要适配启动器（补丁/模型加载器）。

## 作用

```
聊天应用 → vLLM (TP4) → KV 连接器（LMCacheMPConnector）
       → lmcache 服务（独立进程）→ L2（nixl_store_dynamic，POSIX）→ NVMe
```

* **跨重启持久化** —— 数据可挺过 vLLM 重启*以及*缓存服务完整重启
  （`persist_enabled=true`；磁盘索引按文件名惰性重建）。
* **受限存储** —— 硬上限（默认 1.5 TB，可按 NVMe 容量调整），软阈值（90%）
  自动 LRU 淘汰；缓存绝不在启动时自行清空。
* **兼容性命名空间** —— 模型版本、分词器、KV 类型、块布局与聊天模板都是
  缓存键的一部分：不兼容的部署绝不互读 KV 数据；任何不确定性都导致未命中，
  而非错误命中。命名空间通过 `lmcache_model_name()`（嵌入构成 ObjectKey 与
  L2 文件名的 `model_name`）在 LMCache 存储层强制生效。按用户/租户隔离请用
  `cache_salt`（第 15 轮已由 `kvpersist/salt_filename_patch.py` 在磁盘键层面
  修复，所有启动器均使用）或 `lmcache_model_name(extra_discriminator=...)` /
  独立缓存根目录。
* **模型无关内核** —— 缓存策略与模型集成分离；新增模型只需加配置，不改代码。
  不同模型的 KV 数据绝不共享。

## 文档索引

| 主题 | Wiki（EN/RU/ZH) | 仓库内（EN） |
|---|---|---|
| 安装 → 配置 → 启动 → 验证 | [快速开始](https://github.com/sikkatu/kv-persistence/wiki/Quick-Start.zh) | [docs/deployment.md](docs/deployment.md) |
| 拓扑、数据流、故障模型 | [架构](https://github.com/sikkatu/kv-persistence/wiki/Architecture.zh) | [docs/architecture.md](docs/architecture.md) |
| 全部旋钮、限额、配额 | [配置](https://github.com/sikkatu/kv-persistence/wiki/Configuration.zh) | [docs/storage-management.md](docs/storage-management.md) |
| 验收场景 A/B | [持久化与恢复](https://github.com/sikkatu/kv-persistence/wiki/Persistence-and-Recovery.zh) | [docs/benchmarks.md](docs/benchmarks.md) |
| 配方启动器中的 `KV_PERSIST=1` | [配方集成](https://github.com/sikkatu/kv-persistence/wiki/Recipes-Integration.zh) | [docs/compatibility.md](docs/compatibility.md) |
| 安全规则 | [安全](https://github.com/sikkatu/kv-persistence/wiki/Security.zh) | [docs/security.md](docs/security.md) |
| 若实的状态与缺口 | — | [docs/final-report.md](docs/final-report.md) |
| 各轮次更新日志 | — | [CHANGELOG.zh.md](CHANGELOG.zh.md) |

## 快速开始

```bash
git clone https://github.com/sikkatu/kv-persistence ~/kv-persistence
cd ~/kv-persistence
bash scripts/setup-identity.sh "$HOME/models/<your-model-dir>"   # 填写并核验身份固定项
bash scripts/preflight.sh               # 校验主机、存储与身份
bash scripts/start-cache.sh             # 持久缓存服务（NVMe）
bash scripts/run-vllm-with-persist.sh   # vLLM + KV 连接器（无需 root，无需 export）
bash scripts/test-recovery.sh           # 验收：重启 → 缓存复用证据（exit 0 = PASS）
```
自第 48 轮起启动无需 export 和标志：IMAGE 从配方解析（记入 `live/image`），
MODEL_DIR 从 `live/model_dir`/身份固定值/`~/models` 解析，same-UID 为默认模式，
编解码器默认 lossless zstd（关闭：`USE_LOSSLESS_SERDE=0`）。

每条命令都会在出错时明确拒绝，而不是留下半成品环境：`setup-identity.sh`
负责写入并核验身份值，`preflight.sh` 校验物理 NVMe 设备，
`test-recovery.sh` 以机器判定收尾。

生产配方中的常驻开关是 `tp4.env` 里的 `KV_PERSIST=1`
（见 wiki「配方集成」）；回滚只需删除这一行。
## 无损持久化
L2 层**只支持无损**。NVMe 上的 KV 块始终被解码为引擎产生的原始字节：项目中
不存在 fp8 / turboquant / lbq 量化，任何此类编解码在启动时都会被明确拒绝。
提供两种存储模式：
* **逐字节精确的原始模式（默认）** —— `LMCACHE_L2_SERDE=none`。块按原样存储，
  与未压缩的 upstream 路径完全一致。不做任何变换；缓存命中返回引擎的
  原始 KV 字节。
* **无损压缩（可选）** —— `LMCACHE_L2_SERDE=zstd` 或 `lz4`
  （或 `USE_LOSSLESS_SERDE` 开关，见下）。块由 `implementation/kvpersist/
  compress_serde.py` 注册的无损 `kvp-zstd` / `kvp-lz4` serde 压缩。
  未能压缩变小的块按原样存储，因此块永远不会膨胀超过一个 4 KiB 页
  （28 字节帧头加上填充到页边界）。真实 KV 负载上的预期缩减幅度温和 ——
  常被引用的范围约 **1.1–1.4x**，但该数字是*估算值*，不是本仓库的实测结果：
  这里尚未运行压缩率基准测试（见 `docs/final-report.md`）。请将其视为粗略的
  规划参考，而非保证。

每个帧携带**未压缩负载的 CRC32**（`KVCZ` 格式 v2）：被截断的 DMA 读取、
位翻转或用不同编解码写入的帧会在反序列化时被检测，并报告为缓存 **miss**
—— 绝不会作为静默损坏的 KV 返回。

**依赖：** 未压缩的默认模式（`none`）**完全不需要**任何压缩包。`zstd` 仅需
`zstandard`，`lz4` 仅需 `lz4` —— 启动检查（`require_codec`）只在服务器启动前
验证所选的那一个编解码，绝不要求未使用的那个。
为单次运行启用无损压缩：
```bash
USE_LOSSLESS_SERDE=1 bash scripts/start-cache.sh     # zstd
USE_LOSSLESS_SERDE=lz4 bash scripts/start-cache.sh   # lz4
```
或在 `configs/generic/lmcache-server.env(.local)` 中永久设置：
```bash
LMCACHE_L2_SERDE=zstd
```
编解码属于缓存身份命名空间的一部分：在 `none`、`zstd` 与 `lz4` 之间切换会让
已存储的块变成安全的缓存 **miss**（绝不会错误命中），并且每个启动器
（`start-cache.sh`、`run-vllm-with-persist.sh`、`verify-engine-identity.sh`）
都会把开关解析为同一个生效值。
## 监控（Prometheus + Grafana）
`observability/` 目录内置了官方 LMCache 示例的完整可观测性栈
（OTel Collector + Tempo + Prometheus + Grafana），并**已预置官方 LMCache
仪表盘**。一条命令即可启动全部：
```bash
bash scripts/start-observability.sh
# 或等价地：
cd observability && docker compose up -d
```
* **Grafana** → <http://localhost:3000>（匿名 Admin，无需登录）。
  *LMCache* 与 *LMCache Coordinator* 仪表盘会自动导入。

**仪表盘版本说明（第 18 轮）：** 这些仪表盘是 LMCache 的*官方*资产，但它们针对的是
比本仓库固定的 **0.5.6** 更新的 LMCache 版本编写的（下述仪表盘审计基于
0.5.6 源码）。与 0.5.6 源码
（`lmcache/v1/mp_observability/`）静态对比表明：多数面板映射到真实的 0.5.6 指标
（L1/L2 用量 gauge、store/prefetch 流水线、淘汰循环、事件总线、吞吐直方图），但
**28 个面板中有 4 个**引用了 0.5.6 从不输出的指标（lookup 的 prompt/request-hit
token 计数、L0↔L1 inflight store/retrieve 计数、transfer-phase DMA 计数）。这些
面板现已**改用真实的 0.5.6 指标**（命中率用 `lookup_requested`/`lookup_hit`，
L0↔L1 活动用 `l1_read`/`l1_write` 块计数，DMA 吞吐用 `l0_l1_store_throughput`）。
改用是尽力而为的语义匹配，不是上游保证 —— 标签/单位可能与原始意图略有差异。
*LMCache Coordinator* 仪表盘期望 0.5.6 的 `mp_coordinator` 并不导出的协调器遥测
（48 个系列在 0.5.6 中没有对应物）；本仓库不运行协调器，因此该仪表盘会合理地保持
空白。
* **Prometheus** → <http://localhost:9091>
* **Tempo** → <http://localhost:3200>（跟踪）
* **OTLP gRPC** → `localhost:4320` —— 缓存服务向此推送指标。

**安全：** 每个发布端口仅绑定到 `127.0.0.1`（`docs/security.md` 的回环契约）。
Grafana 以匿名 Admin 运行且登录表单被禁用 —— 暴露到 `0.0.0.0` 等于向整个
网络发放管理员权限。若需从其他主机访问，请在栈前放置带认证的反向代理。
所有镜像均固定到确切版本（无 `:latest`），与项目其余部分一致。

**Docker 模式：** 当缓存服务运行在 sidecar 容器中（`KVP_LAUNCH_MODE=docker`）时，
`start-cache.sh` 会自动把 OTLP 端点中的 `localhost`/`127.0.0.1` 改写为
`host.docker.internal` —— 容器内的 `localhost` 是容器自身，否则指标永远
无法到达收集器。
遥测自第 47b 轮起自动启用：`start-cache.sh` 检测到 `127.0.0.1:4320` 上的
收集器后开始推送——先启动监控栈，再（重新）启动缓存：
```bash
bash scripts/start-observability.sh   # 收集器 + Prometheus + Grafana
bash scripts/start-cache.sh           # 指标自动流动
```
需要时可显式覆盖：`USE_OBSERVABILITY=1` 强制开启，
`KVP_NO_OBSERVABILITY=1` 强制关闭，
`KVP_OTLP_ENDPOINT=http://host:port` 指定自定义端点。
LMCache 仪表盘上值得关注的面板：
| 面板 | 含义 |
|---|---|
| cache hit rate | 每个请求的命中/未命中比 —— 首要健康指标 |
| L1 / L2 usage | 各层占用与容量（L1 = 主机内存，L2 = NVMe） |
| store / retrieve throughput | 写入/读取 NVMe 的每秒字节数 |
| evictions | 各层 LRU 淘汰 —— 速率上升意味着预算过小 |
| restore (prefetch) time | 重启后的服务多快从 NVMe 回填 L1 |
用 `bash scripts/start-observability.sh --down` 停止该栈（数据卷保留）。
## 面向约 200 GB KV 的容量建议
KV 最多约 200 GB 的会话需要空间容纳这些数据并留有增长余量：
| 参数 | 建议 |
|---|---|
| `KVP_L1_SIZE_GB`（主机内存缓冲） | 30–40 GB |
| `KVP_CACHE_BUDGET_GB`（L2 硬上限） | 在**专用本地 NVMe** 上 ≥ 250–300 GB |
| `KVP_CACHE_ROOT` | 该 NVMe 上的路径，绝不用网络共享 |
| `KVP_CACHE_DEVICE` | 固定块设备，以便 `preflight.sh` 证明缓存位于正确磁盘 |
随附默认预算为 1500 GB，因为参考主机上 OS、模型与缓存共用一个卷；
在专用 NVMe 上请将其降至 300 GB。`KVP_CACHE_BUDGET_GB`、
`KVP_CACHE_ROOT` 与 `KVP_CACHE_DEVICE` 均位于
`configs/generic/cache.env(.local)`。
## 快速开始：无损 + Grafana
```bash
# 1. 监控（可选但推荐）
bash scripts/start-observability.sh

# 2. LMCache + vLLM（自第 48 轮起 lossless zstd 为默认）
bash scripts/start-cache.sh
bash scripts/run-vllm-with-persist.sh
```
`USE_LOSSLESS_SERDE` 在两条命令中必须一致 —— 它属于缓存身份命名空间。自第 46 轮起，
启动器还将有效编解码器写入 `$KVP_CACHE_ROOT/engine-identity.env`，独立脚本会自动读取
（开关 > `KVP_L2_SERDE` 固定值 > 文件）——新终端不会再报假的
`identity=none != engine/config=zstd` 不匹配。仍可显式在 `identity.env.local` 中固定
`KVP_L2_SERDE`。不设置该变量即保持逐字节精确的原始模式。
## 仓库结构
| 路径 | 内容 |
|---|---|
| `docs/` | 英文技术文档（各语言版本在 wiki） |
| `configs/generic/` | 缓存 + lmcache 服务配置，EN/RU/ZH 注释译文 |
| `configs/mimo-v2.6-pro/` | MiMo 身份固定与 L2 适配器模板，EN/RU/ZH |
| `scripts/` | 预检、缓存启停、启动器、恢复测试、基准、管理 CLI、可观测性 |
| `observability/` | Prometheus + Grafana + Tempo + OTel Collector 栈，含官方 LMCache 仪表盘 |
| `implementation/` | `kvpersist` 包：身份、预算、保留排序、管理 CLI、运行时补丁 |
| `tests/` | 单元（随处可跑）、集成（需 `lmcache`）、恢复（GPU 主机） |

## 状态

实现已完成；离线测试全部通过。GPU 主机上的验收测试已封装为脚本
（`scripts/test-recovery.sh`、`scripts/benchmark.sh`），由运维方执行。
未验证功能在 [docs/final-report.md](docs/final-report.md) 中标记为
`NEEDS TESTING` / `BLOCKED` —— 未测试者绝不说成可用。

## 设计规则

* 生产配方（`sikkatu/recipes`）对本项目**只读**。
* 管理端点（lmcache HTTP + ZMQ）仅限 loopback，缓存根目录权限 `0700`。
  Loopback 并非身份验证：同一主机上的其他本地用户仍可访问这些端口。
  主机共享使用时需要额外隔离（见 docs/security.md）。
* 树中不含凭证、令牌、提示词文本或缓存数据。
* 导出前置变量（`IMAGE`、`MODEL_DIR`、`API_KEY`）后，文档中所有命令均可直接
  执行 —— 「快速开始」给出线性流程。

## 接入自定义模型

缓存层模型无关 —— 添加模型**不需要**改代码：

1. 复制参考示例并重命名为你的模型 id：
   ```bash
   cp -r "$REPO/configs/mimo-v2.6-pro" "$REPO/configs/<你的模型>"
   ```
2. **选择模型配置** —— 当存在第二个配置目录时，脚本不再自动选取
   （歧义布局会被拒绝，而不是猜测）：
   ```bash
   export KVP_MODEL_CONFIG=<你的模型>
   ```
   `KVP_MODEL_CONFIG` 由 `setup-identity.sh`、`start-cache.sh`、
   `run-vllm-with-persist.sh`、`verify-engine-identity.sh` 和 `kvpersist`
   配置加载器读取。`CONFIG_FILE`（仅 `setup-identity.sh`）额外允许指定要写入的
   具体文件；通常不需要。
3. 编辑 `configs/<你的模型>/identity.env`：
   * `KVP_MODEL_ID` = 你的模型的 HF id（或任意唯一标签）。
   * `KVP_TP_SIZE`、`KVP_BLOCK_SIZE`、`KVP_NUM_KV_HEADS`、`KVP_HEAD_DIM`、
     `KVP_MAX_MODEL_LEN`、`KVP_KV_CACHE_DTYPE` = vLLM 对你的模型报出的 KV 几何
     （见 `docs/architecture.md`「identity fields」）。
   * `KVP_CONNECTOR_VERSION` = 你的 vLLM+LMCache 组合的描述性标记。
4. 运行 `bash scripts/setup-identity.sh "$HOME/models/<你的模型>"` 自动填写并
   核验版本/摘要字段。
5. 以导出的 `KVP_MODEL_CONFIG=<你的模型>` 启动；脚本据此解析
   `configs/<你的模型>/`（见 `docs/deployment.md`）。
每个模型拥有独立的缓存命名空间 —— 不同模型的 KV 数据绝不混用，任何身份字段
的变更都会安全地退回缓存未命中。
**注意：** 除 MiMo 外的模型支持尚未在硬件上验证；请把
`configs/mimo-v2.6-pro/` 视为参考配置，对架构差异较大的模型预期
需要适配启动器（补丁/模型加载器）。

## 许可证

Apache-2.0 —— 见 [LICENSE](LICENSE)。若您**分发**修改版、衍生分支、捆绑包或
基于本代码的 SaaS，必须保留 [NOTICE](NOTICE) 内容（作者: **sikkatu** /
项目: **kv-persistence** / https://github.com/sikkatu/kv-persistence），
依 Apache-2.0 第 4(d) 条。在私有后端中未经修改运行不需要署名——§4(d)
约束的是分发者，并非每个内部用户。