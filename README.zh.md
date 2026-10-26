> [English](README.md) | [Русский](README.ru.md) | **中文**

# kv-persistence —— 面向 vLLM 的通用持久化缓存

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

主要目标：4 × NVIDIA RTX PRO 6000 上的 MiMo-V2.6-Pro（配方
`sikkatu/recipes` `recipes/rtx-pro-6000/mimo-v2.6-pro-hotsplit`）。缓存层
模型无关；模型专属配置仅位于 `configs/<model>/`。

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
  而非错误命中。
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

## 快速开始

```bash
git clone https://github.com/sikkatu/kv-persistence ~/kv-persistence
cd ~/kv-persistence
bash scripts/setup-identity.sh "$HOME/models/MiMo-V2.6-Pro-MOPD"   # 填写并核验身份固定项
bash scripts/preflight.sh               # 校验主机、存储与身份
bash scripts/start-cache.sh             # 持久缓存服务（NVMe）
bash scripts/run-vllm-with-persist.sh   # vLLM + KV 连接器（需 IMAGE、MODEL_DIR）
bash scripts/test-recovery.sh           # 验收：重启 → 缓存复用证据（exit 0 = PASS）
```

每条命令都会在出错时明确拒绝，而不是留下半成品环境：`setup-identity.sh`
负责写入并核验身份值，`preflight.sh` 校验物理 NVMe 设备，
`test-recovery.sh` 以机器判定收尾。

生产配方中的常驻开关是 `tp4.env` 里的 `KV_PERSIST=1`
（见 wiki「配方集成」）；回滚只需删除这一行。

## 仓库结构

| 路径 | 内容 |
|---|---|
| `docs/` | 英文技术文档（各语言版本在 wiki） |
| `configs/generic/` | 缓存 + lmcache 服务配置，EN/RU/ZH 注释译文 |
| `configs/mimo-v2.6-pro/` | MiMo 身份固定与 L2 适配器模板，EN/RU/ZH |
| `scripts/` | 预检、缓存启停、启动器、恢复测试、基准、管理 CLI |
| `implementation/` | `kvpersist` 包：身份、预算、保留排序、管理 CLI |
| `tests/` | 单元（49 个，随处可跑）、集成（需 `lmcache`）、恢复（GPU 主机） |

## 状态

实现已完成；离线测试全部通过。4 GPU 主机上的验收测试已封装为脚本
（`scripts/test-recovery.sh`、`scripts/benchmark.sh`），由运维方执行。
未验证功能在 [docs/final-report.md](docs/final-report.md) 中标记为
`NEEDS TESTING` / `BLOCKED` —— 未测试者绝不说成可用。

## 设计规则

* 生产配方（`sikkatu/recipes`）对本项目**只读**。
* 无未认证的管理端点；缓存根目录权限 `0700`。
* 树中不含凭证、令牌、提示词文本或缓存数据。
* 导出前置变量（`IMAGE`、`MODEL_DIR`、`API_KEY`）后，文档中所有命令均可直接
  执行 —— 「快速开始」给出线性流程。

## 许可证

Apache-2.0 —— 见 [LICENSE](LICENSE)。任何使用本项目或发布衍生分支者必须
保留 [NOTICE](NOTICE) 内容（作者: **sikkatu** / 项目: **kv-persistence** /
https://github.com/sikkatu/kv-persistence），依 Apache-2.0 第 4(d) 条。