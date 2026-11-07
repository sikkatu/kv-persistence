> 🌐 本页其他语言版本：[English](final-report.md) | [Русский](final-report.ru.md) | **中文**

# 最终报告 — kv-persistence

> 各轮次变更历史见 [CHANGELOG.zh.md](../CHANGELOG.zh.md)；本页只保留已验证的状态与技术记录。

仓库：github.com/sikkatu/kv-persistence（`main` 分支，提交 `HEAD`（见 `git rev-parse HEAD`） —— 已通过 GitHub remote API 验证）。

## 总状态：验收通过（硬件 2026-11-07）

实现已完成，离线测试全绿（325 个单元测试 + 34 个离线配置/文档测试 = 359 个离线收集，
342 通过 + 2 跳过；共收集 359 个，含集成（10）与 recovery（5）测试；另有伴生仓库
`sikkatu/recipes` 332 通过 + 5 跳过 / 337）。**重启后恢复的硬件验收已通过
（4x RTX PRO 6000，MiMo-V2.6-Pro-MOPD，same-UID 模式，2026-11-07）**：
* 场景 A（仅重启 vLLM）：`cached_tokens=2560`，ttft 0s（冷启动 17.53s），
  L1/RAM 命中；
* 场景 B（两个服务均重启，NVMe 保留）：`cached_tokens=2560`，ttft 0s，
  **L2 读取事件 = 1**（磁盘恢复证据）；
* 身份门禁每阶段通过，**0 个硬失败**。
机器判定仅因未设置 `KVP_CACHE_DEVICE`（无法获得独立 NVMe 计数器）而为
INCONCLUSIVE。其余未验证项为次要项，见下方并如实标注。

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
已通过固定版 LMCache 0.5.6 源码和实际写入/查找运行验证（集成测试
`test_identity_namespace_isolates_lmcache_storage`，见 docs/benchmarks.zh.md）：

* LMCache 缓存键（`ObjectKey.model_name`）同时构成键和 `nixl_store_dynamic`
  L2 文件名（`<safe_model_name>_<kv_rank>_<group>_<chunkhash>.bin`）。
* `CacheIdentity.lmcache_model_name()` 将命名空间嵌入该 `model_name`，
  因此更改**任何**身份字段都会把数据移到不同的键和磁盘上不同的文件。
  按字段证明：更改后的身份查找在 LMCache API 返回 `0/N` 分块，而原始身份
  返回 `N/N`。
* **残余缺口——已在磁盘键层面修复（第 15 轮）**：`cache_salt` 参与键相等性，
  但上游 `nixl_store_dynamic` **不**把它写入文件名（fs/s3/bigtable 适配器会
  包含）。实测泄漏（`store salt=tenantA`、`lookup salt=tenantB` -> 重启后
  `4/4` 分块）已由 `kvpersist/salt_filename_patch.py` 关闭——该补丁把 salt
  追加到 L2 文件名，所有启动器均使用。实测验证：
  `store salt=tenantA`、`lookup salt=tenantB` -> `0/4` 分块
  （`test_cache_salt_isolates_l2_storage` 在泄漏复现时明确失败）。不带补丁
  运行裸 `lmcache server` 保持上游行为：此时请用
  `lmcache_model_name(extra_discriminator=...)` 或独立缓存根目录。

## 恢复测试 — v2 协议（根据审阅意见）

* 日志位置标记；仅统计新事件。
* 重启前先写入验证（L2 必须有真实写入）。
* L2（磁盘）与 L1（RAM）证据区分。
* `cached_tokens == null` → MISSING → INCONCLUSIVE，而非默认当作 0。
* NVMe 物理读取计数器（`/sys/block/<dev>/stat`）作为独立佐证。
* 三态判定：PASS(0) / INCONCLUSIVE(5) / FAIL(1)。

## 已知限制（同前）
1. 硬件验收（A/B 测试）未在本会话执行——目标主机不可达；脚本与协议完全就绪。
2. LMCache 0.5.6 与生产镜像 PyTorch/CUDA 组合待硬件最终验证。
3. 去重文档已完整记录；实际效果需在目标配置上度量。

## L2 无损编解码与可观测性栈

本轮任务要求两项补充；两者均在**不 fork LMCache**、不改动生产配方的前提下实现。

### 1. L2 仅无损存储

上游 LMCache（0.5.6 之前）仅注册有损（fp8、turboquant）与加密（aesgcm）serde ——
上游不存在无损压缩 serde。本仓库通过 `implementation/kvpersist/compress_serde.py`
在运行时向 LMCache 自身注册表注册 `kvp-zstd` / `kvp-lz4` / `kvp-raw` serde
类型（与 salt 文件名补丁相同的启动器模式，现已统一到
`implementation/kvpersist/launch.py`）。

* 自第 48 轮起默认 `LMCACHE_L2_SERDE=zstd`（原为 `none`）——无损压缩，
  `LMCACHE_L2_SERDE=none` 则得到与未压缩 upstream 逐字节精确的原始块，
  与未压缩的 upstream
  路径完全一致。不做任何变换。
* `LMCACHE_L2_SERDE=zstd|lz4`（或操作开关 `USE_LOSSLESS_SERDE`）—— 无损压缩。
  无法压缩变小的块按原样存储，因此块永远不会膨胀超过一个 4 KiB 页
  （28 字节帧头 + 填充到页边界）。常被引用的 1.1–1.4x 缩减是来自 LMCache
  生态的**规划估算**，并非本仓库的实测结果：这里尚未运行压缩率基准测试。
* 有损编解码（fp8 / turboquant / lbq / cachegen / kivi）在启动时被明确
  **拒绝** —— 无损是契约，而不是可以漂移的默认值。
* 编解码被折叠进缓存身份命名空间（`l2_serde`），shell
  （`scripts/env.sh _kvp_resolve_l2_serde`）与 Python 加载器
  （`kvpersist.identity`）均如此；单元测试断言两者一致
  （`tests/unit/test_lossless_serde_switch.py`）。切换编解码是安全的缓存
  miss，绝不会错误命中。
* 帧格式 `KVCZ` v2 携带未压缩负载的 CRC32：被截断的 DMA 读取、位翻转或
  来自其他编解码的帧会在反序列化时被检测并报告为缓存 MISS。
* 负载之后的填充被显式清零 —— 回收的 L1 slab 块绝不能把其他租户的字节
  泄漏进我们的缓存文件（`tests/unit/test_compress_serde.py::test_padding_is_zeroed`）。
* Fail-fast：服务器启动前，`require_codec()` 只验证**所选**编解码的绑定
  （host venv 探测，或 docker 模式下**容器内**探测）。`LMCACHE_L2_SERDE=none`
  的缓存根本不需要任何压缩包 —— lz4 与 zstandard 仅在其编解码被实际选用时
  才必需。缺失绑定会中止启动，而不是达到 READY 后在 serde 线程池中失败。
* L2 文件大小补丁仅在真正构建 kvp-* serde 时安装；`LMCACHE_L2_SERDE=none`
  时适配器类完全不动（逐字节 upstream load/store），因此 raw 路径上短的
  DMA 读取绝不会被我们的包装器掩盖。

**如实状态：** serde 帧逻辑已通过单元测试（往返、成帧、坏帧拒绝、不可压缩
数据透传），且在未安装 torch/lz4/zstandard 的环境中运行。经 LMCache 的
`SerdeL2AdapterWrapper` 与 `nixl_store_dynamic` DMA 写入的端到端路径
**需要在目标 GPU 主机上测试** —— 与其余硬件验收套件状态相同。
两处运行时补丁（`_install_size_patch`）使磁盘文件大小跟随序列化后的帧，
而非完整 L1 分配；这些已被单元级逻辑覆盖，但尚未在硬件上验证。

### 2. Prometheus + Grafana 监控

`observability/` 内联了 LMCache 官方示例栈
（`LMCache/LMCache` `examples/observability`，Apache-2.0）：OTel Collector +
Tempo + Prometheus + Grafana，并预置官方 LMCache 与 Coordinator 仪表盘
（JSON 逐字复制）。

```bash
bash scripts/start-observability.sh     # 一条命令，Grafana 位于 :3000
```

遥测为可选项且默认关闭（`KVP_OTLP_ENDPOINT` / `USE_OBSERVABILITY=1`）；
缓存服务不依赖该栈也能完整工作。
**状态：compose 文件与仪表盘为逐字官方资源，脚本已通过语法检查；本次会话
未在目标主机上启动实际栈**（GPU 主机 / Docker 守护进程不可达）。

