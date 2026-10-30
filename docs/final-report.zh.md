> 🌐 本页其他语言版本：[English](final-report.md) | [Русский](final-report.ru.md) | **中文**

# 最终报告 — kv-persistence

仓库：github.com/sikkatu/kv-persistence（`main` 分支，提交 `HEAD`（见 `git rev-parse HEAD`） —— 已通过 GitHub remote API 验证）。

## 总状态：PARTIAL

实现已完成，离线测试全绿（59 个单元测试，含 3 个 P0 权重哈希回归、配置一致性、预算和保留策略）。
硬件验收场景（重启后 A/B 恢复、32K/128K/256K 基准、bytes-per-token）已封装为脚本
（`scripts/test-recovery.sh`、`scripts/benchmark.sh`），待在 GPU 目标主机
上运行。在硬件验证通过前，PARTIAL 是唯一诚实的状态标签。

## 架构与后端选择

存储层使用 LMCache `nixl_store_dynamic` + POSIX（persist_enabled=true；
两个进程都重启后索引惰性重建）；`max_capacity_gb=1500`，LRU 淘汰 + 0.9 软阈值。

## 权重身份 — P0 修复

`scripts/setup-identity.sh` 现在对每个 shard 的**内容**做 SHA-256（而非仅文件名+大小）。
回归测试证实：同名同大小的字节级替换现在会改变指纹（`tests/unit/test_fingerprint_content.py`，
3 条回归全绿）。shard 哈希有缓存加速重复运行。`KVP_FINGERPRINT_MODE=fast` 提供旧模式，
但附带风险警告，仅适合临时实验。

## 引擎身份验证 — 强制门禁

`scripts/verify-engine-identity.sh`（在 `test-recovery.sh` 使用缓存前调用）以
`docker inspect` 的实际引擎参数和 `config.json` 的模型几何核对 identity.env 的固定值。
三种结果：VERIFIED(0) / MISMATCH(4) / **INCONCLUSIVE(5)**。
INCONCLUSIVE = 拒绝复用缓存，绝不猜测。
## LMCache 存储级别的身份隔离（review P1-3）

身份命名空间现在在**真实** LMCache 存储中强制隔离数据，而不仅是本地标签。
已通过固定版 LMCache 0.5.3 源码和实际写入/查找运行验证（集成测试
`test_identity_namespace_isolates_lmcache_storage`，见 docs/benchmarks.zh.md）：

* LMCache 缓存键（`ObjectKey.model_name`）同时构成键和 `nixl_store_dynamic`
  L2 文件名（`<safe_model_name>_<kv_rank>_<group>_<chunkhash>.bin`）。
* `CacheIdentity.lmcache_model_name()` 将命名空间嵌入该 `model_name`，
  因此更改**任何**身份字段都会把数据移到不同的键和磁盘上不同的文件。
  按字段证明：更改后的身份查找在 LMCache API 返回 `0/N` 分块，而原始身份
  返回 `N/N`。
* **残余缺口（已实测、被测试固定）**：`cache_salt` 参与键相等性，但**不**
  参与 `nixl_store_dynamic` 文件名（fs/s3/bigtable 适配器会包含它）。
  重启后，以**不同** salt 对相同 token 做二级（磁盘）查找可能返回原始 salt
  的对象——实测：`store salt=tenantA`、`lookup salt=tenantB` -> `4/4` 分块。
  在上游把 salt 加入文件名之前，**不要**依赖 `cache_salt` 做 L2 隔离：
  把 per-tenant 判别符放进 `lmcache_model_name(extra_discriminator=...)`，
  或为每个租户使用独立的 L2 缓存根目录。测试
  `test_cache_salt_isolation_gap_in_nixl_store_dynamic` 会在上游修复时明确失败。

## 恢复测试 — v2 协议（根据审阅意见）

* 日志位置标记；仅统计新事件。
* 重启前先写入验证（L2 必须有真实写入）。
* L2（磁盘）与 L1（RAM）证据区分。
* `cached_tokens == null` → MISSING → INCONCLUSIVE，而非默认当作 0。
* NVMe 物理读取计数器（`/sys/block/<dev>/stat`）作为独立佐证。
* 三态判定：PASS(0) / INCONCLUSIVE(5) / FAIL(1)。

## 已修复的审阅意见（本轮）

| 优先级 | 问题 | 处理 |
|---|---|---|
| P0 | 权重指纹不含 shard 内容 | SHA-256 逐 shard 内容哈希 + 回归测试 |
| P1 | 预算文档 750/1TB 与代码 1.5TB 矛盾 | 三语 wiki 同步为 1500；边界 = 设备可用空间 |
| P1 | identity.env vs identity.env.local 混淆 | 全部文档指向 .local（setup-identity 写入处） |
| P1 | 本地指纹 vs HF SHA 未区分 | troubleshooting/deployment 明确两种格式与适用场景 |
| Quick-Start 步骤 | 缺 API_KEY/recipe/MODEL_DIR 前置条件 | 步骤 7 列明三项必需前置 |
| Quick-Start 步骤 | 手动复制 digest 要求 | 已移除；setup-identity 自动写入 |
| Quick-Start 步骤 | Python 3.12/venv 在 Debian/Ubuntu (tested on Debian 12) | 明确安装命令与 `python3.12 -m venv` |
| README | 测试数 29→49、"not code"、"as-is" | 已更正 |

## 已知限制（同前）

1. 硬件验收（A/B 测试）未在本会话执行——目标主机不可达；脚本与协议完全就绪。
2. LMCache 0.5.3 与生产镜像 PyTorch/CUDA 组合待硬件最终验证。
3. 去重文档已完整记录；实际效果需在目标配置上度量。