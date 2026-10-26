> 🌐 本页其他语言版本：[English](storage-management.md) | [Русский](storage-management.ru.md) | **中文**

# 存储与淘汰

## 预算模型

| 限制 | 默认值 | 执行机制 |
|---|---|---|
| 硬上限 | `KVP_CACHE_BUDGET_GB=1500` GB（1.5 TB） | L2 适配器的 `max_capacity_gb` —— 超出的写入被拒绝 |
| 物理上限 | 固定 NVMe 设备的容量（预检确认） | 通过 `KVP_CACHE_DEVICE` 检查可用空间 |
| 软阈值 | 预算的 0.9 | `trigger_watermark` —— 自动淘汰从此开始 |
| 回收目标 | 一个周期后约为预算的 0.81（0.9 阈值 × 已用容量的 0.9） | `eviction_ratio` 为已用容量的 0.1 |
| 命名空间配额 | 可选，`quotas.json` | `kvp-admin quota` 策略层 |

预算仅涵盖持久 KV 缓存数据。模型权重、系统和其他文件不在此核算，
kv-persistence 的任何脚本都不会触碰它们。

默认 1.5 TB 刻意多留一些：更多 prefill 历史保留在 NVMe 上，重新打开对话时
可节省首次加载的重算时间和能耗。

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
* 并发写入安全：即使并行请求，只要会超出硬上限，记账器就会拒绝写入。

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
bash scripts/budget-report.sh purge --namespace kvp-<ns> --yes
bash scripts/budget-report.sh purge --all --yes
```

需要 `--yes`。删除范围严格限定在 `KVP_CACHE_ROOT` 内的 `kvp-*` 目录 ——
其他路径一律拒绝并返回退出码 2。启动绝不会自动清空缓存。

## 命名空间与清理策略

每个兼容性身份对应一个 `kvp-<hash>` 目录。版本变更会创建新命名空间；
旧命名空间绝不自动删除（策略已记录：清理是管理员的显式决定）。