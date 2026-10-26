> 🌐 本页其他语言版本：[English](eviction-and-deduplication.md) | [Русский](eviction-and-deduplication.ru.md) | **中文**

# 淘汰与去重

## 淘汰策略

两层机制协同工作：

1. **自动层（适配器原生，始终启用）。** LMCache `nixl_store_dynamic` 的后台线程
   监视 `get_usage()`。超过 `trigger_watermark`（默认 0.9 `max_capacity_gb`）
   后按配置策略（`LRU`）回收直至用量下降 `eviction_ratio`（默认每周期 0.1）。
   无需外部调度即可保证硬上限与软阈值行为。

2. **价值排序层（策略层）。** `implementation/kvpersist/retention.py` 按实测
   信号排序：新鲜度（24h 指数衰减）、复用频次、命中收益（前缀 token 长度）
   和存储成本（字节）。价值 = (3·hits + 1.5·ln(1+tokens)) ×(0.3+0.7·新鲜度)
   ÷ 字节，按 key 确定性 tie-break。`select_victims()` 返回最小的低价值
   牺牲集，并搁置"贵重长前缀"直到便宜候选耗尽。保证由单元测试覆盖：
   过时先于新近、罕见先于常见、巨大无用先于小垃圾、长高频前缀在危机中存活。

层级间关系：当逐条统计不可用时（并非所有后端都导出），LMCache 使用自己的
LRU。这是诚实的限定：`retention.py` 只在有统计时生效（见 docs/final-report.md）。

## 策略保障

* 常被复用的长前缀被保留（重算代价最高）。
* 巨大的从未复用条目无法永久挤占有效缓存：其价值/字节比极低，最先被淘汰。
* 报告只含 key，不含提示词文本。

## 去重与共享块

LMCache 按内容派生键存储块：相同内容 ⇒ 相同键 ⇒ 单个对象。共享稳定前缀的
会话物理共享对象：

* 同一命名空间 ⇒ 共享键；不同命名空间绝不碰撞。
* 淘汰一条只删其键；共享对象保留到无任何键指向。若共享对象终被清除
  （LRU 判键未用），并发正确性保持：缺失即重算——活性取舍，非正确性问题。
* 去重节省可从 `used_bytes` 前后对比量化（`tests/integration/test_dedup.py`）。

## 测试

* 离线（任意环境）：`tests/unit/test_budget_and_retention.py`。
* 集成（需 `lmcache`）：`tests/integration/test_dedup.py`。
* 硬件淘汰（TЗ 测试 8）：小隔离预算（如 2 GB）验证自动淘汰。