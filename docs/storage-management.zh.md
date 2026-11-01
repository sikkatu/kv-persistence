> 🌐 本页其他语言版本：[English](storage-management.md) | [Русский](storage-management.ru.md) | **中文**

# 存储与淘汰

## 预算模型

| 限制 | 默认值 | 执行机制 |
|---|---|---|
| 硬上限 | `KVP_CACHE_BUDGET_GB=1500` GB（1.5 TB） | L2 适配器的 `max_capacity_gb` —— 超出的写入被拒绝 |
| 物理上限 | 共享 VM 磁盘上的可用空间（同时装着 Docker 镜像、模型权重、系统）。预检通过 `KVP_CACHE_DEVICE` 校验设备并报告可用空间。 |
| 软阈值 | 预算的 0.9 | `trigger_watermark` —— 自动淘汰从此开始 |
| 回收目标 | 一个周期后约为预算的 0.81（0.9 阈值 × 已用容量的 0.9） | `eviction_ratio` 为已用容量的 0.1 |
| 命名空间配额 | 可选，`quotas.json` | `kvp-admin quota` 策略层 |

预算仅涵盖持久 KV 缓存数据。模型权重、系统和其他文件不在此核算，
kv-persistence 的任何脚本都不会触碰它们。

默认 1.5 TB 是共享 VM 磁盘上刻意留出的较大份额（同盘还有 Docker 镜像、模型权重
和操作系统）。更多 prefill 历史可节省重开对话的重算时间与能耗，但需为非缓存写入
预留空间——预检通过 `KVP_CACHE_DEVICE` 校验可用空间。不要把预算设为"磁盘剩下的
全部"：给 OS/Docker/模型需求留 ≥50 GB，并随非缓存写入方（docker pull、日志）增长
监控可用空间。

## 一个对话能有多长？

两个不同的上限不要混淆：

* **单个对话的 token 上限（约 1M）** —— 受模型上下文窗口限制
  （模型 `config.json` 中的 `max_position_embeddings`），而非本缓存。
* **缓存在磁盘上保留多少历史** —— 仅受 `KVP_CACHE_BUDGET_GB` 限制。
  这里没有 token 上限：超出 100 万 token 后，有意义的只是单个上下文的
  前缀复用边界（见「架构」页），而非 kv-persistence 本身的限制。

预算仅适用于持久缓存数据；模型权重、系统与其他文件从不被核算或触碰。

## 容量核算

* `KVP_CACHE_ROOT` 下每个 KV 块对象一个文件（临时文件 + 原子重命名）。
* 没有单独的磁盘索引 —— 确定性文件命名空间本身就是索引，启动时惰性重建。
  索引/元数据开销已计入 `used_bytes`。
* 并发写入安全：只要会超出硬上限，记账器就会拒绝写入。硬上限由 LMCache
  `nixl_store_dynamic` 适配器本身执行（`max_capacity_gb`：超过即拒绝写入）。
  本仓库的 `BudgetLedger.admit()` 为调用方提供相同的准入检查
  （见 `test_concurrent_admits_cannot_exceed_budget`），但在生产写入路径上
  **并未被调用**（review round 13）：LMCache 写入分块时不会咨询它。因此
  `quotas.json` 中的配额目前只是报告/排序数据，而不是写入时的强制执行点——
  在记账器接入写入路径之前，不要假设配额能阻止存储增长。
* **重启后核算（NEEDS TESTING）**：lmcache-server 重启后，上游描述内存索引按需
  懒惰重建（只根据文件尺寸）。在确认锁定的 LMCache 构建会把恢复的文件大小
  计入 get_usage()（从而参与 max_capacity_gb 与 LRU 的判定）之前，不要假设
  重启前的文件仍计入硬上限。必需的硬件测试：填满缓存 → 重启 → 继续写 →
  验证 used_bytes 在预算内持续增长。

随时查看报告：`bash scripts/budget-report.sh status`

## 自动淘汰（强制、始终开启）

* 后台线程监视 `get_usage()`。超过 `trigger_watermark` 后，策略淘汰直至
  用量下降 `eviction_ratio`。
* 服务内策略为 **LRU**（适配器原生），作用于已存块键。
* 细粒度排序（`implementation/kvpersist/retention.py`）在有逐条统计的地方
  按实测价值排序：新鲜度（24 小时衰减）、复用次数、前缀长度和存储成本。
* 存储压力告警：状态报告中 `soft_pressure: true`；启动器在文件系统空闲
  空间低于预算时也会在启动时告警。
### 手动清理 —— 仅显式执行
```bash
bash scripts/budget-report.sh purge --namespace=<model_name> --yes
bash scripts/budget-report.sh purge --all --yes
```
命名空间形如 `--models--MyModel--<hash>`（以短横线开头），因此请用
`--namespace=NAME` 形式——`--namespace NAME`（以及位置参数形式）对以短横线
开头的名称会被 argparse 解析为未知选项。位置参数形式适用于不以短横线
开头的命名空间。
需要 `--yes`。删除范围严格限定在 `KVP_CACHE_ROOT` 内的缓存数据 ——
其他路径一律拒绝并返回退出码 2。命名空间是经过清理的 `model_name`
（L2 文件名中 `_<rank8>_<group>_<hash64>.bin` 后缀之前的部分），按**精确**
匹配 —— 用 `budget-report.sh status` 查看当前使用的命名空间名。若无任何
匹配项，purge 以退出码 1 和明确消息结束（绝不静默无操作）。启动绝不会自动清空缓存。
## 命名空间与清理策略

L2 适配器（`nixl_store_dynamic`）把**扁平**文件（命名形如
`<safe_model_name>_<kv_rank:08x>_<object_group:x>_<chunkhash:64>.bin`）直接
写入 `KVP_CACHE_ROOT` —— 没有按命名空间划分子目录。因此命名空间就是这些文件
中 `model_name` 的前缀（`_<rank8>_<group>_<hash64>.bin` 后缀之前的部分），例如
`--models--my_model--a84fadf1b808e8cfa5ef24f7a2924fa9`。版本变更会创建
新命名空间（新的文件前缀）；旧命名空间绝不会被自动删除
（已记录的策略：清理是管理员的显式决定）。要定向回收旧命名空间，
请列出它们（`budget-report.sh status` → `namespaces`）并按名字 purge
（精确匹配 —— 无子串匹配）。